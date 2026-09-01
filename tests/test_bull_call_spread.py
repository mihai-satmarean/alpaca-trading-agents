"""Tests for the bull call spread strategy module."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.core.options_chain import OptionCandidate
from src.strategies.bull_call_spread import (
    BullCallSpreadStrategy,
    SpreadCandidate,
    SpreadConfig,
)


def _make_call(symbol: str, underlying: str, strike: float, dte: int = 14,
               oi: int = 500, premium: float | None = None) -> OptionCandidate:
    return OptionCandidate(
        symbol=symbol,
        underlying=underlying,
        contract_type="call",
        strike_price=strike,
        expiration=date.today() + timedelta(days=dte),
        open_interest=oi,
        premium_estimate=premium,
        days_to_expiry=dte,
    )


def _strategy(calls=None, quote_mid=100.0, can_trade=True, budget=10_000.0):
    client = MagicMock()
    chain = MagicMock()
    data = MagicMock()
    tracker = MagicMock()
    breaker = MagicMock()
    allocator = MagicMock()

    data.get_latest_quote.return_value = MagicMock(mid=quote_mid)

    if calls is not None:
        chain.get_calls.return_value = calls
        chain.select_best_expiry.return_value = calls
    else:
        chain.get_calls.return_value = []
        chain.select_best_expiry.return_value = []

    breaker.can_trade.return_value = can_trade
    allocator.get_budget.return_value = MagicMock(sixfold_budget=budget * 5)

    s = BullCallSpreadStrategy(
        client=client, chain=chain, data=data, tracker=tracker,
        allocator=allocator, breaker=breaker,
    )
    return s, client


class TestScanFindsValidSpreads:
    def test_two_calls_produce_one_spread(self):
        calls = [
            _make_call("AAPL260101C00099000", "AAPL", 99.0),
            _make_call("AAPL260101C00102000", "AAPL", 102.0),
        ]
        s, _ = _strategy(calls=calls, quote_mid=100.0)
        candidates = s.scan(["AAPL"])
        assert len(candidates) >= 1
        c = candidates[0]
        assert c.underlying == "AAPL"
        assert c.long_call.strike_price < c.short_call.strike_price

    def test_spread_width_is_correct(self):
        calls = [
            _make_call("X260101C00096000", "X", 96.0),
            _make_call("X260101C00099000", "X", 99.0),
        ]
        s, _ = _strategy(calls=calls, quote_mid=97.0)
        candidates = s.scan(["X"])
        assert len(candidates) >= 1
        assert candidates[0].spread_width == pytest.approx(3.0)

    def test_max_debit_is_per_contract_times_100(self):
        calls = [
            _make_call("X260101C00099000", "X", 99.0),
            _make_call("X260101C00102000", "X", 102.0),
        ]
        s, _ = _strategy(calls=calls, quote_mid=100.0)
        candidates = s.scan(["X"])
        if candidates:
            c = candidates[0]
            assert c.max_debit == c.spread_width * 0.55 * 100

    def test_no_calls_produces_no_candidates(self):
        s, _ = _strategy(calls=[], quote_mid=100.0)
        assert s.scan(["AAPL"]) == []

    def test_single_call_produces_no_spread(self):
        calls = [_make_call("X260101C00100000", "X", 100.0)]
        s, _ = _strategy(calls=calls, quote_mid=100.0)
        assert s.scan(["X"]) == []


class TestScoring:
    def test_higher_reward_risk_scores_better(self):
        calls = [
            _make_call("A260101C00099000", "A", 99.0, dte=14),
            _make_call("A260101C00101000", "A", 101.0, dte=14),
            _make_call("A260101C00103000", "A", 103.0, dte=14),
        ]
        s, _ = _strategy(calls=calls, quote_mid=100.0)
        candidates = s.scan(["A"])
        assert len(candidates) >= 2
        scores = [c.score for c in candidates]
        assert scores == sorted(scores, reverse=True)

    def test_breakeven_is_long_strike_plus_debit(self):
        calls = [
            _make_call("X260101C00096000", "X", 96.0),
            _make_call("X260101C00099000", "X", 99.0),
        ]
        s, _ = _strategy(calls=calls, quote_mid=97.0)
        candidates = s.scan(["X"])
        if candidates:
            c = candidates[0]
            expected_be = c.long_call.strike_price + (c.spread_width * 0.55)
            assert c.breakeven == pytest.approx(expected_be)


class TestExecution:
    def test_spread_order_uses_mleg(self):
        calls = [
            _make_call("MSFT260101C00398000", "MSFT", 398.0),
            _make_call("MSFT260101C00406000", "MSFT", 406.0),
        ]
        s, client = _strategy(calls=calls, quote_mid=400.0)
        client.trading.submit_order.return_value = MagicMock(id="order-123")
        orders = s.execute(["MSFT"], budget=5000.0)
        assert len(orders) == 1
        req = client.trading.submit_order.call_args[0][0]
        from alpaca.trading.enums import OrderClass
        assert req.order_class == OrderClass.MLEG

    def test_spread_order_has_two_legs(self):
        calls = [
            _make_call("V260101C00298000", "V", 298.0),
            _make_call("V260101C00307000", "V", 307.0),
        ]
        s, client = _strategy(calls=calls, quote_mid=300.0)
        client.trading.submit_order.return_value = MagicMock(id="order-456")
        s.execute(["V"], budget=5000.0)
        req = client.trading.submit_order.call_args[0][0]
        assert len(req.legs) == 2

    def test_long_leg_is_buy_short_leg_is_sell(self):
        calls = [
            _make_call("V260101C00298000", "V", 298.0),
            _make_call("V260101C00307000", "V", 307.0),
        ]
        s, client = _strategy(calls=calls, quote_mid=300.0)
        client.trading.submit_order.return_value = MagicMock(id="order-789")
        s.execute(["V"], budget=5000.0)
        req = client.trading.submit_order.call_args[0][0]
        from alpaca.trading.enums import OrderSide
        sides = {leg.side for leg in req.legs}
        assert OrderSide.BUY in sides
        assert OrderSide.SELL in sides

    def test_budget_limits_total_spreads(self):
        calls = [
            _make_call("F260101C00010000", "F", 10.0),
            _make_call("F260101C00011000", "F", 11.0),
        ]
        s, client = _strategy(calls=calls, quote_mid=10.5, budget=200.0)
        client.trading.submit_order.return_value = MagicMock(id="ok")
        orders = s.execute(["F"], budget=40.0)
        total_debit = sum(o["max_debit"] for o in orders)
        assert total_debit <= 40.0

    def test_max_spreads_per_symbol_respected(self):
        calls = [
            _make_call("F260101C00009000", "F", 9.0, dte=25),
            _make_call("F260101C00010000", "F", 10.0, dte=25),
            _make_call("F260101C00011000", "F", 11.0, dte=25),
            _make_call("F260101C00012000", "F", 12.0, dte=25),
        ]
        s, client = _strategy(calls=calls, quote_mid=10.0, budget=50_000.0)
        s.cfg.max_spreads_per_symbol = 1
        client.trading.submit_order.return_value = MagicMock(id="ok")
        orders = s.execute(["F"], budget=50_000.0)
        assert len(orders) <= 1


class TestRiskGates:
    def test_breaker_blocks_spread(self):
        calls = [
            _make_call("X260101C00099000", "X", 99.0),
            _make_call("X260101C00102000", "X", 102.0),
        ]
        s, client = _strategy(calls=calls, quote_mid=100.0, can_trade=False)
        orders = s.execute(["X"], budget=5000.0)
        assert orders == []
        client.trading.submit_order.assert_not_called()

    def test_broker_rejection_is_safe(self):
        calls = [
            _make_call("X260101C00099000", "X", 99.0),
            _make_call("X260101C00102000", "X", 102.0),
        ]
        s, client = _strategy(calls=calls, quote_mid=100.0)
        client.trading.submit_order.side_effect = RuntimeError("rejected")
        orders = s.execute(["X"], budget=5000.0)
        assert orders == []
        assert any("broker" in r["reason"] for r in s.last_rejections)

    def test_no_quote_means_no_spread(self):
        calls = [
            _make_call("X260101C00099000", "X", 99.0),
            _make_call("X260101C00102000", "X", 102.0),
        ]
        s, _ = _strategy(calls=calls, quote_mid=0.0)
        assert s.scan(["X"]) == []


class TestSpreadCandidateDataclass:
    def test_max_profit_is_width_minus_debit(self):
        c = SpreadCandidate(
            underlying="TEST",
            long_call=_make_call("L", "TEST", 100.0),
            short_call=_make_call("S", "TEST", 110.0),
            spread_width=10.0,
            max_debit=550.0,
            max_profit=450.0,
            breakeven=105.5,
            days_to_expiry=30,
            score=5.0,
        )
        assert c.max_profit == (c.spread_width * 100) - c.max_debit
