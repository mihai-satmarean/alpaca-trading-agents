"""The tradeable regime set follows the venue's constraint, not a hand setting.

Measured on TQQQ 2026-09-08, the chop-only gate blocked four trend_up windows
worth +0.44 of favourable drift, including the largest move of the day, against
two trend_down windows worth -0.12 it was right to sit out. A two-sided scalper
wants chop only, because a trend runs over mean reversion either way. A book
that cannot short can only buy dips, and trend_up is the regime where the dip
it buys gets bought back.
"""

from __future__ import annotations

import pytest

from src.strategies.regime_advisor import (
    REGIMES,
    TRADEABLE_REGIMES,
    LONG_ONLY_TRADEABLE_REGIMES,
    RegimeAdvisor,
    RegimeVerdict,
    normalize_regimes,
    regimes_for_direction,
)


def _advisor(**kw):
    return RegimeAdvisor(llm_call=lambda *a: "", journal=False,
                         clock=lambda: 1000.0, **kw)


def _seed(adv, symbol, regime, at=1000.0, conf=0.9):
    adv._verdicts[symbol] = RegimeVerdict(
        symbol=symbol, regime=regime, confidence=conf, reason="",
        model="m", at=at, latency=0.1)


class TestTheRuleFollowsTheConstraint:
    def test_a_shortable_book_trades_chop_only(self):
        assert regimes_for_direction(allow_short=True) == frozenset({"chop"})

    def test_a_long_only_book_also_trades_up_trends(self):
        assert regimes_for_direction(allow_short=False) == frozenset({"chop", "trend_up"})

    def test_neither_rule_ever_admits_the_dangerous_regimes(self):
        # trend_down is the tape a long-only dip-buyer gets run over by, and
        # news is unmodelled by construction. No direction may trade either.
        for allow_short in (True, False):
            got = regimes_for_direction(allow_short)
            assert "trend_down" not in got
            assert "news" not in got

    def test_the_long_only_set_is_a_strict_superset(self):
        assert TRADEABLE_REGIMES < LONG_ONLY_TRADEABLE_REGIMES


class TestATypoCannotWidenTheGate:
    def test_unknown_names_are_dropped(self):
        assert normalize_regimes(["chop", "sideways", "ALL"]) == frozenset({"chop"})

    def test_an_all_nonsense_set_never_opens_rather_than_opening_wide(self):
        assert normalize_regimes(["everything"]) == frozenset()

    def test_empty_or_missing_falls_back_to_the_strict_default(self):
        assert normalize_regimes(None) == TRADEABLE_REGIMES
        assert normalize_regimes([]) == TRADEABLE_REGIMES

    def test_case_and_whitespace_are_tolerated(self):
        assert normalize_regimes([" Chop ", "TREND_UP"]) == frozenset({"chop", "trend_up"})

    def test_no_configured_set_can_admit_a_regime_the_model_cannot_emit(self):
        assert normalize_regimes(["chop", "trend_up", "moon"]) <= frozenset(REGIMES)


class TestEntryAllowedHonoursTheOverride:
    @pytest.mark.parametrize("regime,expected", [
        ("chop", True), ("trend_up", False), ("trend_down", False), ("news", False)])
    def test_default_set_is_chop_only(self, regime, expected):
        adv = _advisor()
        _seed(adv, "QQQ", regime)
        assert adv.entry_allowed("QQQ") is expected

    @pytest.mark.parametrize("regime,expected", [
        ("chop", True), ("trend_up", True), ("trend_down", False), ("news", False)])
    def test_long_only_override_adds_only_the_up_trend(self, regime, expected):
        adv = _advisor()
        _seed(adv, "TQQQ", regime)
        got = adv.entry_allowed("TQQQ", tradeable=regimes_for_direction(allow_short=False))
        assert got is expected

    def test_one_advisor_serves_two_symbols_under_different_rules(self):
        # This is the actual deployment: QQQ shortable, TQQQ not.
        adv = _advisor()
        _seed(adv, "QQQ", "trend_up")
        _seed(adv, "TQQQ", "trend_up")
        assert adv.entry_allowed("QQQ", tradeable=regimes_for_direction(True)) is False
        assert adv.entry_allowed("TQQQ", tradeable=regimes_for_direction(False)) is True

    def test_config_can_set_the_advisor_default(self):
        adv = _advisor(tradeable_regimes=["chop", "trend_up"])
        _seed(adv, "QQQ", "trend_up")
        assert adv.entry_allowed("QQQ") is True


class TestWideningNeverDefeatsTheOtherGuards:
    """A wider regime set must not smuggle past staleness or confidence."""

    def test_a_stale_verdict_is_still_refused(self):
        adv = _advisor(ttl_seconds=60)
        _seed(adv, "TQQQ", "trend_up", at=1000.0 - 61)
        assert adv.entry_allowed("TQQQ", tradeable=LONG_ONLY_TRADEABLE_REGIMES) is False

    def test_a_low_confidence_verdict_is_still_refused(self):
        adv = _advisor(min_confidence=0.7)
        _seed(adv, "TQQQ", "trend_up", conf=0.5)
        assert adv.entry_allowed("TQQQ", tradeable=LONG_ONLY_TRADEABLE_REGIMES) is False

    def test_a_missing_verdict_is_still_refused(self):
        adv = _advisor()
        assert adv.entry_allowed("TQQQ", tradeable=LONG_ONLY_TRADEABLE_REGIMES) is False


class TestTheCallSiteDerivesItFromBorrow:
    def _runner_source(self):
        import inspect
        import scripts.run_vampire_tradier as r
        return inspect.getsource(r.build)

    def test_the_gate_reads_allow_short_rather_than_a_fixed_set(self):
        src = self._runner_source()
        assert "regimes_for_direction(cfg.allow_short)" in src

    def test_allow_short_is_read_lazily_not_captured_at_build_time(self):
        # Borrow is resolved after build(); a value captured here would be
        # the default True and the widening would never take effect.
        src = self._runner_source()
        i = src.index("entry_gate")
        window = src[i:i + 400]
        assert "lambda cfg=c:" in window, "the gate must close over the config, not a bool"
