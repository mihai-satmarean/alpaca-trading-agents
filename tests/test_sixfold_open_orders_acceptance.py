"""Open orders that cannot be read must not look like an empty book.

Live 2026-11-16: a missed open-order read let HLNE be bought every cycle.
This test is red until a broker error skips the buy pass entirely.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

COUNCIL = "src.strategies.sixfold_executor.evaluate_equity_buy"


def _approve(symbol, score, fundamentals=None):
    from src.core.finance_advisor import AdvisorOpinion, CouncilDecision

    opinion = AdvisorOpinion(
        "dell4-finance", "Finance", "approve", "ok", True,
    )
    return CouncilDecision(
        action="buy",
        symbol=symbol,
        approved=True,
        votes_for=1,
        votes_against=0,
        abstentions=0,
        opinions=[opinion],
        summary="approved",
    )


@patch(COUNCIL, side_effect=_approve)
def test_unreadable_open_orders_place_no_buy(_mock):
    """A broker error while reading open buys must place nothing."""
    from src.strategies.sixfold_executor import SixfoldExecutor

    client, data, tracker, breaker, allocator, analyst = (
        MagicMock() for _ in range(6)
    )
    data.get_latest_quote.return_value = MagicMock(mid=200.0)
    tracker.get_snapshot.return_value = MagicMock(
        equity=100_000.0,
        cash=100_000.0,
        positions={},
    )
    client.get_orders.side_effect = RuntimeError("orders unread")
    allocator.get_budget.return_value = MagicMock(
        sixfold_budget=50_000.0,
    )
    breaker.check.return_value = True
    breaker.can_trade.return_value = True
    breaker.limits = MagicMock(max_single_trade_pct=0.05)
    analyst.get_buy_candidates.return_value = ["JPM"]
    analyst.scores.get.return_value = MagicMock(
        roic_measured=True,
        composite_score=80.0,
    )
    executor = SixfoldExecutor(
        client, data, tracker, breaker, allocator, analyst,
        excluded={"SPY", "QQQ"},
    )

    result = executor.run_cycle()

    assert result["orders"] == []
    client.trading.submit_order.assert_not_called()
