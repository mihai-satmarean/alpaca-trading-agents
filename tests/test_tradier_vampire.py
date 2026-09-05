"""The Vampire on Tradier: the two semantics that differ from Alpaca.

Verified against the Tradier sandbox on 2026-09-04, not assumed:

1. IOC DOES NOT EXIST. duration=ioc returns 400 "Invalid parameter, duration:
   is not valid". Only day, gtc, pre, post are accepted, and pre/post demand a
   limit order. So IOC maps to day and the unfilled remainder is cancelled.

2. SIDE IS FOUR-VALUED. Alpaca's SELL both closes a long and opens a short.
   Tradier splits them, and choosing wrong is the exact failure that turned a
   hedge into a doubled directional bet on prediction-market-arb in June: a
   side picked from a lagging position read. The side here is resolved from the
   broker's own position, and these tests pin every case including the boundary
   at flat, which is where the June bug lived (a `|| 0` collapsing UNKNOWN into
   "short").
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.core.tradier_client import TradierClient, TradierError, TradierOrder


def _client(**kw):
    c = TradierClient.__new__(TradierClient)
    c._token, c._account, c._base = "t", "VA1", "https://sandbox.tradier.com/v1"
    c.sandbox, c._timeout = True, 10.0
    c._pos_cache, c._pos_at = {}, 0.0
    import threading
    c._lock = threading.Lock()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class TestTlsIsVerifiedWithoutTheCallersHelp:
    """This Python has no system CA bundle, so a bare urlopen raises
    CERTIFICATE_VERIFY_FAILED. notify.py already carries a certifi context;
    relying on the caller to export SSL_CERT_FILE would break the moment
    anything imports the client outside the runner, which is how this was
    found."""

    def test_both_adapters_hold_a_certifi_context(self):
        from src.core import tradier_client, tradier_market_data
        assert tradier_client._SSL_CTX is not None
        assert tradier_market_data._SSL_CTX is not None

    def test_requests_pass_that_context(self):
        import inspect
        from src.core import tradier_client, tradier_market_data
        assert "context=_SSL_CTX" in inspect.getsource(tradier_client.TradierClient._request)
        assert "context=_SSL_CTX" in inspect.getsource(tradier_market_data.TradierMarketData._get)


class TestSideResolution:
    """Pure, total, exhaustively pinned. This is the money-losing surface."""

    @pytest.mark.parametrize("direction,net,expected", [
        ("buy", 0, "buy"),            # flat, going long
        ("buy", 10, "buy"),           # adding to a long
        ("buy", -10, "buy_to_cover"), # covering a short
        ("sell", 0, "sell_short"),    # flat, opening a short
        ("sell", 10, "sell"),         # closing a long
        ("sell", -10, "sell_short"),  # adding to a short
    ])
    def test_every_combination(self, direction, net, expected):
        assert TradierClient.resolve_side(direction, net) == expected

    def test_flat_is_never_treated_as_short(self):
        """The June 2026 bug: an unknown/zero position collapsed to the short
        branch and the engine bought the wrong side. Flat must open, not cover."""
        assert TradierClient.resolve_side("buy", 0) == "buy"
        assert TradierClient.resolve_side("buy", 0.0) == "buy"

    def test_alpaca_enum_values_map(self):
        """The engine passes an OrderSide enum, not a string."""
        from alpaca.trading.enums import OrderSide
        assert TradierClient.resolve_side(OrderSide.BUY.value, 0) == "buy"
        assert TradierClient.resolve_side(OrderSide.SELL.value, 5) == "sell"

    def test_a_fractional_short_still_covers(self):
        assert TradierClient.resolve_side("buy", -0.5) == "buy_to_cover"


class TestWorkingOrdersCountAsPosition:
    """Found by a real sandbox order on 2026-09-04, not by reasoning.

    With a buy still pending and /positions returning null, Tradier rejected a
    sell_short: "Sell short order cannot be placed while you have a current
    long position, please check open orders." The venue validates against
    filled position PLUS working orders. A scalper that fires every few seconds
    almost always has something in flight, so reading filled positions alone
    picks a side the venue refuses."""

    def _client_with(self, filled, orders):
        c = _client()
        c.net_position = lambda sym, force=False: filled
        c.get_orders = lambda status="open": orders
        return c

    def test_a_pending_buy_makes_the_book_effectively_long(self):
        c = self._client_with(0.0, [TradierOrder("1", "pending", 0.0, "QQQ", "buy", 1.0)])
        assert c.working_quantity("QQQ") == 1.0
        assert c.effective_position("QQQ") == 1.0
        assert TradierClient.resolve_side("sell", c.effective_position("QQQ")) == "sell"

    def test_without_this_the_resolver_would_pick_the_rejected_side(self):
        """The regression, stated directly: filled-only reads 0 -> sell_short,
        which is exactly what the sandbox rejected."""
        c = self._client_with(0.0, [TradierOrder("1", "pending", 0.0, "QQQ", "buy", 1.0)])
        assert TradierClient.resolve_side("sell", c.net_position("QQQ")) == "sell_short"
        assert TradierClient.resolve_side("sell", c.effective_position("QQQ")) == "sell"

    def test_a_pending_sell_short_makes_the_book_effectively_short(self):
        c = self._client_with(0.0, [TradierOrder("1", "open", 0.0, "QQQ", "sell_short", 2.0)])
        assert c.effective_position("QQQ") == -2.0
        assert TradierClient.resolve_side("buy", c.effective_position("QQQ")) == "buy_to_cover"

    def test_only_the_unfilled_remainder_counts(self):
        c = self._client_with(3.0, [TradierOrder("1", "partially_filled", 2.0, "QQQ", "buy", 5.0)])
        assert c.working_quantity("QQQ") == 3.0
        assert c.effective_position("QQQ") == 6.0

    def test_other_symbols_are_ignored(self):
        c = self._client_with(0.0, [TradierOrder("1", "open", 0.0, "TQQQ", "buy", 9.0)])
        assert c.working_quantity("QQQ") == 0.0

    def test_an_unreadable_order_book_contributes_nothing_rather_than_guessing(self):
        c = _client()
        c.net_position = lambda sym, force=False: 4.0

        def boom(status="open"):
            raise RuntimeError("api down")
        c.get_orders = boom
        assert c.working_quantity("QQQ") == 0.0
        assert c.effective_position("QQQ") == 4.0

    def test_an_order_in_flight_blocks_a_new_one_on_that_symbol(self):
        """The safest resolution of the whole class: never stack on an
        unsettled book. With nothing working, filled IS effective and the side
        is unambiguous; with something working, wait."""
        cap = []
        c = _client()
        c.net_position = lambda sym, force=False: 0.0
        c.get_orders = lambda status="open": [TradierOrder("1", "pending", 0.0, "QQQ", "buy", 5.0)]
        c._request = lambda m, p, params=None: cap.append(params) or {"order": {"id": 1}}
        from alpaca.trading.enums import OrderSide
        with pytest.raises(TradierError, match="already working"):
            c.market_order("QQQ", 3, OrderSide.SELL)
        assert not cap, "must not submit while one of our own orders is in flight"

    def test_a_quiet_book_submits_normally(self):
        cap = []
        c = _client()
        c.net_position = lambda sym, force=False: 0.0
        c.get_orders = lambda status="open": []
        c.invalidate_positions = lambda: None
        c._request = lambda m, p, params=None: cap.append(params) or {"order": {"id": 1}}
        from alpaca.trading.enums import OrderSide
        c.market_order("QQQ", 3, OrderSide.SELL)
        assert cap and cap[0]["side"] == "sell_short"


class TestClosingQuantityIsClamped:
    """Tradier rejects the whole order if you ask to close more than exists."""

    def test_covering_more_than_the_short_is_clamped(self):
        assert TradierClient._closing_qty("buy", -3, 10) == 3

    def test_selling_more_than_the_long_is_clamped(self):
        assert TradierClient._closing_qty("sell", 4, 10) == 4

    def test_opening_is_never_clamped(self):
        assert TradierClient._closing_qty("buy", 0, 10) == 10
        assert TradierClient._closing_qty("sell", 0, 10) == 10
        assert TradierClient._closing_qty("buy", 5, 10) == 10     # adding to a long


class TestMarketOrder:
    def _submitting(self, net, captured):
        c = _client()
        c.net_position = lambda sym, force=False: net
        c.working_quantity = lambda sym: 0.0        # nothing in flight
        c.invalidate_positions = lambda: None

        def req(method, path, params=None):
            captured.append((method, path, params))
            return {"order": {"id": 123, "status": "ok"}}
        c._request = req
        return c

    def test_a_sell_while_flat_opens_a_short_with_day_duration(self):
        cap = []
        c = self._submitting(0, cap)
        from alpaca.trading.enums import OrderSide, TimeInForce
        o = c.market_order("QQQ", 10, OrderSide.SELL, TimeInForce.IOC)
        _, _, params = cap[0]
        assert params["side"] == "sell_short"
        assert params["duration"] == "day", "Tradier rejects ioc; day is the mapping"
        assert params["type"] == "market"
        assert o.id == "123"

    def test_a_sell_while_long_closes_the_long(self):
        cap = []
        c = self._submitting(10, cap)
        from alpaca.trading.enums import OrderSide
        c.market_order("QQQ", 10, OrderSide.SELL)
        assert cap[0][2]["side"] == "sell"

    def test_a_buy_while_short_covers_and_clamps_to_the_short(self):
        cap = []
        c = self._submitting(-3, cap)
        from alpaca.trading.enums import OrderSide
        c.market_order("QQQ", 10, OrderSide.BUY)
        assert cap[0][2]["side"] == "buy_to_cover"
        assert cap[0][2]["quantity"] == "3", "never ask to cover more than is short"

    def test_nothing_to_close_raises_rather_than_sending_a_wrong_side(self):
        """Clamping to zero must not silently become an opening trade."""
        c = _client()
        c.net_position = lambda sym, force=False: 0
        c.working_quantity = lambda sym: 0.0
        c._request = lambda *a, **k: pytest.fail("must not submit")
        c._closing_qty = staticmethod(lambda d, n, q: 0)
        from alpaca.trading.enums import OrderSide
        with pytest.raises(TradierError):
            c.market_order("QQQ", 10, OrderSide.SELL)

    def test_a_rejection_invalidates_the_position_cache(self):
        """A side-related refusal means the position read was stale; the next
        attempt must re-read rather than repeat the same wrong side."""
        c = _client()
        c.net_position = lambda sym, force=False: 0
        c.working_quantity = lambda sym: 0.0
        invalidated = []
        c.invalidate_positions = lambda: invalidated.append(1)

        def boom(*a, **k):
            raise TradierError("Tradier 400: bad side", body='{"message":"x"}')
        c._request = boom
        from alpaca.trading.enums import OrderSide
        with pytest.raises(TradierError):
            c.market_order("QQQ", 10, OrderSide.SELL)
        assert invalidated, "cache must be dropped after a refusal"

    def test_the_venue_error_body_is_readable_by_the_engines_parser(self):
        """vampire_engine._reject_facts parses exc.response.text as JSON."""
        from src.strategies.vampire_engine import VampireEngine
        exc = TradierError("Tradier 400", body='{"available":"9","existing_qty":"9"}')
        assert VampireEngine._reject_facts(exc) == {"available": 9, "existing_qty": 9}


class TestPositionsAreBrokerTruth:
    def test_net_position_reads_the_broker_not_a_counter(self):
        c = _client()
        c.get_positions = lambda: [type("P", (), {"symbol": "QQQ", "qty": -7.0})()]
        assert c.net_position("QQQ") == -7.0

    def test_an_unknown_symbol_is_flat_not_an_error(self):
        c = _client()
        c.get_positions = lambda: []
        assert c.net_position("QQQ") == 0.0

    def test_a_failed_read_falls_back_to_the_cache_rather_than_guessing_flat(self):
        """Returning 0 on an API failure would open a short against a long."""
        c = _client()
        c._pos_cache, c._pos_at = {"QQQ": -5.0}, 0.0

        def boom():
            raise RuntimeError("api down")
        c.get_positions = boom
        assert c.net_position("QQQ") == -5.0

    def test_positions_are_cached_briefly_to_respect_the_rate_limit(self):
        c = _client()
        calls = []

        def once():
            calls.append(1)
            return [type("P", (), {"symbol": "QQQ", "qty": 3.0})()]
        c.get_positions = once
        c.net_position("QQQ"); c.net_position("QQQ"); c.net_position("QQQ")
        assert len(calls) == 1

    def test_force_bypasses_the_cache(self):
        c = _client()
        calls = []

        def each():
            calls.append(1)
            return [type("P", (), {"symbol": "QQQ", "qty": 3.0})()]
        c.get_positions = each
        c.net_position("QQQ"); c.net_position("QQQ", force=True)
        assert len(calls) == 2


class TestClosePosition:
    def test_a_long_is_closed_with_sell(self):
        cap = []
        c = _client()
        c.net_position = lambda s, force=False: 12
        c.invalidate_positions = lambda: None
        c._request = lambda m, p, params=None: cap.append(params) or {"order": {"id": 1}}
        c.close_position("QQQ")
        assert cap[0]["side"] == "sell" and cap[0]["quantity"] == "12"

    def test_a_short_is_closed_with_buy_to_cover(self):
        cap = []
        c = _client()
        c.net_position = lambda s, force=False: -8
        c.invalidate_positions = lambda: None
        c._request = lambda m, p, params=None: cap.append(params) or {"order": {"id": 1}}
        c.close_position("QQQ")
        assert cap[0]["side"] == "buy_to_cover" and cap[0]["quantity"] == "8"

    def test_flat_sends_nothing(self):
        c = _client()
        c.net_position = lambda s, force=False: 0
        c._request = lambda *a, **k: pytest.fail("must not submit against a flat book")
        assert c.close_position("QQQ") is None


class TestOrderShapeMatchesWhatTheEngineReads:
    def test_get_order_exposes_status_and_filled_qty(self):
        c = _client()
        c._request = lambda *a, **k: {"order": {"id": 9, "status": "filled",
                                                "exec_quantity": "10", "symbol": "QQQ"}}
        o = c.get_order("9")
        assert o.status == "filled" and o.filled_qty == 10.0 and o.symbol == "QQQ"

    def test_the_engine_can_read_a_terminal_status_off_it(self):
        from src.strategies.vampire_engine import VampireEngine
        assert VampireEngine._is_terminal(TradierOrder("1", "filled", 10, "QQQ").status)
        assert not VampireEngine._is_terminal(TradierOrder("1", "open", 0, "QQQ").status)

    def test_the_engine_can_read_filled_qty_off_it(self):
        from src.strategies.vampire_engine import VampireEngine
        assert VampireEngine._filled_or(TradierOrder("1", "filled", 7, "QQQ"), 10) == 7

    def test_get_orders_open_filters_terminal_ones(self):
        c = _client()
        c._request = lambda *a, **k: {"orders": {"order": [
            {"id": 1, "status": "filled", "symbol": "QQQ"},
            {"id": 2, "status": "open", "symbol": "QQQ"}]}}
        assert [o.id for o in c.get_orders("open")] == ["2"]

    def test_no_orders_is_an_empty_list_not_a_crash(self):
        c = _client()
        c._request = lambda *a, **k: {"orders": "null"}
        assert c.get_orders() == []

    def test_no_positions_is_an_empty_list_not_a_crash(self):
        c = _client()
        c._request = lambda *a, **k: {"positions": "null"}
        assert c.get_positions() == []


class TestMarketDataAdapter:
    def _md(self):
        from src.core.tradier_market_data import TradierMarketData
        m = TradierMarketData.__new__(TradierMarketData)
        import threading
        from collections import deque
        m._token, m._base, m._timeout = "t", "x", 10.0
        m.poll_seconds, m._symbols, m._on_quote = 1.0, [], None
        m._stop, m._lock = threading.Event(), threading.Lock()
        m._history, m._last = {}, {}
        return m

    def test_one_request_covers_every_symbol(self):
        m = self._md()
        seen = []
        m._get = lambda path: seen.append(path) or {"quotes": {"quote": [
            {"symbol": "QQQ", "bid": 717.5, "ask": 717.54},
            {"symbol": "TQQQ", "bid": 72.04, "ask": 72.05}]}}
        got = m.fetch_quotes(["QQQ", "TQQQ"])
        # The comma is percent-encoded by urllib.parse.quote. Verified against
        # the live sandbox on 2026-09-04: Tradier returns both quotes for the
        # encoded and the literal form, so either is correct on the wire.
        assert len(seen) == 1
        assert "QQQ" in seen[0] and "TQQQ" in seen[0]
        assert round(got["QQQ"].mid, 3) == 717.52

    def test_crossed_and_zero_quotes_are_dropped(self):
        m = self._md()
        m._get = lambda path: {"quotes": {"quote": [
            {"symbol": "A", "bid": 0, "ask": 0},
            {"symbol": "B", "bid": 10, "ask": 9},
            {"symbol": "C", "bid": 10, "ask": 10.02}]}}
        assert list(m.fetch_quotes(["A", "B", "C"])) == ["C"]

    def test_recent_spread_refuses_a_thin_window(self):
        """Fail safe: too little of the book means keep the configured
        threshold, never derive one from a handful of polls."""
        m = self._md()
        m._get = lambda path: {"quotes": {"quote": [{"symbol": "QQQ", "bid": 10, "ask": 10.05}]}}
        for _ in range(5):
            m.fetch_quotes(["QQQ"])
        assert m.recent_spread("QQQ") is None

    def test_recent_spread_reports_the_distribution_once_deep_enough(self):
        from src.core.tradier_market_data import MIN_SPREAD_SAMPLES
        m = self._md()
        m._get = lambda path: {"quotes": {"quote": [{"symbol": "QQQ", "bid": 10, "ask": 10.05}]}}
        for _ in range(MIN_SPREAD_SAMPLES + 5):
            m.fetch_quotes(["QQQ"])
        s = m.recent_spread("QQQ")
        assert s["n"] >= MIN_SPREAD_SAMPLES
        assert round(s["median"], 4) == 0.05
        assert round(s["price"], 3) == 10.025

    def test_vwap_averages_the_recent_window(self):
        m = self._md()
        m._get = lambda path: {"quotes": {"quote": [{"symbol": "QQQ", "bid": 10, "ask": 10.02}]}}
        m.fetch_quotes(["QQQ"])
        assert round(m.get_vwap("QQQ", 60), 3) == 10.01

    def test_vwap_is_none_with_no_history(self):
        assert self._md().get_vwap("QQQ", 60) is None

    def test_minute_bars_are_shaped_for_the_regime_advisor(self):
        from src.strategies.regime_advisor import format_bars
        m = self._md()
        m._get = lambda path: {"series": {"data": [
            {"timestamp": 1788534000, "open": 717.75, "high": 718.0,
             "low": 717.63, "close": 717.87, "volume": 76765}]}}
        bars = m.get_recent_minute_bars("QQQ", 90)
        assert len(bars) == 1
        assert format_bars(bars)[0].endswith("o=717.75 h=718.00 l=717.63 c=717.87 v=76765")
