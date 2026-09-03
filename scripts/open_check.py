"""Independent post-open verification, run by a systemd timer on the box.

The agent reports on itself at its own checkpoints, which is useless in the one
case that matters most: when the agent is dead. This runs as a separate
process, on a separate timer, and can therefore report that the agent is not
running at all.

It checks the failure modes that actually occurred on 2026-08-31 rather than a
generic health ping: the reject storm that rate-limited the account, the
fill-confirmation poll that silently failed 101 times, the MCP server that
stopped CSP scanning for half a session, and the sleeve caps.

Read-only. It places no orders and changes nothing.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LOG = "/opt/alpaca-agent/logs/session.err"
SCALPER = {"QQQ", "SPY", "HOOD", "TQQQ", "SOXL"}
OCC = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")
# 2026-09-03: this used to be a frozen dict, right for whatever the split was
# on the day someone wrote it and silently wrong every time the split changed
# since. It said "SixFold over cap" all morning against the live 75% target
# because the number here still said 50%. Derive it from the same file the
# allocator reads, every call, so it can never go stale again.
def sleeve_caps(equity: float) -> dict[str, float]:
    from src.core.config import load_config
    cfg = load_config()
    return {"Vampire": equity * cfg.vampire_pct, "CSP": equity * cfg.options_pct,
            "SixFold": equity * cfg.sixfold_pct}


REGIME_LOG = "/opt/alpaca-agent/logs/regime.jsonl"
REJECT_STORM = 50          # 4,700 in 29 minutes on 2026-08-31
UNITS = ("alpaca-agent", "alpaca-watchdog")

_CTX = ssl.create_default_context()


def _api(path: str):
    req = urllib.request.Request(
        "https://paper-api.alpaca.markets" + path,
        headers={
            "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        },
    )
    return json.load(urllib.request.urlopen(req, timeout=30, context=_CTX))


def units_status() -> tuple[list[str], bool]:
    """systemctl is the only thing that can say the agent is gone."""
    lines, healthy = [], True
    for u in UNITS:
        try:
            active = subprocess.run(["systemctl", "is-active", u],
                                    capture_output=True, text=True, check=False).stdout.strip()
            restarts = subprocess.run(["systemctl", "show", u, "-p", "NRestarts", "--value"],
                                      capture_output=True, text=True, check=False).stdout.strip() or "0"
        except Exception as exc:                       # pragma: no cover
            lines.append(f"{u}: unreadable ({exc})")
            healthy = False
            continue
        lines.append(f"{u} {active} restarts={restarts}")
        if active != "active" or restarts not in ("0", ""):
            healthy = False
    return lines, healthy


def log_signatures(since: str) -> dict:
    """Count today's known failure signatures in timestamped lines only.

    Tracebacks are not timestamped, so an unanchored grep counts one failure
    many times and sweeps in the whole day's history besides.
    """
    stamped = re.compile(r"^(\d{2}:\d{2}:\d{2})\s")
    counts = Counter()
    try:
        with open(LOG, errors="replace") as fh:
            for line in fh:
                m = stamped.match(line)
                if not m or m.group(1) < since:
                    continue
                if "submit rejected" in line:
                    counts["rejects"] += 1
                if "cannot read order" in line:
                    counts["failed_polls"] += 1
                if "Could not start the MCP server" in line:
                    counts["mcp_failures"] += 1
                if "consecutive rejects, pausing" in line:
                    counts["backoff_trips"] += 1
                if "MCP quote source ready" in line:
                    counts["mcp_ready"] += 1
    except FileNotFoundError:
        counts["log_missing"] = 1
    return dict(counts)


def collateral(symbol: str, qty: float, market_value: float) -> tuple[str, float]:
    """A short put ties up strike x 100, not its market value."""
    m = OCC.match(symbol)
    if m:
        return "CSP", int(m.group(4)) / 1000 * 100 * abs(qty)
    if symbol in SCALPER:
        return "Vampire", abs(market_value)
    return "SixFold", abs(market_value)


def fills_today(day: str) -> tuple[Counter, dict]:
    """Signed cash flow per symbol, which with the open mark gives real P&L."""
    acts, token = [], None
    while True:
        url = f"/v2/account/activities/FILL?date={day}&page_size=100&direction=asc"
        if token:
            url += f"&page_token={token}"
        batch = _api(url)
        if not batch:
            break
        acts += batch
        token = batch[-1]["id"]
        if len(batch) < 100:
            break
    n, cash = Counter(), defaultdict(float)
    for f in acts:
        sym = f["symbol"]
        if sym not in SCALPER:
            continue
        qty, px = float(f["qty"]), float(f["price"])
        n[sym] += 1
        cash[sym] += (-qty * px) if f["side"].startswith("buy") else (qty * px)
    return n, dict(cash)


def vampire_gate_opened_today(day: str) -> bool | None:
    """Did the LLM regime gate say "chop" for either Vampire symbol today?

    True: the gate was open at least once; a fill was possible and didn't
    happen, which is worth escalating. False: verdicts exist and none was
    "chop"; zero fills is the gate doing its job. None: no verdicts at all,
    which on an open market past the first window means the advisor itself
    may be down -- that stays a problem, same as before this check existed.
    """
    try:
        with open(REGIME_LOG, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    saw_any = False
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        at = rec.get("at")
        if not at:
            continue
        if datetime.fromtimestamp(at, ET).strftime("%Y-%m-%d") != day:
            continue
        saw_any = True
        if rec.get("regime") == "chop":
            return True
    return False if saw_any else None


def build_report(since: str) -> tuple[str, str, str]:
    now = datetime.now(ET)
    day = now.strftime("%Y-%m-%d")
    problems: list[str] = []

    unit_lines, units_ok = units_status()
    if not units_ok:
        problems.append("a service is down or restarting")

    sig = log_signatures(since)
    if sig.get("rejects", 0) > REJECT_STORM:
        problems.append(f"{sig['rejects']} order rejects")
    if sig.get("failed_polls"):
        problems.append(f"{sig['failed_polls']} failed fill polls")
    if sig.get("mcp_failures"):
        problems.append("MCP down, CSP cannot scan")

    # systemd OnCalendar knows weekdays, not market holidays. Without this the
    # check alarms on Thanksgiving and Christmas, and a monitor that cries wolf
    # on a closed market is one nobody reads by December.
    try:
        session_open = bool(_api("/v2/clock")["is_open"])
    except Exception:
        session_open = True          # unknown: prefer a false alarm to silence

    acct = _api("/v2/account")
    equity, prior = float(acct["equity"]), float(acct["last_equity"])

    used = defaultdict(float)
    for p in _api("/v2/positions"):
        sleeve, amount = collateral(p["symbol"], float(p["qty"]), float(p["market_value"]))
        used[sleeve] += amount
    marks = {p["symbol"]: float(p["market_value"]) for p in _api("/v2/positions")}
    caps = sleeve_caps(equity)
    for sleeve, cap in caps.items():
        if used[sleeve] > cap * 1.02:
            problems.append(f"{sleeve} over cap")

    n, cash = fills_today(day)
    total_fills = sum(n.values())
    if total_fills == 0 and session_open:
        # Since PR #85/#86 zero fills has a legitimate cause: the LLM regime
        # gate only opens entries in "chop", and it correctly stayed shut all
        # morning of 2026-09-03 while dell4-chat read trend_up. Flagging that
        # as a "PROBLEM" every trending morning is the same staleness bug as
        # the hardcoded caps above, just against a design that predates the
        # gate. Distinguish "the gate said no" (expected, not a problem) from
        # "the gate never got a verdict" (the advisor may be dead) and "the
        # gate said yes and nothing happened anyway" (a real failure).
        gate_open_today = vampire_gate_opened_today(day)
        if gate_open_today is None:
            problems.append("zero Vampire fills, no regime verdicts today")
        elif gate_open_today:
            problems.append("zero Vampire fills despite an open regime gate")
        # gate_open_today is False: the gate never said chop. Working as designed.

    if problems:
        headline = "PROBLEM: " + "; ".join(problems)
    elif not session_open:
        headline = "Market closed, nothing to verify"
    else:
        headline = "Open OK"
    lines = [headline, f"equity ${equity:,.2f}  day {equity - prior:+,.2f}"]

    if n:
        pnl_total = 0.0
        parts = []
        for sym in sorted(n, key=lambda s: -n[s]):
            pnl = cash[sym] + marks.get(sym, 0.0)
            pnl_total += pnl
            parts.append(f"{sym} {n[sym]}f {pnl:+.2f}")
        lines.append(f"Vampire {total_fills} fills {pnl_total:+.2f}: " + ", ".join(parts))
    else:
        lines.append("Vampire: no fills yet")

    lines.append("  ".join(f"{s} ${used[s]:,.0f}/{caps[s]:,.0f}" for s in ("Vampire", "CSP", "SixFold")))
    lines.append("  ".join(unit_lines))
    lines.append("log: " + (", ".join(f"{k}={v}" for k, v in sorted(sig.items())) or "clean"))

    severity = "high" if problems else "default"
    if problems:
        title = "Alpaca PROBLEM at the open"
    elif not session_open:
        title = f"Alpaca market closed {now:%H:%M} ET"
    else:
        title = f"Alpaca open OK {now:%H:%M} ET"
    return title, "\n".join(lines), severity


def publish(title: str, body: str, severity: str) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC unset; printing instead\n" + body)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=body.encode(),
        headers={"Title": title, "Priority": severity},
    )
    urllib.request.urlopen(req, timeout=20, context=_CTX).read()


def main() -> int:
    since = sys.argv[1] if len(sys.argv) > 1 else "09:30:00"
    title, body, severity = build_report(since)
    print(f"{title}\n{body}")
    try:
        publish(title, body, severity)
    except Exception as exc:                            # pragma: no cover
        print(f"ntfy publish failed: {exc}", file=sys.stderr)
        return 2
    return 1 if severity == "high" else 0


if __name__ == "__main__":
    raise SystemExit(main())
