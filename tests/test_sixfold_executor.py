"""SIXFOLD is the largest sleeve and the only signal that has never traded.

That combination is why every gate applies here rather than fewer of them.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.core.finance_advisor import AdvisorOpinion, CouncilDecision
from src.strategies.sixfold_executor import SixfoldExecutor

COUNCIL_PATCH = "src.strategies.sixfold_executor.evaluate_equity_buy"


def _council_approve(symbol, score, fundamentals=None):
    return CouncilDecision(
        action="buy", symbol=symbol, approved=True,
        votes_for=3, votes_against=0, abstentions=0,
        opinions=[
            AdvisorOpinion("dell4-finance", "Finance Specialist", "approve", "Solid fundamentals", True),
            AdvisorOpinion("dell4-chat", "General Strategist", "approve", "Good timing", True),
            AdvisorOpinion("dell4-qwen38", "Risk Analyst", "approve", "Acceptable risk", True),
        ],
        summary="Council approved: 3 for, 0 against, 0 abstain",
    )


def _council_reject(symbol, score, fundamentals=None):
    return CouncilDecision(
        action="buy", symbol=symbol, approved=False,
        votes_for=0, votes_against=3, abstentions=0,
        opinions=[
            AdvisorOpinion("dell4-finance", "Finance Specialist", "reject", "Overvalued", True),
            AdvisorOpinion("dell4-chat", "General Strategist", "reject", "Bad timing", True),
            AdvisorOpinion("dell4-qwen38", "Risk Analyst", "reject", "High risk", True),
        ],
        summary="Council rejected: 0 for, 3 against, 0 abstain",
    )


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
    analyst.get_disposal_candidates.return_value = []
    analyst.scores = {}
    ex = SixfoldExecutor(client, data, tracker, breaker, allocator, analyst,
                         excluded=set(excluded))
    return ex, client


@patch(COUNCIL_PATCH, side_effect=_council_approve)
class TestItActuallyTrades:
    def test_a_candidate_becomes_an_order(self, _m):
        ex, client = _exec(["JPM"], price=200.0)
        result = ex.run_cycle()
        assert result["status"] == "ok" and len(result["orders"]) == 1
        client.submit_order.assert_called_once()

    def test_size_is_the_sleeve_split_capped_by_the_per_trade_limit(self, _m):
        """$50k over 10 names is $5,000; the 5% cap on $100k is also $5,000."""
        ex, _ = _exec()
        assert ex.position_budget() == pytest.approx(5_000.0)

    def test_quantity_fits_the_per_name_budget(self, _m):
        ex, _ = _exec(["JPM"], price=200.0)
        order = ex.run_cycle()["orders"][0]
        assert order["qty"] == 25 and order["notional"] <= 5_000.0

    def test_orders_are_limit_not_market(self, _m):
        ex, client = _exec(["JPM"], price=200.0)
        ex.run_cycle()
        req = client.submit_order.call_args[0][0]
        assert req.limit_price is not None and req.limit_price >= 200.0

    def test_several_candidates_all_trade_within_the_sleeve(self, _m):
        ex, _ = _exec(["JPM", "V", "KO", "PG"], price=100.0)
        orders = ex.run_cycle()["orders"]
        assert len(orders) == 4
        assert sum(o["notional"] for o in orders) <= 50_000.0


@patch(COUNCIL_PATCH, side_effect=_council_approve)
class TestGatesApply:
    def test_a_tripped_breaker_stops_everything(self, _m):
        ex, client = _exec(breaker_ok=False)
        assert ex.run_cycle()["status"] == "breaker_active"
        client.submit_order.assert_not_called()

    def test_the_per_trade_limit_is_consulted(self, _m):
        ex, client = _exec(can_trade=False)
        assert ex.run_cycle()["orders"] == []
        client.submit_order.assert_not_called()

    def test_the_sleeve_budget_bounds_total_exposure(self, _m):
        ex, _ = _exec(["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"],
                      price=400.0)
        orders = ex.run_cycle()["orders"]
        assert sum(o["notional"] for o in orders) <= 50_000.0

    def test_the_concurrent_position_limit_holds(self, _m):
        ex, _ = _exec([f"S{i}" for i in range(20)], price=10.0)
        assert len(ex.run_cycle()["orders"]) <= 10

    def test_symbols_another_sleeve_trades_are_refused(self, _m):
        """AAPL is in both the sixfold universe and the scalper's, so a position
        in it could not be attributed to either."""
        ex, client = _exec(["AAPL"])
        assert ex.run_cycle()["orders"] == []
        assert any("another sleeve" in r["reason"] for r in ex.last_rejections)

    def test_an_existing_holding_is_not_doubled(self, _m):
        ex, _ = _exec(["JPM"], positions={"JPM": {"market_value": 4_000.0}})
        assert ex.run_cycle()["orders"] == []

    def test_a_name_too_expensive_for_one_share_is_refused(self, _m):
        ex, _ = _exec(["BRK.A"], price=700_000.0)
        assert ex.run_cycle()["orders"] == []
        assert any("exceeds" in r["reason"] for r in ex.last_rejections)

    def test_no_quote_means_no_order(self, _m):
        ex, client = _exec(["JPM"])
        ex._data.get_latest_quote.return_value = None
        assert ex.run_cycle()["orders"] == []
        client.submit_order.assert_not_called()

    def test_zero_sleeve_trades_nothing(self, _m):
        ex, client = _exec(sleeve=0.0)
        assert ex.run_cycle()["status"] == "no_sleeve"
        client.submit_order.assert_not_called()


@patch(COUNCIL_PATCH, side_effect=_council_approve)
class TestFailureIsSafe:
    def test_an_analyst_error_does_not_raise_or_trade(self, _m):
        ex, client = _exec()
        ex._analyst.get_buy_candidates.side_effect = RuntimeError("yfinance down")
        assert ex.run_cycle()["status"] == "analyst_error"
        client.submit_order.assert_not_called()

    def test_a_broker_rejection_is_recorded_and_the_cycle_continues(self, _m):
        ex, client = _exec(["JPM", "V"], price=100.0)
        client.submit_order.side_effect = [RuntimeError("rejected"), MagicMock(id="2")]
        result = ex.run_cycle()
        assert len(result["orders"]) == 1
        assert any("broker" in r["reason"] for r in ex.last_rejections)

    def test_rejections_are_capped(self, _m):
        ex, _ = _exec([f"S{i}" for i in range(60)], price=700_000.0)
        ex.run_cycle()
        assert len(ex.last_rejections) <= 20


@patch(COUNCIL_PATCH, side_effect=_council_reject)
class TestCouncilVeto:
    def test_council_rejection_prevents_order(self, _m):
        ex, client = _exec(["JPM"], price=200.0)
        result = ex.run_cycle()
        assert result["orders"] == []
        client.submit_order.assert_not_called()
        assert any("Council rejected" in r["reason"] for r in ex.last_rejections)

    def test_council_rejection_logs_advisor_reasons(self, _m):
        ex, _ = _exec(["MSFT"], price=400.0)
        ex.run_cycle()
        rejection = ex.last_rejections[0]
        assert "Council rejected" in rejection["reason"]


@patch(COUNCIL_PATCH, side_effect=_council_approve)
class TestTheConcurrencyLimitCountsOnlyThisSleeve:
    """_held() returns every equity position in the account, so counting it
    raw let another sleeve's open names consume SIXFOLD's position budget.

    Live on 2026-09-01: SIXFOLD held 8 names and the scalper held 2-4, so the
    count was 10-12 against a limit of 10 and every new candidate was rejected
    as "at the limit". The sleeve sat at $38K of its $50K while KO (66.0) and
    HD (65.0) both scored above the buy threshold. committed() already drew
    the sleeve boundary for dollars; the count did not draw it for slots.
    """

    def test_another_sleeves_positions_do_not_consume_the_limit(self, _m):
        """8 owned + 4 excluded is 12 raw, but only 8 belong to this sleeve,
        so a 9th name must still be buyable."""
        held = {f"OWN{i}": {"market_value": 4_800.0} for i in range(8)}
        held.update({s: {"market_value": 3_000.0}
                     for s in ("SPY", "QQQ", "HOOD", "TQQQ")})
        ex, client = _exec(["KO"], price=100.0, positions=held,
                           excluded=("SPY", "QQQ", "HOOD", "TQQQ"))
        result = ex.run_cycle()
        assert len(result["orders"]) == 1, (
            f"expected KO to be bought; rejections were {ex.last_rejections}"
        )
        client.submit_order.assert_called_once()

    def test_the_limit_still_binds_on_this_sleeves_own_positions(self, _m):
        """The guard must still work: 10 owned means no room for an 11th."""
        held = {f"OWN{i}": {"market_value": 4_800.0} for i in range(10)}
        ex, client = _exec(["KO"], price=100.0, positions=held,
                           excluded=("SPY", "QQQ"), sleeve=200_000.0)
        ex.run_cycle()
        client.submit_order.assert_not_called()
        assert any("position limit" in r["reason"] for r in ex.last_rejections)


@patch(COUNCIL_PATCH, side_effect=_council_approve)
class TestInFlightOrdersAreNotDoubled:
    def test_a_second_cycle_does_not_rebuy_before_the_broker_shows_the_fill(self, _m):
        """Staging 2026-09-01: 59 submits of the same 8 names while positions
        in the coordinator snapshot were still 0."""
        ex, client = _exec(["JPM"], price=200.0)
        first = ex.run_cycle()
        assert len(first["orders"]) == 1
        second = ex.run_cycle()
        assert second["orders"] == []
        assert client.submit_order.call_count == 1
        assert any("in-flight" in r["reason"] for r in ex.last_rejections)

    def test_in_flight_clears_once_the_position_is_visible(self, _m):
        ex, client = _exec(["JPM"], price=200.0)
        ex.run_cycle()
        ex._tracker.get_snapshot.return_value = MagicMock(
            equity=100_000.0, positions={"JPM": {"market_value": 5_000.0}},
        )
        ex.run_cycle()
        assert client.submit_order.call_count == 1
        assert any("already held" in r["reason"] for r in ex.last_rejections)
