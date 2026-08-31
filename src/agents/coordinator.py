"""Coordinator Agent: capital allocation, strategy delegation, rebalancing.

This is the main entry point for the trading system.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from src.core.alpaca_client import AlpacaClient
from src.core.config import get_config
from src.core.market_data import MarketDataService
from src.core.position_tracker import PositionTracker
from src.risk.circuit_breakers import CircuitBreaker, RiskLimits
from src.risk.allocation import AllocationManager, AllocationConfig, parse_occ
from src.agents.options_income import OptionsIncomeAgent
from src.agents.vampire import VampireAgent
from src.agents.risk_manager import RiskManagerAgent
from src.strategies.sixfold_executor import SixfoldExecutor
from src.agents.sixfold_analyst import SixfoldAnalystAgent

log = logging.getLogger(__name__)

REBALANCE_INTERVAL = 600  # 10 minutes


class Coordinator:
    """Top-level orchestrator that manages all trading agents.

    Lifecycle:
    1. Initialize Alpaca client and shared services
    2. Create strategy agents (options, vampire) and risk manager
    3. Start all agents in separate threads/tasks
    4. Periodically rebalance allocations
    5. Shut down gracefully on signal or EOD
    """

    def __init__(self):
        self._client = AlpacaClient()
        self._data = MarketDataService(self._client)
        self._tracker = PositionTracker(self._client)

        cfg = get_config()
        for problem in cfg.validate():
            log.warning("Config problem: %s", problem)

        self._breaker = CircuitBreaker(self._tracker, RiskLimits.from_config())
        self._allocator = AllocationManager(self._tracker, AllocationConfig.from_config())

        self._options_agent = OptionsIncomeAgent(
            self._client, self._data, self._tracker, self._breaker, self._allocator
        )
        self._vampire_agent = VampireAgent(
            self._client,
            self._data,
            self._tracker,
            self._breaker,
            self._allocator,
            symbols=cfg.vampire_symbols or ["SPY", "QQQ"],
        )
        self._risk_agent = RiskManagerAgent(
            self._client, self._tracker, self._breaker, self._allocator
        )

        self._sixfold_agent = SixfoldAnalystAgent(
            self._client,
            universe=[
                "SPY", "QQQ", "AAPL", "MSFT", "GOOGL", "AMZN",
                "NVDA", "META", "JPM", "V", "JNJ", "UNH",
                "PG", "HD", "COST", "ABBV", "LLY", "MRK",
            ],
        )

        # SIXFOLD's analyst scores names and places no orders; this is what
        # lets the largest sleeve actually deploy. Same gates as every other
        # strategy: it is the least proven signal here, not the most trusted.
        self._sixfold_executor = None
        analyst = getattr(self, "_sixfold_agent", None) or getattr(self, "_sixfold", None)
        if analyst is not None:
            self._sixfold_executor = SixfoldExecutor(
                self._client, self._data, self._tracker, self._breaker,
                self._allocator, analyst,
                excluded=set(cfg.vampire_symbols or []),
            )

        self._running = False

    def status(self) -> dict:
        snapshot = self._tracker.get_snapshot()
        budget = self._allocator.get_budget()
        risk_report = self._risk_agent.check()

        sixfold_summary = {}
        if self._sixfold_agent.last_scan:
            buy_candidates = self._sixfold_agent.get_buy_candidates()
            sixfold_summary = {
                "last_scan": self._sixfold_agent.last_scan.isoformat(),
                "buy_candidates": buy_candidates,
                "total_scored": len(self._sixfold_agent.scores),
            }

        return {
            "timestamp": datetime.now().isoformat(),
            "equity": snapshot.equity,
            "cash": snapshot.cash,
            "daily_pnl": snapshot.daily_pnl,
            "total_pnl": snapshot.total_pnl,
            "positions": len(snapshot.positions),
            "trades_today": self._tracker.trade_count_today,
            "allocation": {
                "options_used": budget.options_used,
                "options_budget": budget.options_budget,
                "vampire_used": budget.vampire_used,
                "vampire_budget": budget.vampire_budget,
                "unattributed_used": budget.unattributed_used,
                "reserve_target": budget.reserve_target,
            },
            "vampire_status": self._vampire_agent.get_status(),
            "risk": risk_report,
            "sixfold": sixfold_summary,
        }

    def start(self):
        """Start all agents and the coordination loop."""
        log.info("=== ProductAdvisors Trading System Starting ===")

        account = self._client.get_account()
        log.info("Account equity: $%s | Cash: $%s", account.equity, account.cash)

        self._running = True
        self._setup_signal_handlers()

        sixfold_thread = threading.Thread(target=self._sixfold_agent.run_loop, daemon=True)
        sixfold_thread.start()

        risk_thread = threading.Thread(target=self._risk_agent.run_loop, daemon=True)
        risk_thread.start()

        options_thread = threading.Thread(target=self._options_agent.run_loop, daemon=True)
        options_thread.start()

        loop = asyncio.new_event_loop()
        vampire_thread = threading.Thread(
            target=lambda: loop.run_until_complete(self._vampire_agent.run()),
            daemon=True,
        )
        vampire_thread.start()

        log.info("All agents started (including SIXFOLD analyst). Entering coordination loop.")
        self._coordination_loop()

    def _is_market_open(self) -> bool:
        try:
            clock = self._client.get_clock()
            return clock.is_open
        except Exception:
            now = datetime.now(ZoneInfo("America/New_York"))
            if now.weekday() >= 5:
                return False
            t = now.time()
            return dt_time(9, 30) <= t <= dt_time(16, 0)

    def _coordination_loop(self):
        while self._running:
            try:
                if not self._is_market_open():
                    log.debug("Market closed, sleeping")
                    time.sleep(60)
                    continue

                if self._sixfold_executor is not None:
                    try:
                        result = self._sixfold_executor.run_cycle()
                        if result.get("orders"):
                            log.info("SIXFOLD placed %d orders", len(result["orders"]))
                    except Exception:
                        log.exception("SIXFOLD cycle failed")

                if self._allocator.needs_rebalance():
                    log.info("Rebalancing allocations")
                    budget = self._allocator.get_budget()
                    log.info(
                        "Options: $%.0f/$%.0f | Vampire: $%.0f/$%.0f",
                        budget.options_used,
                        budget.options_budget,
                        budget.vampire_used,
                        budget.vampire_budget,
                    )

                status = self.status()
                log.info(
                    "Status: equity=$%.0f pnl=$%.0f trades=%d positions=%d",
                    status["equity"],
                    status["daily_pnl"],
                    status["trades_today"],
                    status["positions"],
                )

            except Exception:
                log.exception("Coordination cycle error")

            time.sleep(REBALANCE_INTERVAL)

    def stop(self):
        log.info("=== Shutting down trading system ===")
        # SIXFOLD's analyst scores names and places no orders; this is what
        # lets the largest sleeve actually deploy. Same gates as every other
        # strategy: it is the least proven signal here, not the most trusted.
        self._sixfold_executor = None
        analyst = getattr(self, "_sixfold_agent", None) or getattr(self, "_sixfold", None)
        if analyst is not None:
            self._sixfold_executor = SixfoldExecutor(
                self._client, self._data, self._tracker, self._breaker,
                self._allocator, analyst,
                excluded=set(cfg.vampire_symbols or []),
            )

        self._running = False
        self._sixfold_agent.stop()
        self._vampire_agent.stop_all()
        self.cancel_intraday_orders()
        log.info("Shutdown complete")

    def cancel_intraday_orders(self) -> list[str]:
        """Cancel the scalper's resting orders and leave the options book alone.

        Shutdown used to cancel everything. A restart therefore killed resting
        CSP limit orders that were waiting to fill, which over a multi-day run
        quietly forfeits fills the strategy had already earned. The scalper's
        orders genuinely should not survive the process; the options sleeve's
        should, for the same reason its positions survive the daily flatten.
        """
        cancelled: list[str] = []
        try:
            for order in self._client.get_orders(status="open"):
                symbol = str(getattr(order, "symbol", "")).upper()
                if parse_occ(symbol) is not None:
                    continue                      # options sleeve: leave resting
                try:
                    self._client.cancel_order(str(order.id))
                    cancelled.append(symbol)
                except Exception:
                    log.warning("could not cancel %s", symbol, exc_info=True)
            log.info("Cancelled intraday orders: %s", cancelled or "(none)")
        except Exception:
            log.exception("Could not enumerate open orders")
        return cancelled

    def _setup_signal_handlers(self):
        def handler(signum, frame):
            log.info("Signal %d received, shutting down", signum)
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    coordinator = Coordinator()
    coordinator.start()


if __name__ == "__main__":
    main()
