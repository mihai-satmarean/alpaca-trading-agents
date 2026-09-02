"""A halt that expires on its own.

The scalper was stopped on 2026-09-01 for negative expectancy (TQQQ -$1.03 a
trade over 142 closes at a 0% win rate) and had to be trading again at the
next open. A halt someone has to remember to lift is a halt that gets left on;
a halt lifted by a machine outside EC2 is a halt that depends on that machine
being awake. So the pause carries its own expiry date and the engine reads it.

Every test here asks the same question from a different angle: can this gate
ever start trading that would otherwise be stopped? It must only be able to
stop trading that would otherwise happen.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

from src.strategies.vampire_engine import VampireConfig, VampireEngine, VampireState

TODAY = date.today()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()


def _engine(paused_until=None):
    c = MagicMock()

    def _order(symbol, qty, side, tif=None):
        o = MagicMock()
        o.status, o.filled_qty, o.id = "filled", str(qty), "t"
        return o

    c.market_order.side_effect = _order
    e = VampireEngine(c, MagicMock(), MagicMock(),
                      VampireConfig(symbol="TQQQ", tick_threshold=0.02,
                                    position_size=10, max_daily_loss=1e9,
                                    paused_until=paused_until))
    e._is_market_hours = lambda: True
    return e


class TestThePauseStopsTrading:
    def test_a_paused_engine_places_no_orders(self):
        e = _engine(paused_until=TOMORROW)
        for _ in range(20):
            e.tick(99.0, vwap=100.0)
        assert e.net_position == 0
        assert e._client.market_order.call_count == 0

    def test_a_paused_engine_reports_idle_not_watching(self):
        e = _engine(paused_until=TOMORROW)
        e.tick(99.0, vwap=100.0)
        assert e.state is VampireState.IDLE

    def test_a_position_held_when_the_pause_begins_is_flattened(self):
        """Halting while long would leave an unmanaged position with nothing
        watching it: the engine that would normally exit is the one just
        switched off."""
        e = _engine(paused_until=None)
        e.tick(99.0, vwap=100.0)
        assert e.net_position > 0

        e.cfg.paused_until = TOMORROW
        e.tick(99.0, vwap=100.0)
        assert e.net_position == 0
        e._client.close_position.assert_called_once_with("TQQQ")


class TestThePauseExpires:
    def test_the_resume_date_itself_trades(self):
        """Set to tomorrow's date, it must trade tomorrow, not the day after."""
        e = _engine(paused_until=TODAY.isoformat())
        for _ in range(3):
            e.tick(99.0, vwap=100.0)
        assert e.net_position > 0

    def test_a_past_date_trades(self):
        e = _engine(paused_until=YESTERDAY)
        for _ in range(3):
            e.tick(99.0, vwap=100.0)
        assert e.net_position > 0

    def test_unset_trades(self):
        e = _engine(paused_until=None)
        for _ in range(3):
            e.tick(99.0, vwap=100.0)
        assert e.net_position > 0


class TestItCanOnlyEverStopTrading:
    """The gate is one-directional by construction. A bug in it must not be
    able to start trading, only to prevent it."""

    def test_an_unparseable_date_does_not_pause(self):
        """A typo must not silently halt the strategy forever. That failure is
        invisible - the engine simply never trades and nothing says why."""
        for bad in ("tomorrow", "2026-13-45", "09/02/2026", ""):
            e = _engine(paused_until=bad)
            e.tick(99.0, vwap=100.0)
            assert e.net_position > 0, f"{bad!r} should not pause"

    def test_the_pause_does_not_override_the_session_window(self):
        """Outside market hours it stays stopped whether paused or not."""
        e = _engine(paused_until=YESTERDAY)
        e._is_market_hours = lambda: False
        for _ in range(5):
            e.tick(99.0, vwap=100.0)
        assert e.net_position == 0


class TestTheConfigIsWiredToTheEngine:
    """The gate is worthless if the yml value never reaches the engine. It did
    not, until this change: VampireAgent accepted config_overrides and the
    coordinator never passed any."""

    def test_the_repo_config_pauses_today_and_resumes_tomorrow(self):
        from src.core.config import load_config
        cfg = load_config()
        assert cfg.vampire_paused_until == "2026-09-02"

    def test_the_agent_applies_an_override_to_every_engine(self):
        from src.agents.vampire import VampireAgent
        client, data, tracker, breaker, allocator = (MagicMock() for _ in range(5))
        a = VampireAgent(client, data, tracker, breaker, allocator,
                         symbols=["QQQ", "TQQQ"],
                         config_overrides={"paused_until": "2026-09-02"})
        assert all(e.cfg.paused_until == "2026-09-02" for e in a._engines.values())

    def test_the_coordinator_actually_passes_it(self):
        """Asserts the call site, not just that the agent would honour it.

        The first version of this test built a VampireAgent by hand with the
        override already supplied, which proves the agent works and says
        nothing about whether anything ever calls it that way. The coordinator
        did not: it accepted config_overrides and passed none, so the yml value
        reached nothing. Deleting the wiring left that test green.
        """
        from unittest.mock import patch

        from src.agents.coordinator import Coordinator
        with patch("src.agents.coordinator.VampireAgent") as VA:
            try:
                Coordinator()
            except Exception:
                # Construction touches the broker; only the call matters here.
                pass
        assert VA.called, "the coordinator never built a VampireAgent"
        overrides = VA.call_args.kwargs.get("config_overrides") or {}
        # A subset check: since 2026-09-02 the coordinator forwards every
        # vampire key in the yml, not only the pause, so the dict is larger.
        assert overrides.get("paused_until") == "2026-09-02", (
            f"the coordinator passed {overrides!r}; the halt in strategies.yml "
            "would never reach the engine"
        )
