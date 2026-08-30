"""Capital allocation logic: 80/15/5 split across strategies."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.position_tracker import PositionTracker

log = logging.getLogger(__name__)


@dataclass
class AllocationConfig:
    options_pct: float = 0.80
    vampire_pct: float = 0.15
    reserve_pct: float = 0.05


@dataclass
class AllocationBudget:
    total_equity: float
    options_budget: float
    vampire_budget: float
    reserve_target: float
    options_used: float
    vampire_used: float
    options_available: float
    vampire_available: float


class AllocationManager:
    """Enforces the 80/15/5 capital split between strategies."""

    def __init__(self, tracker: PositionTracker, config: AllocationConfig | None = None):
        self._tracker = tracker
        self.config = config or AllocationConfig()

    def get_budget(self) -> AllocationBudget:
        snapshot = self._tracker.get_snapshot()
        equity = snapshot.equity

        options_budget = equity * self.config.options_pct
        vampire_budget = equity * self.config.vampire_pct
        reserve_target = equity * self.config.reserve_pct

        options_used = 0.0
        vampire_used = 0.0

        for sym, pos in snapshot.positions.items():
            mv = abs(pos["market_value"])
            # Heuristic: option positions have OCC-format symbols (length > 10)
            if len(sym) > 10:
                options_used += mv
            else:
                vampire_used += mv

        return AllocationBudget(
            total_equity=equity,
            options_budget=options_budget,
            vampire_budget=vampire_budget,
            reserve_target=reserve_target,
            options_used=options_used,
            vampire_used=vampire_used,
            options_available=max(0, options_budget - options_used),
            vampire_available=max(0, vampire_budget - vampire_used),
        )

    def can_allocate_options(self, amount: float) -> bool:
        budget = self.get_budget()
        if amount > budget.options_available:
            log.info(
                "Options allocation denied: $%.0f requested, $%.0f available",
                amount,
                budget.options_available,
            )
            return False
        return True

    def can_allocate_vampire(self, amount: float) -> bool:
        budget = self.get_budget()
        if amount > budget.vampire_available:
            log.info(
                "Vampire allocation denied: $%.0f requested, $%.0f available",
                amount,
                budget.vampire_available,
            )
            return False
        return True

    def needs_rebalance(self, tolerance_pct: float = 0.05) -> bool:
        """Check if allocations have drifted beyond tolerance."""
        budget = self.get_budget()
        if budget.total_equity == 0:
            return False

        options_actual = budget.options_used / budget.total_equity
        vampire_actual = budget.vampire_used / budget.total_equity

        options_drift = abs(options_actual - self.config.options_pct)
        vampire_drift = abs(vampire_actual - self.config.vampire_pct)

        return options_drift > tolerance_pct or vampire_drift > tolerance_pct
