"""Options Income Agent: orchestrates CSP + Covered Call strategies."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from src.core.alpaca_client import AlpacaClient
from src.core.market_data import MarketDataService
from src.core.mcp_client import AlpacaMCPClient
from src.core.option_quotes import mcp_quote_provider
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
        self._alpaca = client
        self._chain = OptionsChain(self._alpaca)
        self._data = data
        self._tracker = tracker
        self._breaker = breaker
        self._allocator = allocator

        self._mcp: AlpacaMCPClient | None = None
        self._csp = CashSecuredPutStrategy(
            self._alpaca, self._chain, data, tracker,
            allocator=allocator, breaker=breaker,
            quote_provider=self._build_quote_provider(),
        )
        self._cc = CoveredCallStrategy(self._alpaca, self._chain, data, tracker)

    def _build_quote_provider(self):
        """Live option quotes through Alpaca's MCP server.

        Returning None is a deliberate outcome, not a failure to handle: the
        scanner refuses to price a contract it cannot quote, so a broken MCP
        path costs us trades instead of producing blind ones.
        """
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            log.error("ALPACA_API_KEY/SECRET_KEY unset; CSP scanning disabled")
            return None
        try:
            self._mcp = AlpacaMCPClient(key, secret, paper=True)
            self._mcp.start()
            log.info("MCP quote source ready (%d tools)", len(self._mcp.list_tools()))
            return mcp_quote_provider(self._mcp)
        except Exception:
            log.exception("Could not start the MCP server; CSP scanning disabled")
            self._mcp = None
            return None

    def close(self) -> None:
        if self._mcp is not None:
            self._mcp.stop()
            self._mcp = None

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

    def _is_market_open(self) -> bool:
        try:
            clock = self._alpaca.get_clock()
            return clock.is_open
        except Exception:
            now = datetime.now(ZoneInfo("America/New_York"))
            if now.weekday() >= 5:
                return False
            return dt_time(9, 35) <= now.time() <= dt_time(15, 30)

    def run_loop(self):
        """Blocking loop that runs cycles during market hours."""
        log.info("Options Income Agent started")
        while True:
            if self._is_market_open():
                try:
                    self.run_cycle()
                except Exception:
                    log.exception("Options cycle failed")
            else:
                log.debug("Market closed, sleeping")

            time.sleep(SCAN_INTERVAL)
