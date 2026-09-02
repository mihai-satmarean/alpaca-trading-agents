"""Agent thought/decision journal: stdout plus an optional JSONL file.

Set DECISION_LOG to a path (run_staging.sh does this) to persist every record.
Vampire ticks are throttled by the caller so a live stream does not flood.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("decision")

_last: dict[str, float] = {}


def journal_path() -> str | None:
    path = os.environ.get("DECISION_LOG", "").strip()
    return path or None


def default_journal_path() -> str:
    """Path the cockpit uses when DECISION_LOG is unset (dashboard process)."""
    return str(Path(__file__).resolve().parents[2] / "logs" / "staging-decisions.jsonl")


def recent(limit: int = 40, *, agent: str | None = None) -> list[dict]:
    target = journal_path() or default_journal_path()
    path = Path(target)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict] = []
    for raw in lines[-max(limit * 4, limit):]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if agent and str(row.get("agent", "")) != agent:
            continue
        rows.append(row)
    return rows[-limit:]


def record(
    agent: str,
    event: str,
    *,
    symbol: str = "",
    thought: str = "",
    decision: str = "",
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent": agent,
        "event": event,
        "symbol": symbol,
        "thought": thought,
        "decision": decision,
    }
    for key, value in extra.items():
        if value is not None:
            payload[key] = value

    line = f"[DECISION] {agent} {event}"
    if symbol:
        line += f" {symbol}"
    if thought:
        line += f" | {thought}"
    if decision:
        line += f" => {decision}"
    log.info("%s", line)

    path = journal_path()
    if not path:
        return
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        log.warning("could not write decision log %s", path, exc_info=True)


_TRADE_EVENTS = {
    "long_entry", "short_entry", "long_exit", "short_exit", "order",
}


def count_trades_today() -> int:
    """Fills recorded in the journal for the current UTC day.

    The dashboard process has its own empty PositionTracker, so it cannot
    use tracker.trade_count_today. The journal is the shared source.
    """
    target = journal_path() or default_journal_path()
    path = Path(target)
    if not path.exists():
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    n = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                ts = str(row.get("ts") or "")
                if not ts.startswith(today):
                    continue
                if row.get("event") in _TRADE_EVENTS:
                    n += 1
    except OSError:
        return 0
    return n


def record_throttled(
    key: str,
    interval: float,
    agent: str,
    event: str,
    **kwargs: Any,
) -> None:
    now = time.time()
    if now - _last.get(key, 0.0) < interval:
        return
    _last[key] = now
    record(agent, event, **kwargs)


def reset_throttle() -> None:
    _last.clear()


def follow(path: str | None = None, *, history: int = 40) -> None:
    """Print recent journal lines, then follow new ones. Blocking."""
    target = path or journal_path()
    if not target:
        raise SystemExit("DECISION_LOG is not set")
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    open(target, "a", encoding="utf-8").close()
    print(f"Following {target}", flush=True)
    with open(target, encoding="utf-8") as fh:
        lines = fh.readlines()
        for raw in lines[-history:]:
            _print_line(raw)
        while True:
            raw = fh.readline()
            if raw:
                _print_line(raw)
                continue
            time.sleep(0.25)


def _print_line(raw: str) -> None:
    raw = raw.strip()
    if not raw:
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(raw, flush=True)
        return
    ts = str(data.get("ts", ""))[11:19]
    votes = data.get("votes")
    extra = ""
    if isinstance(votes, list) and votes:
        bits = [
            f"    {v.get('role')}: {v.get('verdict')} — {v.get('reasoning', '')}"
            for v in votes
            if isinstance(v, dict)
        ]
        if bits:
            extra = "\n" + "\n".join(bits)
    print(
        f"{ts} {data.get('agent', '')} {data.get('event', '')} "
        f"{data.get('symbol', '')} | {data.get('thought', '')} "
        f"=> {data.get('decision', '')}{extra}",
        flush=True,
    )
