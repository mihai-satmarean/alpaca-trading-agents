"""Vampire hunt LLM veto and CSP council gate."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from src.core.finance_advisor import CouncilDecision, AdvisorOpinion
from src.core.hunt_advisor import HuntDecision, evaluate_hunt, filter_hunt_candidates
from src.core.options_chain import OptionCandidate
from src.strategies.csp import CSPOpportunity, CashSecuredPutStrategy
from src.strategies.vampire_symbol_picker import PickerConfig, SymbolMetrics
from tests.test_allocation_enforcement import _snapshot
from tests.test_vampire_symbol_picker import _mock_picker


def _reject(symbol="HOOD"):
    return HuntDecision(symbol, False, f"Hunt veto {symbol}", [
        {"model": "dell4-fino1-14b", "role": "Financial Reasoner",
         "verdict": "reject", "reasoning": "gap-and-go"},
        {"model": "dell4-finance", "role": "Finance Specialist",
         "verdict": "approve", "reasoning": "ok"},
    ])


def _keep(symbol="QQQ"):
    return HuntDecision(symbol, True, f"Hunt keep {symbol}", [
        {"model": "dell4-fino1-14b", "role": "Financial Reasoner",
         "verdict": "approve", "reasoning": "tight spread"},
        {"model": "dell4-finance", "role": "Finance Specialist",
         "verdict": "approve", "reasoning": "ok"},
    ])


class TestEvaluateHunt:
    @patch("src.core.hunt_advisor._safe_call", return_value="APPROVE. Tight book.")
    def test_both_approve_keeps_the_name(self, _m):
        d = evaluate_hunt("QQQ", {"atr_pct": "1.2%"})
        assert d.approved
        assert d.symbol == "QQQ"

    @patch(
        "src.core.hunt_advisor._safe_call",
        return_value="## Thinking\nREJECT the gap.\n\n## Answer\nAPPROVE. Tight book.",
    )
    def test_thinking_prefix_does_not_veto_the_answer(self, _m):
        d = evaluate_hunt("QQQ")
        assert d.approved

    @patch("src.core.hunt_advisor._safe_call", return_value="REJECT. Trend day.")
    def test_either_reject_is_a_veto(self, _m):
        d = evaluate_hunt("HOOD")
        assert not d.approved

    @patch("src.core.hunt_advisor._safe_call", return_value=None)
    def test_dead_models_leave_the_quant_rank(self, _m):
        d = evaluate_hunt("IWM")
        assert d.approved

    @patch.dict("os.environ", {"VAMPIRE_HUNT_LLM": "0"})
    def test_env_off_skips_models(self):
        d = evaluate_hunt("SPY")
        assert d.approved
        assert d.votes == []


class TestFilterHuntCandidates:
    def test_disabled_returns_top_n(self):
        ranked = [
            SymbolMetrics(symbol="A", score=0.1),
            SymbolMetrics(symbol="B", score=0.2),
            SymbolMetrics(symbol="C", score=0.3),
        ]
        out = filter_hunt_candidates(ranked, count=2, enabled=False)
        assert [m.symbol for m in out] == ["A", "B"]

    @patch("src.core.hunt_advisor.evaluate_hunt", side_effect=[
        _reject("A"), _keep("B"), _keep("C"),
    ])
    def test_veto_skips_to_the_next_rank(self, _m):
        ranked = [
            SymbolMetrics(symbol="A", score=0.1),
            SymbolMetrics(symbol="B", score=0.2),
            SymbolMetrics(symbol="C", score=0.3),
        ]
        out = filter_hunt_candidates(ranked, count=2, enabled=True)
        assert [m.symbol for m in out] == ["B", "C"]

    @patch("src.core.hunt_advisor.evaluate_hunt", side_effect=[
        _reject("HOOD"), _reject("SPY"), _reject("QQQ"),
    ])
    def test_mass_veto_keeps_the_live_edge_name(self, _m):
        ranked = [
            SymbolMetrics(symbol="HOOD", score=0.1),
            SymbolMetrics(symbol="SPY", score=0.2),
            SymbolMetrics(symbol="QQQ", score=0.3),
        ]
        out = filter_hunt_candidates(
            ranked, count=2, enabled=True, keep_symbols={"QQQ"},
        )
        assert [m.symbol for m in out] == ["QQQ"]


class TestPickerHuntFilter:
    @patch("src.core.hunt_advisor.evaluate_hunt", side_effect=[
        _reject("IWM"), _keep("AMD"),
    ])
    def test_replacements_skip_vetoed_names(self, _m):
        picker = _mock_picker(cfg=PickerConfig(target_count=3, llm_hunt=True))
        picker._all_metrics = {
            "IWM": SymbolMetrics(symbol="IWM", score=0.1, shortable=True),
            "AMD": SymbolMetrics(symbol="AMD", score=0.5, shortable=True),
        }
        replacements = picker.find_replacements(["QQQ"], count=1)
        assert replacements == ["AMD"]


def _csp_strategy():
    client, chain, data, tracker = (MagicMock() for _ in range(4))
    tracker.get_snapshot.return_value = _snapshot()
    allocator = MagicMock()
    allocator.get_budget.return_value = MagicMock(options_available=80_000.0)
    allocator.can_allocate_options.return_value = True
    breaker = MagicMock()
    breaker.can_trade.return_value = True
    strat = CashSecuredPutStrategy(
        client, chain, data, tracker, allocator=allocator, breaker=breaker,
    )
    candidate = OptionCandidate(
        symbol="MARA260911P00010000",
        underlying="MARA",
        contract_type="put",
        strike_price=10.0,
        expiration=date.today() + timedelta(days=10),
        open_interest=500,
        premium_estimate=None,
        days_to_expiry=10,
    )
    opp = CSPOpportunity(
        candidate=candidate,
        current_price=12.0,
        cash_required=1000.0,
        premium_pct=0.03,
        annualized_return=0.4,
        score=5.0,
    )
    opp.bid = 0.32
    strat.scan = lambda symbols=None: [opp]
    return strat, client


class TestCspCouncilGate:
    def test_council_reject_blocks_the_order(self):
        strat, client = _csp_strategy()
        decision = CouncilDecision(
            action="sell_put", symbol="MARA", approved=False,
            votes_for=0, votes_against=3, abstentions=1,
            opinions=[AdvisorOpinion("dell4-finance", "Finance Specialist",
                                     "reject", "earnings risk", True)],
            summary="Council rejected",
        )
        with patch("src.strategies.csp.evaluate_csp", return_value=decision):
            executed = strat.execute_best(max_trades=1)
        assert executed == []
        client.submit_order.assert_not_called()
        assert any("Council rejected" in r["reason"] for r in strat.last_rejections)

    def test_council_approve_places_the_order(self):
        strat, client = _csp_strategy()
        decision = CouncilDecision(
            action="sell_put", symbol="MARA", approved=True,
            votes_for=3, votes_against=0, abstentions=1,
            opinions=[],
            summary="Council approved",
        )
        with patch("src.strategies.csp.evaluate_csp", return_value=decision):
            executed = strat.execute_best(max_trades=1)
        assert len(executed) == 1
        client.submit_order.assert_called_once()
        assert executed[0]["council"] == "Council approved"
