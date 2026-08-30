"""SIXFOLD Equity Analysis Framework -- Scoring Engine.

Six independent lenses, one question: does a business generate returns
above its cost of capital in a way competitors cannot easily replicate,
and is it currently available at a price below its intrinsic value?

Reference: SIXFOLD Framework Methodology v2.0 (August 2026, Tashi).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from src.core.financial_data import FundamentalData, _safe_float

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Damodaran sector data (hardcoded subset for hackathon speed)
# Source: pages.stern.nyu.edu/~adamodar/
# ---------------------------------------------------------------------------

DAMODARAN_SECTOR_WACC: dict[str, float] = {
    "Technology": 0.0950,
    "Healthcare": 0.0754,
    "Financial Services": 0.0800,
    "Consumer Cyclical": 0.0850,
    "Consumer Defensive": 0.0700,
    "Communication Services": 0.0820,
    "Industrials": 0.0810,
    "Basic Materials": 0.0900,
    "Energy": 0.1050,
    "Utilities": 0.0550,
    "Real Estate": 0.0650,
}

DAMODARAN_SECTOR_BETA: dict[str, float] = {
    "Technology": 1.18,
    "Healthcare": 1.01,
    "Financial Services": 0.87,
    "Consumer Cyclical": 1.08,
    "Consumer Defensive": 0.69,
    "Communication Services": 1.04,
    "Industrials": 1.01,
    "Basic Materials": 1.07,
    "Energy": 1.26,
    "Utilities": 0.59,
    "Real Estate": 0.75,
}

DAMODARAN_SECTOR_MARGINS: dict[str, dict[str, float]] = {
    "Technology": {"gross": 0.55, "net": 0.20, "roe": 0.25},
    "Healthcare": {"gross": 0.50, "net": 0.12, "roe": 0.18},
    "Financial Services": {"gross": 0.60, "net": 0.22, "roe": 0.12},
    "Consumer Cyclical": {"gross": 0.35, "net": 0.08, "roe": 0.15},
    "Consumer Defensive": {"gross": 0.35, "net": 0.09, "roe": 0.20},
    "Communication Services": {"gross": 0.50, "net": 0.15, "roe": 0.12},
    "Industrials": {"gross": 0.30, "net": 0.08, "roe": 0.15},
    "Basic Materials": {"gross": 0.30, "net": 0.08, "roe": 0.12},
    "Energy": {"gross": 0.40, "net": 0.10, "roe": 0.15},
    "Utilities": {"gross": 0.40, "net": 0.12, "roe": 0.10},
    "Real Estate": {"gross": 0.50, "net": 0.20, "roe": 0.08},
}


class ConfidenceTier(str, Enum):
    VERIFIED = "verified"
    SCREENING = "screening"
    TRIAGE = "triage"


@dataclass
class LensResult:
    name: str
    score: float  # 0-100 contribution
    weight: float
    passed_criteria: list[str] = field(default_factory=list)
    failed_criteria: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SixfoldScore:
    """Complete SIXFOLD analysis result for a single security."""
    symbol: str
    composite_score: float
    confidence: ConfidenceTier
    lens_results: list[LensResult] = field(default_factory=list)
    would_raise: list[str] = field(default_factory=list)
    would_lower: list[str] = field(default_factory=list)
    in_scope: bool = True
    out_of_scope_reason: str = ""
    sector: str = ""
    historical_percentile: float | None = None


class SixfoldEngine:
    """Implements the six-lens SIXFOLD scoring methodology.

    Lens weights (hackathon screening tier):
        1. Buffett Durable Advantage:  20 pts
        2. ROIC:                       25 pts (most important)
        3. Valuation Mismatch:         15 pts
        4. Damodaran Regression:       15 pts
        5. Capital Signals:            10 pts (adjustment +/-4)
        6. Historical Returns:         15 pts
        Total:                        100 pts
    """

    LENS_WEIGHTS = {
        "buffett": 20.0,
        "roic": 25.0,
        "mismatch": 15.0,
        "regression": 15.0,
        "capital_signals": 10.0,
        "historical": 15.0,
    }

    def score(self, data: FundamentalData) -> SixfoldScore:
        scope_check = self._check_scope(data)
        if not scope_check["in_scope"]:
            return SixfoldScore(
                symbol=data.symbol,
                composite_score=0.0,
                confidence=ConfidenceTier.TRIAGE,
                in_scope=False,
                out_of_scope_reason=scope_check["reason"],
                sector=data.sector,
            )

        lens_results = [
            self._lens_buffett(data),
            self._lens_roic(data),
            self._lens_mismatch(data),
            self._lens_regression(data),
            self._lens_capital_signals(data),
            self._lens_historical(data),
        ]

        composite = sum(lr.score * lr.weight for lr in lens_results)
        composite = max(0.0, min(100.0, composite))

        would_raise = []
        would_lower = []
        for lr in lens_results:
            would_raise.extend(
                f"[{lr.name}] {c}" for c in lr.failed_criteria[:2]
            )
            would_lower.extend(
                f"[{lr.name}] lose {c}" for c in lr.passed_criteria[:1]
            )

        hist_lens = next((lr for lr in lens_results if lr.name == "Historical Returns"), None)
        hist_pct = None
        if hist_lens and hist_lens.notes:
            for note in hist_lens.notes:
                if "percentile" in note.lower():
                    try:
                        hist_pct = float(note.split(":")[-1].strip().replace("%", ""))
                    except ValueError:
                        pass

        return SixfoldScore(
            symbol=data.symbol,
            composite_score=round(composite, 1),
            confidence=ConfidenceTier.SCREENING,
            lens_results=lens_results,
            would_raise=would_raise[:5],
            would_lower=would_lower[:3],
            sector=data.sector,
            historical_percentile=hist_pct,
        )

    def _check_scope(self, data: FundamentalData) -> dict:
        """Step 1 of operating procedure: confirm security is within scope."""
        if not data.income_statements:
            return {"in_scope": False, "reason": "No income statement data available"}

        latest = data.income_statements[0]
        if latest.revenue <= 0:
            return {"in_scope": False, "reason": "No revenue -- pre-revenue or non-operating entity"}

        if latest.net_income < 0 and latest.revenue < 10_000_000:
            return {"in_scope": False, "reason": "Negative earnings with minimal revenue"}

        return {"in_scope": True, "reason": ""}

    # -------------------------------------------------------------------
    # Lens 1: Buffett Durable Competitive Advantage
    # -------------------------------------------------------------------
    def _lens_buffett(self, data: FundamentalData) -> LensResult:
        result = LensResult(
            name="Buffett Durable Advantage",
            score=0.0,
            weight=self.LENS_WEIGHTS["buffett"] / 100.0,
        )

        if not data.income_statements or not data.balance_sheets:
            result.notes.append("Insufficient data for Buffett analysis")
            return result

        inc = data.income_statements[0]
        bal = data.balance_sheets[0]
        cf = data.cash_flows[0] if data.cash_flows else None

        criteria_score = 0.0
        total_criteria = 8

        # Gross margin >= 40%
        if inc.revenue > 0:
            gross_margin = inc.gross_profit / inc.revenue
            if gross_margin >= 0.40:
                criteria_score += 1
                result.passed_criteria.append(f"Gross margin {gross_margin:.1%} >= 40%")
            else:
                result.failed_criteria.append(f"Gross margin {gross_margin:.1%} < 40%")

        # SGA as % of gross profit <= 30%
        if inc.gross_profit > 0 and inc.sga_expense > 0:
            sga_pct = inc.sga_expense / inc.revenue
            if sga_pct <= 0.30:
                criteria_score += 1
                result.passed_criteria.append(f"SGA/revenue {sga_pct:.1%} <= 30%")
            else:
                result.failed_criteria.append(f"SGA/revenue {sga_pct:.1%} > 30%")

        # Net margin >= 20%
        if inc.revenue > 0:
            net_margin = inc.net_income / inc.revenue
            if net_margin >= 0.20:
                criteria_score += 1
                result.passed_criteria.append(f"Net margin {net_margin:.1%} >= 20%")
            else:
                result.failed_criteria.append(f"Net margin {net_margin:.1%} < 20%")

        # EPS consistency (no down years) -- the most informative gate
        eps_consistent = self._check_eps_consistency(data.eps_history)
        if eps_consistent:
            criteria_score += 1.5  # weighted higher per methodology
            result.passed_criteria.append("EPS consistent (no down years)")
        else:
            result.failed_criteria.append("EPS inconsistent (down years detected)")

        # ROE >= 25%
        if bal.stockholders_equity > 0 and inc.net_income > 0:
            roe = inc.net_income / bal.stockholders_equity
            if roe >= 0.25:
                criteria_score += 1
                result.passed_criteria.append(f"ROE {roe:.1%} >= 25%")
            else:
                result.failed_criteria.append(f"ROE {roe:.1%} < 25%")
        elif bal.stockholders_equity < 0:
            result.notes.append("Negative equity -- ROE not meaningful (buyback distortion?)")

        # Debt to equity <= 0.8
        if bal.stockholders_equity > 0:
            de_ratio = bal.total_debt / bal.stockholders_equity
            if de_ratio <= 0.80:
                criteria_score += 1
                result.passed_criteria.append(f"D/E {de_ratio:.2f} <= 0.8")
            else:
                result.failed_criteria.append(f"D/E {de_ratio:.2f} > 0.8")

        # Long-term debt payoff <= 4 years of net income
        if inc.net_income > 0 and bal.long_term_debt > 0:
            payoff_years = bal.long_term_debt / inc.net_income
            if payoff_years <= 4.0:
                criteria_score += 0.75
                result.passed_criteria.append(f"Debt payoff {payoff_years:.1f}yr <= 4yr")
            else:
                result.failed_criteria.append(f"Debt payoff {payoff_years:.1f}yr > 4yr")

        # Capex <= 50% of net income
        if cf and inc.net_income > 0 and cf.capital_expenditures > 0:
            capex_pct = cf.capital_expenditures / inc.net_income
            if capex_pct <= 0.50:
                criteria_score += 0.75
                result.passed_criteria.append(f"Capex/income {capex_pct:.1%} <= 50%")
            else:
                result.failed_criteria.append(f"Capex/income {capex_pct:.1%} > 50%")

        result.score = (criteria_score / total_criteria) * 100.0
        return result

    def _check_eps_consistency(self, eps_history: list[float]) -> bool:
        if len(eps_history) < 2:
            return False
        for i in range(1, len(eps_history)):
            if eps_history[i] < eps_history[i - 1] * 0.90:
                return False
        return True

    # -------------------------------------------------------------------
    # Lens 2: ROIC (Morgan Stanley Operating Approach)
    # -------------------------------------------------------------------
    def _lens_roic(self, data: FundamentalData) -> LensResult:
        result = LensResult(
            name="Return on Invested Capital",
            score=0.0,
            weight=self.LENS_WEIGHTS["roic"] / 100.0,
        )

        if not data.income_statements or not data.balance_sheets:
            result.notes.append("Insufficient data for ROIC calculation")
            return result

        inc = data.income_statements[0]
        bal = data.balance_sheets[0]

        # NOPAT = Operating Income * (1 - effective tax rate)
        if inc.operating_income <= 0:
            result.notes.append("Non-positive operating income -- ROIC not calculable")
            result.failed_criteria.append("Negative/zero operating income")
            return result

        effective_tax_rate = 0.21  # default US corporate
        if inc.tax_provision > 0 and inc.operating_income > 0:
            effective_tax_rate = min(inc.tax_provision / inc.operating_income, 0.40)

        nopat = inc.operating_income * (1.0 - effective_tax_rate)

        # Invested Capital (operating approach)
        # Net working capital + Net PPE + Goodwill + Intangibles
        # Excess cash excluded (2% of revenue rule)
        operating_cash = min(bal.cash_and_equivalents, inc.revenue * 0.02)
        excess_cash = bal.cash_and_equivalents - operating_cash

        net_working_capital = bal.current_assets - bal.current_liabilities - excess_cash
        invested_capital = (
            net_working_capital
            + bal.net_ppe
            + bal.goodwill
            + bal.intangible_assets
        )

        if invested_capital <= 0:
            result.notes.append("Non-positive invested capital -- ROIC not meaningful")
            return result

        roic = nopat / invested_capital

        # Organic ROIC (excluding goodwill) for acquisitive companies
        invested_no_gw = invested_capital - bal.goodwill
        organic_roic = nopat / invested_no_gw if invested_no_gw > 0 else roic

        # Get sector WACC
        wacc = DAMODARAN_SECTOR_WACC.get(data.sector, 0.09)
        spread = roic - wacc

        result.notes.append(f"ROIC: {roic:.1%} | WACC: {wacc:.1%} | Spread: {spread:.1%}")
        result.notes.append(f"NOPAT: ${nopat:,.0f} | Invested Capital: ${invested_capital:,.0f}")

        if bal.goodwill > 0:
            result.notes.append(f"Organic ROIC (ex-goodwill): {organic_roic:.1%}")

        # Score ROIC lens
        if spread > 0.15:
            result.score = 100.0
            result.passed_criteria.append(f"ROIC spread {spread:.1%} -- exceptional value creation")
        elif spread > 0.08:
            result.score = 85.0
            result.passed_criteria.append(f"ROIC spread {spread:.1%} -- strong value creation")
        elif spread > 0.03:
            result.score = 70.0
            result.passed_criteria.append(f"ROIC spread {spread:.1%} -- above cost of capital")
        elif spread > 0:
            result.score = 55.0
            result.passed_criteria.append(f"ROIC spread {spread:.1%} -- marginal value creation")
        elif spread > -0.03:
            result.score = 35.0
            result.failed_criteria.append(f"ROIC spread {spread:.1%} -- near cost of capital")
        else:
            result.score = 15.0
            result.failed_criteria.append(f"ROIC spread {spread:.1%} -- destroying value")

        return result

    # -------------------------------------------------------------------
    # Lens 3: Valuation Mismatch Signals (Damodaran Table 4.4)
    # -------------------------------------------------------------------
    def _lens_mismatch(self, data: FundamentalData) -> LensResult:
        result = LensResult(
            name="Valuation Mismatch",
            score=0.0,
            weight=self.LENS_WEIGHTS["mismatch"] / 100.0,
        )

        stats = data.stats
        inc = data.income_statements[0] if data.income_statements else None
        bal = data.balance_sheets[0] if data.balance_sheets else None
        cf = data.cash_flows[0] if data.cash_flows else None

        signal_count = 0

        # Signal 1: Low PE with high expected growth
        if stats.trailing_pe > 0 and stats.earnings_growth > 0.10:
            if stats.trailing_pe < 15:
                signal_count += 1
                result.passed_criteria.append(
                    f"Low PE ({stats.trailing_pe:.1f}) + high growth ({stats.earnings_growth:.1%})"
                )

        # Signal 2: Low P/S with high net margin
        if stats.price_to_sales > 0 and stats.profit_margins > 0.15:
            if stats.price_to_sales < 2.0:
                signal_count += 1
                result.passed_criteria.append(
                    f"Low P/S ({stats.price_to_sales:.1f}) + high margin ({stats.profit_margins:.1%})"
                )

        # Signal 3: Low EV/EBITDA with low reinvestment needs
        if inc and inc.ebitda > 0 and stats.enterprise_value > 0:
            ev_ebitda = stats.enterprise_value / inc.ebitda
            low_reinvestment = False
            if cf and inc.net_income > 0:
                low_reinvestment = (cf.capital_expenditures / inc.net_income) < 0.30
            if ev_ebitda < 10 and low_reinvestment:
                signal_count += 1
                result.passed_criteria.append(
                    f"Low EV/EBITDA ({ev_ebitda:.1f}) + low reinvestment"
                )

        # Signal 4: ROIC materially above cost of capital
        wacc = DAMODARAN_SECTOR_WACC.get(data.sector, 0.09)
        if inc and bal:
            if inc.operating_income > 0 and bal.total_assets > 0:
                rough_roic = inc.operating_income * 0.79 / bal.total_assets
                if rough_roic > wacc + 0.05:
                    signal_count += 1
                    result.passed_criteria.append(
                        f"ROIC ({rough_roic:.1%}) materially above WACC ({wacc:.1%})"
                    )

        # Signal 5: High free cash flow yield
        if cf and stats.market_cap > 0:
            fcf_yield = cf.free_cash_flow / stats.market_cap
            if fcf_yield > 0.06:
                signal_count += 1
                result.passed_criteria.append(f"High FCF yield ({fcf_yield:.1%})")

        # Signal 6: Cash exceeding 30% of market cap
        if bal and stats.market_cap > 0:
            cash_pct = bal.cash_and_equivalents / stats.market_cap
            if cash_pct > 0.30:
                signal_count += 1
                result.passed_criteria.append(f"Cash {cash_pct:.0%} of market cap (>30%)")

        # 4+ signals = genuine mispricing; 0-1 = correctly priced
        if signal_count >= 4:
            result.score = 100.0
            result.notes.append(f"{signal_count} mismatch signals -- genuine mispricing")
        elif signal_count == 3:
            result.score = 75.0
            result.notes.append(f"{signal_count} mismatch signals -- probable mispricing")
        elif signal_count == 2:
            result.score = 50.0
            result.notes.append(f"{signal_count} mismatch signals -- mixed")
        elif signal_count == 1:
            result.score = 25.0
            result.notes.append(f"{signal_count} mismatch signal -- likely fairly priced")
        else:
            result.score = 10.0
            result.notes.append("No mismatch signals -- market pricing appears correct")
            result.failed_criteria.append("No valuation mismatch signals detected")

        return result

    # -------------------------------------------------------------------
    # Lens 4: Damodaran Regression
    # PE = 16.09 + 9.30*Beta + 51.92*Growth + 7.53*Payout
    # -------------------------------------------------------------------
    def _lens_regression(self, data: FundamentalData) -> LensResult:
        result = LensResult(
            name="Damodaran Regression",
            score=0.0,
            weight=self.LENS_WEIGHTS["regression"] / 100.0,
        )

        stats = data.stats

        if stats.trailing_pe <= 0:
            result.notes.append("No valid trailing PE -- regression not applicable")
            result.failed_criteria.append("Cannot compute without positive PE")
            return result

        beta = stats.beta or DAMODARAN_SECTOR_BETA.get(data.sector, 1.0)
        growth = max(stats.earnings_growth, 0.0) if stats.earnings_growth else 0.0
        payout = stats.payout_ratio if stats.payout_ratio and stats.payout_ratio > 0 else 0.0

        # Damodaran market-wide regression (current year coefficients)
        justified_pe = 16.09 + 9.30 * beta + 51.92 * growth + 7.53 * payout

        actual_pe = stats.trailing_pe
        discount = (justified_pe - actual_pe) / justified_pe if justified_pe > 0 else 0.0

        result.notes.append(
            f"Justified PE: {justified_pe:.1f} | Actual PE: {actual_pe:.1f} | "
            f"Discount: {discount:.1%}"
        )
        result.notes.append(
            f"Inputs: Beta={beta:.2f}, Growth={growth:.1%}, Payout={payout:.1%}"
        )

        if discount > 0.30:
            result.score = 100.0
            result.passed_criteria.append(f"Trading at {discount:.0%} discount to justified PE")
        elif discount > 0.15:
            result.score = 80.0
            result.passed_criteria.append(f"Trading at {discount:.0%} discount to justified PE")
        elif discount > 0.05:
            result.score = 60.0
            result.passed_criteria.append(f"Modest {discount:.0%} discount to justified PE")
        elif discount > -0.05:
            result.score = 45.0
            result.notes.append("Trading near justified PE -- fairly valued")
        elif discount > -0.20:
            result.score = 25.0
            result.failed_criteria.append(f"Trading at {-discount:.0%} premium to justified PE")
        else:
            result.score = 10.0
            result.failed_criteria.append(f"Trading at {-discount:.0%} premium -- expensive")

        return result

    # -------------------------------------------------------------------
    # Lens 5: Capital Signals (insider transactions, buybacks)
    # -------------------------------------------------------------------
    def _lens_capital_signals(self, data: FundamentalData) -> LensResult:
        result = LensResult(
            name="Capital Signals",
            score=50.0,  # neutral baseline
            weight=self.LENS_WEIGHTS["capital_signals"] / 100.0,
        )

        adjustment = 0.0

        # Analyze insider transactions
        purchase_count = 0
        sell_count = 0
        purchase_value = 0.0
        sell_value = 0.0

        for txn in data.insider_transactions:
            txn_type = txn.transaction_type.lower()
            if any(kw in txn_type for kw in ["purchase", "buy", "acquisition"]):
                purchase_count += 1
                purchase_value += abs(txn.value)
            elif any(kw in txn_type for kw in ["sale", "sell", "disposition"]):
                sell_count += 1
                sell_value += abs(txn.value)

        if purchase_count > 3:
            adjustment += 20.0
            result.passed_criteria.append(
                f"{purchase_count} insider purchases (${purchase_value:,.0f})"
            )
        elif purchase_count > 0:
            adjustment += 10.0
            result.passed_criteria.append(
                f"{purchase_count} insider purchase(s) (${purchase_value:,.0f})"
            )

        if sell_count > 5:
            adjustment -= 15.0
            result.failed_criteria.append(
                f"{sell_count} insider sales (${sell_value:,.0f})"
            )
        elif sell_count > 2:
            adjustment -= 8.0
            result.failed_criteria.append(
                f"{sell_count} insider sales (${sell_value:,.0f})"
            )

        # Buyback signal
        if data.cash_flows:
            cf = data.cash_flows[0]
            if cf.repurchase_of_stock > 0 and data.stats.market_cap > 0:
                buyback_pct = cf.repurchase_of_stock / data.stats.market_cap
                if buyback_pct > 0.03:
                    adjustment += 15.0
                    result.passed_criteria.append(
                        f"Significant buybacks ({buyback_pct:.1%} of mkt cap)"
                    )
                elif buyback_pct > 0.01:
                    adjustment += 8.0
                    result.passed_criteria.append(
                        f"Moderate buybacks ({buyback_pct:.1%} of mkt cap)"
                    )

            # Equity issuance / dilution
            if cf.issuance_of_stock > 0 and data.stats.market_cap > 0:
                dilution_pct = cf.issuance_of_stock / data.stats.market_cap
                if dilution_pct > 0.02:
                    adjustment -= 12.0
                    result.failed_criteria.append(
                        f"Equity dilution ({dilution_pct:.1%} of mkt cap)"
                    )

        result.score = max(0.0, min(100.0, 50.0 + adjustment))
        result.notes.append(f"Capital signal adjustment: {adjustment:+.0f} pts")

        return result

    # -------------------------------------------------------------------
    # Lens 6: Historical Return Benchmark
    # -------------------------------------------------------------------
    def _lens_historical(self, data: FundamentalData) -> LensResult:
        result = LensResult(
            name="Historical Returns",
            score=0.0,
            weight=self.LENS_WEIGHTS["historical"] / 100.0,
        )

        if data.listing_years < 3:
            result.notes.append(
                f"Only {data.listing_years:.1f} years of history -- insufficient track record"
            )
            result.score = 50.0  # neutral for short history
            return result

        cagr = data.cagr

        # CRSP benchmarks (1925-2023, Bessembinder methodology)
        # Median stock: -0.84% CAGR. Only 46.7% produce positive lifetime return.
        crsp_percentiles = [
            (0.722, 99), (0.336, 95), (0.221, 90), (0.114, 75),
            (-0.0084, 50), (-0.302, 25),
        ]

        percentile = 5.0  # default low
        for threshold, pct in crsp_percentiles:
            if cagr >= threshold:
                percentile = float(pct)
                break

        result.notes.append(
            f"CAGR: {cagr:.1%} over {data.listing_years:.0f} years | "
            f"CRSP percentile: {percentile:.0f}%"
        )

        if percentile >= 90:
            result.score = 100.0
            result.passed_criteria.append(f"Top decile historical compounder ({percentile:.0f}th pct)")
        elif percentile >= 75:
            result.score = 85.0
            result.passed_criteria.append(f"Top quartile returns ({percentile:.0f}th pct)")
        elif percentile >= 50:
            result.score = 60.0
            result.passed_criteria.append(f"Above-median returns ({percentile:.0f}th pct)")
        elif percentile >= 25:
            result.score = 30.0
            result.failed_criteria.append(f"Below-median returns ({percentile:.0f}th pct)")
        else:
            result.score = 10.0
            result.failed_criteria.append(f"Bottom quartile ({percentile:.0f}th pct)")

        return result

    # -------------------------------------------------------------------
    # Batch scoring
    # -------------------------------------------------------------------
    def score_universe(self, data_list: list[FundamentalData]) -> list[SixfoldScore]:
        scores = []
        for data in data_list:
            try:
                scores.append(self.score(data))
            except Exception:
                log.exception("Failed to score %s", data.symbol)
                scores.append(SixfoldScore(
                    symbol=data.symbol,
                    composite_score=0.0,
                    confidence=ConfidenceTier.TRIAGE,
                    in_scope=False,
                    out_of_scope_reason="Scoring error",
                ))
        return sorted(scores, key=lambda s: s.composite_score, reverse=True)

    def format_report(self, score: SixfoldScore) -> str:
        lines = [
            f"{'='*60}",
            f"SIXFOLD Analysis: {score.symbol}",
            f"{'='*60}",
            f"Composite Score: {score.composite_score:.1f}/100 ({score.confidence.value})",
            f"Sector: {score.sector}",
        ]

        if not score.in_scope:
            lines.append(f"OUT OF SCOPE: {score.out_of_scope_reason}")
            return "\n".join(lines)

        lines.append("")
        for lr in score.lens_results:
            lines.append(f"  [{lr.name}] Score: {lr.score:.0f} (weight: {lr.weight:.0%})")
            for c in lr.passed_criteria:
                lines.append(f"    + {c}")
            for c in lr.failed_criteria:
                lines.append(f"    - {c}")
            for n in lr.notes:
                lines.append(f"    > {n}")

        if score.would_raise:
            lines.append("\nWould raise score:")
            for item in score.would_raise:
                lines.append(f"  ^ {item}")

        if score.would_lower:
            lines.append("\nWould lower score:")
            for item in score.would_lower:
                lines.append(f"  v {item}")

        if score.historical_percentile is not None:
            lines.append(f"\nHistorical CRSP percentile: {score.historical_percentile:.0f}%")

        lines.append(f"{'='*60}")
        return "\n".join(lines)
