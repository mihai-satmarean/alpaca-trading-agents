"""Ported exit rules, ported alongside the tests that pin them.

Source: Options-Trader/sixfold_trader/selftest.mjs, live on Lambda
sixfold-trader since 2026-09-01. Test names and boundary values mirror that
file directly, so a future divergence between the two builds is visible by
diffing test names, not by re-deriving the spec from scratch.
"""

from __future__ import annotations

import pytest

from src.strategies.sixfold_exits import DEFAULT_CFG, ExitCfg, exit_decision


class TestTakeProfit:
    def test_at_35_percent_fires(self):
        d = exit_decision(0.35, False, age_days=10, registry_tracked=True)
        assert d.exit and "profit target" in d.reason

    def test_just_under_the_target_holds(self):
        d = exit_decision(0.349, False, age_days=10, registry_tracked=True)
        assert not d.exit

    def test_well_past_the_target_also_fires(self):
        d = exit_decision(1.20, False, age_days=1, registry_tracked=True)
        assert d.exit


class TestTimeStop:
    def test_at_183_days_fires_independent_of_price(self):
        d = exit_decision(None, False, age_days=183, registry_tracked=True)
        assert d.exit and "time stop" in d.reason

    def test_just_under_the_stop_holds(self):
        d = exit_decision(-0.10, False, age_days=182.9, registry_tracked=True)
        assert not d.exit

    def test_fires_even_with_no_usable_mark(self):
        # "An exit rule that stops working during a data outage is not an
        # exit rule" -- exits.mjs's own comment, ported verbatim as intent.
        d = exit_decision(None, False, age_days=200, registry_tracked=True)
        assert d.exit and "time stop" in d.reason

    def test_unknown_age_never_fires(self):
        d = exit_decision(None, False, age_days=None, registry_tracked=True)
        assert not d.exit


class TestNoStopLoss:
    def test_no_price_level_alone_triggers_an_exit(self):
        # exits.mjs: "NO stop-loss fires at any loss -- SPEC 3.7 is
        # deliberate." Swept -10% through -99%, registry-tracked so the
        # take-profit branch is live and simply never satisfied.
        for pct in (-0.10, -0.30, -0.50, -0.70, -0.99):
            d = exit_decision(pct, False, age_days=10, registry_tracked=True)
            assert not d.exit, f"{pct:.0%} must not trigger an exit on price alone"

    def test_a_deep_loser_is_still_exited_once_score_flagged_not_for_the_loss(self):
        d = exit_decision(-0.60, True, age_days=10, registry_tracked=True)
        assert d.exit
        assert "score" in d.reason
        assert "loss" not in d.reason.lower() and "stop" not in d.reason.lower()


class TestScoreDispose:
    """Unchanged pre-existing rule: a decided bool in, not a recomputed
    threshold -- see the module docstring for why re-deriving it from a
    raw score number is the bug this design specifically avoids."""

    def test_flagged_disposes_even_with_no_price_or_age_data(self):
        d = exit_decision(None, True, age_days=None, registry_tracked=False)
        assert d.exit and "score" in d.reason

    def test_not_flagged_and_nothing_else_triggers_holds(self):
        d = exit_decision(None, False, age_days=None, registry_tracked=False)
        assert not d.exit

    def test_flagged_but_not_registry_tracked_still_disposes(self):
        # A CSP-assignment or externally-acquired position: no recorded
        # open, so time-stop/take-profit cannot apply, but the pre-existing
        # score rule is untouched and still fires.
        d = exit_decision(0.50, True, age_days=None, registry_tracked=False)
        assert d.exit and "score" in d.reason


class TestRegistryGating:
    """Take-profit and time-stop must never fire on a symbol this process
    did not register opening -- the fix for the blast-radius finding in
    the adversarial review: those two rules increase exposure beyond the
    pre-existing score-only trigger unless explicitly scoped."""

    def test_a_35_percent_gain_on_an_untracked_symbol_does_not_exit(self):
        d = exit_decision(0.50, False, age_days=None, registry_tracked=False)
        assert not d.exit

    def test_a_183_day_old_untracked_symbol_does_not_exit(self):
        d = exit_decision(None, False, age_days=300, registry_tracked=False)
        assert not d.exit

    def test_tracked_but_no_age_or_price_data_and_not_flagged_holds(self):
        d = exit_decision(None, False, age_days=None, registry_tracked=True)
        assert not d.exit


class TestPrecedence:
    def test_time_stop_outranks_take_profit_when_both_qualify(self):
        d = exit_decision(0.50, False, age_days=190, registry_tracked=True)
        assert "time stop" in d.reason

    def test_take_profit_outranks_score_dispose_when_both_qualify(self):
        d = exit_decision(0.40, True, age_days=5, registry_tracked=True)
        assert "profit target" in d.reason


class TestDefaultsMatchTheLiveTradierDeployment:
    def test_defaults(self):
        assert DEFAULT_CFG.take_profit_pct == 35.0
        assert DEFAULT_CFG.time_stop_days == 183.0

    def test_custom_cfg_is_actually_used_not_ignored(self):
        cfg = ExitCfg(take_profit_pct=10.0, time_stop_days=5.0)
        assert exit_decision(0.11, False, age_days=1, registry_tracked=True, cfg=cfg).exit
        assert exit_decision(None, False, age_days=6, registry_tracked=True, cfg=cfg).exit
