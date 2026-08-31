"""Portfolio-wide circuit breakers and risk gates."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.core.config import get_config
from src.core.position_tracker import PositionTracker

log = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    max_daily_loss_pct: float = 0.02
    max_position_pct: float = 0.10
    max_single_trade_pct: float = 0.05
    min_cash_reserve: float = 5000.0
    cooldown_minutes: int = 30

    @classmethod
    def from_config(cls) -> "RiskLimits":
        r = get_config().risk
        d = cls()
        return cls(
            max_daily_loss_pct=float(r.get("max_daily_loss_pct", d.max_daily_loss_pct)),
            max_position_pct=float(r.get("max_position_pct", d.max_position_pct)),
            max_single_trade_pct=float(r.get("max_single_trade_pct", d.max_single_trade_pct)),
            min_cash_reserve=float(r.get("min_cash_reserve", d.min_cash_reserve)),
            cooldown_minutes=int(r.get("circuit_breaker_cooldown_minutes", d.cooldown_minutes)),
        )


class CircuitBreaker:
    """Enforces portfolio-wide risk limits and can halt trading."""

    def __init__(self, tracker: PositionTracker, limits: RiskLimits | None = None):
        self._tracker = tracker
        self.limits = limits or RiskLimits()
        self._tripped = False
        self._trip_time: datetime | None = None
        self._trip_reason: str = ""

    @property
    def is_tripped(self) -> bool:
        if self._tripped and self._trip_time:
            elapsed = datetime.now() - self._trip_time
            if elapsed > timedelta(minutes=self.limits.cooldown_minutes):
                log.info("Circuit breaker cooldown expired, resetting")
                self._tripped = False
                self._trip_time = None
                self._trip_reason = ""
        return self._tripped

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    def check(self) -> bool:
        """Run all risk checks. Returns True if trading is allowed."""
        if self.is_tripped:
            return False

        snapshot = self._tracker.get_snapshot()

        if self._check_daily_loss(snapshot):
            return False
        if self._check_cash_reserve(snapshot):
            return False

        return True

    def can_trade(self, symbol: str, notional: float) -> bool:
        """Check if a specific trade is within limits."""
        if not self.check():
            return False

        snapshot = self._tracker.get_snapshot()

        if notional > snapshot.equity * self.limits.max_single_trade_pct:
            log.warning(
                "Trade $%.0f exceeds single-trade limit (%.0f%% of $%.0f equity)",
                notional,
                self.limits.max_single_trade_pct * 100,
                snapshot.equity,
            )
            return False

        pos = snapshot.positions.get(symbol, {})
        pos_value = abs(pos.get("market_value", 0))
        if (pos_value + notional) > snapshot.equity * self.limits.max_position_pct:
            log.warning(
                "Position in %s would exceed %.0f%% of equity",
                symbol,
                self.limits.max_position_pct * 100,
            )
            return False

        return True

    def _check_daily_loss(self, snapshot) -> bool:
        max_loss = snapshot.equity * self.limits.max_daily_loss_pct
        if snapshot.daily_pnl <= -max_loss:
            self._trip("daily_loss", f"Daily loss ${abs(snapshot.daily_pnl):.0f} exceeds limit ${max_loss:.0f}")
            return True
        return False

    def _check_cash_reserve(self, snapshot) -> bool:
        if snapshot.cash < self.limits.min_cash_reserve:
            self._trip("cash_reserve", f"Cash ${snapshot.cash:.0f} below minimum ${self.limits.min_cash_reserve:.0f}")
            return True
        return False

    def _trip(self, reason: str, message: str):
        log.warning("CIRCUIT BREAKER TRIPPED: %s -- %s", reason, message)
        self._tripped = True
        self._trip_time = datetime.now()
        self._trip_reason = message

    def reset(self):
        self._tripped = False
        self._trip_time = None
        self._trip_reason = ""
