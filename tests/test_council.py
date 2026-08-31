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

    def test_ambiguous_is_an_abstention_not_an_approval(self):
        """Changed in review. An unparseable answer counted as approve
        neutralises the gate while logging a vote that was never cast;
        abstaining reduces quorum, which falls back to the deterministic
        signal honestly."""
        assert _parse_verdict("The stock looks interesting but uncertain.", "APPROVE", "REJECT") == "abstain"

    def test_an_explicit_negative_is_never_an_approval(self):
        """'NOT APPROVE' contains APPROVE, and approve-first substring parsing
        inverted it into a yes vote."""
        for text in ("I would NOT APPROVE this trade given the leverage.",
                     "I cannot approve this purchase.",
                     "Do not approve: the balance sheet is deteriorating."):
            assert _parse_verdict(text, "APPROVE", "REJECT") != "approve"

    def test_reject_wins_the_substring_phase(self):
        """A sentence carrying both words is a rejection: errors in the reject
        direction only skip one buy, the cheap direction for a veto gate."""
        assert _parse_verdict("Hard to approve; overall I REJECT it.", "APPROVE", "REJECT") == "reject"

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



class TestWallTimeoutIsContained:
    """An advisor overrunning the wall must cost an abstention, not the cycle."""

    def test_timeout_backfills_abstentions_and_falls_back(self):
        from unittest.mock import patch
        from src.core import finance_advisor as fa

        with patch.object(fa, "as_completed", side_effect=TimeoutError):
            d = fa._run_council("buy", "JPM", {"default": "x"}, "prompt")
        assert d.approved, "quorum 0 must fall back to the deterministic signal"
        assert d.abstentions == 3
        assert all(not o.responded for o in d.opinions)
        assert "quorum" in d.summary.lower() or "deterministic" in d.summary.lower()


class TestExecutorPassesTheRealScore:
    def test_council_receives_composite_score_not_zero(self):
        from unittest.mock import MagicMock, patch
        from src.strategies.sixfold_executor import SixfoldExecutor

        client, data, tracker, breaker, allocator, analyst = (MagicMock() for _ in range(6))
        data.get_latest_quote.return_value = MagicMock(mid=200.0)
        tracker.get_snapshot.return_value = MagicMock(equity=100_000.0, positions={})
        allocator.get_budget.return_value = MagicMock(sixfold_budget=50_000.0)
        breaker.check.return_value = True
        breaker.can_trade.return_value = True
        breaker.limits = MagicMock(max_single_trade_pct=0.05)
        analyst.get_buy_candidates.return_value = ["JPM"]
        analyst.scores = {"JPM": MagicMock(composite_score=78.5)}

        ex = SixfoldExecutor(client, data, tracker, breaker, allocator, analyst,
                             excluded=set())
        with patch("src.strategies.sixfold_executor.evaluate_equity_buy") as council:
            council.return_value = MagicMock(approved=True, opinions=[],
                                             summary="approved")
            ex.run_cycle()
        council.assert_called_once()
        assert council.call_args[0] == ("JPM", 78.5), \
            "the dict-style read passed 0.0 for every real SixfoldScore"
