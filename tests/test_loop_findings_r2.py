"""Buying must not consult account cash: the slice never asked for a cash gate.

The ticket (unreadable open orders must skip the buy pass) says not to change
the cash gate, buy size, or thresholds except where the pinned acceptance test
requires it -- and it does not: that test supplies cash=100_000 and only
exercises the unreadable-book path, which skips the buy pass before any cash
check runs. The working tree nevertheless added a cash extraction, a
`cash < notional` rejection, and a per-order `cash -= notional` decrement in
run_cycle. Before that diff the executor never read cash at all; the sleeve's
own room is the dollar boundary the spec works with.

These tests pin the requested behaviour (no cash gate). They fail while the
gate exists and go green once it is removed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

COUNCIL_PATCH = "src.strategies.sixfold_executor.evaluate_equity_buy"


def _approve(symbol, score, fundamentals=None):
    from src.core.finance_advisor import AdvisorOpinion, CouncilDecision

    return CouncilDecision(
        action="buy", symbol=symbol, approved=True,
        votes_for=1, votes_against=0, abstentions=0,
        opinions=[AdvisorOpinion("dell4-finance", "Finance", "approve", "ok", True)],
        summary="approved",
    )


def _exec(candidates=("JPM",), price=200.0, sleeve=50_000.0,
          equity=100_000.0, cash=0.0):
    from src.strategies.sixfold_executor import SixfoldExecutor

    client, data, tracker, breaker, allocator, analyst = (
        MagicMock() for _ in range(6)
    )
    data.get_latest_quote.return_value = MagicMock(mid=price)
    tracker.get_snapshot.return_value = MagicMock(
        equity=equity, cash=cash, positions={},
    )
    client.get_orders.return_value = []
    allocator.get_budget.return_value = MagicMock(sixfold_budget=sleeve)
    breaker.check.return_value = True
    breaker.can_trade.return_value = True
    breaker.limits = MagicMock(max_single_trade_pct=0.05)
    analyst.get_buy_candidates.return_value = list(candidates)
    ex = SixfoldExecutor(client, data, tracker, breaker, allocator, analyst,
                         excluded={"SPY", "QQQ"})
    return ex, client


@patch(COUNCIL_PATCH, side_effect=_approve)
def test_insufficient_cash_does_not_refuse_a_buy(_m):
    """A $5,000 order must not be rejected because the snapshot shows $1,000
    of cash: no gate was authorized to compare the two. Sleeve room ($50,000
    against $0 committed) is the only dollar bound the spec keeps."""
    ex, client = _exec(["JPM"], price=200.0, cash=1_000.0)

    result = ex.run_cycle()

    assert len(result["orders"]) == 1, (
        f"a cash gate is refusing buys; rejections were {ex.last_rejections}")
    assert not any("cash" in r["reason"] for r in ex.last_rejections)
    client.trading.submit_order.assert_called_once()


@patch(COUNCIL_PATCH, side_effect=_approve)
def test_orders_do_not_draw_down_a_shared_cash_balance(_m):
    """Even the per-order cash decrement is out of spec: two $5,000 orders
    against an $8,000 balance must both be placed. The live account, not a
    value cached at cycle start, decides whether an order is fundable."""
    ex, client = _exec(["JPM", "V"], price=200.0, cash=8_000.0)

    result = ex.run_cycle()

    assert len(result["orders"]) == 2, (
        "a per-order cash decrement is rejecting later buys; "
        f"rejections were {ex.last_rejections}")
    assert client.trading.submit_order.call_count == 2
