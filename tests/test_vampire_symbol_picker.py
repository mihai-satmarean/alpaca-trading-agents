"""Tests for VampireSymbolPicker: ATR-based selection, bleed budgets, rotation."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.strategies.vampire_symbol_picker import (
    BleedBudget,
    PickerConfig,
    SymbolMetrics,
    VampireSymbolPicker,
)

ET = ZoneInfo("America/New_York")


def _mock_picker(sleeve: float = 10_000.0, cfg: PickerConfig | None = None):
    client = MagicMock()
    data = MagicMock()
    return VampireSymbolPicker(
        client=client, data=data,
        sleeve_budget=sleeve,
        config=cfg or PickerConfig(target_count=3),
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class TestScoring:

    def test_higher_atr_scores_better(self):
        """With identical spreads, higher ATR should rank first."""
        picker = _mock_picker()
        metrics = [
            SymbolMetrics(symbol="SPY", price=590, spread=0.12, spread_pct=0.0002,
                          atr_pct=0.007, shortable=True),
            SymbolMetrics(symbol="QQQ", price=540, spread=0.10, spread_pct=0.0002,
                          atr_pct=0.012, shortable=True),
            SymbolMetrics(symbol="AMD", price=170, spread=0.10, spread_pct=0.0002,
                          atr_pct=0.018, shortable=True),
        ]
        scored = picker._score_all(metrics)
        by_score = sorted(scored, key=lambda m: m.score)
        assert by_score[0].symbol == "AMD", "highest ATR should rank first"
        assert by_score[-1].symbol == "SPY", "lowest ATR should rank last"

    def test_wider_spread_penalized(self):
        picker = _mock_picker()
        metrics = [
            SymbolMetrics(symbol="A", price=100, spread=0.01, spread_pct=0.0001,
                          atr_pct=0.01, shortable=True),
            SymbolMetrics(symbol="B", price=100, spread=0.50, spread_pct=0.005,
                          atr_pct=0.01, shortable=True),
            SymbolMetrics(symbol="C", price=100, spread=0.20, spread_pct=0.002,
                          atr_pct=0.01, shortable=True),
        ]
        scored = picker._score_all(metrics)
        by_score = sorted(scored, key=lambda m: m.score)
        assert by_score[0].symbol == "A", "tightest spread should rank first"

    def test_gap_penalty_penalizes_extremes(self):
        assert VampireSymbolPicker._gap_penalty(0.001) == 2.0, "tiny gap = dead"
        assert VampireSymbolPicker._gap_penalty(0.003) == 0.0, "moderate gap = ideal"
        assert VampireSymbolPicker._gap_penalty(0.05) == 3.0, "huge gap = trending"


# ---------------------------------------------------------------------------
# Hard filters
# ---------------------------------------------------------------------------

class TestHardFilters:

    def test_non_shortable_dropped(self):
        picker = _mock_picker()
        asset = MagicMock()
        asset.shortable = False
        asset.tradable = True
        picker._client.trading.get_asset.return_value = asset

        metrics = [SymbolMetrics(symbol="SOXL", price=50, spread_pct=0.001)]
        result = picker._apply_hard_filters(metrics)
        assert len(result) == 0

    def test_cheap_stock_dropped(self):
        picker = _mock_picker()
        metrics = [SymbolMetrics(symbol="PENNY", price=2.50, spread_pct=0.001)]
        result = picker._apply_hard_filters(metrics)
        assert len(result) == 0

    def test_wide_spread_dropped(self):
        picker = _mock_picker()
        asset = MagicMock()
        asset.shortable = True
        asset.tradable = True
        picker._client.trading.get_asset.return_value = asset

        metrics = [SymbolMetrics(symbol="WIDE", price=10, spread=0.20,
                                 spread_pct=0.02)]
        result = picker._apply_hard_filters(metrics)
        assert len(result) == 0

    def test_good_stock_passes(self):
        picker = _mock_picker()
        asset = MagicMock()
        asset.shortable = True
        asset.tradable = True
        picker._client.trading.get_asset.return_value = asset

        metrics = [SymbolMetrics(symbol="QQQ", price=540, spread=0.10,
                                 spread_pct=0.00019)]
        result = picker._apply_hard_filters(metrics)
        assert len(result) == 1
        assert result[0].symbol == "QQQ"


# ---------------------------------------------------------------------------
# Bleed budgets
# ---------------------------------------------------------------------------

class TestBleedBudgets:

    def test_budgets_assigned_to_each_symbol(self):
        picker = _mock_picker()
        selected = [
            SymbolMetrics(symbol="SPY", price=590, atr_pct=0.007),
            SymbolMetrics(symbol="QQQ", price=540, atr_pct=0.012),
        ]
        budgets = picker._assign_bleed_budgets(selected)
        assert "SPY" in budgets
        assert "QQQ" in budgets
        assert budgets["SPY"].profit_target > 0
        assert budgets["QQQ"].profit_target > 0

    def test_higher_atr_gets_higher_target(self):
        picker = _mock_picker()
        selected = [
            SymbolMetrics(symbol="CALM", price=100, atr_pct=0.005),
            SymbolMetrics(symbol="WILD", price=100, atr_pct=0.020),
        ]
        budgets = picker._assign_bleed_budgets(selected)
        assert budgets["WILD"].profit_target > budgets["CALM"].profit_target

    def test_target_reached_flags_retirement(self):
        b = BleedBudget(symbol="SPY", profit_target=50, loss_limit=25,
                        realized_pnl=55)
        assert b.target_reached
        assert b.should_retire

    def test_loss_limit_flags_retirement(self):
        b = BleedBudget(symbol="SPY", profit_target=50, loss_limit=25,
                        realized_pnl=-30)
        assert b.limit_hit
        assert b.should_retire

    def test_active_position_does_not_retire(self):
        b = BleedBudget(symbol="SPY", profit_target=50, loss_limit=25,
                        realized_pnl=10)
        assert not b.should_retire
        assert b.is_active


# ---------------------------------------------------------------------------
# Health check and rotation
# ---------------------------------------------------------------------------

class TestHealthCheck:

    def test_target_reached_retires_symbol(self):
        picker = _mock_picker()
        engine = MagicMock()
        engine.daily_pnl = 60.0
        engine.bleeds = list(range(10))

        picker._bleed_budgets["SPY"] = BleedBudget(
            symbol="SPY", profit_target=50, loss_limit=25,
            started_at=datetime.now(ET),
        )
        retirements = picker.check_health({"SPY": engine})
        assert len(retirements) == 1
        assert retirements[0][0] == "SPY"
        assert "target reached" in retirements[0][1]

    def test_loss_limit_retires_symbol(self):
        picker = _mock_picker()
        engine = MagicMock()
        engine.daily_pnl = -30.0
        engine.bleeds = list(range(10))

        picker._bleed_budgets["QQQ"] = BleedBudget(
            symbol="QQQ", profit_target=50, loss_limit=25,
            started_at=datetime.now(ET),
        )
        retirements = picker.check_health({"QQQ": engine})
        assert len(retirements) == 1
        assert "loss limit" in retirements[0][1]

    def test_healthy_symbol_not_retired(self):
        picker = _mock_picker()
        engine = MagicMock()
        engine.daily_pnl = 10.0
        engine.bleeds = list(range(5))

        picker._bleed_budgets["SPY"] = BleedBudget(
            symbol="SPY", profit_target=50, loss_limit=25,
            started_at=datetime.now(ET),
        )
        retirements = picker.check_health({"SPY": engine})
        assert len(retirements) == 0


class TestReplacement:

    def test_replacement_excludes_current_symbols(self):
        picker = _mock_picker()
        picker._all_metrics = {
            "SPY": SymbolMetrics(symbol="SPY", score=1.0, shortable=True),
            "QQQ": SymbolMetrics(symbol="QQQ", score=2.0, shortable=True),
            "AMD": SymbolMetrics(symbol="AMD", score=0.5, shortable=True),
        }
        replacements = picker.find_replacements(["SPY", "QQQ"], count=1)
        assert replacements == ["AMD"]

    def test_replacement_respects_cooldown(self):
        picker = _mock_picker()
        picker._all_metrics = {
            "SPY": SymbolMetrics(symbol="SPY", score=1.0, shortable=True),
            "AMD": SymbolMetrics(symbol="AMD", score=0.5, shortable=True),
        }
        picker._retired["AMD"] = datetime.now(ET)
        replacements = picker.find_replacements(["SPY"], count=1)
        assert "AMD" not in replacements

    def test_no_metrics_returns_empty(self):
        picker = _mock_picker()
        assert picker.find_replacements(["SPY"]) == []

    def test_fasting_mode_blocks_replacements(self):
        picker = _mock_picker()
        picker._all_metrics = {
            "AMD": SymbolMetrics(symbol="AMD", score=0.5, shortable=True),
        }
        picker._is_fasting = True
        replacements = picker.find_replacements(["SPY"], count=1)
        assert replacements == []


# ---------------------------------------------------------------------------
# Patience / time limits
# ---------------------------------------------------------------------------

class TestPatience:

    def test_patience_not_expired_within_window(self):
        b = BleedBudget(
            symbol="SPY", profit_target=50, loss_limit=25,
            patience_minutes=60.0,
            started_at=datetime.now(ET) - timedelta(minutes=30),
            realized_pnl=-2.0,
        )
        assert not b.patience_expired
        assert not b.should_retire

    def test_patience_expired_after_window_with_no_profit(self):
        b = BleedBudget(
            symbol="SPY", profit_target=50, loss_limit=25,
            patience_minutes=60.0,
            started_at=datetime.now(ET) - timedelta(minutes=90),
            realized_pnl=-2.0,
        )
        assert b.patience_expired
        assert b.should_retire

    def test_patience_not_expired_if_profitable(self):
        """A symbol making money keeps going even past the patience window."""
        b = BleedBudget(
            symbol="SPY", profit_target=50, loss_limit=25,
            patience_minutes=60.0,
            started_at=datetime.now(ET) - timedelta(minutes=90),
            realized_pnl=10.0,
        )
        assert not b.patience_expired

    def test_patience_not_expired_if_not_started(self):
        b = BleedBudget(symbol="SPY", profit_target=50, loss_limit=25,
                        patience_minutes=60.0, started_at=None)
        assert not b.patience_expired

    def test_check_health_detects_patience_expiry(self):
        picker = _mock_picker()
        engine = MagicMock()
        engine.daily_pnl = -1.0
        engine.bleeds = list(range(5))

        picker._bleed_budgets["SPY"] = BleedBudget(
            symbol="SPY", profit_target=50, loss_limit=25,
            patience_minutes=60.0,
            started_at=datetime.now(ET) - timedelta(minutes=90),
        )
        retirements = picker.check_health({"SPY": engine})
        assert len(retirements) == 1
        assert "patience expired" in retirements[0][1]


# ---------------------------------------------------------------------------
# Starvation floor / fasting mode
# ---------------------------------------------------------------------------

class TestStarvationFloor:

    def test_fasting_triggers_when_session_losses_exceed_floor(self):
        cfg = PickerConfig(target_count=3, starvation_floor_pct=0.30)
        picker = _mock_picker(sleeve=10_000.0, cfg=cfg)

        engine_a = MagicMock()
        engine_a.daily_pnl = -2000.0
        engine_a.bleeds = []
        engine_b = MagicMock()
        engine_b.daily_pnl = -1500.0
        engine_b.bleeds = []

        picker._bleed_budgets["A"] = BleedBudget(
            symbol="A", profit_target=50, loss_limit=25,
            started_at=datetime.now(ET),
        )
        picker._bleed_budgets["B"] = BleedBudget(
            symbol="B", profit_target=50, loss_limit=25,
            started_at=datetime.now(ET),
        )

        retirements = picker.check_health({"A": engine_a, "B": engine_b})

        assert picker.is_fasting
        assert len(retirements) == 2
        assert all("starvation floor" in r[1] for r in retirements)

    def test_fasting_mode_returns_no_retirements_on_subsequent_check(self):
        picker = _mock_picker()
        picker._is_fasting = True
        engine = MagicMock()
        engine.daily_pnl = -100.0
        engine.bleeds = []
        retirements = picker.check_health({"SPY": engine})
        assert retirements == []

    def test_starvation_floor_calculation(self):
        cfg = PickerConfig(target_count=3, starvation_floor_pct=0.30)
        picker = _mock_picker(sleeve=10_000.0, cfg=cfg)
        assert picker.starvation_floor == 3_000.0

    def test_normal_loss_does_not_trigger_fasting(self):
        cfg = PickerConfig(target_count=3, starvation_floor_pct=0.30)
        picker = _mock_picker(sleeve=10_000.0, cfg=cfg)

        engine = MagicMock()
        engine.daily_pnl = -500.0
        engine.bleeds = list(range(3))

        picker._bleed_budgets["SPY"] = BleedBudget(
            symbol="SPY", profit_target=50, loss_limit=25,
            started_at=datetime.now(ET),
        )
        retirements = picker.check_health({"SPY": engine})
        assert not picker.is_fasting


class TestHardExclude:
    def test_hood_and_spy_are_not_in_the_default_universe(self):
        from src.strategies.vampire_symbol_picker import HARD_EXCLUDE
        picker = _mock_picker()
        assert "HOOD" not in picker._universe
        assert "SPY" not in picker._universe
        assert HARD_EXCLUDE == frozenset({"HOOD", "SPY"})

    def test_replacements_skip_hood_even_when_it_ranks_first(self):
        picker = _mock_picker()
        picker._all_metrics = {
            "HOOD": SymbolMetrics(symbol="HOOD", score=0.01, shortable=True),
            "AMD": SymbolMetrics(symbol="AMD", score=0.50, shortable=True),
        }
        assert picker.find_replacements(["QQQ"], count=1) == ["AMD"]
