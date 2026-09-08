"""Seven viewers must not cost seven times the broker reads.

On 2026-09-08 the dashboard died mid-demo with
alpaca.common.exceptions.APIError {"code":42910000,"message":"rate limit
exceeded"}. get_client() was undecorated, so every rerun of every session
built its own client with no shared state, and each render made roughly
fourteen account/position/order calls. Seven established sessions at one
render per 15s is about 390 calls/minute against a 200/minute ceiling.
"""

from __future__ import annotations

import threading

import pytest

from dashboard.read_cache import ReadThroughCache, CACHED_READS, MUTATORS


class FakeClient:
    def __init__(self):
        self.positions_calls = 0
        self.account_calls = 0
        self.orders_calls = 0
        self.closed = []
        self.name = "inner"

    def get_positions(self):
        self.positions_calls += 1
        return [f"pos{self.positions_calls}"]

    def get_account(self):
        self.account_calls += 1
        return f"acct{self.account_calls}"

    def get_orders(self, status="open"):
        self.orders_calls += 1
        return [f"{status}{self.orders_calls}"]

    def close_position(self, symbol):
        self.closed.append(symbol)
        return "closed"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TestReadsAreSharedNotMultiplied:
    def test_repeat_reads_inside_the_ttl_hit_the_broker_once(self):
        inner, clock = FakeClient(), Clock()
        c = ReadThroughCache(inner, ttl=12, clock=clock)
        for _ in range(14):
            c.get_positions()
        assert inner.positions_calls == 1

    def test_the_read_cost_is_independent_of_viewer_count(self):
        # The whole point: the same shared instance serves every session.
        inner, clock = FakeClient(), Clock()
        shared = ReadThroughCache(inner, ttl=12, clock=clock)
        for _viewer in range(7):
            for _call_in_render in range(14):
                shared.get_positions()
        assert inner.positions_calls == 1

    def test_the_cache_expires_so_the_page_is_not_frozen(self):
        inner, clock = FakeClient(), Clock()
        c = ReadThroughCache(inner, ttl=12, clock=clock)
        assert c.get_positions() == ["pos1"]
        clock.t = 12.1
        assert c.get_positions() == ["pos2"]
        assert inner.positions_calls == 2

    def test_distinct_arguments_are_cached_separately(self):
        inner, clock = FakeClient(), Clock()
        c = ReadThroughCache(inner, ttl=12, clock=clock)
        assert c.get_orders("open") == ["open1"]
        assert c.get_orders("closed") == ["closed2"]
        assert c.get_orders("open") == ["open1"]
        assert inner.orders_calls == 2

    def test_every_cached_read_is_actually_memoized(self):
        inner, clock = FakeClient(), Clock()
        c = ReadThroughCache(inner, ttl=12, clock=clock)
        for name in CACHED_READS:
            getattr(c, name)()
            getattr(c, name)()
        assert (inner.positions_calls, inner.account_calls, inner.orders_calls) == (1, 1, 1)


class TestWritesAreNeverCached:
    def test_a_mutator_passes_through_every_time(self):
        inner, clock = FakeClient(), Clock()
        c = ReadThroughCache(inner, ttl=12, clock=clock)
        c.close_position("QQQ")
        c.close_position("QQQ")
        assert inner.closed == ["QQQ", "QQQ"]

    def test_a_write_invalidates_the_read_cache(self):
        # Otherwise the render after an action shows the world before it.
        inner, clock = FakeClient(), Clock()
        c = ReadThroughCache(inner, ttl=12, clock=clock)
        assert c.get_positions() == ["pos1"]
        c.close_position("QQQ")
        assert c.get_positions() == ["pos2"]

    def test_invalidation_happens_even_if_the_write_raises(self):
        inner, clock = FakeClient(), Clock()

        def boom(symbol):
            raise RuntimeError("broker said no")

        inner.close_position = boom
        c = ReadThroughCache(inner, ttl=12, clock=clock)
        c.get_positions()
        with pytest.raises(RuntimeError):
            c.close_position("QQQ")
        c.get_positions()
        assert inner.positions_calls == 2

    def test_every_declared_mutator_clears_the_cache(self):
        for name in MUTATORS:
            inner, clock = FakeClient(), Clock()
            setattr(inner, name, lambda *a, **k: None)
            c = ReadThroughCache(inner, ttl=12, clock=clock)
            c.get_positions()
            getattr(c, name)()
            c.get_positions()
            assert inner.positions_calls == 2, f"{name} did not invalidate"


class TestPassThrough:
    def test_unknown_attributes_reach_the_real_client(self):
        c = ReadThroughCache(FakeClient(), ttl=12)
        assert c.name == "inner"

    def test_concurrent_readers_do_not_multiply_broker_calls(self):
        inner, clock = FakeClient(), Clock()
        c = ReadThroughCache(inner, ttl=12, clock=clock)
        c.get_positions()          # prime
        errors = []

        def hammer():
            try:
                for _ in range(50):
                    c.get_positions()
            except Exception as exc:      # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        assert inner.positions_calls == 1


class TestTheCacheCannotReachATradingEngine:
    """A stale position inside a trading loop is the June hedge-inversion bug."""

    def test_no_module_outside_the_dashboard_imports_it(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        offenders = []
        for path in list((root / "src").rglob("*.py")) + list((root / "scripts").rglob("*.py")):
            if "read_cache" in path.read_text():
                offenders.append(str(path.relative_to(root)))
        assert offenders == [], f"read_cache must stay display-only: {offenders}"


class TestTheCallSiteActuallyUsesIt:
    """Constructing the wrapper by hand in a test proves nothing about app.py."""

    def _source(self):
        import inspect
        import dashboard.app as app
        return inspect.getsource(app)

    def test_get_client_is_cache_resource_decorated(self):
        # Undecorated, each session builds its own client and shares no cache,
        # which is precisely how seven viewers became seven times the reads.
        src = self._source()
        i = src.index("def get_client():")
        assert "@st.cache_resource" in src[max(0, i - 200):i]

    def test_get_client_returns_the_wrapper_not_a_raw_client(self):
        src = self._source()
        body = src[src.index("def get_client():"):]
        body = body[:body.index("\n@st.cache_resource")]
        assert "ReadThroughCache(" in body
        assert "return AlpacaClient()" not in body

    def test_the_ttl_is_under_the_autorefresh_interval(self):
        # A TTL longer than the refresh would serve the same numbers twice.
        import dashboard.app as app
        assert 0 < app.DASHBOARD_READ_TTL <= 15


class TestOnePanelCannotKillThePage:
    def test_a_rate_limited_panel_degrades_to_a_notice(self):
        import dashboard.app as app
        with app.panel("The sleeve allocation"):
            raise RuntimeError('{"code":42910000,"message":"rate limit exceeded"}')
        # Reaching here at all is the assertion: the error did not propagate.

    def test_an_unexpected_error_also_stays_contained(self):
        import dashboard.app as app
        with app.panel("The positions table"):
            raise ValueError("something else entirely")

    def test_the_hero_and_sleeves_are_both_guarded(self):
        import inspect
        import dashboard.app as app
        src = inspect.getsource(app.main)
        for call in ("render_hero(", "render_sleeves(", "render_positions("):
            i = src.index(call)
            assert "with panel(" in src[max(0, i - 120):i], f"{call} is unguarded"
