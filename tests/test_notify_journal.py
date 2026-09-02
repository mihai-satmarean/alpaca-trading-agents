"""Every notification is recorded, including the ones that never arrived.

A delivery channel only records successes. When a publish fails there is no
trace anywhere, so an alerting outage looks exactly like a quiet system and
survives for days. That has happened before on the ntfy path (a string
priority silently 400ing every push), so the journal records the attempt and
its outcome rather than the delivery.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

import src.core.notify as N


@pytest.fixture(autouse=True)
def _tmp_journal(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "notifications.jsonl")
    monkeypatch.setattr(N, "JOURNAL_PATH", path)
    return path


class TestJournal:
    def test_a_successful_send_is_recorded(self, monkeypatch):
        monkeypatch.setattr(N, "publish_sns", lambda *a, **k: True)
        monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:t")
        assert N.notify("t", "m") is True
        j = N.read_journal()
        assert len(j) == 1 and j[0]["delivered"] is True and j[0]["transport"] == "sns"

    def test_a_failed_send_is_ALSO_recorded(self, monkeypatch):
        """The whole point. A channel that drops the message records nothing;
        this must record that we tried and it did not land."""
        def boom(*a, **k):
            raise OSError("network down")
        monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
        monkeypatch.setattr(N.urllib.request, "urlopen", boom)
        assert N.notify("t", "m") is False
        j = N.read_journal()
        assert len(j) == 1
        assert j[0]["delivered"] is False
        assert "network down" in j[0]["error"]

    def test_an_sns_failure_that_falls_back_is_marked(self, monkeypatch):
        monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:t")
        monkeypatch.setattr(N, "publish_sns", lambda *a, **k: False)

        class R:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(N.urllib.request, "urlopen", lambda *a, **k: R())
        assert N.notify("t", "m") is True
        j = N.read_journal()
        assert j[0]["sns_failed"] is True and j[0]["transport"] == "ntfy"

    def test_journaling_never_breaks_a_send(self, monkeypatch):
        """Monitoring must not be able to take down trading."""
        monkeypatch.setattr(N, "JOURNAL_PATH", "/nonexistent/nope/x.jsonl")
        monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)

        class R:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(N.urllib.request, "urlopen", lambda *a, **k: R())
        assert N.notify("t", "m") is True     # did not raise

    def test_read_journal_is_newest_first_and_survives_junk(self, _tmp_journal):
        with open(_tmp_journal, "w") as fh:
            fh.write(json.dumps({"ts": "1", "title": "old"}) + "\n")
            fh.write("{ not json\n")
            fh.write(json.dumps({"ts": "2", "title": "new"}) + "\n")
        j = N.read_journal()
        assert [x["title"] for x in j] == ["new", "old"]

    def test_a_missing_journal_reads_as_empty_not_an_error(self, monkeypatch):
        monkeypatch.setattr(N, "JOURNAL_PATH", "/nonexistent/x.jsonl")
        assert N.read_journal() == []

    def test_the_journal_is_trimmed_rather_than_growing_without_bound(self, monkeypatch, _tmp_journal):
        monkeypatch.setattr(N, "JOURNAL_MAX_BYTES", 500)
        for i in range(200):
            N._journal({"ts": str(i), "title": "x" * 40})
        assert os.path.getsize(_tmp_journal) <= 500 * 40


class TestTheDashboardCannotFakeAZero:
    """A fetch failure must not render as 'no notifications'. That reading is
    identical to a quiet system and wrong in the direction that hides an
    outage."""

    def test_a_fetch_failure_returns_an_error_not_an_empty_list(self, monkeypatch):
        import dashboard.app as app
        monkeypatch.setattr(app.urllib.request if hasattr(app, "urllib") else N.urllib.request,
                            "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("tls boom")))
        msgs, err = app._ntfy_live("some-topic")
        assert msgs == []
        assert err and "tls boom" in err, (
            "an unreachable ntfy must be reported, never rendered as zero messages"
        )
