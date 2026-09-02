"""Risk Manager Agent: monitors portfolio and enforces risk constraints."""

from __future__ import annotations

import logging
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from src.core.alpaca_client import AlpacaClient
from src.core.config import get_config
from src.core.position_tracker import PositionTracker
from src.risk.circuit_breakers import CircuitBreaker, RiskLimits
from src.risk.allocation import AllocationManager, parse_occ

log = logging.getLogger(__name__)

CHECK_INTERVAL = 30  # seconds between risk checks

# The intraday sleeve is flattened inside this window, once per session. The
# options sleeve is deliberately excluded: it holds 7-45 DTE contracts whose
# entire thesis is carrying them overnight to collect theta.
EOD_FLATTEN_START = dt_time(15, 50)
EOD_FLATTEN_END = dt_time(16, 0)


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
        self._intraday_symbols = {s.upper() for s in get_config().vampire_symbols}
        self._last_flatten_date = None

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
        """True only inside the end-of-day window, and only once per session.

        Keeps main's broker-clock gate and explicit Eastern timezone, which are
        both better than reading the machine clock. Adds an upper bound and a
        per-session latch: `now >= 15:50` stays true until midnight, and the
        loop calls this every CHECK_INTERVAL seconds, so the book was being
        flattened roughly 980 times an evening.
        """
        now = datetime.now(ZoneInfo("America/New_York"))

        try:
            if not self._alpaca.get_clock().is_open:
                return False
        except Exception:
            if now.weekday() >= 5:
                return False

        if not (EOD_FLATTEN_START <= now.time() <= EOD_FLATTEN_END):
            return False
        return self._last_flatten_date != now.date()

    def flatten_intraday(self) -> list[str]:
        """Close the intraday (vampire) sleeve only, leaving the options book.

        The scalper must not carry overnight risk. The options income sleeve
        must carry it, or it earns nothing: closing a 30-DTE put the same
        afternoon it was opened forfeits all remaining theta and pays the
        option spread twice for the privilege.
        """
        closed: list[str] = []
        try:
            # Two defects lived here together, and the first hid the second.
            # This method read self._client, which the constructor never set
            # (it stores self._alpaca), so it raised AttributeError on its
            # first line every evening since it was written and never closed
            # anything: the scalper carried positions overnight and the
            # startup log showed "adopted broker position" each morning. Had
            # it run, cancel_all_orders() would have killed the CSP sleeve's
            # resting limit orders and SIXFOLD's DAY limits too, which is
            # exactly the forfeiture the docstring says this must not do.
            # Cancels are scoped to the intraday symbols, like the closes.
            for order in self._alpaca.get_orders(status="open"):
                sym = str(getattr(order, "symbol", "")).upper()
                if sym in self._intraday_symbols:
                    try:
                        self._alpaca.cancel_order(str(order.id))
                    except Exception:
                        log.warning("EOD: could not cancel %s order %s", sym,
                                    getattr(order, "id", "?"), exc_info=True)
            for pos in self._alpaca.get_positions():
                symbol = str(pos.symbol).upper()
                if parse_occ(symbol) is not None:
                    continue  # options sleeve: hold
                if symbol not in self._intraday_symbols:
                    continue  # not ours to close
                self._alpaca.close_position(symbol)
                closed.append(symbol)

            self._last_flatten_date = datetime.now(ZoneInfo("America/New_York")).date()
            self._alerts.append({
                "level": "info",
                "message": f"Intraday sleeve flattened: {closed or 'nothing open'}",
                "timestamp": datetime.now().isoformat(),
            })
            log.info("EOD: flattened intraday sleeve %s", closed or "(nothing open)")
        except Exception:
            log.exception("Intraday flatten failed")
        return closed

    def emergency_flatten_all(self):
        """Close everything, including options. Reserved for a tripped breaker."""
        log.warning("RISK MANAGER: emergency flatten of ALL positions")
        try:
            self._alpaca.cancel_all_orders()
            self._alpaca.close_all_positions()
            self._alerts.append({
                "level": "critical",
                "message": "All positions flattened (emergency)",
                "timestamp": datetime.now().isoformat(),
            })
        except Exception:
            log.exception("Emergency flatten failed")

    def run_loop(self):
        """Blocking loop for continuous risk monitoring."""
        log.info("Risk Manager Agent started")
        while True:
            try:
                report = self.check()
                if not report["trading_allowed"]:
                    log.warning("Trading halted: %s", report["alerts"])

                if self.should_flatten_eod():
                    self.flatten_intraday()
            except Exception:
                log.exception("Risk check failed")

            time.sleep(CHECK_INTERVAL)
