"""Scoring corrections back-ported from the live-money Tradier deployment.

Each test here corresponds to a defect that was found by running the same
framework against real filings, so the fixtures are shaped like the companies
that exposed them rather than like tidy archetypes:

  * AAPL   -- genuinely negative invested capital, every input reported
  * JPM    -- a bank, which files no classified balance sheet at all
  * META   -- clears the 40% gross-margin gate AND meets the addendum's
              conditions, and must not lose the point it earned
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.core.financial_data import (
    BALANCE_KEYS,
    BalanceSheet,
    CashFlow,
    FundamentalData,
    IncomeStatement,
    KeyStats,
    _build,
    _opt_float,
    _safe_float,
)
from src.strategies.sixfold_engine import SixfoldEngine, gross_margin_suspension


@pytest.fixture
def engine():
    return SixfoldEngine()


def _roic_lens(engine, data):
    return engine._lens_roic(data)


def _buffett_lens(engine, data):
    return engine._lens_buffett(data)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _capital_light_company(missing_net_ppe: bool = False) -> FundamentalData:
    """AAPL-shaped: current liabilities exceed current assets by more than net
    PPE, so invested capital is about -$6.2B with positive NOPAT."""
    bal = BalanceSheet(
        total_assets=365_000_000_000,
        total_liabilities=308_000_000_000,
        stockholders_equity=57_000_000_000,
        total_debt=106_000_000_000,
        long_term_debt=85_000_000_000,
        cash_and_equivalents=46_200_000_000,
        current_assets=148_000_000_000,
        current_liabilities=165_600_000_000,
        goodwill=0.0,
        intangible_assets=0.0,
        net_ppe=0.0 if missing_net_ppe else 49_800_000_000,
        missing=frozenset({"net_ppe"}) if missing_net_ppe else frozenset(),
    )
    return FundamentalData(
        symbol="AAPL",
        sector="Technology",
        income_statements=[IncomeStatement(
            revenue=391_000_000_000,
            gross_profit=180_600_000_000,
            operating_income=123_200_000_000,
            net_income=93_700_000_000,
            tax_provision=29_700_000_000,
        )],
        balance_sheets=[bal],
    )


def _bank() -> FundamentalData:
    """JPM-shaped: a bank files no classified balance sheet, so current assets
    and current liabilities are absent rather than zero."""
    return FundamentalData(
        symbol="JPM",
        sector="Financial Services",
        income_statements=[IncomeStatement(
            revenue=158_000_000_000,
            gross_profit=158_000_000_000,
            operating_income=61_600_000_000,
            net_income=49_600_000_000,
            tax_provision=12_000_000_000,
        )],
        balance_sheets=[BalanceSheet(
            total_assets=3_875_000_000_000,
            stockholders_equity=327_000_000_000,
            cash_and_equivalents=469_000_000_000,
            current_assets=0.0,
            current_liabilities=0.0,
            net_ppe=30_000_000_000,
            missing=frozenset({"current_assets", "current_liabilities"}),
        )],
    )


def _grower(gross_margin: float, revenue_growth: float,
            heavy_debt: bool = False) -> FundamentalData:
    """A profitable, growing company with a controllable gross margin.

    Everything except the named arguments is held constant, so two of these
    differ only in the dimension under test.

    `heavy_debt` fails the D/E criterion. It exists so the denominator test
    below can run on a company that does NOT earn exactly 7 of the 8 criteria:
    at 7 earned, "remove the criterion from the denominator" (7/7) and "award
    the point and keep the denominator" (8/8) both come to 100.0 and no
    assertion can tell them apart.
    """
    revenue = 100_000_000_000.0
    prior = revenue / (1.0 + revenue_growth)
    income = [
        IncomeStatement(
            revenue=revenue,
            gross_profit=revenue * gross_margin,
            sga_expense=revenue * 0.20,
            operating_income=revenue * 0.35,
            net_income=revenue * 0.30,
            tax_provision=revenue * 0.05,
        ),
        IncomeStatement(revenue=prior, net_income=prior * 0.28),
        IncomeStatement(revenue=prior * 0.8, net_income=prior * 0.8 * 0.26),
        IncomeStatement(revenue=prior * 0.6, net_income=prior * 0.6 * 0.24),
    ]
    return FundamentalData(
        symbol="GROW",
        sector="Technology",
        income_statements=income,
        balance_sheets=[BalanceSheet(
            stockholders_equity=80_000_000_000,
            total_debt=90_000_000_000 if heavy_debt else 20_000_000_000,
            long_term_debt=15_000_000_000,
            cash_and_equivalents=10_000_000_000,
            current_assets=60_000_000_000,
            current_liabilities=25_000_000_000,
            net_ppe=40_000_000_000,
        )],
        cash_flows=[CashFlow(capital_expenditures=5_000_000_000)],
        eps_history=[1.0, 2.0, 3.0, 4.0],
        stats=KeyStats(),
    )


# ---------------------------------------------------------------------------
# 1. Negative invested capital is the best outcome, not the worst
# ---------------------------------------------------------------------------

class TestNegativeInvestedCapital:

    def test_scores_100_when_every_input_is_reported(self, engine):
        """AAPL's invested capital really is negative. Positive NOPAT on
        negative capital employed is an unbounded return on capital."""
        lens = _roic_lens(engine, _capital_light_company())

        assert lens.score == 100.0
        assert lens.measured is True
        assert any("unbounded return on capital" in c for c in lens.passed_criteria)

    def test_the_fixture_really_has_negative_invested_capital(self, engine):
        """Guards the test above: if the fixture drifted into positive invested
        capital it would score 100 down the ordinary path and prove nothing."""
        lens = _roic_lens(engine, _capital_light_company())
        claim = next(c for c in lens.passed_criteria if "invested capital" in c.lower())
        assert "$-6,1" in claim or "$-6,2" in claim, claim

    def test_a_missing_input_scores_neutral_rather_than_100(self, engine):
        """Absence of data is not evidence of capital efficiency. With net PPE
        unreported the same balance sheet is unmeasurable, not excellent."""
        lens = _roic_lens(engine, _capital_light_company(missing_net_ppe=True))

        assert lens.score == 50.0
        assert lens.measured is False
        assert lens.passed_criteria == []

    def test_a_reported_zero_net_ppe_is_still_measurable(self, engine):
        """The distinction is reported-vs-absent, not zero-vs-nonzero. A company
        that genuinely owns no PPE has a measurable, capital-light balance
        sheet and must not be demoted to unmeasurable."""
        data = _capital_light_company()
        data.balance_sheets[0].net_ppe = 0.0  # reported, and genuinely zero

        lens = _roic_lens(engine, data)

        assert lens.score == 100.0
        assert lens.measured is True


# ---------------------------------------------------------------------------
# 2. Unmeasurable ROIC is neutral, and disqualifying
# ---------------------------------------------------------------------------

class TestUnmeasurableRoic:

    def test_a_bank_scores_neutral_not_zero(self, engine):
        """Scoring 0 reads as 'destroying value'. The truth is 'not measured'."""
        lens = _roic_lens(engine, _bank())

        assert lens.score == 50.0
        assert lens.measured is False
        assert any("unmeasurable" in n for n in lens.notes)

    def test_unreported_operating_income_scores_neutral(self, engine):
        data = _capital_light_company()
        data.income_statements[0].missing = frozenset({"operating_income"})

        lens = _roic_lens(engine, data)

        assert lens.score == 50.0
        assert lens.measured is False

    def test_a_genuine_operating_loss_is_measured_and_scores_zero(self, engine):
        """A reported loss is an answer, not a gap. It keeps its bad score and
        stays eligible for the ranking on its merits."""
        data = _capital_light_company()
        data.income_statements[0].operating_income = -5_000_000_000

        lens = _roic_lens(engine, data)

        assert lens.score == 0.0
        assert lens.measured is True

    def test_the_flag_reaches_the_score_object(self, engine):
        """The executor reads SixfoldScore.roic_measured, so the lens flag has
        to survive the trip through score()."""
        assert engine.score(_bank()).roic_measured is False
        assert engine.score(_capital_light_company()).roic_measured is True

    def test_an_out_of_scope_name_is_not_marked_measured(self, engine):
        score = engine.score(FundamentalData(symbol="EMPTY"))
        assert score.in_scope is False
        assert score.roic_measured is False


# ---------------------------------------------------------------------------
# 3. Tashi addendum: the gross-margin suspension
# ---------------------------------------------------------------------------

class TestGrossMarginSuspension:

    def test_it_fires_for_a_fast_growing_low_margin_company(self, engine):
        lens = _buffett_lens(engine, _grower(gross_margin=0.35, revenue_growth=0.25))

        assert lens.gross_margin_suspended is True
        assert any("SUSPENDED" in n for n in lens.notes)

    def test_it_does_not_lower_a_company_that_clears_40_percent(self, engine):
        """The live regression. META and LLY both meet the addendum's growth and
        profitability conditions AND clear 40%; applying the suspension
        unconditionally stripped a point they had earned and cut their scores
        (75.9 -> 75.1 and 73.4 -> 72.2). Relief must never cost a company that
        qualifies for it."""
        data = _grower(gross_margin=0.75, revenue_growth=0.25)

        # The company genuinely satisfies the addendum's conditions ...
        assert gross_margin_suspension(data).suspended is True

        # ... and the lens still declines to suspend, because the point was earned.
        lens = _buffett_lens(engine, data)
        assert lens.gross_margin_suspended is False
        assert any("Gross margin 75.0% >= 40%" in c for c in lens.passed_criteria)

    def test_suspension_removes_the_criterion_instead_of_awarding_it(self, engine):
        """Two identical low-margin companies; only the growth rate differs, so
        only one is suspended. Both fail the gross-margin criterion and both
        earn the same 6 of the remaining criteria, so the suspended one must
        score 6/7 and the control 6/8. Awarding the point instead would give
        the suspended company 7/8 = 87.5, which these assertions reject.

        Scored as absolute values rather than as a ratio on purpose: the two
        readings coincide at exactly 7 earned criteria, so a ratio assertion
        silently passes against a fixture that happens to sit there.
        """
        suspended = _buffett_lens(
            engine, _grower(gross_margin=0.35, revenue_growth=0.25, heavy_debt=True))
        control = _buffett_lens(
            engine, _grower(gross_margin=0.35, revenue_growth=0.05, heavy_debt=True))

        assert suspended.gross_margin_suspended is True
        assert control.gross_margin_suspended is False

        assert control.score == pytest.approx(6 / 8 * 100)        # 75.0
        assert suspended.score == pytest.approx(6 / 7 * 100)      # 85.71
        assert suspended.score != pytest.approx(7 / 8 * 100)      # not 87.5

    def test_a_suspended_company_earns_no_gross_margin_point(self, engine):
        """The criterion is absent from the record entirely -- neither passed
        nor failed -- which is what "suspended" has to mean."""
        lens = _buffett_lens(engine, _grower(gross_margin=0.35, revenue_growth=0.25))

        assert not any("Gross margin" in c for c in lens.passed_criteria)
        assert not any("Gross margin" in c for c in lens.failed_criteria)

    def test_never_negative_needs_a_real_history(self):
        data = _grower(gross_margin=0.35, revenue_growth=0.25)
        data.income_statements = data.income_statements[:2]  # only 2 reported years

        check = gross_margin_suspension(data)

        assert check.suspended is False
        assert "need 3" in check.reason

    def test_an_unreported_year_blocks_the_never_negative_claim(self):
        data = _grower(gross_margin=0.35, revenue_growth=0.25)
        data.income_statements[2].missing = frozenset({"net_income"})

        check = gross_margin_suspension(data)

        assert check.suspended is False
        assert "unreported" in check.reason

    def test_a_single_negative_year_blocks_it(self):
        """AMZN's 2022 loss is the live example."""
        data = _grower(gross_margin=0.35, revenue_growth=0.25)
        data.income_statements[2].net_income = -1_000_000_000

        check = gross_margin_suspension(data)

        assert check.suspended is False
        assert "negative net income" in check.reason

    def test_growth_below_the_bar_blocks_it(self):
        """ORCL at 17.3% and NFLX at 15.9% are the live near-misses."""
        check = gross_margin_suspension(_grower(gross_margin=0.35, revenue_growth=0.17))

        assert check.suspended is False
        assert "< 20.0%" in check.reason

    def test_the_flag_reaches_the_score_object(self, engine):
        data = _grower(gross_margin=0.35, revenue_growth=0.25)
        assert engine.score(data).gross_margin_suspended is True


# ---------------------------------------------------------------------------
# 4. Missing-value discipline
# ---------------------------------------------------------------------------

class TestMissingValueDiscipline:

    @pytest.mark.parametrize("bad", [None, "", "   ", float("nan"), float("inf"),
                                     True, False, [], {}, "abc"])
    def test_non_values_are_missing_not_zero(self, bad):
        assert _opt_float(bad) is None

    @pytest.mark.parametrize("good,expected", [(0, 0.0), (0.0, 0.0), ("0", 0.0),
                                               (-6.2, -6.2), ("3.5", 3.5)])
    def test_real_numbers_survive_including_zero(self, good, expected):
        assert _opt_float(good) == expected

    def test_safe_float_still_falls_back(self):
        assert _safe_float(None) == 0.0
        assert _safe_float(None, default=1.0) == 1.0
        assert _safe_float("2.5") == 2.5

    def test_a_bool_is_not_a_financial_figure(self):
        """float(True) is 1.0, which would enter the books as a real dollar."""
        assert _safe_float(True) == 0.0

    def test_the_provider_records_an_absent_row_as_missing(self):
        """Wiring: it is _build, not the helper, that the provider calls, and a
        bank's balance sheet is missing rows rather than carrying zeros."""
        df = pd.DataFrame(
            {"2025": [3_875_000_000_000.0, 327_000_000_000.0]},
            index=["Total Assets", "Stockholders Equity"],
        )

        sheet = _build(BalanceSheet, df, "2025", BALANCE_KEYS)

        assert sheet.total_assets == 3_875_000_000_000.0
        assert sheet.is_reported("total_assets") is True
        assert sheet.current_assets == 0.0
        assert sheet.is_reported("current_assets") is False

    def test_a_reported_zero_is_reported(self):
        df = pd.DataFrame({"2025": [0.0]}, index=["Goodwill"])

        sheet = _build(BalanceSheet, df, "2025", BALANCE_KEYS)

        assert sheet.goodwill == 0.0
        assert sheet.is_reported("goodwill") is True

    def test_a_hand_built_statement_reads_as_fully_reported(self):
        """Backwards compatibility: every existing fixture states its numbers,
        so nothing in it is missing."""
        assert BalanceSheet(current_assets=5.0).is_reported("current_assets") is True
        assert IncomeStatement(revenue=5.0).is_reported("operating_income") is True
