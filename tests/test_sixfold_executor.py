"""SIXFOLD is the largest sleeve and the only signal that has never traded.

That combination is why every gate applies here rather than fewer of them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.strategies.sixfold_executor import SixfoldExecutor


def _exec(candidates=("JPM",), price=200.0, positions=None, sleeve=50_000.0,
          equity=100_000.0, can_trade=True, breaker_ok=True, excluded=("AAPL", "SPY", "QQQ")):
    client, data, tracker, breaker, allocator, analyst = (MagicMock() for _ in range(6))
    data.get_latest_quote.return_value = MagicMock(mid=price)
    tracker.get_snapshot.return_value = MagicMock(equity=equity, positions=positions or {})
    allocator.get_budget.return_value = MagicMock(sixfold_budget=sleeve)
    breaker.check.return_value = breaker_ok
    breaker.can_trade.return_value = can_trade
    breaker.limits = MagicMock(max_single_trade_pct=0.05)
    analyst.get_buy_candidates.return_value = list(candidates)
    ex = SixfoldExecutor(client, data, tracker, breaker, allocator, analyst,
                         excluded=set(excluded))
    return ex, client


class TestItActuallyTrades:
    def test_a_candidate_becomes_an_order(self):
        ex, client = _exec(["JPM"], price=200.0)
        result = ex.run_cycle()
        assert result["status"] == "ok" and len(result["orders"]) == 1
        client.trading.submit_order.assert_called_once()

    def test_size_is_the_sleeve_split_capped_by_the_per_trade_limit(self):
        """$50k over 10 names is $5,000; the 5% cap on $100k is also $5,000."""
        ex, _ = _exec()
        assert ex.position_budget() == pytest.approx(5_000.0)

    def test_quantity_fits_the_per_name_budget(self):
        ex, _ = _exec(["JPM"], price=200.0)
        order = ex.run_cycle()["orders"][0]
        assert order["qty"] == 25 and order["notional"] <= 5_000.0

    def test_orders_are_limit_not_market(self):
        ex, client = _exec(["JPM"], price=200.0)
        ex.run_cycle()
        req = client.trading.submit_order.call_args[0][0]
        assert req.limit_price is not None and req.limit_price >= 200.0

    def test_several_candidates_all_trade_within_the_sleeve(self):
        ex, _ = _exec(["JPM", "V", "KO", "PG"], price=100.0)
        orders = ex.run_cycle()["orders"]
        assert len(orders) == 4
        assert sum(o["notional"] for o in orders) <= 50_000.0


class TestGatesApply:
    def test_a_tripped_breaker_stops_everything(self):
        ex, client = _exec(breaker_ok=False)
        assert ex.run_cycle()["status"] == "breaker_active"
        client.trading.submit_order.assert_not_called()

    def test_the_per_trade_limit_is_consulted(self):
        ex, client = _exec(can_trade=False)
        assert ex.run_cycle()["orders"] == []
        client.trading.submit_order.assert_not_called()

    def test_the_sleeve_budget_bounds_total_exposure(self):
        ex, _ = _exec(["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"],
                      price=400.0)
        orders = ex.run_cycle()["orders"]
        assert sum(o["notional"] for o in orders) <= 50_000.0

    def test_the_concurrent_position_limit_holds(self):
        ex, _ = _exec([f"S{i}" for i in range(20)], price=10.0)
        assert len(ex.run_cycle()["orders"]) <= 10

    def test_symbols_another_sleeve_trades_are_refused(self):
        """AAPL is in both the sixfold universe and the scalper's, so a position
        in it could not be attributed to either."""
        ex, client = _exec(["AAPL"])
        assert ex.run_cycle()["orders"] == []
        assert any("another sleeve" in r["reason"] for r in ex.last_rejections)

    def test_an_existing_holding_is_not_doubled(self):
        ex, _ = _exec(["JPM"], positions={"JPM": {"market_value": 4_000.0}})
        assert ex.run_cycle()["orders"] == []

    def test_a_name_too_expensive_for_one_share_is_refused(self):
        ex, _ = _exec(["BRK.A"], price=700_000.0)
        assert ex.run_cycle()["orders"] == []
        assert any("exceeds" in r["reason"] for r in ex.last_rejections)

    def test_no_quote_means_no_order(self):
        ex, client = _exec(["JPM"])
        ex._data.get_latest_quote.return_value = None
        assert ex.run_cycle()["orders"] == []
        client.trading.submit_order.assert_not_called()

    def test_zero_sleeve_trades_nothing(self):
        ex, client = _exec(sleeve=0.0)
        assert ex.run_cycle()["status"] == "no_sleeve"
        client.trading.submit_order.assert_not_called()


class TestFailureIsSafe:
    def test_an_analyst_error_does_not_raise_or_trade(self):
        ex, client = _exec()
        ex._analyst.get_buy_candidates.side_effect = RuntimeError("yfinance down")
        assert ex.run_cycle()["status"] == "analyst_error"
        client.trading.submit_order.assert_not_called()

    def test_a_broker_rejection_is_recorded_and_the_cycle_continues(self):
        ex, client = _exec(["JPM", "V"], price=100.0)
        client.trading.submit_order.side_effect = [RuntimeError("rejected"), MagicMock(id="2")]
        result = ex.run_cycle()
        assert len(result["orders"]) == 1
        assert any("broker" in r["reason"] for r in ex.last_rejections)

    def test_rejections_are_capped(self):
        ex, _ = _exec([f"S{i}" for i in range(60)], price=700_000.0)
        ex.run_cycle()
        assert len(ex.last_rejections) <= 20
