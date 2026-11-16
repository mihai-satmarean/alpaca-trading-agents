"""Tests for the loop findings on the SIXFOLD open-orders slice.

Each test here is RED on the current tree because of a specific defect the
reviewer flagged, and goes GREEN once that defect is fixed. None of them fix
the code; they only pin the required behavior.

- Finding 1 (high): an out-of-scope cash gate was added to the buy path. The
  ticket says do not change the cash gate unless required; the pinned
  acceptance test fails closed before reaching it, so it is not required. The
  gate must be removed, which is what these existing-buy shapes demand.
- Finding 2 (medium): committed() re-reads the order book and fails OPEN on
  that second read, treating an unreadable book as $0 pending. The room
  computation must reflect the notional from the read that already succeeded.
- Finding 3 (medium): pending-buy notional is summed across the whole account,
  so an excluded sleeve's open buy silently consumes SIXFOLD's room. It must
  be scoped to non-excluded names, mirroring the held-side filter.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

COUNCIL = "src.strategies.sixfold_executor.evaluate_equity_buy"


def _approved_council():
    """A council stub that approves everything it is asked about."""
    return MagicMock(approved=True, summary="approved", opinions=[],
                     votes_for=1, votes_against=0, abstentions=0)


def _order(symbol, side, qty, limit_price):
    return MagicMock(symbol=symbol, side=side, qty=qty, limit_price=limit_price)


def _components(price=200.0, sleeve=50_000.0, equity=100_000.0,
                positions=None, candidates=("JPM",), cash=100_000.0,
                open_orders=(), open_orders_error=None,
                excluded=("SPY", "QQQ")):
    """Build the six injected collaborators for a SixfoldExecutor.

    cash=None leaves the snapshot without a usable cash value, so the current
    float(getattr(snap, 'cash', 0.0) or 0.0) read collapses to 1.0 -- the exact
    shape the seven broken suites hit. open_orders_error makes get_orders raise.
    """
    client, data, tracker, breaker, allocator, analyst = (
        MagicMock() for _ in range(6)
    )
    snap_kwargs = {"equity": equity, "positions": positions or {}}
    snap = MagicMock(**snap_kwargs)
    if cash is not None:
        snap.cash = cash
    tracker.get_snapshot.return_value = snap
    data.get_latest_quote.return_value = MagicMock(mid=price)
    if open_orders_error is not None:
        client.get_orders.side_effect = open_orders_error
    else:
        client.get_orders.return_value = list(open_orders)
    allocator.get_budget.return_value = MagicMock(sixfold_budget=sleeve)
    breaker.check.return_value = True
    breaker.can_trade.return_value = True
    breaker.limits = MagicMock(max_single_trade_pct=0.05)
    analyst.get_buy_candidates.return_value = list(candidates)
    analyst.get_disposal_candidates.return_value = []
    analyst.scores.get.return_value = MagicMock(roic_measured=True,
                                               composite_score=80.0)
    return client, data, tracker, breaker, allocator, analyst, set(excluded)


@patch(COUNCIL, return_value=_approved_council())
def test_no_cash_attribute_still_buys(_council):
    """Finding 1: the cash gate is out of scope and must not block the buy.

    A snapshot that carries no usable cash must not stop a candidate that
    passes every other gate -- this is the fixture shape seven existing suites
    build (MagicMock with equity/positions but no cash), and the ticket forbids
    a cash gate that rejects them.
    """
    from src.strategies.sixfold_executor import SixfoldExecutor

    client, data, tracker, breaker, allocator, analyst, excluded = (
        _components(price=200.0, cash=None)
    )
    ex = SixfoldExecutor(client, data, tracker, breaker, allocator, analyst,
                         excluded=excluded)
    result = ex.run_cycle()

    assert len(result["orders"]) == 1, (
        f"expected JPM to be bought; rejections were {ex.last_rejections}"
    )
    client.trading.submit_order.assert_called_once()


@patch(COUNCIL, return_value=_approved_council())
def test_failed_second_order_book_read_does_not_inflate_room(_council):
    """Finding 2: a failed committed() re-read must not read as $0 pending.

    The first order-book read in run_cycle succeeds and shows a working $48k
    buy; the second read, inside committed(), raises. The sleeve room must
    still account for that $48k, so a further $5k name is not bought on top of
    it. Today the failed re-read yields pending_notional=0.0 and the $5k buy is
    placed -- the HLNE restack, this time measured in dollars.
    """
    from src.strategies.sixfold_executor import SixfoldExecutor

    working = _order("LOPE", "buy", 400, 120.0)  # $48,000 already working
    client, data, tracker, breaker, allocator, analyst, excluded = (
        _components(price=200.0, sleeve=50_000.0, cash=100_000.0,
                    candidates=("JPM",))
    )
    # First get_orders (the readable check) returns the working buy; the second
    # (inside committed()) fails.
    client.get_orders.side_effect = [[working], RuntimeError("orders unread")]
    ex = SixfoldExecutor(client, data, tracker, breaker, allocator, analyst,
                         excluded=excluded)
    result = ex.run_cycle()

    assert result["orders"] == []
    client.trading.submit_order.assert_not_called()


@patch(COUNCIL, return_value=_approved_council())
def test_excluded_sleeve_open_buy_does_not_consume_room(_council):
    """Finding 3: pending-buy notional must be scoped to this sleeve.

    A working SPY buy belongs to another sleeve (SPY is excluded), so it must
    not eat into SIXFOLD's sleeve room -- exactly as committed() already filters
    SPY out of the held side. A JPM candidate must therefore still be bought.
    Today the unfiltered pending total zeroes the room and JPM is rejected.
    """
    from src.strategies.sixfold_executor import SixfoldExecutor

    spy_buy = _order("SPY", "buy", 100, 500.0)  # $50,000, not this sleeve's
    client, data, tracker, breaker, allocator, analyst, excluded = (
        _components(price=200.0, sleeve=50_000.0, cash=100_000.0,
                    candidates=("JPM",), open_orders=[spy_buy],
                    excluded=("SPY", "QQQ"))
    )
    ex = SixfoldExecutor(client, data, tracker, breaker, allocator, analyst,
                         excluded=excluded)
    result = ex.run_cycle()

    assert len(result["orders"]) == 1, (
        f"expected JPM to be bought; rejections were {ex.last_rejections}"
    )
    client.trading.submit_order.assert_called_once()
