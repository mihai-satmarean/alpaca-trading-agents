"""Shared agent snapshot used by the Streamlit cockpit."""

from __future__ import annotations

import json
from unittest.mock import patch

from src.core.agent_status import read_snapshot, write_snapshot
from src.core.decision_log import recent
from src.core.finance_advisor import AdvisorOpinion, CouncilDecision


class TestAgentStatus:
    def test_write_then_read_roundtrip(self, tmp_path):
        path = tmp_path / "agent-status.json"
        payload = {
            "environment": "staging",
            "dry_run": True,
            "vampire": {"SPY": {"state": "watching", "net_position": 0}},
        }
        with patch.dict("os.environ", {"AGENT_STATUS_PATH": str(path)}, clear=False):
            written = write_snapshot(payload)
            assert written == path
            got = read_snapshot()
        assert got["environment"] == "staging"
        assert got["dry_run"] is True
        assert got["vampire"]["SPY"]["state"] == "watching"
        assert "ts" in got

    def test_atomic_replace_leaves_valid_json(self, tmp_path):
        path = tmp_path / "agent-status.json"
        with patch.dict("os.environ", {"AGENT_STATUS_PATH": str(path)}, clear=False):
            write_snapshot({"n": 1})
            write_snapshot({"n": 2})
            got = read_snapshot()
        assert got["n"] == 2
        json.loads(path.read_text())

    def test_missing_file_is_empty_dict(self, tmp_path):
        path = tmp_path / "missing.json"
        with patch.dict("os.environ", {"AGENT_STATUS_PATH": str(path)}, clear=False):
            assert read_snapshot() == {}


class TestRecentJournal:
    def test_recent_filters_by_agent(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        rows = [
            {"agent": "vampire", "event": "watching", "thought": "a", "decision": "idle"},
            {"agent": "council", "event": "verdict", "thought": "b", "decision": "approved"},
            {"agent": "vampire", "event": "watching", "thought": "c", "decision": "idle"},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        with patch.dict("os.environ", {"DECISION_LOG": str(path)}, clear=False):
            vamp = recent(limit=10, agent="vampire")
            council = recent(limit=10, agent="council")
        assert [r["thought"] for r in vamp] == ["a", "c"]
        assert [r["thought"] for r in council] == ["b"]

    def test_recent_limit(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        path.write_text(
            "".join(
                json.dumps({"agent": "observer", "event": "heartbeat", "n": i}) + "\n"
                for i in range(10)
            ),
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"DECISION_LOG": str(path)}, clear=False):
            rows = recent(limit=3)
        assert [r["n"] for r in rows] == [7, 8, 9]


class TestLastCouncil:
    def test_log_decision_updates_module_state(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        with patch.dict("os.environ", {"DECISION_LOG": str(path)}, clear=False):
            CouncilDecision(
                action="buy", symbol="JPM", approved=True,
                votes_for=2, votes_against=1, abstentions=0,
                opinions=[
                    AdvisorOpinion("dell4-finance", "Finance Specialist",
                                   "approve", "Cheap relative to book", True),
                ],
                summary="Council approved",
            ).log_decision()
        from src.core import finance_advisor
        assert finance_advisor.last_council is not None
        assert finance_advisor.last_council["symbol"] == "JPM"
        assert finance_advisor.last_council["votes"][0]["verdict"] == "approve"


class TestCockpitCatalog:
    def test_live_agent_keys(self):
        import sys
        from pathlib import Path

        dash = Path(__file__).resolve().parents[1] / "dashboard"
        sys.path.insert(0, str(dash))
        from cockpit import AGENTS

        keys = {k for k, _, _ in AGENTS}
        for needed in (
            "vampire", "vampire_picker", "sixfold_analyst", "sixfold",
            "council", "options", "risk", "coordinator", "observer",
        ):
            assert needed in keys


class TestPickerStatus:
    def test_picker_status_shape(self):
        from src.agents.vampire import VampireAgent

        assert hasattr(VampireAgent, "picker_status")
