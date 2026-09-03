"""SHA-256 hash-chained journals (src/core/journal.py).

The claim being pinned: an edited, deleted, or reordered journal entry is
detectable by anyone with the file; a legitimately trimmed file still
verifies from its first retained line; and both live writers (notify's send
journal, the regime advisor's verdict journal) actually go through the chain
rather than a plain append.
"""

from __future__ import annotations

import json

from src.core import journal as J


def _lines(path):
    return [json.loads(ln) for ln in open(path, encoding="utf-8") if ln.strip()]


class TestTheChainForms:
    def test_three_appends_link_and_verify(self, tmp_path):
        p = str(tmp_path / "j.jsonl")
        for i in range(3):
            J.append_chained(p, {"n": i, "msg": f"entry {i}"}, max_bytes=10**6)
        recs = _lines(p)
        assert recs[0]["prev_hash"] == ""
        assert recs[1]["prev_hash"] == recs[0]["hash"]
        assert recs[2]["prev_hash"] == recs[1]["hash"]
        rep = J.verify_chain(p)
        assert rep["intact"] is True and rep["chained"] == 3 and rep["legacy"] == 0

    def test_the_hash_does_not_depend_on_key_order(self):
        assert J.entry_hash("", {"a": 1, "b": 2}) == J.entry_hash("", {"b": 2, "a": 1})

    def test_the_hash_depends_on_the_predecessor(self):
        assert J.entry_hash("abc", {"a": 1}) != J.entry_hash("xyz", {"a": 1})

    def test_a_missing_file_is_nothing_to_verify_not_a_failure(self, tmp_path):
        assert J.verify_chain(str(tmp_path / "nope.jsonl"))["intact"] is None


class TestTamperingIsDetected:
    def _three(self, tmp_path):
        p = str(tmp_path / "j.jsonl")
        for i in range(3):
            J.append_chained(p, {"n": i, "msg": f"entry {i}"}, max_bytes=10**6)
        return p

    def test_editing_a_middle_entry_breaks_its_hash(self, tmp_path):
        p = self._three(tmp_path)
        recs = _lines(p)
        recs[1]["msg"] = "entry 1, quietly rewritten"
        with open(p, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in recs)
        rep = J.verify_chain(p)
        assert rep["intact"] is False
        assert rep["first_break"] == (1, "entry hash does not match its content")

    def test_deleting_a_middle_entry_breaks_the_link(self, tmp_path):
        p = self._three(tmp_path)
        recs = _lines(p)
        del recs[1]
        with open(p, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in recs)
        rep = J.verify_chain(p)
        assert rep["intact"] is False
        assert rep["first_break"] == (1, "prev_hash does not link to the previous entry")

    def test_reordering_entries_breaks_the_link(self, tmp_path):
        p = self._three(tmp_path)
        recs = _lines(p)
        recs[1], recs[2] = recs[2], recs[1]
        with open(p, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in recs)
        assert J.verify_chain(p)["intact"] is False

    def test_a_forged_prev_hash_is_caught_by_the_content_hash(self, tmp_path):
        """Rewriting prev_hash alone cannot hide a deletion: the hash was
        computed over the predecessor too."""
        p = self._three(tmp_path)
        recs = _lines(p)
        del recs[1]
        recs[1]["prev_hash"] = recs[0]["hash"]
        with open(p, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in recs)
        assert J.verify_chain(p)["intact"] is False


class TestLegacyAndTrim:
    def test_unhashed_entries_before_the_chain_are_legacy_not_failures(self, tmp_path):
        p = str(tmp_path / "j.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": "old-1"}) + "\n")
            fh.write(json.dumps({"ts": "old-2"}) + "\n")
        J.append_chained(p, {"ts": "new-1"}, max_bytes=10**6)
        J.append_chained(p, {"ts": "new-2"}, max_bytes=10**6)
        rep = J.verify_chain(p)
        assert rep["intact"] is True
        assert rep["legacy"] == 2 and rep["chained"] == 2
        assert rep["anchor_index"] == 2 and rep["anchor_ts"] == "new-1"

    def test_a_trimmed_file_re_anchors_and_still_verifies(self, tmp_path):
        p = str(tmp_path / "j.jsonl")
        for i in range(6):
            J.append_chained(p, {"n": i, "pad": "x" * 50}, max_bytes=600, keep_lines=2)
        recs = _lines(p)
        assert len(recs) == 2 and recs[-1]["n"] == 5
        rep = J.verify_chain(p)
        assert rep["intact"] is True and rep["chained"] == 2

    def test_the_byte_limit_bounds_the_file_even_when_the_line_cap_would_not(self, tmp_path):
        p = str(tmp_path / "j.jsonl")
        for i in range(200):
            J.append_chained(p, {"n": i, "pad": "x" * 40}, max_bytes=2000, keep_lines=2000)
        import os
        assert os.path.getsize(p) <= 2000 + 400   # the limit plus at most one fresh entry
        rep = J.verify_chain(p)
        assert rep["intact"] is True and 1 <= rep["chained"] < 200

    def test_describe_is_one_readable_line(self, tmp_path):
        p = str(tmp_path / "j.jsonl")
        J.append_chained(p, {"ts": "2026-09-03T14:00:00+00:00"}, max_bytes=10**6)
        text = J.describe(J.verify_chain(p), "Send journal")
        assert text.startswith("Send journal: 1 entries verified, SHA-256 linked since 2026-09-03T14:00:00")
        broken = J.describe({"intact": False, "first_break": (4, "x"), "entries": 5, "chained": 5,
                             "legacy": 0, "anchor_index": 0, "anchor_ts": ""}, "j")
        assert "CHAIN BROKEN at line 5" in broken


class TestTheLiveWritersUseTheChain:
    def test_notify_send_journal_entries_are_chained(self, tmp_path, monkeypatch):
        from src.core import notify
        monkeypatch.setattr(notify, "JOURNAL_PATH", str(tmp_path / "n.jsonl"))
        notify._journal({"ts": "t1", "title": "a", "message": "m", "delivered": True})
        notify._journal({"ts": "t2", "title": "b", "message": "m", "delivered": False})
        recs = notify.read_journal()
        assert all("hash" in r and "prev_hash" in r for r in recs)
        assert J.verify_chain(notify.JOURNAL_PATH)["chained"] == 2

    def test_regime_advisor_verdicts_are_chained(self, tmp_path, monkeypatch):
        from src.strategies import regime_advisor as ra
        monkeypatch.setattr(ra, "JOURNAL_PATH", str(tmp_path / "r.jsonl"))
        adv = ra.RegimeAdvisor("m", llm_call=lambda *a: '{"regime":"chop","confidence":0.8}',
                               journal=True, clock=lambda: 1000.0)
        bars = [{"timestamp": "2026-09-03T14:00:00+00:00", "open": 1, "high": 1, "low": 1,
                 "close": 1, "volume": 1}] * 12
        adv.refresh("QQQ", bars)
        adv.refresh("QQQ", bars)
        rep = J.verify_chain(ra.JOURNAL_PATH)
        assert rep["intact"] is True and rep["chained"] == 2
