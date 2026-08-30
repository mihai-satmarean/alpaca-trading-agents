"""Risk Manager Agent: monitors portfolio and enforces risk constraints."""

from __future__ import annotations

import logging
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from src.core.alpaca_client import AlpacaClient
from src.core.position_tracker import PositionTracker
from src.risk.circuit_breakers import CircuitBreaker, RiskLimits
from src.risk.allocation import AllocationManager

log = logging.getLogger(__name__)

CHECK_INTERVAL = 30  # seconds between risk checks


class RiskManagerAgent:
    """Continuously monitors portfolio health and can halt all trading.

    Responsibilities:
    - Periodic portfolio health checks
    - Circuit breaker enforcement
    - End-of-day position flattening
    - Allocation drift detection
    """

    def __init__(
        self,
        client: AlpacaClient,
        tracker: PositionTracker,
        breaker: CircuitBreaker,
        allocator: AllocationManager,
    ):
        self._alpaca = client
        self._tracker = tracker
        self._breaker = breaker
        self._allocator = allocator
        self._alerts: list[dict] = []

    @property
    def alerts(self) -> list[dict]:
        return list(self._alerts)

    def check(self) -> dict:
        """Run all risk checks and return status report."""
        report = {
            "timestamp": datetime.now().isoformat(),
            "trading_allowed": True,
            "alerts": [],
        }

        if not self._breaker.check():
            report["trading_allowed"] = False
            report["alerts"].append({
                "level": "critical",
                "message": f"Circuit breaker: {self._breaker.trip_reason}",
            })

        snapshot = self._tracker.get_snapshot()
        report["equity"] = snapshot.equity
        report["cash"] = snapshot.cash
        report["daily_pnl"] = snapshot.daily_pnl
        report["position_count"] = len(snapshot.positions)

        if self._allocator.needs_rebalance():
            budget = self._allocator.get_budget()
            report["alerts"].append({
                "level": "warning",
                "message": f"Allocation drift detected: options ${budget.options_used:.0f}/{budget.options_budget:.0f}, vampire ${budget.vampire_used:.0f}/{budget.vampire_budget:.0f}",
            })

        if snapshot.cash < 3000:
            report["alerts"].append({
                "level": "warning",
                "message": f"Low cash: ${snapshot.cash:.0f}",
            })

        for alert in report["alerts"]:
            self._alerts.append(alert)

        return report

    def should_flatten_eod(self) -> bool:
        try:
            clock = self._alpaca.get_clock()
            if not clock.is_open:
                return False
            now_et = datetime.now(ZoneInfo("America/New_York")).time()
            return now_et >= dt_time(15, 50)
        except Exception:
            now = datetime.now(ZoneInfo("America/New_York"))
            if now.weekday() >= 5:
                return False
            return now.time() >= dt_time(15, 50)

    def flatten_all(self):
        """Emergency flatten: close all positions and cancel all orders."""
        log.warning("RISK MANAGER: Flattening all positions")
        try:
            self._alpaca.cancel_all_orders()
            self._alpaca.close_all_positions()
            self._alerts.append({
                "level": "critical",
                "message": "All positions flattened",
                "timestamp": datetime.now().isoformat(),
            })
        except Exception:
            log.exception("Flatten failed")

    def run_loop(self):
        """Blocking loop for continuous risk monitoring."""
        log.info("Risk Manager Agent started")
        while True:
            try:
                report = self.check()
                if not report["trading_allowed"]:
                    log.warning("Trading halted: %s", report["alerts"])

                if self.should_flatten_eod():
                    self.flatten_all()
            except Exception:
                log.exception("Risk check failed")

            time.sleep(CHECK_INTERVAL)
