"""Tests for the CouncilMetrics tracker (finance_advisor.py)."""

from __future__ import annotations

from src.core.finance_advisor import (
    CouncilDecision,
    AdvisorOpinion,
    CouncilMetrics,
)


def _make_decision(
    symbol: str = "JPM",
    approved: bool = True,
    votes_for: int = 3,
    votes_against: int = 1,
    model_verdicts: dict | None = None,
) -> CouncilDecision:
    opinions = []
    verdicts = model_verdicts or {
        "dell4-finance": "approve",
        "dell4-fino1-14b": "approve",
        "dell4-chat": "approve",
        "dell4-qwen38": "reject",
    }
    for model, verdict in verdicts.items():
        opinions.append(AdvisorOpinion(
            model=model,
            role=model,
            verdict=verdict,
            reasoning="test",
            responded=True,
        ))
    return CouncilDecision(
        action="buy", symbol=symbol, approved=approved,
        votes_for=votes_for, votes_against=votes_against,
        abstentions=0, opinions=opinions, summary="test",
    )


class TestCouncilMetrics:
    def test_empty_metrics(self):
        m = CouncilMetrics()
        assert m.total_decisions == 0
        assert m.approval_rate == 0.0
        assert m.model_agreement_rate() == {}

    def test_record_and_count(self):
        m = CouncilMetrics()
        m.record(_make_decision())
        m.record(_make_decision(approved=False, votes_for=1, votes_against=3))
        assert m.total_decisions == 2

    def test_approval_rate(self):
        m = CouncilMetrics()
        m.record(_make_decision(approved=True))
        m.record(_make_decision(approved=True))
        m.record(_make_decision(approved=False, votes_for=1, votes_against=3))
        assert abs(m.approval_rate - 2 / 3) < 0.01

    def test_model_agreement(self):
        m = CouncilMetrics()
        d1 = _make_decision(
            approved=True,
            model_verdicts={
                "dell4-finance": "approve",
                "dell4-chat": "approve",
                "dell4-qwen38": "reject",
            },
        )
        m.record(d1)
        agreement = m.model_agreement_rate()
        assert agreement["dell4-finance"] == 1.0
        assert agreement["dell4-chat"] == 1.0
        assert agreement["dell4-qwen38"] == 0.0

    def test_outcome_correlation(self):
        m = CouncilMetrics()
        m.record(_make_decision(approved=True), outcome_pnl=50.0)
        m.record(_make_decision(approved=True), outcome_pnl=-20.0)
        m.record(_make_decision(approved=False, votes_for=1, votes_against=3),
                 outcome_pnl=30.0)
        corr = m.outcome_correlation()
        assert corr["approved_profit"] == 1
        assert corr["approved_loss"] == 1
        assert corr["rejected_would_profit"] == 1

    def test_update_outcome(self):
        m = CouncilMetrics()
        m.record(_make_decision(symbol="AAPL"))
        m.update_outcome("AAPL", 75.0)
        corr = m.outcome_correlation()
        assert corr["approved_profit"] == 1

    def test_summary_readable(self):
        m = CouncilMetrics()
        m.record(_make_decision(), outcome_pnl=50.0)
        text = m.summary()
        assert "Council Metrics" in text
        assert "1 decisions" in text
        assert "Approval rate" in text
