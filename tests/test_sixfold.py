"""Tests for the SIXFOLD equity analysis framework.

Uses mock financial data to verify each lens independently
and the composite scoring logic.
"""

from __future__ import annotations

import pytest

from src.core.financial_data import (
    FundamentalData,
    IncomeStatement,
    BalanceSheet,
    CashFlow,
    KeyStats,
    InsiderTransaction,
)
from src.strategies.sixfold_engine import (
    SixfoldEngine,
    SixfoldScore,
    ConfidenceTier,
    LensResult,
)


# ---------------------------------------------------------------------------
# Fixtures: mock company archetypes
# ---------------------------------------------------------------------------

def _make_elite_company() -> FundamentalData:
    """A company that should score 70+: high margins, strong ROIC, undervalued."""
    return FundamentalData(
        symbol="ELITE",
        sector="Technology",
        industry="Software",
        income_statements=[IncomeStatement(
            revenue=50_000_000_000,
            cost_of_revenue=15_000_000_000,
            gross_profit=35_000_000_000,
            sga_expense=10_000_000_000,
            operating_income=20_000_000_000,
            net_income=15_000_000_000,
            ebitda=22_000_000_000,
            interest_expense=500_000_000,
            tax_provision=4_000_000_000,
            basic_eps=10.0,
        )],
        balance_sheets=[BalanceSheet(
            total_assets=100_000_000_000,
            total_liabilities=30_000_000_000,
            stockholders_equity=70_000_000_000,
            total_debt=10_000_000_000,
            long_term_debt=8_000_000_000,
            cash_and_equivalents=20_000_000_000,
            current_assets=40_000_000_000,
            current_liabilities=15_000_000_000,
            goodwill=5_000_000_000,
            intangible_assets=3_000_000_000,
            net_ppe=15_000_000_000,
            total_capitalization=80_000_000_000,
        )],
        cash_flows=[CashFlow(
            operating_cash_flow=18_000_000_000,
            capital_expenditures=5_000_000_000,
            free_cash_flow=13_000_000_000,
            repurchase_of_stock=8_000_000_000,
            issuance_of_stock=0,
            dividends_paid=3_000_000_000,
        )],
        stats=KeyStats(
            market_cap=500_000_000_000,
            enterprise_value=490_000_000_000,
            trailing_pe=18.0,
            forward_pe=16.0,
            peg_ratio=1.2,
            price_to_sales=10.0,
            price_to_book=7.0,
            beta=1.1,
            dividend_yield=0.01,
            payout_ratio=0.20,
            earnings_growth=0.15,
            revenue_growth=0.12,
            profit_margins=0.30,
            shares_outstanding=1_500_000_000,
            current_price=333.0,
        ),
        eps_history=[7.0, 8.0, 9.0, 10.0],
        annual_returns=[0.15, 0.20, 0.18, 0.25, 0.12, 0.30, 0.22, 0.16, 0.19, 0.28],
        listing_years=15.0,
        cagr=0.20,
        insider_transactions=[
            InsiderTransaction(
                holder="CEO", transaction_type="Purchase", shares=10000, value=3_000_000, date="2026-07-15"
            ),
            InsiderTransaction(
                holder="CFO", transaction_type="Purchase", shares=5000, value=1_500_000, date="2026-06-20"
            ),
        ],
    )


def _make_poor_company() -> FundamentalData:
    """A company that should score <40: thin margins, negative ROIC spread."""
    return FundamentalData(
        symbol="POOR",
        sector="Consumer Cyclical",
        industry="Retail",
        income_statements=[IncomeStatement(
            revenue=10_000_000_000,
            cost_of_revenue=7_500_000_000,
            gross_profit=2_500_000_000,
            sga_expense=2_000_000_000,
            operating_income=300_000_000,
            net_income=100_000_000,
            ebitda=800_000_000,
            interest_expense=400_000_000,
            tax_provision=50_000_000,
            basic_eps=0.50,
        )],
        balance_sheets=[BalanceSheet(
            total_assets=15_000_000_000,
            total_liabilities=12_000_000_000,
            stockholders_equity=3_000_000_000,
            total_debt=9_000_000_000,
            long_term_debt=7_000_000_000,
            cash_and_equivalents=500_000_000,
            current_assets=3_000_000_000,
            current_liabilities=4_000_000_000,
            goodwill=2_000_000_000,
            intangible_assets=500_000_000,
            net_ppe=8_000_000_000,
            total_capitalization=12_000_000_000,
        )],
        cash_flows=[CashFlow(
            operating_cash_flow=600_000_000,
            capital_expenditures=500_000_000,
            free_cash_flow=100_000_000,
            repurchase_of_stock=0,
            issuance_of_stock=200_000_000,
            dividends_paid=50_000_000,
        )],
        stats=KeyStats(
            market_cap=5_000_000_000,
            enterprise_value=13_500_000_000,
            trailing_pe=50.0,
            forward_pe=35.0,
            peg_ratio=3.5,
            price_to_sales=0.5,
            price_to_book=1.7,
            beta=1.5,
            dividend_yield=0.01,
            payout_ratio=0.50,
            earnings_growth=0.02,
            revenue_growth=-0.03,
            profit_margins=0.01,
            shares_outstanding=200_000_000,
            current_price=25.0,
        ),
        eps_history=[1.50, 1.20, 0.80, 0.50],
        annual_returns=[-0.05, -0.10, 0.03, -0.15, 0.02, -0.08, 0.01, -0.20],
        listing_years=12.0,
        cagr=-0.08,
        insider_transactions=[
            InsiderTransaction(
                holder="CEO", transaction_type="Sale", shares=50000, value=1_250_000, date="2026-07-01"
            ),
            InsiderTransaction(
                holder="CFO", transaction_type="Sale", shares=30000, value=750_000, date="2026-06-15"
            ),
            InsiderTransaction(
                holder="CTO", transaction_type="Sale", shares=20000, value=500_000, date="2026-05-01"
            ),
        ],
    )


def _make_out_of_scope() -> FundamentalData:
    """An ETF/fund-like entity that should be flagged out of scope."""
    return FundamentalData(
        symbol="SPYETF",
        sector="",
        industry="",
        income_statements=[],
        balance_sheets=[],
        cash_flows=[],
        stats=KeyStats(),
    )


def _make_pre_profit_company() -> FundamentalData:
    """A pre-profitability company -- should be flagged but not zero-scored."""
    return FundamentalData(
        symbol="BIOTECH",
        sector="Healthcare",
        industry="Biotechnology",
        income_statements=[IncomeStatement(
            revenue=50_000_000,
            cost_of_revenue=30_000_000,
            gross_profit=20_000_000,
            sga_expense=40_000_000,
            operating_income=-60_000_000,
            net_income=-80_000_000,
            ebitda=-50_000_000,
            basic_eps=-2.0,
        )],
        balance_sheets=[BalanceSheet(
            total_assets=500_000_000,
            total_liabilities=100_000_000,
            stockholders_equity=400_000_000,
            cash_and_equivalents=300_000_000,
            current_assets=320_000_000,
            current_liabilities=50_000_000,
        )],
        stats=KeyStats(
            market_cap=2_000_000_000,
            trailing_pe=-25.0,
            current_price=50.0,
        ),
        eps_history=[-3.0, -2.5, -2.0],
        listing_years=5.0,
        cagr=-0.15,
    )


# ---------------------------------------------------------------------------
# Engine fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def engine():
    return SixfoldEngine()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScopeCheck:
    def test_out_of_scope_no_data(self, engine):
        data = _make_out_of_scope()
        score = engine.score(data)
        assert not score.in_scope
        assert score.composite_score == 0.0

    def test_pre_profit_is_in_scope_with_revenue(self, engine):
        data = _make_pre_profit_company()
        score = engine.score(data)
        assert score.in_scope


class TestLensBuffett:
    def test_elite_passes_most_gates(self, engine):
        data = _make_elite_company()
        result = engine._lens_buffett(data)
        assert result.score > 60
        assert any("Gross margin" in c for c in result.passed_criteria)
        assert any("Net margin" in c for c in result.passed_criteria)

    def test_poor_fails_most_gates(self, engine):
        data = _make_poor_company()
        result = engine._lens_buffett(data)
        assert result.score < 40

    def test_eps_consistency(self, engine):
        assert engine._check_eps_consistency([7.0, 8.0, 9.0, 10.0]) is True
        assert engine._check_eps_consistency([10.0, 8.0, 9.0, 7.0]) is False
        assert engine._check_eps_consistency([5.0]) is False


class TestLensROIC:
    def test_elite_high_roic(self, engine):
        data = _make_elite_company()
        result = engine._lens_roic(data)
        assert result.score >= 70
        assert any("value creation" in c for c in result.passed_criteria)

    def test_poor_low_roic(self, engine):
        data = _make_poor_company()
        result = engine._lens_roic(data)
        assert result.score < 60

    def test_negative_operating_income(self, engine):
        data = _make_pre_profit_company()
        result = engine._lens_roic(data)
        assert result.score == 0.0
        assert any("Non-positive" in n for n in result.notes)


class TestLensMismatch:
    def test_elite_has_some_signals(self, engine):
        data = _make_elite_company()
        result = engine._lens_mismatch(data)
        assert result.score > 0

    def test_poor_has_few_signals(self, engine):
        data = _make_poor_company()
        result = engine._lens_mismatch(data)
        assert result.score <= 50


class TestLensRegression:
    def test_elite_regression_discount(self, engine):
        data = _make_elite_company()
        result = engine._lens_regression(data)
        assert any("Justified PE" in n for n in result.notes)
        assert result.score > 0

    def test_negative_pe_skipped(self, engine):
        data = _make_pre_profit_company()
        result = engine._lens_regression(data)
        assert result.score == 0.0

    def test_regression_formula(self, engine):
        """Verify the Damodaran regression produces expected output."""
        data = _make_elite_company()
        result = engine._lens_regression(data)
        # PE = 16.09 + 9.30*1.1 + 51.92*0.15 + 7.53*0.20
        # = 16.09 + 10.23 + 7.788 + 1.506 = 35.614
        expected_justified = 16.09 + 9.30 * 1.1 + 51.92 * 0.15 + 7.53 * 0.20
        assert any(f"{expected_justified:.1f}" in n for n in result.notes)


class TestLensCapitalSignals:
    def test_insider_buying_positive(self, engine):
        data = _make_elite_company()
        result = engine._lens_capital_signals(data)
        assert result.score > 50
        assert any("purchase" in c.lower() for c in result.passed_criteria)

    def test_insider_selling_negative(self, engine):
        data = _make_poor_company()
        result = engine._lens_capital_signals(data)
        assert result.score < 50
        assert any("sale" in c.lower() for c in result.failed_criteria)


class TestLensHistorical:
    def test_elite_high_returns(self, engine):
        data = _make_elite_company()
        result = engine._lens_historical(data)
        assert result.score >= 85  # 20% CAGR = top decile

    def test_poor_negative_returns(self, engine):
        data = _make_poor_company()
        result = engine._lens_historical(data)
        assert result.score <= 30

    def test_short_history_neutral(self, engine):
        data = _make_pre_profit_company()
        data.listing_years = 2.0  # too short
        result = engine._lens_historical(data)
        assert result.score == 50.0


class TestCompositeScoring:
    def test_elite_scores_high(self, engine):
        data = _make_elite_company()
        score = engine.score(data)
        assert score.composite_score >= 65
        assert score.confidence == ConfidenceTier.SCREENING
        assert score.in_scope

    def test_poor_scores_low(self, engine):
        data = _make_poor_company()
        score = engine.score(data)
        assert score.composite_score < 45
        assert score.in_scope

    def test_out_of_scope_scores_zero(self, engine):
        data = _make_out_of_scope()
        score = engine.score(data)
        assert score.composite_score == 0.0
        assert not score.in_scope

    def test_score_has_would_raise_lower(self, engine):
        data = _make_elite_company()
        score = engine.score(data)
        assert isinstance(score.would_raise, list)
        assert isinstance(score.would_lower, list)


class TestBatchScoring:
    def test_universe_sorted_descending(self, engine):
        data_list = [_make_poor_company(), _make_elite_company()]
        scores = engine.score_universe(data_list)
        assert scores[0].symbol == "ELITE"
        assert scores[1].symbol == "POOR"
        assert scores[0].composite_score > scores[1].composite_score


class TestFormatReport:
    def test_report_contains_key_sections(self, engine):
        data = _make_elite_company()
        score = engine.score(data)
        report = engine.format_report(score)
        assert "SIXFOLD Analysis: ELITE" in report
        assert "Composite Score" in report
        assert "Buffett" in report
        assert "ROIC" in report
        assert "Mismatch" in report
        assert "Regression" in report
        assert "Capital Signals" in report
        assert "Historical" in report
