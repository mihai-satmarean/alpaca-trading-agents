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
from datetime import datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from src.agents.options_income import OptionsIncomeAgent
from src.agents.pendulum import PendulumAgent
from src.agents.risk_manager import RiskManagerAgent
from src.agents.sixfold_analyst import SixfoldAnalystAgent
from src.agents.vampire import VampireAgent
from src.core.alpaca_client import AlpacaClient
from src.core.config import get_config
from src.core.market_data import MarketDataService
from src.core.position_tracker import PositionTracker
from src.risk.allocation import AllocationConfig, AllocationManager, parse_occ
from src.risk.circuit_breakers import CircuitBreaker, RiskLimits
from src.strategies.pendulum import PendulumParams
from src.strategies.sixfold_executor import SixfoldExecutor

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
            config_overrides=cfg.vampire_engine_overrides or None,
        )
        pend = dict(cfg.pendulum or {})
        self._pendulum_agent = PendulumAgent(
            self._client, self._data, self._tracker, self._breaker, self._allocator,
            symbol=cfg.pendulum_symbol,
            params=PendulumParams(
                entry_z=float(pend.get("entry_z", -2.0)),
                entry_rsi=float(pend.get("entry_rsi", 10)),
                add_z=float(pend.get("add_z", -2.75)),
                exit_rsi=float(pend.get("exit_rsi", 70)),
                time_stop_days=int(pend.get("time_stop_days", 10)),
                atr_mult=float(pend.get("atr_mult", 1.5)),
                hard_stop_pct=float(pend.get("hard_stop_pct", 0.05)),
                regime_lookback=int(pend.get("regime_lookback", 200)),
                allow_below_regime=bool(pend.get("allow_below_regime", False)),
                below_regime_size_mult=float(pend.get("below_regime_size_mult", 0.5)),
                below_regime_atr_mult=float(pend.get("below_regime_atr_mult", 1.0)),
            ),
            risk_per_trade=float(pend.get("risk_per_trade", 0.01)),
            first_tranche=float(pend.get("first_tranche", 0.6)),
        ) if cfg.pendulum_pct > 0 else None

        self._risk_agent = RiskManagerAgent(
            self._client, self._tracker, self._breaker, self._allocator
        )

        # Universe and bands come from strategies.yml. A hardcoded list here
        # outranked the file for two days: it carried SPY and QQQ, which score
        # zero (no income statement), and omitted KO and PEP, which were in the
        # config and never scored once.
        self._sixfold_agent = SixfoldAnalystAgent(
            self._client,
            universe=cfg.sixfold_universe or None,
            buy_threshold=cfg.sixfold_buy_threshold,
            hold_threshold=cfg.sixfold_hold_threshold,
            dispose_threshold=cfg.sixfold_dispose_threshold,
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
                excluded=set(cfg.vampire_symbols or []) | {cfg.pendulum_symbol},
                max_concurrent=cfg.sixfold_max_concurrent,
            )

        self._running = False
        self._sixfold_thread: threading.Thread | None = None

    def prewarm(self) -> bool:
        """Start the SIXFOLD analyst before the market opens. Idempotent.

        The analyst only reads fundamentals; it places nothing, so it is safe
        to run outside the session. With the S&P 400 in the universe a full
        scan is about seven minutes, and start() only runs once the market is
        open, so without this the first executor cycles of the day would see
        an empty candidate list and the first buys would land around 09:40.
        Returns True if a scan thread was started by this call.
        """
        if self._sixfold_thread is not None and self._sixfold_thread.is_alive():
            return False
        self._sixfold_thread = threading.Thread(
            target=self._sixfold_agent.run_loop, daemon=True, name="sixfold-analyst")
        self._sixfold_thread.start()
        log.info("SIXFOLD analyst pre-warmed (%d names)", len(self._sixfold_agent._universe))
        return True

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
                "pendulum_budget": budget.pendulum_budget,
                "pendulum_used": budget.pendulum_used,
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

        # A pre-warmed analyst is already scanning; do not start a second one.
        self.prewarm()

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

                # Pendulum decides once a day on the prior close and acts at
                # the open. should_run() carries the once-a-day guard, so the
                # 10-minute loop calling it repeatedly is harmless.
                if self._pendulum_agent is not None and self._pendulum_agent.should_run():
                    try:
                        r = self._pendulum_agent.run_cycle()
                        log.info("PENDULUM %s -> %s (%s)", r.get("signal"),
                                 r.get("action", "none"), r.get("reason", ""))
                    except Exception:
                        log.exception("PENDULUM cycle failed")

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
