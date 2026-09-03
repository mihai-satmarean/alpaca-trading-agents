"""Verify the hash chains of the engine's journals. Exit 1 if any is broken.

    python scripts/verify_journals.py
"""
from __future__ import annotations

import sys

from src.core.journal import describe, verify_chain
from src.core.notify import JOURNAL_PATH as NOTIFY_JOURNAL
from src.strategies.regime_advisor import JOURNAL_PATH as REGIME_JOURNAL


def main() -> int:
    bad = 0
    for name, path in (("notifications", NOTIFY_JOURNAL), ("regime", REGIME_JOURNAL)):
        rep = verify_chain(path)
        print(describe(rep, name), f"({path})")
        if rep["intact"] is False:
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
