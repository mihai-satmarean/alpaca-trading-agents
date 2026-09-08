"""Run the Vampire against Tradier, defaulting to the sandbox paper account.

    .venv/bin/python scripts/run_vampire_tradier.py --symbols QQQ,TQQQ

The strategy, its regime gate and its risk logic are the Alpaca build
unchanged; only the broker and the quote source are swapped. Production is
reachable with --live but is refused unless --i-understand-this-is-real is
also passed, because the same code against api.tradier.com is Frank's own
money in a shared account.

Reports to the ntfy topic the rest of the desk already uses (NTFY_TOPIC), so
this shows up where the Alpaca engine's alerts show up.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import pathlib
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import certifi
from dotenv import load_dotenv

# Python puts the script's own directory on sys.path, not the working
# directory, so an absolute-import runner started as a file needs the repo
# root added by hand. Same fix as scripts/run_live.py:25.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

load_dotenv()
load_dotenv(os.path.expanduser("~/Documents/Development/Options-Trader/.env"), override=False)
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from src.core.config import load_config              # noqa: E402
from src.core.notify import notify                   # noqa: E402
from src.core.tradier_client import TradierClient     # noqa: E402
from src.core.tradier_market_data import TradierMarketData  # noqa: E402
from src.strategies.regime_advisor import RegimeAdvisor     # noqa: E402
from src.strategies.vampire_engine import VampireConfig, VampireEngine  # noqa: E402

ET = ZoneInfo("America/New_York")
log = logging.getLogger("vampire-tradier")


class Tracker:
    """The engine records trades through this; here it just counts them."""

    def __init__(self):
        self.trades = []

    def record_trade(self, symbol, action, qty, price, strategy):
        self.trades.append((symbol, action, qty, price))


def venue_is_open(client) -> tuple[bool, str]:
    """Ask Tradier, not the wall clock.

    systemd's OnCalendar knows weekdays; it does not know Labor Day. Tradier's
    own /markets/clock does, and it is the same venue we would be trading, so
    it cannot disagree with itself.
    """
    try:
        clk = client._request("GET", "/markets/clock").get("clock") or {}
    except Exception as exc:
        return True, f"clock unreadable ({type(exc).__name__}); assuming open"
    state = str(clk.get("state") or "").lower()
    return state == "open", f"{clk.get('date')} {state} ({clk.get('description')})"


def orders_are_accepted(client) -> tuple[bool, str]:
    """Preflight: can this venue take an order at all?

    Tradier's sandbox order endpoint returned HTTP 500 on every symbol, type,
    duration and order class on 2026-09-08 while every read endpoint stayed
    healthy, and the identical request returned 200 against production. The
    body was the Apigee gateway's plain-text "An error occurred while
    communicating with the backend", i.e. the upstream order service, not
    auth and not the payload. A scalping loop against a venue that cannot
    accept orders produces nothing but a reject storm and a misleading log,
    so we check first with a preview that places nothing.
    """
    try:
        client._request("POST", f"/accounts/{client._account}/orders", {
            "class": "equity", "symbol": "QQQ", "side": "buy", "quantity": "1",
            "type": "market", "duration": "day", "preview": "true"})
        return True, "order endpoint healthy"
    except Exception as exc:
        return False, str(exc)[:200]


def wait_for_order_endpoint(client, budget_seconds, probe_seconds=60,
                            probe=None, sleep=None, clock=None, alert=None):
    """Wait out a broken order endpoint instead of forfeiting the session.

    An outage that starts before the bell is not a reason to idle until
    tomorrow: the venue may come back mid-morning, and whatever is left of
    the session is still tradeable. So we probe until the endpoint accepts
    an order or the budget runs out, and we alert exactly twice, once when
    the wait starts and once when it ends, rather than once per probe.

    Returns (healthy, detail, waited_seconds).
    """
    probe = probe or (lambda: orders_are_accepted(client))
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    alert = alert or (lambda *a, **k: None)

    started = clock()
    healthy, detail = probe()
    if healthy:
        return True, detail, 0.0

    alert("waiting", detail, 0.0)
    while True:
        waited = clock() - started
        if waited + probe_seconds > budget_seconds:
            alert("gave-up", detail, waited)
            return False, detail, waited
        sleep(probe_seconds)
        healthy, detail = probe()
        if healthy:
            waited = clock() - started
            alert("recovered", detail, waited)
            return True, detail, waited


def build(args):
    cfg = load_config()
    sandbox = not args.live
    client = TradierClient(sandbox=sandbox)
    data = TradierMarketData(sandbox=sandbox, poll_seconds=args.poll)
    tracker = Tracker()

    adv_cfg = cfg.vampire_regime_advisor
    advisor = None
    if adv_cfg and not args.no_gate:
        advisor = RegimeAdvisor(
            model=str(adv_cfg.get("model", "dell4-chat")),
            window_seconds=int(adv_cfg.get("window_minutes", 15)) * 60,
            bars_needed=int(adv_cfg.get("bars", 30)),
            ttl_seconds=int(adv_cfg.get("ttl_minutes", 20)) * 60,
            min_confidence=float(adv_cfg.get("min_confidence", 0.0)),
        )

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    overrides = dict(cfg.vampire_engine_overrides or {})
    overrides.pop("paused_until", None)          # the pause is an Alpaca-side decision
    engines = {}
    for sym in symbols:
        c = VampireConfig(symbol=sym, **overrides)
        c.max_notional = args.notional
        c.max_daily_loss = args.max_loss
        if advisor is not None:
            c.entry_gate = (lambda s=sym: advisor.entry_allowed(s))
        engines[sym] = VampireEngine(client, data, tracker, c)
    return cfg, client, data, tracker, advisor, engines


def calibrate(engines, data):
    """Same window discipline as the Alpaca build: never trade off a guess."""
    for sym, engine in engines.items():
        sample = data.recent_spread(sym)
        if not sample:
            log.info("%s: not enough book observed yet; keeping threshold %.4f",
                     sym, engine.cfg.tick_threshold)
            continue
        engine.cfg.tick_threshold = round(max(sample["median"] * 2.5, 0.02), 4)
        log.info("%s spread median %.3f p90 %.3f over %d polls -> tick_threshold %.4f",
                 sym, sample["median"], sample["p90"], sample["n"], engine.cfg.tick_threshold)


async def main_async(args):
    cfg, client, data, tracker, advisor, engines = build(args)
    venue = "SANDBOX (paper)" if not args.live else "PRODUCTION (REAL MONEY)"
    acct = client.get_account()
    log.info("Vampire on Tradier %s, account equity %s, symbols %s",
             venue, acct.equity, list(engines))

    is_open, why = venue_is_open(client)
    if not is_open and not args.ignore_clock:
        log.info("venue closed: %s. Nothing to do.", why)
        notify(f"Vampire/Tradier idle: market closed",
               f"{why}\nNo session run. The timer will try again next trading day.",
               severity="low")
        return
    log.info("venue: %s", why)

    # Spend at most the session itself waiting; whatever is left is tradeable.
    budget = args.duration * 60 if args.duration else 6 * 3600
    if args.preflight_wait is not None:
        budget = args.preflight_wait * 60

    def preflight_alert(stage, detail, waited):
        if stage == "waiting":
            log.warning("venue will not accept orders: %s. Waiting.", detail)
            notify("Vampire/Tradier waiting: venue will not accept orders",
                   f"{detail}\n\nReads are healthy; this is the order endpoint. "
                   f"Nothing traded yet. Retrying every {args.preflight_probe:.0f}s "
                   f"for up to {budget / 60:.0f} min, and the session starts the "
                   f"moment it comes back.",
                   severity="high")
        elif stage == "recovered":
            log.info("order endpoint recovered after %.0fs", waited)
            notify("Vampire/Tradier venue recovered",
                   f"Order endpoint accepted a preview after {waited / 60:.1f} min. "
                   f"Starting the session now.",
                   severity="default")
        else:
            log.error("order endpoint still down after %.0fs; standing down", waited)
            notify("Vampire/Tradier stood down: venue never accepted orders",
                   f"Waited {waited / 60:.1f} min. Last response:\n\n{detail}\n\n"
                   f"Nothing was traded. The timer will try again next trading day.",
                   severity="high")

    healthy, detail, _ = wait_for_order_endpoint(
        client, budget, probe_seconds=args.preflight_probe, alert=preflight_alert)
    if not healthy:
        return
    log.info("preflight: %s", detail)

    # Resolve borrow once, from flat, before any tick can fire a short. TQQQ is
    # not shortable at Tradier at all, so the alternative is a reject per tick.
    long_only = []
    for sym, engine in engines.items():
        if int(client.net_position(sym)) == 0 and not client.is_shortable(sym):
            engine.cfg.allow_short = False
            long_only.append(sym)
    if long_only:
        log.warning("long-only this session (no borrow at Tradier): %s",
                    ", ".join(long_only))
        notify("Vampire/Tradier running long-only on some symbols",
               f"No borrow at Tradier for: {', '.join(long_only)}.\n"
               f"Those symbols buy dips and sell them back only; the short leg "
               f"is disabled rather than rejected once per tick. "
               f"Bi-directional: {', '.join(s for s in engines if s not in long_only) or 'none'}.",
               severity="default")

    for engine in engines.values():
        engine.reconcile(int(client.net_position(engine.cfg.symbol)), None)

    notify(f"Vampire on Tradier {venue}",
           f"symbols {', '.join(engines)}\nequity {acct.equity}\n"
           f"poll {args.poll}s, notional cap ${args.notional:,.0f}/symbol, "
           f"daily loss cap ${args.max_loss:,.0f}\n"
           f"regime gate: {'on (' + advisor.model + ')' if advisor else 'OFF'}",
           severity="default")

    stop = threading.Event()

    def regime_loop():
        while not stop.is_set():
            if advisor is not None:
                for sym in list(engines):
                    try:
                        advisor.refresh(sym, data.get_recent_minute_bars(sym, minutes=90))
                    except Exception:
                        log.warning("%s: regime refresh failed; entries stay closed", sym,
                                    exc_info=True)
            calibrate(engines, data)
            stop.wait(advisor.window_seconds if advisor else 900)

    threading.Thread(target=regime_loop, name="regime", daemon=True).start()

    async def on_quote(quote):
        engine = engines.get(quote.symbol)
        if engine:
            engine.tick(quote.mid, data.get_vwap(quote.symbol, engine.cfg.bleed_window_seconds))

    await data.subscribe_quotes(list(engines), on_quote)

    async def reporter():
        while not stop.is_set():
            await asyncio.sleep(args.report * 60)
            lines = []
            for sym, e in engines.items():
                st = advisor.status().get(sym, {}) if advisor else {}
                lines.append(f"{sym}: net {e.net_position:+d} realized ${e.realized_pnl:+.2f} "
                             f"trades {len(e.bleeds)} thr ${e.cfg.tick_threshold:.4f} "
                             f"regime {st.get('regime') or 'n/a'}")
            total = sum(e.realized_pnl for e in engines.values())
            notify(f"Vampire/Tradier {venue.split()[0]} ${total:+.2f}",
                   "\n".join(lines), severity="default")

    asyncio.create_task(reporter())

    def shutdown(*_):
        stop.set()
        data.stop()
        for sym, e in engines.items():
            try:
                e._flatten_all("shutdown")
            except Exception:
                log.exception("could not flatten %s", sym)
        total = sum(e.realized_pnl for e in engines.values())
        notify(f"Vampire/Tradier stopped ${total:+.2f}",
               "\n".join(f"{s}: realized ${e.realized_pnl:+.2f} over {len(e.bleeds)} trades"
                         for s, e in engines.items()), severity="default")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (shutdown(), sys.exit(0)))

    if args.duration:
        asyncio.get_event_loop().call_later(args.duration * 60, lambda: stop.set())
    await data.run_stream()
    shutdown()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="QQQ,TQQQ")
    p.add_argument("--poll", type=float, default=1.0, help="seconds between quote polls")
    p.add_argument("--notional", type=float, default=2500.0, help="max $ exposure per symbol")
    p.add_argument("--max-loss", dest="max_loss", type=float, default=50.0)
    p.add_argument("--report", type=float, default=15.0, help="ntfy report interval, minutes")
    p.add_argument("--duration", type=float, default=0, help="minutes to run, 0 = until stopped")
    p.add_argument("--preflight-wait", type=float, default=None,
                   help="minutes to wait for a broken order endpoint (default: the session)")
    p.add_argument("--preflight-probe", type=float, default=60,
                   help="seconds between order-endpoint probes while waiting")
    p.add_argument("--no-gate", action="store_true", help="run without the LLM regime gate")
    p.add_argument("--ignore-clock", action="store_true", help="run even if the venue reports closed")
    p.add_argument("--live", action="store_true", help="production Tradier, real money")
    p.add_argument("--i-understand-this-is-real", dest="confirmed", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.live and not a.confirmed:
        print("--live targets real money. Re-run with --i-understand-this-is-real.", file=sys.stderr)
        return 2
    asyncio.run(main_async(a))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
