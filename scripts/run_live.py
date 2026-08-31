#!/usr/bin/env python3
"""Run the trading system for a session, with per-sleeve reporting to ntfy.

Wraps the coordinator rather than replacing it, and adds the thing a coordinator
does not give you: periodic attribution of what each strategy is doing, so a
single account P&L number is not the only signal available mid-session.
"""

from __future__ import annotations

import logging
import pathlib
import signal
import sys
import threading
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Python puts the script's own directory on sys.path, not the working
# directory, so `src` is not importable when run as scripts/run_live.py.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

load_dotenv()

from src.agents.coordinator import Coordinator  # noqa: E402
from src.core.notify import fmt_money, notify  # noqa: E402
from src.core.strategy_report import build_report, render  # noqa: E402

ET = ZoneInfo("America/New_York")
OPEN, CLOSE = dt_time(9, 30), dt_time(16, 0)
REPORT_EVERY = 1800  # seconds

log = logging.getLogger("run_live")


def market_is_open() -> bool:
    now = datetime.now(ET)
    return now.weekday() < 5 and OPEN <= now.time() <= CLOSE


def report(coord: Coordinator, *, prefix: str = "", severity: str = "default") -> None:
    try:
        snap = coord._tracker.get_snapshot()
        body = render(snap, build_report(snap))
        notify(f"{prefix}Alpaca agent · {fmt_money(snap.daily_pnl)}", body,
               severity=severity, tags=["chart_with_upwards_trend"])
        log.info("reported: equity %.2f daily %.2f", snap.equity, snap.daily_pnl)
    except Exception:
        log.exception("report failed")


def reporter(coord: Coordinator, stop: threading.Event) -> None:
    """Report on a fixed cadence. Never allowed to raise into the trading loop."""
    while not stop.wait(REPORT_EVERY):
        if market_is_open():
            report(coord)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    coord = Coordinator()
    stop = threading.Event()

    def shutdown(signum, _frame):
        log.info("signal %s, shutting down", signum)
        stop.set()
        report(coord, prefix="STOPPED · ", severity="high")
        try:
            coord.stop()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    budget = coord._allocator.get_budget()
    notify(
        "Alpaca agent · session starting",
        f"CSP ${budget.options_budget:,.0f} · Vampire ${budget.vampire_budget:,.0f} "
        f"· Reserve ${budget.reserve_target:,.0f}\n\n"
        f"Equity ${budget.total_equity:,.2f}",
        tags=["rocket"],
    )

    while not market_is_open():
        now = datetime.now(ET)
        if now.time() > CLOSE or now.weekday() >= 5:
            log.info("outside a trading session; idling")
        else:
            log.info("pre-market %s ET, waiting for 09:30", now.strftime("%H:%M"))
        if stop.wait(30):
            return 0

    log.info("market open — starting agents")
    threading.Thread(target=reporter, args=(coord, stop), daemon=True).start()
    report(coord, prefix="OPEN · ")

    coord.start()   # blocks in the coordination loop
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
