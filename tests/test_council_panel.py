"""Mihai's AI Council panel, ported read-only.

The panel can ask the advisory models for an allocation and, on his branch,
write the result into config/strategies.yml from the browser. On a public,
token-shared dashboard that write path must fail closed.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.argv = ["streamlit", "run"]
from dashboard import council as C  # noqa: E402

GOOD = {"sixfold_pct": 0.60, "options_pct": 0.15, "vampire_pct": 0.0,
        "pendulum_pct": 0.15, "reserve_pct": 0.10}


class TestValidation:
    def test_the_live_split_validates(self):
        ok, msg = C._validate_allocation(GOOD)
        assert ok, msg

    def test_reserve_floor_and_total_are_enforced(self):
        bad = dict(GOOD, reserve_pct=0.02, sixfold_pct=0.68)
        assert not C._validate_allocation(bad)[0]
        assert not C._validate_allocation(dict(GOOD, sixfold_pct=0.90))[0]

    def test_a_near_miss_total_is_normalised_to_one(self):
        out = C._normalize_allocation(dict(GOOD, sixfold_pct=0.65))   # sums to 1.05
        assert abs(sum(out.values()) - 1.0) < 0.011


class TestApplyIsFailClosed:
    def test_apply_refuses_without_the_operator_flag(self, tmp_path, monkeypatch):
        """A public dashboard must not be able to rewrite the live config."""
        cfg = tmp_path / "strategies.yml"; cfg.write_text("allocation:\n  sixfold_pct: 0.60\n")
        monkeypatch.setattr(C, "STRATEGIES_PATH", cfg)
        monkeypatch.setattr(C, "ALLOW_REALLOCATION", False)
        ok, msg = C._apply_allocation(GOOD)
        assert not ok and "disabled" in msg
        assert cfg.read_text() == "allocation:\n  sixfold_pct: 0.60\n", "file must be untouched"
        assert not list(tmp_path.glob("*.bak.*")), "no backup means no write was attempted"

    def test_the_flag_is_off_unless_explicitly_one(self):
        """Only the literal "1" enables writes; "true"/"yes" must not."""
        for v in ("", "0", "true", "yes", " 1x"):
            assert (v.strip() == "1") is False
        assert (" 1 ".strip() == "1") is True


class TestEndpointCompatibility:
    def test_a_base_url_with_v1_is_not_doubled(self, monkeypatch):
        seen = {}
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"choices":[{"message":{"content":"ok"}}]}'
        def fake_urlopen(req, timeout=0, context=None):
            seen["url"] = req.full_url; return R()
        monkeypatch.setattr(C.urllib.request, "urlopen", fake_urlopen)
        C._query_model({"id": "m", "label": "m", "base_url": "http://h:4000/v1", "max_tokens": 10}, "ctx")
        assert seen["url"] == "http://h:4000/v1/chat/completions"

    def test_the_default_models_are_the_engines_council(self):
        ids = {m["id"] for m in C.COUNCIL_MODELS}
        assert ids == {"dell4-finance", "dell4-chat", "dell4-qwen38"}


class TestContextBuildsOnMain:
    def test_portfolio_context_does_not_need_the_decision_log_module(self):
        client = MagicMock(); client.get_account.return_value = MagicMock(equity="99000", cash="40000", buying_power="200000")
        client.get_positions.return_value = []
        allocator = MagicMock()
        allocator.config = MagicMock(sixfold_pct=0.6, options_pct=0.15, vampire_pct=0.0, pendulum_pct=0.15, reserve_pct=0.1)
        allocator.get_budget.return_value = MagicMock(sixfold_budget=59400.0, options_budget=14850.0, options_used=20000.0,
            vampire_budget=0.0, vampire_used=0.0, pendulum_budget=14850.0, pendulum_used=0.0, reserve_target=9900.0, unattributed_used=0.0)
        tracker = MagicMock(); tracker.get_snapshot.return_value = MagicMock(daily_pnl=500.0, positions={}, equity=99000.0)
        ctx = C._build_portfolio_context(client, allocator, tracker)
        assert "SIXFOLD" in ctx and "Pendulum" in ctx


class TestReasoningModelsWithNoContent:
    """Live crash on 2026-09-02: one advisor returned content None (its text
    was in reasoning_content) and _parse_yaml_block raised TypeError, taking
    the whole dashboard page down with a traceback."""

    def test_none_content_is_parsed_as_no_proposal_not_a_crash(self):
        assert C._parse_yaml_block(None) is None
        assert C._parse_yaml_block("") is None

    def test_reasoning_content_is_used_when_content_is_none(self, monkeypatch):
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"choices":[{"message":{"content":null,"reasoning_content":"```yaml\\nsixfold_pct: 0.6\\n```"}}]}'
        monkeypatch.setattr(C.urllib.request, "urlopen", lambda *a, **k: R())
        r = C._query_model({"id": "m", "label": "m", "base_url": "http://h", "max_tokens": 10}, "ctx")
        assert "sixfold_pct" in (r.get("content") or "")

    def test_the_token_budget_is_large_enough_for_a_thinking_model(self):
        assert all(m["max_tokens"] >= 4096 for m in C.COUNCIL_MODELS)


class TestConsultLatencyBudget:
    def test_one_attempt_with_a_timeout_that_fits_a_thinking_model(self):
        assert C.MAX_RETRIES == 1
        assert C.MODEL_TIMEOUT_S >= 240
