"""Tests for the Advisory Council (finance_advisor.py).

All LLM calls are mocked -- these tests verify consensus logic,
parallel execution, and graceful degradation.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from src.core.finance_advisor import (
    CouncilDecision,
    AdvisorOpinion,
    _parse_verdict,
    _run_council,
    evaluate_equity_buy,
    evaluate_csp,
    COUNCIL_MODELS,
    CONSENSUS_THRESHOLD,
)


class TestParseVerdict:
    def test_approve_at_start(self):
        assert _parse_verdict("APPROVE. Strong fundamentals.", "APPROVE", "REJECT") == "approve"

    def test_reject_at_start(self):
        assert _parse_verdict("REJECT. Overvalued stock.", "APPROVE", "REJECT") == "reject"

    def test_approve_within_first_60_chars(self):
        assert _parse_verdict("Based on analysis, APPROVE this trade.", "APPROVE", "REJECT") == "approve"

    def test_reject_within_first_60_chars(self):
        assert _parse_verdict("Given the risks, I REJECT this proposal.", "APPROVE", "REJECT") == "reject"

    def test_ambiguous_defaults_to_approve(self):
        assert _parse_verdict("The stock looks interesting but uncertain.", "APPROVE", "REJECT") == "approve"

    def test_case_insensitive(self):
        assert _parse_verdict("approve. Looks good.", "APPROVE", "REJECT") == "approve"


class TestCouncilDecision:
    def test_approved_when_unanimous(self):
        d = CouncilDecision(
            action="buy", symbol="JPM", approved=True,
            votes_for=3, votes_against=0, abstentions=0,
        )
        assert d.approved

    def test_approved_with_2_of_3(self):
        d = CouncilDecision(
            action="buy", symbol="JPM", approved=True,
            votes_for=2, votes_against=1, abstentions=0,
        )
        assert d.approved

    def test_rejected_with_2_against(self):
        d = CouncilDecision(
            action="buy", symbol="JPM", approved=False,
            votes_for=1, votes_against=2, abstentions=0,
        )
        assert not d.approved


@patch("src.core.finance_advisor._llm_call")
class TestRunCouncil:
    def test_unanimous_approval(self, mock_llm):
        mock_llm.return_value = "APPROVE. Strong fundamentals support this trade."
        decision = _run_council(
            "buy", "JPM",
            {"Finance Specialist": "sys", "General Strategist": "sys", "Risk Analyst": "sys"},
            "Buy JPM",
        )
        assert decision.approved
        assert decision.votes_for == 3
        assert decision.votes_against == 0
        assert len(decision.opinions) == 3

    def test_unanimous_rejection(self, mock_llm):
        mock_llm.return_value = "REJECT. Overvalued and risky."
        decision = _run_council(
            "buy", "TSLA",
            {"Finance Specialist": "sys", "General Strategist": "sys", "Risk Analyst": "sys"},
            "Buy TSLA",
        )
        assert not decision.approved
        assert decision.votes_against == 3

    def test_split_decision_2_1_passes(self, mock_llm):
        mock_llm.side_effect = [
            "APPROVE. Good value.",
            "APPROVE. Market timing is right.",
            "REJECT. Too much downside risk.",
        ]
        decision = _run_council(
            "buy", "V",
            {"Finance Specialist": "sys", "General Strategist": "sys", "Risk Analyst": "sys"},
            "Buy V",
        )
        assert decision.approved
        assert decision.votes_for == 2
        assert decision.votes_against == 1

    def test_split_decision_1_2_fails(self, mock_llm):
        mock_llm.side_effect = [
            "APPROVE. Decent fundamentals.",
            "REJECT. Sector is weak.",
            "REJECT. Concentration risk.",
        ]
        decision = _run_council(
            "buy", "KO",
            {"Finance Specialist": "sys", "General Strategist": "sys", "Risk Analyst": "sys"},
            "Buy KO",
        )
        assert not decision.approved
        assert decision.votes_for == 1
        assert decision.votes_against == 2

    def test_model_failure_counts_as_abstention(self, mock_llm):
        mock_llm.side_effect = [
            "APPROVE. Solid pick.",
            Exception("model timeout"),
            "APPROVE. Low risk.",
        ]
        decision = _run_council(
            "buy", "HD",
            {"Finance Specialist": "sys", "General Strategist": "sys", "Risk Analyst": "sys"},
            "Buy HD",
        )
        assert decision.approved
        assert decision.votes_for == 2
        assert decision.abstentions == 1

    def test_all_models_fail_proceeds_on_deterministic(self, mock_llm):
        mock_llm.side_effect = Exception("all down")
        decision = _run_council(
            "buy", "PG",
            {"Finance Specialist": "sys", "General Strategist": "sys", "Risk Analyst": "sys"},
            "Buy PG",
        )
        assert decision.approved  # insufficient quorum -> proceed
        assert decision.abstentions == 3
        assert "quorum" in decision.summary.lower()


@patch("src.core.finance_advisor._llm_call")
class TestEvaluateEquityBuy:
    def test_returns_council_decision(self, mock_llm):
        mock_llm.return_value = "APPROVE. Good value proposition."
        result = evaluate_equity_buy("AAPL", 75.0)
        assert isinstance(result, CouncilDecision)
        assert result.symbol == "AAPL"
        assert result.action == "buy"

    def test_passes_fundamentals_to_prompt(self, mock_llm):
        mock_llm.return_value = "APPROVE. Looks good."
        evaluate_equity_buy("MSFT", 80.0, {"PE": 25, "Revenue Growth": "12%"})
        prompts_used = [call[0][2] for call in mock_llm.call_args_list]
        assert any("PE" in p for p in prompts_used)


@patch("src.core.finance_advisor._llm_call")
class TestEvaluateCSP:
    def test_returns_council_decision(self, mock_llm):
        mock_llm.return_value = "APPROVE. Premium is adequate."
        result = evaluate_csp("SOFI", 8.0, 30, 0.50, 9.0)
        assert isinstance(result, CouncilDecision)
        assert result.action == "sell_put"
        assert result.symbol == "SOFI"
