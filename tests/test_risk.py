"""Tests for circuit breakers and allocation logic."""

from __future__ import annotations

from unittest.mock import MagicMock
from datetime import datetime

import pytest

from src.risk.circuit_breakers import CircuitBreaker, RiskLimits
from src.risk.allocation import AllocationManager, AllocationConfig
from src.core.position_tracker import PortfolioSnapshot


def _make_snapshot(equity=100000, cash=50000, daily_pnl=0, positions=None):
    return PortfolioSnapshot(
        equity=equity,
        cash=cash,
        buying_power=cash * 2,
        positions=positions or {},
        daily_pnl=daily_pnl,
        total_pnl=0,
        timestamp=datetime.now(),
    )


class TestCircuitBreaker:
    def test_normal_trading_allowed(self):
        tracker = MagicMock()
        tracker.get_snapshot.return_value = _make_snapshot()
        breaker = CircuitBreaker(tracker, RiskLimits())
        assert breaker.check() is True

    def test_daily_loss_trips_breaker(self):
        tracker = MagicMock()
        tracker.get_snapshot.return_value = _make_snapshot(daily_pnl=-2500)
        breaker = CircuitBreaker(tracker, RiskLimits(max_daily_loss_pct=0.02))
        assert breaker.check() is False
        assert breaker.is_tripped is True

    def test_low_cash_trips_breaker(self):
        tracker = MagicMock()
        tracker.get_snapshot.return_value = _make_snapshot(cash=3000)
        breaker = CircuitBreaker(tracker, RiskLimits(min_cash_reserve=5000))
        assert breaker.check() is False

    def test_single_trade_limit(self):
        tracker = MagicMock()
        tracker.get_snapshot.return_value = _make_snapshot()
        breaker = CircuitBreaker(tracker, RiskLimits(max_single_trade_pct=0.05))
        assert breaker.can_trade("SPY", 4000) is True
        assert breaker.can_trade("SPY", 6000) is False

    def test_reset_clears_trip(self):
        tracker = MagicMock()
        tracker.get_snapshot.return_value = _make_snapshot(daily_pnl=-3000)
        breaker = CircuitBreaker(tracker, RiskLimits())
        breaker.check()
        assert breaker.is_tripped is True
        breaker.reset()
        assert breaker.is_tripped is False


class TestAllocation:
    def test_budget_calculation(self):
        tracker = MagicMock()
        tracker.get_snapshot.return_value = _make_snapshot(equity=100000, positions={})
        allocator = AllocationManager(tracker, AllocationConfig())
        budget = allocator.get_budget()
        assert budget.options_budget == 80000
        assert budget.vampire_budget == 15000
        assert budget.reserve_target == 5000

    def test_can_allocate_within_budget(self):
        tracker = MagicMock()
        tracker.get_snapshot.return_value = _make_snapshot(equity=100000, positions={})
        allocator = AllocationManager(tracker, AllocationConfig())
        assert allocator.can_allocate_options(50000) is True
        assert allocator.can_allocate_options(90000) is False

    def test_vampire_budget(self):
        tracker = MagicMock()
        tracker.get_snapshot.return_value = _make_snapshot(equity=100000, positions={})
        allocator = AllocationManager(tracker, AllocationConfig())
        assert allocator.can_allocate_vampire(10000) is True
        assert allocator.can_allocate_vampire(20000) is False
