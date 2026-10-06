"""Shared test fixtures.

isolate_sixfold_registry is autouse: without it, sixfold_registry's module-
level POSITIONS_PATH/LOCKOUTS_PATH default to the real logs/ directory in
this checkout, so one test's disposal writes a real lockout row that a LATER,
unrelated test then reads and is silently rejected by -- confirmed live: a
full-suite run produced a real logs/sixfold_lockouts.jsonl with a leaked JPM
row that made an unrelated buy test fail. Every test gets its own tmp_path
instead, matching the same discipline this repo already applies to
NOTIFY_JOURNAL/REGIME_JOURNAL for exactly this reason.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_sixfold_registry(tmp_path, monkeypatch):
    from src.core import sixfold_registry as registry
    monkeypatch.setattr(registry, "POSITIONS_PATH", str(tmp_path / "sixfold_positions.jsonl"))
    monkeypatch.setattr(registry, "LOCKOUTS_PATH", str(tmp_path / "sixfold_lockouts.jsonl"))
