"""Append-only JSONL journals with a SHA-256 hash chain.

Every entry carries ``prev_hash`` (the previous entry's ``hash``, or "" at
the head) and ``hash`` = sha256(prev_hash + canonical JSON of the entry
without those two fields). Editing an entry changes its hash; deleting or
reordering one breaks the next entry's ``prev_hash`` link. Verification
therefore proves that the retained history is unedited and unbroken, which
is the claim a skeptical reader can check without trusting us.

What it does not prove: that nothing was ever trimmed. The journals keep
their tail when they grow past a size limit, and the chain re-anchors at the
first retained line. Entries written before the chain existed carry no hash
and are reported as legacy rather than counted as verified.
"""

from __future__ import annotations

import hashlib
import json
import os

HASH_FIELDS = ("prev_hash", "hash")


def canonical(entry: dict) -> str:
    """The bytes that get hashed: sorted keys, no whitespace, hash fields out."""
    body = {k: v for k, v in entry.items() if k not in HASH_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(prev_hash: str, entry: dict) -> str:
    return hashlib.sha256((prev_hash + canonical(entry)).encode("utf-8")).hexdigest()


def _last_hash(path: str) -> str:
    """The hash of the last well-formed entry on file, or "" for a fresh chain."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for ln in reversed(tail.splitlines()):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        return str(rec.get("hash") or "")
    return ""


def append_chained(path: str, entry: dict, max_bytes: int, keep_lines: int = 2000) -> dict:
    """Append ``entry`` linked to the previous one. Returns what was written.

    Trims by rewriting the tail rather than rotating, because the readers
    open a fixed path and a rotation would silently hide recent history.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    prev = _last_hash(path)
    rec = {k: v for k, v in entry.items() if k not in HASH_FIELDS}
    rec["prev_hash"] = prev
    rec["hash"] = entry_hash(prev, rec)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if os.path.getsize(path) > max_bytes:
        with open(path, "r", encoding="utf-8") as fh:
            keep = fh.readlines()[-keep_lines:]
        # The line cap alone does not bound the file: hashed entries are
        # larger than the ones the cap was sized for. Keep the longest
        # suffix that fits the byte limit, never fewer than one line.
        while len(keep) > 1 and sum(len(ln.encode("utf-8")) for ln in keep) > max_bytes:
            keep.pop(0)
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
    return rec


def verify_chain(path: str) -> dict:
    """Walk the file and check every hashed entry against its content and
    its predecessor. ``intact`` is None when there is no file to verify."""
    report = {"entries": 0, "chained": 0, "legacy": 0, "intact": True,
              "first_break": None, "anchor_index": None, "anchor_ts": None}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        report["intact"] = None
        return report

    def broken(i: int, why: str) -> None:
        if report["intact"]:
            report["first_break"] = (i, why)
        report["intact"] = False

    prev: str | None = None
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        report["entries"] += 1
        try:
            rec = json.loads(ln)
        except ValueError:
            broken(i, "unparseable line")
            continue
        if "hash" not in rec:
            report["legacy"] += 1
            continue
        report["chained"] += 1
        if report["anchor_index"] is None:
            report["anchor_index"] = i
            report["anchor_ts"] = rec.get("ts") or rec.get("at")
        if rec["hash"] != entry_hash(str(rec.get("prev_hash", "")), rec):
            broken(i, "entry hash does not match its content")
        elif prev is not None and rec.get("prev_hash") != prev:
            broken(i, "prev_hash does not link to the previous entry")
        prev = rec["hash"]
    return report


def describe(report: dict, name: str) -> str:
    """One line a person can read."""
    if report["intact"] is None:
        return f"{name}: no journal on file"
    if not report["intact"]:
        i, why = report["first_break"]
        return f"{name}: CHAIN BROKEN at line {i + 1}: {why}"
    if report["chained"] == 0:
        return f"{name}: {report['entries']} entries, none chained yet"
    legacy = f", {report['legacy']} earlier entries predate the chain" if report["legacy"] else ""
    return (f"{name}: {report['chained']} entries verified, SHA-256 linked since "
            f"{str(report['anchor_ts'])[:19]}{legacy}")
