"""Decision journal: stdout + JSONL, throttle, council votes."""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

from src.core.decision_log import record, record_throttled, reset_throttle
from src.core.finance_advisor import AdvisorOpinion, CouncilDecision
from src.strategies.vampire_engine import VampireConfig, VampireEngine
from src.strategies.sixfold_executor import SixfoldExecutor
from tests.test_sixfold_executor import _council_approve, _exec, COUNCIL_PATCH


class TestJournal:
    def test_record_writes_jsonl(self, tmp_path, caplog):
        path = tmp_path / "decisions.jsonl"
        reset_throttle()
        with patch.dict("os.environ", {"DECISION_LOG": str(path)}, clear=False):
            with caplog.at_level(logging.INFO, logger="decision"):
                record(
                    "vampire", "watching",
                    symbol="SPY",
                    thought="mid=500 delta=+0.01 < thresh=0.05",
                    decision="below_threshold",
                )
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["agent"] == "vampire"
        assert row["symbol"] == "SPY"
        assert row["decision"] == "below_threshold"
        assert "mid=500" in row["thought"]
        assert "[DECISION] vampire watching SPY" in caplog.text

    def test_throttle_drops_repeats(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        reset_throttle()
        with patch.dict("os.environ", {"DECISION_LOG": str(path)}, clear=False):
            record_throttled("k", 60, "vampire", "watching", thought="a", decision="idle")
            record_throttled("k", 60, "vampire", "watching", thought="b", decision="idle")
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        assert len(rows) == 1
        assert rows[0]["thought"] == "a"


class TestCouncilJournal:
    def test_log_decision_stores_full_votes(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        with patch.dict("os.environ", {"DECISION_LOG": str(path)}, clear=False):
            CouncilDecision(
                action="buy", symbol="JPM", approved=True,
                votes_for=2, votes_against=1, abstentions=0,
                opinions=[
                    AdvisorOpinion("dell4-finance", "Finance Specialist",
                                   "approve", "Cheap relative to book", True),
                    AdvisorOpinion("dell4-chat", "General Strategist",
                                   "reject", "Momentum faded", True),
                ],
                summary="Council approved: 2 for, 1 against, 0 abstain",
            ).log_decision()
        row = json.loads(path.read_text().strip().splitlines()[-1])
        assert row["agent"] == "council"
        assert row["decision"] == "approved"
        assert row["votes"][0]["reasoning"] == "Cheap relative to book"
        assert row["votes"][1]["verdict"] == "reject"


class TestVampireThoughts:
    def test_tick_records_below_threshold(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        reset_throttle()
        client = type("C", (), {"is_dry_run": True})()
        engine = VampireEngine(client, None, None, VampireConfig(symbol="SPY", tick_threshold=0.10))
        engine._is_market_hours = lambda: True
        with patch.dict("os.environ", {"DECISION_LOG": str(path)}, clear=False):
            engine.tick(100.01, vwap=100.0)
        assert engine.last_thought["decision"] == "below_threshold"
        row = json.loads(path.read_text().strip().splitlines()[-1])
        assert row["symbol"] == "SPY"
        assert row["decision"] == "below_threshold"


@patch(COUNCIL_PATCH, side_effect=_council_approve)
class TestSixfoldJournal:
    def test_skip_reason_is_journaled(self, _m, tmp_path):
        path = tmp_path / "decisions.jsonl"
        ex, client = _exec(["AAPL"])
        with patch.dict("os.environ", {"DECISION_LOG": str(path)}, clear=False):
            ex.run_cycle()
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        skips = [r for r in rows if r["agent"] == "sixfold" and r["event"] == "skip"]
        assert skips
        assert "another sleeve" in skips[0]["thought"]
        client.submit_order.assert_not_called()


class TestCountTradesToday:
    def test_counts_fill_events_for_today(self, tmp_path):
        from datetime import datetime, timezone

        from src.core.decision_log import count_trades_today

        path = tmp_path / "decisions.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        rows = [
            {"ts": f"{today}T15:00:00+00:00", "agent": "vampire", "event": "long_entry"},
            {"ts": f"{today}T15:00:01+00:00", "agent": "vampire", "event": "watching"},
            {"ts": "1999-01-01T00:00:00+00:00", "agent": "vampire", "event": "long_exit"},
            {"ts": f"{today}T15:00:02+00:00", "agent": "sixfold", "event": "order"},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        with patch.dict("os.environ", {"DECISION_LOG": str(path)}, clear=False):
            assert count_trades_today() == 2
