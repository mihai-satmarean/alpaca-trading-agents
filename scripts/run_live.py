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
from src.agents.narrator import NarrationRequest, narrate, summarise_session  # noqa: E402
from src.core.strategy_report import build_report, render  # noqa: E402

ET = ZoneInfo("America/New_York")
OPEN, CLOSE = dt_time(9, 30), dt_time(16, 0)
REPORT_EVERY = 1800  # seconds

log = logging.getLogger("run_live")


def market_is_open() -> bool:
    now = datetime.now(ET)
    return now.weekday() < 5 and OPEN <= now.time() <= CLOSE


def _recent_actions(coord: Coordinator) -> list[dict]:
    """Whatever the options agent placed on its most recent cycle.

    Read off the agent rather than threaded through the coordinator, because the
    agent owns its own loop and the reporter is a passive observer of it.
    """
    cycle = getattr(getattr(coord, "_options_agent", None), "last_cycle", None) or {}
    out: list[dict] = []
    for trade in (cycle.get("csp_trades") or []):
        out.append({"strategy": "csp", "side": "sell_to_open", **trade})
    for trade in (cycle.get("cc_trades") or []):
        out.append({"strategy": "covered_call", "side": "sell_to_open", **trade})

    # The scalper is a fifth of the account; a report that omits it is wrong by
    # omission rather than merely thin.
    agent = getattr(coord, "_vampire_agent", None)
    if agent is not None and hasattr(agent, "activity_summary"):
        try:
            for row in agent.activity_summary():
                if row.get("trades") or row.get("net_position"):
                    out.append({
                        "strategy": "vampire",
                        "symbol": row["symbol"],
                        "side": f"{row['trades']} trades, net {row['net_position']:+d}",
                        "realized_pnl": row.get("realized_pnl"),
                        "reason": f"scalper {row.get('state')}",
                    })
        except Exception:
            log.warning("could not read vampire activity", exc_info=True)
    return out


def _recent_rejections(coord: Coordinator) -> list[dict]:
    """Why the options scanner refused what it refused."""
    csp = getattr(getattr(coord, "_options_agent", None), "_csp", None)
    return list(getattr(csp, "last_rejections", []) or [])


def report(coord: Coordinator, *, prefix: str = "", severity: str = "default",
           closing: bool = False) -> None:
    try:
        snap = coord._tracker.get_snapshot()
        sleeves = build_report(snap)
        try:
            orders = coord._client.get_orders(status="open")
        except Exception:
            log.warning("could not read working orders", exc_info=True)
            orders = []
        body = render(snap, sleeves, orders)

        # The narrative is additive. It is generated from the numbers above,
        # after the fact, and its absence changes nothing but the prose.
        request = NarrationRequest(
            equity=snap.equity,
            cash=snap.cash,
            daily_pnl=snap.daily_pnl,
            sleeves={n: {"committed": s.committed, "budget": s.budget,
                         "unrealized": s.unrealized, "positions": s.positions}
                     for n, s in sleeves.items()},
            actions=_recent_actions(coord),
            rejections=_recent_rejections(coord),
        )
        story = summarise_session(request) if closing else narrate(request)
        if story:
            body = f"{body}\n\n---\n{story}"
        else:
            log.info("narration unavailable; reporting numbers only")

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
        report(coord, prefix="STOPPED · ", severity="high", closing=True)
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
