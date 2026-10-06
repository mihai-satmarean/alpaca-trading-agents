"""Durable open-date and lockout tracking. See sixfold_registry.py's module
docstring for why the read/write failure postures deliberately differ.

isolate_sixfold_registry (tests/conftest.py, autouse) points POSITIONS_PATH/
LOCKOUTS_PATH at a fresh tmp_path per test, so nothing here touches the real
logs/ directory -- confirmed necessary: a full-suite run before that fixture
existed produced a real leaked logs/sixfold_lockouts.jsonl that made an
unrelated test fail.
"""

from __future__ import annotations

import json
import time

import pytest

from src.core import sixfold_registry as registry


class TestOpenDateSurvivesARestart:
    def test_recorded_once_then_readable(self):
        registry.record_open("AAPL")
        assert registry.age_days("AAPL") is not None
        assert registry.age_days("AAPL") < 0.01  # just opened

    def test_the_read_path_has_no_in_memory_cache(self):
        """The actual restart-survival property: age_days() must depend on
        nothing but the file's contents, since PositionTracker._trades (the
        thing this registry replaces) failed exactly because its state lived
        in a process-local list. Proven directly: write the raw file with no
        call to record_open() at all, and confirm it reads back -- nothing
        in this module's own writes is required for the read to work."""
        with open(registry.POSITIONS_PATH, "w") as fh:
            fh.write(json.dumps({"symbol": "MSFT", "event": "open", "at": time.time()}) + "\n")
        assert registry.age_days("MSFT") is not None

    def test_an_unregistered_symbol_has_unknown_age(self):
        assert registry.age_days("NVDA") is None

    def test_open_symbols_lists_what_was_recorded(self):
        registry.record_open("KO")
        registry.record_open("PEP")
        assert registry.open_symbols() == {"KO", "PEP"}


class TestReconciliation:
    """The self-healing half of the optimistic open-date write at buy time:
    an order that never fills leaves at most one cycle's phantom entry."""

    def test_a_symbol_no_longer_held_is_cleared(self):
        registry.record_open("PHANTOM")
        cleared = registry.reconcile(held=set())
        assert cleared == {"PHANTOM"}
        assert registry.age_days("PHANTOM") is None

    def test_a_symbol_still_held_survives_reconciliation(self):
        registry.record_open("HELD")
        registry.reconcile(held={"HELD"})
        assert registry.age_days("HELD") is not None

    def test_reconciliation_does_not_touch_unrelated_symbols(self):
        registry.record_open("A")
        registry.record_open("B")
        registry.reconcile(held={"A"})
        assert registry.age_days("A") is not None
        assert registry.age_days("B") is None


class TestDisposalClearsTheOpenDateAndStartsALockout:
    def test_clear_removes_a_recorded_open(self):
        registry.record_open("XYZ")
        registry.clear("XYZ")
        assert registry.age_days("XYZ") is None

    def test_a_rebuy_after_disposal_gets_a_fresh_open_date_not_the_old_one(self):
        registry.record_open("ABC")
        registry.clear("ABC")
        registry.record_open("ABC")
        assert registry.age_days("ABC") is not None
        assert registry.age_days("ABC") < 0.01

    def test_record_disposal_locks_the_symbol_out(self):
        registry.record_disposal("QRS", "profit target +36.0%", lockout_days=56.0)
        assert "QRS" in registry.load_lockouts()

    def test_lockout_window_matches_the_configured_days(self):
        registry.record_disposal("QRS", "reason", lockout_days=56.0)
        until = registry.load_lockouts()["QRS"]
        import time
        remaining_days = (until - time.time()) / 86400.0
        assert 55.9 < remaining_days < 56.1


class TestLockoutReadFailsClosedNotOpen:
    """The one deliberate asymmetry in this module: an unreadable
    load_lockouts() must not silently read as 'nothing is locked out'."""

    def test_a_missing_file_is_the_safe_empty_state(self):
        # Day one on a fresh deployment: no disposal has ever happened.
        assert registry.load_lockouts() == {}

    def test_a_corrupted_file_raises_rather_than_returning_empty(self, monkeypatch):
        with open(registry.LOCKOUTS_PATH, "w") as fh:
            fh.write("not valid json at all\n")
        with pytest.raises(Exception):
            registry.load_lockouts()

    def test_expired_lockouts_are_dropped(self):
        import time
        with open(registry.LOCKOUTS_PATH, "w") as fh:
            fh.write(json.dumps({
                "symbol": "OLD", "until": time.time() - 1000, "recorded_at": time.time() - 2000,
            }) + "\n")
        assert "OLD" not in registry.load_lockouts()

    def test_the_latest_row_wins_for_a_symbol_disposed_more_than_once(self):
        registry.record_disposal("DUP", "first", lockout_days=1.0)
        registry.record_disposal("DUP", "second", lockout_days=56.0)
        until = registry.load_lockouts()["DUP"]
        import time
        remaining_days = (until - time.time()) / 86400.0
        assert remaining_days > 50  # the second, longer lockout is in effect


class TestWritesNeverRaise:
    """Writes must not block the trading path -- a failed audit-trail write
    is a monitoring gap, not a reason to stop trading."""

    def test_record_open_survives_an_unwritable_path(self, monkeypatch):
        monkeypatch.setattr(registry, "POSITIONS_PATH", "/nonexistent-dir-xyz/positions.jsonl")
        registry.record_open("ANY")  # must not raise

    def test_record_disposal_survives_an_unwritable_path(self, monkeypatch):
        monkeypatch.setattr(registry, "LOCKOUTS_PATH", "/nonexistent-dir-xyz/lockouts.jsonl")
        registry.record_disposal("ANY", "reason", lockout_days=1.0)  # must not raise

    def test_age_days_survives_an_unreadable_positions_file(self, monkeypatch):
        with open(registry.POSITIONS_PATH, "w") as fh:
            fh.write("garbage\n")
        assert registry.age_days("ANY") is None  # safe direction for THIS read
