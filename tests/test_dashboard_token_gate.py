"""The dashboard is public; the token decides who sees the book.

The one property that matters is fail-closed: an unset or empty token must
lock the page, because a missing environment variable after a redeploy is
the failure nobody notices until the positions are on the internet.
"""

from __future__ import annotations

import sys

sys.argv = ["streamlit", "run"]
from dashboard.app import _token_ok  # noqa: E402


class TestTokenGate:
    def test_the_right_token_opens(self):
        assert _token_ok("abc123", "abc123")

    def test_a_wrong_token_locks(self):
        assert not _token_ok("abc124", "abc123")

    def test_no_configured_token_locks_everyone(self):
        """Fail closed. The opposite default publishes the account."""
        assert not _token_ok("anything", None)
        assert not _token_ok("anything", "")

    def test_no_supplied_token_locks(self):
        assert not _token_ok(None, "abc123")
        assert not _token_ok("", "abc123")

    def test_comparison_is_not_prefix_based(self):
        assert not _token_ok("abc", "abc123")
        assert not _token_ok("abc123x", "abc123")


class TestTheGateIsActuallyCalled:
    def test_main_gates_before_touching_the_broker(self, monkeypatch):
        """Asserts the call site. A gate function nobody calls protects
        nothing, and this is the wiring class that has bitten this repo
        four times this week."""
        import dashboard.app as app

        class Halt(Exception):
            pass

        def gate():
            raise Halt()
        monkeypatch.setattr(app, "require_token", gate)
        broker = app.get_client
        monkeypatch.setattr(app, "get_client", lambda: (_ for _ in ()).throw(AssertionError("broker touched before the gate")))
        try:
            app.main()
        except Halt:
            return
        raise AssertionError("main() did not call require_token()")
