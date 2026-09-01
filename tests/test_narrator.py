"""The narrator explains decisions. It must never be able to make one.

This is the project's AI surface, and the constraint on it is the one from
CLAUDE.md: LLM only where ambiguity exists, math everywhere else, risk logic
never inside the LLM. So the narrator is given facts after the fact and has no
handle on anything that can trade. These tests are what make that structural
rather than a promise in a docstring.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.agents.narrator import (
    NarrationRequest,
    build_prompt,
    narrate,
    summarise_session,
)


def _req(**kw) -> NarrationRequest:
    base = dict(
        equity=100_000.0,
        cash=80_000.0,
        daily_pnl=-125.50,
        sleeves={"CSP (options)": {"committed": 12_400.0, "budget": 20_000.0, "unrealized": 88.0,
                                   "positions": ["CLF261016P00011000"]},
                 "Vampire (scalper)": {"committed": 5_900.0, "budget": 20_000.0, "unrealized": -213.5,
                                       "positions": ["SPY"]}},
        actions=[{"strategy": "csp", "symbol": "CLF261016P00011000", "side": "sell_to_open",
                  "credit": 41.0, "collateral": 1_100.0, "annualized": 0.31,
                  "reason": "1.09% on capital, 31% annualised, 640 OI"}],
        rejections=[{"symbol": "NIO261016P00004000", "reason": "return on capital 0.22% below the 0.5% floor"}],
    )
    base.update(kw)
    return NarrationRequest(**base)


class TestPromptCarriesTheFacts:
    def test_prompt_includes_pnl_and_sleeves(self):
        p = build_prompt(_req())
        assert "100,000" in p or "100000" in p
        assert "CSP" in p and "Vampire" in p

    def test_prompt_includes_the_actions_taken(self):
        assert "CLF261016P00011000" in build_prompt(_req())

    def test_prompt_includes_why_things_were_rejected(self):
        """Rejections are the interesting half: they show the gates working."""
        assert "NIO261016P00004000" in build_prompt(_req())

    def test_prompt_forbids_recommending_trades(self):
        p = build_prompt(_req()).lower()
        assert "do not" in p or "never" in p
        assert "recommend" in p or "advice" in p or "decide" in p

    def test_empty_session_still_builds_a_prompt(self):
        assert build_prompt(_req(actions=[], rejections=[]))


class TestNarratorCannotTrade:
    """The property that matters. If these ever fail, the narrator has grown a
    capability it must not have."""

    def test_request_carries_no_client_or_strategy_handle(self):
        r = _req()
        for field in vars(r).values():
            assert not hasattr(field, "submit_order")
            assert not hasattr(field, "trading")

    def test_module_imports_nothing_that_can_place_an_order(self):
        import src.agents.narrator as n

        source = open(n.__file__).read()
        for forbidden in ("submit_order", "place_order", "AlpacaClient",
                          "OrderSide", "LimitOrderRequest", "MarketOrderRequest"):
            assert forbidden not in source, f"narrator must not reference {forbidden}"

    def test_narration_failure_returns_none_and_does_not_raise(self):
        with patch("src.agents.narrator._chat", side_effect=RuntimeError("cluster down")):
            assert narrate(_req()) is None

    def test_timeout_returns_none(self):
        with patch("src.agents.narrator._chat", side_effect=TimeoutError):
            assert narrate(_req()) is None

    def test_empty_model_reply_returns_none(self):
        with patch("src.agents.narrator._chat", return_value="   "):
            assert narrate(_req()) is None


class TestNarrationOutput:
    def test_returns_the_model_text(self):
        with patch("src.agents.narrator._chat", return_value="Sold one CLF put for $41."):
            assert narrate(_req()) == "Sold one CLF put for $41."

    def test_output_is_truncated_to_a_notification_sized_body(self):
        with patch("src.agents.narrator._chat", return_value="x" * 5000):
            out = narrate(_req())
            assert out is not None and len(out) <= 2000

    def test_surrounding_whitespace_is_stripped(self):
        with patch("src.agents.narrator._chat", return_value="\n\n  done  \n"):
            assert narrate(_req()) == "done"


class TestSessionSummary:
    def test_summary_reports_when_nothing_traded(self):
        with patch("src.agents.narrator._chat", return_value="No trades: no contract cleared the premium floor."):
            out = summarise_session(_req(actions=[]))
            assert out and "No trades" in out

    def test_summary_degrades_to_none_without_a_model(self):
        with patch("src.agents.narrator._chat", side_effect=ConnectionError):
            assert summarise_session(_req()) is None


class TestTokenBudget:
    """A reasoning model spends tokens on hidden reasoning_content before it
    answers. At 700 max_tokens every reasoning-capable model tested against
    the real cluster returned content: null with finish_reason "length" -
    not an error, so nothing upstream ever caught it. This cluster is
    self-hosted with no per-token cost, so a small hardcoded budget was the
    wrong economy. Confirmed live: 4000 tokens is enough for real content.
    """

    def _sent_body(self, monkeypatch, env=None):
        import urllib.request

        import src.agents.narrator as n

        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "ok"}}]}
                ).encode()

        def fake_urlopen(req, timeout=None, context=None):
            captured["body"] = json.loads(req.data)
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        n._chat("system", "user")
        return captured["body"]

    def test_default_budget_is_generous_not_700(self, monkeypatch):
        body = self._sent_body(monkeypatch)
        assert body["max_tokens"] >= 2000, (
            "700 was consistently exhausted by hidden reasoning tokens alone; "
            "this must not regress back to a budget that size"
        )

    def test_the_budget_is_tunable_without_a_code_change(self, monkeypatch):
        body = self._sent_body(monkeypatch, env={"NARRATOR_MAX_TOKENS": "9000"})
        assert body["max_tokens"] == 9000
