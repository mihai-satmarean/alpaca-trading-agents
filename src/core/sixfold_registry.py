"""Durable record of which symbols SIXFOLD opened, and when.

Ported alongside the Tradier build's exit rules (see sixfold_exits.py). That
build keeps a DynamoDB row per position with an openedAt field, surviving
Lambda cold starts by construction. This process has no equivalent: the
in-memory PositionTracker._trades list is wiped on every restart, which
happens routinely on this box, so a time-stop built on it would silently
forget every position's age -- a worse failure than not having the rule at
all, because it looks correct and occasionally reads every open position as
newly bought.

Two files, following this repo's own JOURNAL_PATH convention exactly
(notify.py, regime_advisor.py): plain JSONL, env-path-overridable, durable
because they are files rather than process memory.

The read/write postures are DELIBERATELY different, and that split is
load-bearing:

  - Writes (record_open, record_disposal, clear) never raise. A failed write
    to the audit trail must not block the trading path -- same reasoning as
    notify.py's _journal().
  - age_days() is a read whose safe failure direction is "unknown", and
    "unknown" correctly makes the time-stop not fire. Swallowing an error
    here is the RIGHT default, not a shortcut.
  - load_lockouts() is a read whose safe failure direction is the opposite:
    an unreadable lockout table must not silently read as "nothing is
    locked out", or the one check that exists to stop an immediate rebuy of
    a name just sold would fail exactly when it matters. It raises, and the
    caller is required to treat that as "cannot verify; reject every
    candidate this cycle" -- matching the Tradier source's own explicit
    choice (executor.mjs's exclusionsFor(): "Fail CLOSED on an unreadable
    lockout table").
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "logs")

POSITIONS_PATH = os.environ.get(
    "SIXFOLD_POSITIONS_JOURNAL", os.path.join(_LOGS_DIR, "sixfold_positions.jsonl"))
LOCKOUTS_PATH = os.environ.get(
    "SIXFOLD_LOCKOUT_JOURNAL", os.path.join(_LOGS_DIR, "sixfold_lockouts.jsonl"))

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _append(path: str, entry: dict) -> None:
    """Append one record. Never raises: an audit-trail write must not stop trading."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
    except Exception:
        log.warning("sixfold registry write to %s failed", path, exc_info=True)


def _replay_latest(path: str) -> dict[str, dict]:
    """Last record per symbol, in file order. Missing file reads as empty."""
    latest: dict[str, dict] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return latest
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except Exception:
            continue
        sym = str(row.get("symbol") or "").upper()
        if sym:
            latest[sym] = row
    return latest


def record_open(symbol: str, at: float | None = None) -> None:
    """Write once at buy submission. Optimistic: reconcile() below is what
    makes an unfilled/canceled order's phantom entry self-heal, rather than
    a fill-confirmation poll added to the buy loop's hot path."""
    _append(POSITIONS_PATH, {
        "symbol": symbol.upper(), "event": "open",
        "at": at if at is not None else time.time(),
    })


def clear(symbol: str) -> None:
    """Remove a symbol from the open-position registry: a confirmed
    disposal, or reconcile() finding it no longer held."""
    _append(POSITIONS_PATH, {"symbol": symbol.upper(), "event": "clear", "at": time.time()})


def age_days(symbol: str) -> float | None:
    """Days since the open was recorded, or None if unknown.

    None is the correct, safe answer for an unreadable file or a symbol with
    no recorded open: it makes the time-stop simply not fire, which is the
    same direction as "we don't know, so don't force a sale."
    """
    try:
        rows = _replay_latest(POSITIONS_PATH)
    except Exception:
        log.warning("sixfold registry read failed for age_days(%s)", symbol, exc_info=True)
        return None
    row = rows.get(symbol.upper())
    if not row or row.get("event") != "open":
        return None
    opened_at = row.get("at")
    if not isinstance(opened_at, (int, float)):
        return None
    return max(0.0, (time.time() - opened_at) / 86400.0)


def open_symbols() -> set[str]:
    """Every symbol currently tracked as open. Unreadable file reads as empty
    (the same direction as age_days: unknown means the new rules simply
    don't apply to it, score-dispose is untouched)."""
    try:
        rows = _replay_latest(POSITIONS_PATH)
    except Exception:
        log.warning("sixfold registry read failed for open_symbols()", exc_info=True)
        return set()
    return {sym for sym, row in rows.items() if row.get("event") == "open"}


def reconcile(held: set[str]) -> set[str]:
    """Clear any registry-tracked symbol no longer actually held.

    Called at the top of every run_disposals() pass, before that cycle's own
    buy loop can resubmit anything. Bounds a phantom open-date (an order
    that never filled, or was canceled) to exactly one cycle: this call
    clears it before it could ever be used to make a disposal decision.

    Returns the symbols cleared, for logging.
    """
    tracked = open_symbols()
    stale = tracked - {s.upper() for s in held}
    for sym in stale:
        clear(sym)
    return stale


def record_disposal(symbol: str, reason: str, lockout_days: float) -> None:
    """Write once on a confirmed disposal. Never raises."""
    until = _now().timestamp() + lockout_days * 86400.0
    _append(LOCKOUTS_PATH, {
        "symbol": symbol.upper(), "reason": reason,
        "until": until, "recorded_at": time.time(),
    })


def load_lockouts() -> dict[str, float]:
    """Every symbol currently locked out, mapped to its until-epoch.

    RAISES on an unreadable file, deliberately: this is the one read in this
    module whose safe failure direction is the opposite of age_days()/
    open_symbols(). An unreadable lockout table must not silently read as
    "nothing is locked out" -- that is precisely the check whose purpose is
    stopping an immediate rebuy of a name just sold. The caller is required
    to treat a raise here as "cannot verify this cycle; reject every SIXFOLD
    buy candidate," matching the Tradier source's own explicit choice.

    Expired entries are dropped so a stale row cannot lock a symbol out
    forever if reconcile-equivalent cleanup never runs on this file.

    A missing file is NOT a failure to fail closed on: it means no disposal
    has ever been recorded, which is a legitimate, safe empty state (day one
    on a fresh deployment must not block every buy candidate). Anything else
    -- permission denied, a corrupted line, a disk error -- raises, because
    those mean the table exists and cannot currently be trusted.
    """
    try:
        with open(LOCKOUTS_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return {}
    latest: dict[str, float] = {}
    now = time.time()
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        row = json.loads(ln)
        sym = str(row.get("symbol") or "").upper()
        until = row.get("until")
        if sym and isinstance(until, (int, float)):
            latest[sym] = float(until)
    return {sym: until for sym, until in latest.items() if until > now}
