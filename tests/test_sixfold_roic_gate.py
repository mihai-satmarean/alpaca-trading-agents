"""A name whose ROIC could not be measured is not bought.

Raising an unmeasurable ROIC lens from 0 to a neutral 50 is right for the
ranking and, on its own, wrong for the buy list: 50 on the heaviest lens can
carry a name over the buy threshold on the strength of the five lenses that
happened to be computable. Not being able to answer the framework's central
question is a reason to pass on a name, so the buy side filters on it.

The existing executor tests hand the analyst a MagicMock, whose every attribute
is truthy, so they cannot see this gate at all. These use real SixfoldScore
objects for that reason.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.core.finance_advisor import AdvisorOpinion, CouncilDecision
from src.strategies.sixfold_engine import ConfidenceTier, SixfoldScore
from src.strategies.sixfold_executor import SixfoldExecutor

COUNCIL_PATCH = "src.strategies.sixfold_executor.evaluate_equity_buy"


def _approve(symbol, score, fundamentals=None):
    return CouncilDecision(
        action="buy", symbol=symbol, approved=True,
        votes_for=3, votes_against=0, abstentions=0,
        opinions=[AdvisorOpinion("dell4-finance", "Finance Specialist",
                                 "approve", "Solid fundamentals", True)],
        summary="Council approved: 3 for, 0 against, 0 abstain",
    )


def _score(symbol: str, roic_measured: bool) -> SixfoldScore:
    return SixfoldScore(
        symbol=symbol,
        composite_score=72.0,
        confidence=ConfidenceTier.SCREENING,
        roic_measured=roic_measured,
    )


def _exec(scores: dict, candidates, price=200.0, sleeve=50_000.0, equity=100_000.0):
    client, data, tracker, breaker, allocator, analyst = (MagicMock() for _ in range(6))
    data.get_latest_quote.return_value = MagicMock(mid=price)
    tracker.get_snapshot.return_value = MagicMock(equity=equity, positions={})
    allocator.get_budget.return_value = MagicMock(sixfold_budget=sleeve)
    breaker.check.return_value = True
    breaker.can_trade.return_value = True
    breaker.limits = MagicMock(max_single_trade_pct=0.05)
    analyst.get_buy_candidates.return_value = list(candidates)
    analyst.scores = scores
    ex = SixfoldExecutor(client, data, tracker, breaker, allocator, analyst,
                         excluded={"SPY", "QQQ"})
    return ex, client


@patch(COUNCIL_PATCH, side_effect=_approve)
class TestTheRoicGate:

    def test_a_bank_is_not_bought(self, _m):
        """JPM scores 72 with a neutral ROIC lens it never actually measured."""
        ex, client = _exec({"JPM": _score("JPM", roic_measured=False)}, ["JPM"])

        result = ex.run_cycle()

        assert result["orders"] == []
        client.trading.submit_order.assert_not_called()
        assert any("ROIC could not be measured" in r["reason"]
                   for r in result["rejections"])

    def test_a_measured_name_still_trades(self, _m):
        """The gate must not close the buy side; this is the control."""
        ex, client = _exec({"MSFT": _score("MSFT", roic_measured=True)}, ["MSFT"])

        result = ex.run_cycle()

        assert len(result["orders"]) == 1
        client.trading.submit_order.assert_called_once()

    def test_it_filters_rather_than_halting(self, _m):
        """One unmeasurable name must not block the measurable ones behind it."""
        ex, _client = _exec(
            {"JPM": _score("JPM", roic_measured=False),
             "MSFT": _score("MSFT", roic_measured=True)},
            ["JPM", "MSFT"],
        )

        result = ex.run_cycle()

        assert [o["symbol"] for o in result["orders"]] == ["MSFT"]

    def test_a_missing_score_object_does_not_buy(self, _m):
        """Absence of evidence is not evidence of a measured ROIC."""
        ex, client = _exec({}, ["JPM"])

        result = ex.run_cycle()

        assert result["orders"] == []
        client.trading.submit_order.assert_not_called()

    def test_the_gate_runs_before_the_council(self, _m):
        """An unmeasurable name should never reach the advisors: the council is
        an expensive remote call and the answer is already known."""
        ex, _ = _exec({"JPM": _score("JPM", roic_measured=False)}, ["JPM"])

        ex.run_cycle()

        _m.assert_not_called()


@patch(COUNCIL_PATCH, side_effect=_approve)
class TestDisposalsAreUnaffected:

    def test_an_unmeasurable_name_can_still_be_sold(self, _m):
        """The gate is a buy gate. Blocking an exit on unmeasurable data would
        strand a position the analyst has already downgraded."""
        client, data, tracker, breaker, allocator, analyst = (MagicMock() for _ in range(6))
        tracker.get_snapshot.return_value = MagicMock(
            equity=100_000.0, positions={"JPM": {"market_value": 4_000.0, "qty": 20}})
        allocator.get_budget.return_value = MagicMock(sixfold_budget=0.0)
        breaker.check.return_value = True
        analyst.get_disposal_candidates.return_value = ["JPM"]
        analyst.get_buy_candidates.return_value = []
        analyst.scores = {"JPM": _score("JPM", roic_measured=False)}
        ex = SixfoldExecutor(client, data, tracker, breaker, allocator, analyst, excluded=set())

        result = ex.run_cycle()

        client.close_position.assert_called_once_with("JPM")
        assert [d["symbol"] for d in result["disposals"]] == ["JPM"]
