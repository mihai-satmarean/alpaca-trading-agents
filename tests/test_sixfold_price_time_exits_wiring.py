"""Call-site regression tests for the three BLOCKING findings the adversarial
review caught in the first draft of this port. Each test is the literal
proof for one finding; supplying a value to a function in isolation would
not have caught any of these -- they only fail if the WIRING breaks.

1. run_disposals() iterated `flagged & held` alone, so a position that is up
   35%+ or 183+ days old but still scores fine (the common case for a
   winner) would never be considered -- the new rules would be dead code
   for the scenario they exist to handle.
2. is_locked_out()-equivalent, following this repo's own read-journal
   convention, would fail OPEN (return "not locked out") on an unreadable
   file -- the opposite of the one check whose purpose is not immediately
   rebuying a name just sold.
3. record_open() at the original call site had no fill confirmation and no
   self-healing path for an order that never fills.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.core import sixfold_registry as registry
from src.core.finance_advisor import AdvisorOpinion, CouncilDecision
from src.strategies.sixfold_executor import SixfoldExecutor
from src.strategies.sixfold_exits import ExitCfg

COUNCIL_PATCH = "src.strategies.sixfold_executor.evaluate_equity_buy"


def _council_approve(symbol, score, fundamentals=None):
    return CouncilDecision(
        action="buy", symbol=symbol, approved=True,
        votes_for=3, votes_against=0, abstentions=0,
        opinions=[AdvisorOpinion("dell4-finance", "Finance Specialist", "approve", "ok", True)],
        summary="Council approved",
    )


def _snapshot(positions, equity=100_000.0):
    snap = MagicMock()
    snap.positions = positions
    snap.equity = equity
    return snap


def _exec(positions=None, flagged=(), scores=None, sleeve=50_000.0,
          equity=100_000.0, excluded=(), exit_cfg=None):
    client, data, tracker, breaker, allocator, analyst = (MagicMock() for _ in range(6))
    tracker.get_snapshot.return_value = _snapshot(positions or {}, equity)
    allocator.get_budget.return_value = MagicMock(sixfold_budget=sleeve)
    breaker.check.return_value = True
    breaker.can_trade.return_value = True
    breaker.limits = MagicMock(max_single_trade_pct=0.05)
    analyst.get_disposal_candidates.return_value = list(flagged)
    analyst.get_buy_candidates.return_value = []
    analyst.scores = scores or {}
    kw = {}
    if exit_cfg is not None:
        kw["exit_cfg"] = exit_cfg
    ex = SixfoldExecutor(client, data, tracker, breaker, allocator, analyst,
                         excluded=set(excluded), **kw)
    return ex, client


class TestFinding1_NonScoreFlaggedPositionsAreConsidered:
    """A winner sitting at +35% almost never scores badly -- that IS the
    scenario the take-profit rule exists for, and the first draft's
    iteration set (flagged & held) would never look at it."""

    def test_a_registry_tracked_winner_not_score_flagged_is_disposed(self):
        ex, client = _exec(
            positions={"NVDA": {"qty": 10, "market_value": 13_500.0, "unrealized_plpc": 0.40}},
            flagged=[],  # NOT in the analyst's disposal set -- scores fine
        )
        registry.record_open("NVDA")
        sold = ex.run_disposals()
        assert len(sold) == 1 and sold[0]["symbol"] == "NVDA"
        client.close_position.assert_called_once_with("NVDA")

    def test_a_registry_tracked_old_position_not_score_flagged_is_disposed(self):
        cfg = ExitCfg(take_profit_pct=35.0, time_stop_days=1.0)
        ex, client = _exec(
            positions={"KO": {"qty": 100, "market_value": 8_886.0, "unrealized_plpc": 0.01}},
            flagged=[], exit_cfg=cfg,
        )
        registry.record_open("KO", at=0.0)  # epoch 0: enormously old
        sold = ex.run_disposals()
        assert len(sold) == 1 and "time stop" in sold[0]["reason"]

    def test_the_original_score_dispose_path_still_works_unchanged(self):
        ex, client = _exec(
            positions={"PG": {"qty": 34, "market_value": 5_000.0}},
            flagged=["PG"],
        )
        sold = ex.run_disposals()
        assert len(sold) == 1 and sold[0]["symbol"] == "PG"


class TestFinding2_LockoutFailsClosedAtTheCallSite:
    """The regression test IS the call site behavior, not a bare
    pytest.raises on the registry function in isolation."""

    def test_an_unreadable_lockout_table_rejects_every_candidate_this_cycle(self):
        with patch(COUNCIL_PATCH, side_effect=_council_approve):
            ex, client = _exec()
            ex._analyst.get_buy_candidates.return_value = ["JPM", "MSFT", "AAPL"]
            ex._data.get_latest_quote.return_value = MagicMock(mid=200.0)
            with patch.object(registry, "load_lockouts", side_effect=RuntimeError("disk error")):
                result = ex.run_cycle()
        assert result["orders"] == []
        client.trading.submit_order.assert_not_called()
        assert result["status"] == "lockouts_unreadable"

    def test_a_locked_out_symbol_is_rejected_before_a_quote_is_spent(self):
        with patch(COUNCIL_PATCH, side_effect=_council_approve):
            ex, client = _exec()
            ex._analyst.get_buy_candidates.return_value = ["JPM"]
            registry.record_disposal("JPM", "score deteriorated", lockout_days=56.0)
            result = ex.run_cycle()
        assert result["orders"] == []
        client.trading.submit_order.assert_not_called()
        ex._data.get_latest_quote.assert_not_called()
        assert any("locked out" in r["reason"] for r in ex.last_rejections)

    def test_an_expired_lockout_does_not_block_a_rebuy(self):
        with patch(COUNCIL_PATCH, side_effect=_council_approve):
            ex, client = _exec()
            ex._analyst.get_buy_candidates.return_value = ["JPM"]
            ex._data.get_latest_quote.return_value = MagicMock(mid=200.0)
            registry.record_disposal("JPM", "reason", lockout_days=-1.0)  # already expired
            result = ex.run_cycle()
        assert len(result["orders"]) == 1

    def test_no_lockout_at_all_is_the_normal_case_and_still_trades(self):
        with patch(COUNCIL_PATCH, side_effect=_council_approve):
            ex, client = _exec()
            ex._analyst.get_buy_candidates.return_value = ["JPM"]
            ex._data.get_latest_quote.return_value = MagicMock(mid=200.0)
            result = ex.run_cycle()
        assert len(result["orders"]) == 1


class TestFinding3_UnfilledOrdersSelfHealWithinOneCycle:
    """record_open() writes optimistically at submission; reconcile() at
    the top of the NEXT run_disposals() clears it if the symbol was never
    actually held -- bounding a phantom entry to one cycle rather than
    needing a synchronous fill-confirmation poll in the buy loop."""

    def test_a_buy_fill_registers_the_open_date(self):
        with patch(COUNCIL_PATCH, side_effect=_council_approve):
            ex, client = _exec()
            ex._analyst.get_buy_candidates.return_value = ["JPM"]
            ex._data.get_latest_quote.return_value = MagicMock(mid=200.0)
            ex.run_cycle()
        assert registry.age_days("JPM") is not None

    def test_an_order_that_never_fills_is_cleared_by_the_next_cycles_reconcile(self):
        # Cycle 1: buy submitted, optimistic open-date written, but the
        # position never actually shows up in the account (DAY limit never
        # filled).
        with patch(COUNCIL_PATCH, side_effect=_council_approve):
            ex, client = _exec()
            ex._analyst.get_buy_candidates.return_value = ["JPM"]
            ex._data.get_latest_quote.return_value = MagicMock(mid=200.0)
            ex.run_cycle()
        assert registry.age_days("JPM") is not None

        # Cycle 2: run_disposals() (called at the top of run_cycle) reconciles
        # against the REAL snapshot, which never shows JPM held.
        with patch(COUNCIL_PATCH, side_effect=_council_approve):
            ex2, _ = _exec(positions={})  # JPM not actually held
            ex2._analyst.get_buy_candidates.return_value = []
            ex2.run_disposals()
        assert registry.age_days("JPM") is None

    def test_a_filled_position_survives_reconciliation(self):
        with patch(COUNCIL_PATCH, side_effect=_council_approve):
            ex, client = _exec()
            ex._analyst.get_buy_candidates.return_value = ["JPM"]
            ex._data.get_latest_quote.return_value = MagicMock(mid=200.0)
            ex.run_cycle()

        ex2, _ = _exec(positions={"JPM": {"qty": 25, "market_value": 5_000.0}})
        ex2.run_disposals()
        assert registry.age_days("JPM") is not None


class TestRegistryTrackedNessNeverAffectsScoreDispose:
    """The score-dispose path (pre-existing, unaffected) must still sell a
    symbol regardless of whether the registry knows anything about it --
    the excluded/covered-call guards are the same for every trigger."""

    def test_excluded_sleeve_still_blocks_a_registry_tracked_winner(self):
        ex, client = _exec(
            positions={"QQQ": {"qty": 10, "market_value": 1000.0, "unrealized_plpc": 0.50}},
            excluded=("QQQ",),
        )
        registry.record_open("QQQ")
        sold = ex.run_disposals()
        assert sold == []
        client.close_position.assert_not_called()
        assert any("another sleeve" in r["reason"] for r in ex.last_rejections)
