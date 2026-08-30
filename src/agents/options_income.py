"""Options Income Agent: orchestrates CSP + Covered Call strategies."""

from __future__ import annotations

import logging
import time
from datetime import datetime, time as dt_time

from src.core.alpaca_client import AlpacaClient
from src.core.market_data import MarketDataService
from src.core.options_chain import OptionsChain
from src.core.position_tracker import PositionTracker
from src.strategies.csp import CashSecuredPutStrategy
from src.strategies.covered_call import CoveredCallStrategy
from src.risk.circuit_breakers import CircuitBreaker
from src.risk.allocation import AllocationManager

log = logging.getLogger(__name__)

SCAN_INTERVAL = 300  # seconds between scans


class OptionsIncomeAgent:
    """Manages options income generation through CSP and covered calls.

    Runs on a periodic schedule during market hours:
    1. Check if circuit breaker allows trading
    2. Check allocation budget
    3. Scan for CSP opportunities and execute best
    4. Scan for CC opportunities on existing positions and execute best
    """

    def __init__(
        self,
        client: AlpacaClient,
        data: MarketDataService,
        tracker: PositionTracker,
        breaker: CircuitBreaker,
        allocator: AllocationManager,
    ):
        self._client = client
        self._chain = OptionsChain(client)
        self._data = data
        self._tracker = tracker
        self._breaker = breaker
        self._allocator = allocator

        self._csp = CashSecuredPutStrategy(
            client, self._chain, data, tracker, allocator=allocator, breaker=breaker
        )
        self._cc = CoveredCallStrategy(client, self._chain, data, tracker)

    def run_cycle(self) -> dict:
        """Execute one scan-and-trade cycle. Returns summary of actions taken."""
        if not self._breaker.check():
            log.warning("Circuit breaker active, skipping options cycle")
            return {"status": "breaker_active", "reason": self._breaker.trip_reason}

        budget = self._allocator.get_budget()
        if budget.options_available < 1000:
            log.info("Insufficient options budget ($%.0f), skipping", budget.options_available)
            return {"status": "insufficient_budget", "available": budget.options_available}

        results = {"csp_trades": [], "cc_trades": [], "status": "ok"}

        csp_trades = self._csp.execute_best(max_trades=2, budget=budget.options_available)
        results["csp_trades"] = csp_trades

        cc_trades = self._cc.execute_best(max_trades=2)
        results["cc_trades"] = cc_trades

        log.info(
            "Options cycle complete: %d CSP, %d CC trades",
            len(csp_trades),
            len(cc_trades),
        )
        return results

    def run_loop(self):
        """Blocking loop that runs cycles during market hours."""
        log.info("Options Income Agent started")
        while True:
            now = datetime.now().time()
            if dt_time(9, 35) <= now <= dt_time(15, 30):
                try:
                    self.run_cycle()
                except Exception:
                    log.exception("Options cycle failed")
            else:
                log.debug("Outside market hours, sleeping")

            time.sleep(SCAN_INTERVAL)
