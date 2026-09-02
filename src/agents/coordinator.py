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
from src.core.alpaca_client import AlpacaClient, load_config
from src.core.config import get_config
from src.core.decision_log import record
from src.core.agent_status import write_snapshot
from src.core.operator_commands import drain_pending, mark_applied, snapshot as operator_snapshot
from src.core.market_data import MarketDataService
from src.core.position_tracker import PositionTracker
from src.risk.allocation import AllocationConfig, AllocationManager, parse_occ
from src.risk.circuit_breakers import CircuitBreaker, RiskLimits
from src.strategies.pendulum import PendulumParams
from src.strategies.sixfold_executor import SixfoldExecutor

log = logging.getLogger(__name__)

REBALANCE_INTERVAL = 600  # 10 minutes
DRY_RUN_INTERVAL = 30


class Coordinator:
    """Top-level orchestrator that manages all trading agents.

    Lifecycle:
    1. Initialize Alpaca client and shared services
    2. Create strategy agents (options, vampire) and risk manager
    3. Start all agents in separate threads/tasks
    4. Periodically rebalance allocations
    5. Shut down gracefully on signal or EOD
    """

    def __init__(self, *, staging: bool = False, dry_run: bool = False):
        config = load_config(staging=staging)
        self._client = AlpacaClient(config=config, dry_run=dry_run)
        log.info(
            "Coordinator env=%s dry_run=%s key=%s...",
            self._client.environment,
            self._client.is_dry_run,
            config.api_key[:6],
        )
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
        vcfg = dict(cfg.vampire or {})
        overrides = {}
        if cfg.vampire_paused_until:
            overrides["paused_until"] = cfg.vampire_paused_until
        if vcfg.get("max_daily_loss") is not None:
            overrides["max_daily_loss"] = float(vcfg["max_daily_loss"])
        if vcfg.get("max_trades_per_min") is not None:
            overrides["max_trades_per_min"] = int(vcfg["max_trades_per_min"])
        self._vampire_agent = VampireAgent(
            self._client,
            self._data,
            self._tracker,
            self._breaker,
            self._allocator,
            symbols=cfg.vampire_symbols or ["SPY", "QQQ"],
            config_overrides=overrides or None,
        )
        self._skip_sixfold = False
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
                excluded=set(cfg.vampire_symbols or []) | {cfg.pendulum_symbol},
            )

        self._running = False
        self._last_heavy_cycle = 0.0

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
        self._publish_snapshot()
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

    def _publish_snapshot(self) -> None:
        from src.core.finance_advisor import last_council

        six = self._sixfold_executor
        scan = self._sixfold_agent.last_scan
        vampire = self._vampire_agent.get_status()
        picker = self._vampire_agent.picker_status()
        risk = self._risk_agent.check()
        try:
            snapshot = self._tracker.get_snapshot()
            budget = self._allocator.get_budget()
            coordinator = {
                "equity": snapshot.equity,
                "cash": snapshot.cash,
                "daily_pnl": snapshot.daily_pnl,
                "positions": len(snapshot.positions),
                "trades_today": self._tracker.trade_count_today,
                "allocation": {
                    "options_used": budget.options_used,
                    "vampire_used": budget.vampire_used,
                    "reserve_target": budget.reserve_target,
                },
            }
        except Exception:
            coordinator = {}
        payload = {
            "environment": self._client.environment,
            "dry_run": self._client.is_dry_run,
            "market_open": self._is_market_open(),
            "vampire": vampire,
            "vampire_picker": picker,
            "sixfold_analyst": {
                "last_scan": scan.isoformat() if scan else None,
                "buy_candidates": (
                    self._sixfold_agent.get_buy_candidates() if scan else []
                ),
            },
            "sixfold_executor": {
                "orders": list(getattr(six, "last_orders", []) or []),
                "rejections": list(getattr(six, "last_rejections", []) or []),
            },
            "council": last_council,
            "options": getattr(self._options_agent, "last_cycle", {}) or {},
            "risk": risk,
            "coordinator": coordinator,
            "observer": {
                "source": "coordinator_snapshot",
                "vampire_thoughts": {
                    sym: (info.get("last_thought") or {})
                    for sym, info in (vampire or {}).items()
                },
            },
            "operator": operator_snapshot(),
        }
        try:
            write_snapshot(payload)
        except Exception:
            log.warning("could not write agent snapshot", exc_info=True)

    def _coordination_loop(self):
        while self._running:
            try:
                if not self._is_market_open():
                    log.debug("Market closed, sleeping")
                    self._apply_operator_commands()
                    self._publish_snapshot()
                    time.sleep(60)
                    continue

                self._apply_operator_commands()

                heavy_every = (
                    DRY_RUN_INTERVAL if self._client.is_dry_run else REBALANCE_INTERVAL
                )
                now = time.time()
                run_heavy = now - self._last_heavy_cycle >= heavy_every
                if run_heavy:
                    self._last_heavy_cycle = now
                    if self._skip_sixfold:
                        self._skip_sixfold = False
                        record("coordinator", "sixfold_cycle",
                               thought="operator skipped this cycle",
                               decision="skipped")
                    elif self._sixfold_executor is not None:
                        try:
                            result = self._sixfold_executor.run_cycle()
                            orders = result.get("orders") or []
                            rejections = result.get("rejections") or []
                            if orders:
                                log.info("SIXFOLD placed %d orders", len(orders))
                            record(
                                "coordinator", "sixfold_cycle",
                                thought=(
                                    f"status={result.get('status')} "
                                    f"orders={len(orders)} skips={len(rejections)}"
                                ),
                                decision=str(result.get("status") or "ok"),
                                orders=orders,
                                rejections=rejections,
                            )
                        except Exception:
                            log.exception("SIXFOLD cycle failed")

                    # Pendulum decides once a day on the prior close and acts at
                    # the open. should_run() carries the once-a-day guard, so the
                    # 10-minute heavy cycle calling it is harmless.
                    if self._pendulum_agent is not None and self._pendulum_agent.should_run():
                        try:
                            r = self._pendulum_agent.run_cycle()
                            log.info("PENDULUM %s -> %s (%s)", r.get("signal"),
                                     r.get("action", "none"), r.get("reason", ""))
                            record(
                                "coordinator", "pendulum_cycle",
                                symbol=str(r.get("symbol") or ""),
                                thought=str(r.get("reason") or ""),
                                decision=str(r.get("action") or r.get("signal") or "none"),
                            )
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
                    record(
                        "coordinator", "status",
                        thought=(
                            f"equity=${status['equity']:.0f} pnl=${status['daily_pnl']:.0f} "
                            f"trades={status['trades_today']} positions={status['positions']}"
                        ),
                        decision="ok",
                        vampire=status.get("vampire_status"),
                        sixfold=status.get("sixfold"),
                    )
                self._publish_snapshot()

            except Exception:
                log.exception("Coordination cycle error")

            time.sleep(DRY_RUN_INTERVAL)

    def _apply_operator_commands(self) -> None:
        try:
            pending = drain_pending()
        except Exception:
            log.warning("could not read operator queue", exc_info=True)
            return
        for cmd in pending:
            action = str(cmd.get("action") or "")
            try:
                if action == "pause_vampire":
                    self._vampire_agent.halt()
                    mark_applied(cmd, result="vampire halted")
                elif action == "resume_vampire":
                    self._vampire_agent.unhalt()
                    mark_applied(cmd, result="vampire resumed")
                elif action == "force_hunt":
                    self._vampire_agent.request_hunt()
                    mark_applied(cmd, result="hunt requested; waiting for council")
                elif action == "skip_sixfold":
                    self._skip_sixfold = True
                    mark_applied(cmd, result="next SIXFOLD cycle skipped")
                elif action == "skip_options":
                    self._options_agent.skip_next_cycle = True
                    mark_applied(cmd, result="next options cycle skipped")
                else:
                    mark_applied(cmd, result=f"unknown action {action}", ok=False)
                record(
                    "coordinator", "operator",
                    thought=action,
                    decision=cmd.get("id") or "",
                )
            except Exception as exc:
                log.exception("operator command %s failed", action)
                mark_applied(cmd, result=str(exc), ok=False)

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
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="ProductAdvisors trading coordinator")
    parser.add_argument(
        "--staging",
        action="store_true",
        help="Use ALPACA_STAGING_* keys (isolated paper account)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log orders without submitting them",
    )
    args = parser.parse_args()
    coordinator = Coordinator(staging=args.staging, dry_run=args.dry_run)
    coordinator.start()


if __name__ == "__main__":
    main()
