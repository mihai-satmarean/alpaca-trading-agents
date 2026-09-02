"""Shared live snapshot for the Streamlit cockpit.

The coordinator (and any agent) writes a JSON file. The dashboard only reads it.
This keeps Streamlit from importing engines or submitting orders.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_NAME = "agent-status.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def snapshot_path() -> Path:
    env = os.environ.get("AGENT_STATUS_PATH", "").strip()
    if env:
        return Path(env)
    return repo_root() / "logs" / _DEFAULT_NAME


def write_snapshot(payload: dict[str, Any]) -> Path:
    path = snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read_snapshot() -> dict[str, Any]:
    path = snapshot_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
