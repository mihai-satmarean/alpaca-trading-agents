"""Tests for the 4-tier profit cascade (allocation.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.risk.allocation import (
    CascadeConfig,
    ProfitCascade,
    AllocationManager,
    AllocationConfig,
)


def _make_allocator() -> AllocationManager:
    tracker = MagicMock()
    tracker.get_snapshot.return_value = MagicMock(
        equity=100_000.0,
        cash=20_000.0,
        positions={},
    )
    return AllocationManager(tracker, AllocationConfig())


class TestCascadeInit:
    def test_default_tiers_start_at_zero(self):
        cascade = ProfitCascade(_make_allocator())
        assert cascade.tier_pnl == {
            "vampire": 0.0, "bull_spread": 0.0,
            "sixfold_csp": 0.0, "reserve": 0.0,
        }

    def test_custom_config(self):
        cfg = CascadeConfig(tier1_promotion=50.0, cascade_pct=0.75)
        cascade = ProfitCascade(_make_allocator(), config=cfg)
        assert cascade.cfg.tier1_promotion == 50.0
        assert cascade.cfg.cascade_pct == 0.75


class TestRecordPnl:
    def test_record_accumulates(self):
        cascade = ProfitCascade(_make_allocator())
        cascade.record_pnl("vampire", 50.0)
        cascade.record_pnl("vampire", 30.0)
        assert cascade.tier_pnl["vampire"] == 80.0

    def test_record_unknown_tier_warns(self, caplog):
        cascade = ProfitCascade(_make_allocator())
        cascade.record_pnl("nonexistent", 100.0)
        assert "Unknown cascade tier" in caplog.text


class TestCascadeProfits:
    def test_no_cascade_below_threshold(self):
        cascade = ProfitCascade(_make_allocator())
        cascade.record_pnl("vampire", 50.0)
        actions = cascade.cascade_profits()
        assert actions == []
        assert cascade.tier_pnl["vampire"] == 50.0
        assert cascade.tier_pnl["bull_spread"] == 0.0

    def test_cascade_when_above_threshold(self):
        cfg = CascadeConfig(tier1_promotion=100.0, cascade_pct=0.50)
        cascade = ProfitCascade(_make_allocator(), config=cfg)
        cascade.record_pnl("vampire", 200.0)

        actions = cascade.cascade_profits()
        assert len(actions) == 1
        assert actions[0]["from"] == "vampire"
        assert actions[0]["to"] == "bull_spread"
        assert actions[0]["amount"] == 50.0  # (200-100) * 0.50
        assert cascade.tier_pnl["vampire"] == 150.0
        assert cascade.tier_pnl["bull_spread"] == 50.0

    def test_multi_tier_cascade(self):
        cfg = CascadeConfig(
            tier1_promotion=100.0, tier2_promotion=50.0,
            tier3_promotion=100.0, cascade_pct=1.0,
        )
        cascade = ProfitCascade(_make_allocator(), config=cfg)
        cascade.record_pnl("vampire", 300.0)
        cascade.record_pnl("bull_spread", 100.0)
        cascade.record_pnl("sixfold_csp", 200.0)

        actions = cascade.cascade_profits()
        assert len(actions) == 3
        assert actions[0]["from"] == "vampire"
        assert actions[1]["from"] == "bull_spread"
        assert actions[2]["from"] == "sixfold_csp"

    def test_cascade_history_grows(self):
        cfg = CascadeConfig(tier1_promotion=10.0, cascade_pct=0.50)
        cascade = ProfitCascade(_make_allocator(), config=cfg)
        cascade.record_pnl("vampire", 50.0)
        cascade.cascade_profits()
        cascade.record_pnl("vampire", 30.0)
        cascade.cascade_profits()

        assert len(cascade.cascade_history) == 2

    def test_adjusted_budgets(self):
        cascade = ProfitCascade(_make_allocator())
        cascade.record_pnl("vampire", 80.0)
        cascade.record_pnl("reserve", 20.0)

        adj = cascade.get_adjusted_budgets()
        assert adj["vampire_extra"] == 80.0
        assert adj["reserve_extra"] == 20.0
        assert adj["bull_spread_extra"] == 0.0

    def test_summary_readable(self):
        cascade = ProfitCascade(_make_allocator())
        cascade.record_pnl("vampire", 150.0)
        text = cascade.summary()
        assert "Tier 1" in text
        assert "$+150.00" in text
