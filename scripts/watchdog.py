#!/usr/bin/env python3
"""Independent breach watchdog for the scalper sleeve.

Reads the BROKER's positions, never the engine's opinion of them. Every breach
today happened while the engine believed it was inside its cap, so a guard that
asks the engine is a guard that agrees with the failure.

It contains rather than only alerting: the three breaches escalated from $95k to
$278k in roughly a minute each, which is faster than a person can read a
notification and act.
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import certifi
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from src.core.alpaca_client import AlpacaClient  # noqa: E402
from src.core.config import get_config  # noqa: E402
from src.core.notify import notify  # noqa: E402

log = logging.getLogger("watchdog")

WARN_AT = 1.2      # of the sleeve
CONTAIN_AT = 1.5


def scalper_exposure(client, symbols: set[str]) -> tuple[float, dict[str, float]]:
    detail: dict[str, float] = {}
    for p in client.get_positions():
        sym = str(p.symbol).upper()
        if len(sym) <= 6 and sym in symbols:
            detail[sym] = abs(float(p.market_value))
    return sum(detail.values()), detail


def contain(client, symbols: set[str], exposure: float, cap: float, detail: dict) -> None:
    """Stop the agent, cancel orders, flatten the scalper. Options are left."""
    log.error("BREACH: $%.0f against a $%.0f sleeve. Containing.", exposure, cap)
    subprocess.run(["pkill", "-f", "supervise.sh"], check=False)
    time.sleep(1)
    subprocess.run(["pkill", "-f", "run_live.py"], check=False)

    closed = []
    try:
        client.cancel_all_orders()
    except Exception:
        log.exception("cancel_all_orders failed")
    for sym in list(detail):
        try:
            client.close_position(sym)
            closed.append(sym)
        except Exception:
            log.exception("could not close %s", sym)

    notify(
        "SCALPER BREACH · agent stopped",
        f"Exposure ${exposure:,.0f} against a ${cap:,.0f} sleeve "
        f"({exposure / cap:.1f}x).\n\n"
        + "\n".join(f"  {s}: ${v:,.0f}" for s, v in detail.items())
        + f"\n\nAgent stopped, orders cancelled, closed: {closed or 'nothing'}.\n"
          "Options positions were left alone.",
        severity="urgent", tags=["rotating_light"],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=20.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s watchdog: %(message)s",
                        datefmt="%H:%M:%S")

    client = AlpacaClient()
    cfg = get_config()
    symbols = {s.upper() for s in cfg.vampire_symbols}
    warned = False

    while True:
        try:
            equity = float(client.get_account().equity)
            cap = equity * cfg.vampire_pct
            exposure, detail = scalper_exposure(client, symbols)

            if cap <= 0:
                log.info("scalper allocation is zero; exposure $%.0f", exposure)
            elif exposure >= cap * CONTAIN_AT:
                contain(client, symbols, exposure, cap, detail)
                return 1
            elif exposure >= cap * WARN_AT:
                if not warned:
                    notify("Scalper above its sleeve",
                           f"${exposure:,.0f} against ${cap:,.0f} "
                           f"({exposure / cap:.1f}x). Containment at "
                           f"{CONTAIN_AT:.1f}x.",
                           severity="high", tags=["warning"])
                    warned = True
                log.warning("exposure $%.0f of $%.0f cap", exposure, cap)
            else:
                warned = False
                log.info("ok: scalper $%.0f of $%.0f (%.0f%%)",
                         exposure, cap, 100 * exposure / cap if cap else 0)
        except Exception:
            log.exception("check failed")

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
