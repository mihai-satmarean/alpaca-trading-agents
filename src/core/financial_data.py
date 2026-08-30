"""Financial data provider for SIXFOLD analysis.

Wraps yfinance to provide fundamental data, insider transactions,
and historical returns for the SIXFOLD equity analysis framework.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import yfinance as yf

log = logging.getLogger(__name__)


@dataclass
class IncomeStatement:
    revenue: float = 0.0
    cost_of_revenue: float = 0.0
    gross_profit: float = 0.0
    sga_expense: float = 0.0
    operating_income: float = 0.0
    net_income: float = 0.0
    ebitda: float = 0.0
    interest_expense: float = 0.0
    tax_provision: float = 0.0
    basic_eps: float = 0.0


@dataclass
class BalanceSheet:
    total_assets: float = 0.0
    total_liabilities: float = 0.0
    stockholders_equity: float = 0.0
    total_debt: float = 0.0
    long_term_debt: float = 0.0
    cash_and_equivalents: float = 0.0
    current_assets: float = 0.0
    current_liabilities: float = 0.0
    goodwill: float = 0.0
    intangible_assets: float = 0.0
    net_ppe: float = 0.0
    total_capitalization: float = 0.0


@dataclass
class CashFlow:
    operating_cash_flow: float = 0.0
    capital_expenditures: float = 0.0
    free_cash_flow: float = 0.0
    repurchase_of_stock: float = 0.0
    issuance_of_stock: float = 0.0
    dividends_paid: float = 0.0


@dataclass
class KeyStats:
    market_cap: float = 0.0
    enterprise_value: float = 0.0
    trailing_pe: float = 0.0
    forward_pe: float = 0.0
    peg_ratio: float = 0.0
    price_to_sales: float = 0.0
    price_to_book: float = 0.0
    beta: float = 1.0
    dividend_yield: float = 0.0
    payout_ratio: float = 0.0
    earnings_growth: float = 0.0
    revenue_growth: float = 0.0
    profit_margins: float = 0.0
    shares_outstanding: float = 0.0
    current_price: float = 0.0


@dataclass
class InsiderTransaction:
    holder: str = ""
    transaction_type: str = ""
    shares: float = 0.0
    value: float = 0.0
    date: str = ""


@dataclass
class FundamentalData:
    """Complete fundamental data package for a security."""
    symbol: str
    income_statements: list[IncomeStatement] = field(default_factory=list)
    balance_sheets: list[BalanceSheet] = field(default_factory=list)
    cash_flows: list[CashFlow] = field(default_factory=list)
    stats: KeyStats = field(default_factory=KeyStats)
    insider_transactions: list[InsiderTransaction] = field(default_factory=list)
    eps_history: list[float] = field(default_factory=list)
    annual_returns: list[float] = field(default_factory=list)
    listing_years: float = 0.0
    cagr: float = 0.0
    sector: str = ""
    industry: str = ""


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        f = float(val)
        if f != f:  # NaN check
            return default
        return f
    except (ValueError, TypeError):
        return default


class FinancialDataProvider:
    """Fetch fundamental data from yfinance."""

    def __init__(self):
        self._cache: dict[str, FundamentalData] = {}

    def get_fundamentals(self, symbol: str, force_refresh: bool = False) -> FundamentalData:
        if symbol in self._cache and not force_refresh:
            return self._cache[symbol]

        log.info("Fetching fundamentals for %s", symbol)
        ticker = yf.Ticker(symbol)
        data = FundamentalData(symbol=symbol)

        try:
            info = ticker.info or {}
            data.sector = info.get("sector", "")
            data.industry = info.get("industry", "")
            data.stats = self._extract_stats(info)
        except Exception:
            log.warning("Failed to fetch info for %s", symbol)

        try:
            data.income_statements = self._extract_income(ticker)
        except Exception:
            log.warning("Failed to fetch income statements for %s", symbol)

        try:
            data.balance_sheets = self._extract_balance(ticker)
        except Exception:
            log.warning("Failed to fetch balance sheets for %s", symbol)

        try:
            data.cash_flows = self._extract_cashflow(ticker)
        except Exception:
            log.warning("Failed to fetch cash flows for %s", symbol)

        try:
            data.eps_history = self._extract_eps_history(ticker)
        except Exception:
            log.warning("Failed to fetch EPS history for %s", symbol)

        try:
            data.insider_transactions = self._extract_insiders(ticker)
        except Exception:
            log.warning("Failed to fetch insider data for %s", symbol)

        try:
            returns_data = self._compute_historical_returns(ticker)
            data.annual_returns = returns_data["annual_returns"]
            data.listing_years = returns_data["years"]
            data.cagr = returns_data["cagr"]
        except Exception:
            log.warning("Failed to compute historical returns for %s", symbol)

        self._cache[symbol] = data
        return data

    def _extract_stats(self, info: dict) -> KeyStats:
        return KeyStats(
            market_cap=_safe_float(info.get("marketCap")),
            enterprise_value=_safe_float(info.get("enterpriseValue")),
            trailing_pe=_safe_float(info.get("trailingPE")),
            forward_pe=_safe_float(info.get("forwardPE")),
            peg_ratio=_safe_float(info.get("pegRatio")),
            price_to_sales=_safe_float(info.get("priceToSalesTrailing12Months")),
            price_to_book=_safe_float(info.get("priceToBook")),
            beta=_safe_float(info.get("beta"), default=1.0),
            dividend_yield=_safe_float(info.get("dividendYield")),
            payout_ratio=_safe_float(info.get("payoutRatio")),
            earnings_growth=_safe_float(info.get("earningsGrowth")),
            revenue_growth=_safe_float(info.get("revenueGrowth")),
            profit_margins=_safe_float(info.get("profitMargins")),
            shares_outstanding=_safe_float(info.get("sharesOutstanding")),
            current_price=_safe_float(
                info.get("currentPrice") or info.get("regularMarketPrice")
            ),
        )

    def _extract_income(self, ticker: yf.Ticker) -> list[IncomeStatement]:
        df = ticker.income_stmt
        if df is None or df.empty:
            return []

        statements = []
        for col in df.columns:
            statements.append(IncomeStatement(
                revenue=_safe_float(df.loc["Total Revenue", col] if "Total Revenue" in df.index else None),
                cost_of_revenue=_safe_float(df.loc["Cost Of Revenue", col] if "Cost Of Revenue" in df.index else None),
                gross_profit=_safe_float(df.loc["Gross Profit", col] if "Gross Profit" in df.index else None),
                sga_expense=_safe_float(df.loc["Selling General And Administration", col] if "Selling General And Administration" in df.index else None),
                operating_income=_safe_float(df.loc["Operating Income", col] if "Operating Income" in df.index else None),
                net_income=_safe_float(df.loc["Net Income", col] if "Net Income" in df.index else None),
                ebitda=_safe_float(df.loc["EBITDA", col] if "EBITDA" in df.index else None),
                interest_expense=_safe_float(df.loc["Interest Expense", col] if "Interest Expense" in df.index else None),
                tax_provision=_safe_float(df.loc["Tax Provision", col] if "Tax Provision" in df.index else None),
                basic_eps=_safe_float(df.loc["Basic EPS", col] if "Basic EPS" in df.index else None),
            ))
        return statements

    def _extract_balance(self, ticker: yf.Ticker) -> list[BalanceSheet]:
        df = ticker.balance_sheet
        if df is None or df.empty:
            return []

        sheets = []
        for col in df.columns:
            def g(key):
                return _safe_float(df.loc[key, col] if key in df.index else None)

            sheets.append(BalanceSheet(
                total_assets=g("Total Assets"),
                total_liabilities=g("Total Liabilities Net Minority Interest"),
                stockholders_equity=g("Stockholders Equity"),
                total_debt=g("Total Debt"),
                long_term_debt=g("Long Term Debt"),
                cash_and_equivalents=g("Cash And Cash Equivalents"),
                current_assets=g("Current Assets"),
                current_liabilities=g("Current Liabilities"),
                goodwill=g("Goodwill"),
                intangible_assets=g("Other Intangible Assets"),
                net_ppe=g("Net PPE"),
                total_capitalization=g("Total Capitalization"),
            ))
        return sheets

    def _extract_cashflow(self, ticker: yf.Ticker) -> list[CashFlow]:
        df = ticker.cashflow
        if df is None or df.empty:
            return []

        flows = []
        for col in df.columns:
            def g(key):
                return _safe_float(df.loc[key, col] if key in df.index else None)

            op_cf = g("Operating Cash Flow")
            capex = g("Capital Expenditure")
            flows.append(CashFlow(
                operating_cash_flow=op_cf,
                capital_expenditures=abs(capex) if capex else 0.0,
                free_cash_flow=g("Free Cash Flow"),
                repurchase_of_stock=abs(g("Repurchase Of Capital Stock")),
                issuance_of_stock=g("Issuance Of Capital Stock"),
                dividends_paid=abs(g("Common Stock Dividend Paid")),
            ))
        return flows

    def _extract_eps_history(self, ticker: yf.Ticker) -> list[float]:
        df = ticker.income_stmt
        if df is None or df.empty:
            return []

        eps_values = []
        for col in df.columns:
            if "Basic EPS" in df.index:
                eps = _safe_float(df.loc["Basic EPS", col])
                eps_values.append(eps)
        return eps_values

    def _extract_insiders(self, ticker: yf.Ticker) -> list[InsiderTransaction]:
        try:
            df = ticker.insider_transactions
            if df is None or df.empty:
                return []
        except Exception:
            return []

        transactions = []
        for _, row in df.head(50).iterrows():
            transactions.append(InsiderTransaction(
                holder=str(row.get("Insider", "")),
                transaction_type=str(row.get("Text", row.get("Transaction", ""))),
                shares=_safe_float(row.get("Shares", 0)),
                value=_safe_float(row.get("Value", 0)),
                date=str(row.get("Start Date", row.get("Date", ""))),
            ))
        return transactions

    def _compute_historical_returns(self, ticker: yf.Ticker) -> dict:
        hist = ticker.history(period="max", interval="1mo")
        if hist.empty or len(hist) < 12:
            return {"annual_returns": [], "years": 0.0, "cagr": 0.0}

        close = hist["Close"]
        years = len(close) / 12.0

        annual_returns = []
        for i in range(12, len(close), 12):
            yr_ret = (close.iloc[i] / close.iloc[i - 12]) - 1.0
            annual_returns.append(yr_ret)

        start_price = close.iloc[0]
        end_price = close.iloc[-1]
        if start_price > 0 and years > 0:
            cagr = (end_price / start_price) ** (1 / years) - 1.0
        else:
            cagr = 0.0

        return {
            "annual_returns": annual_returns,
            "years": years,
            "cagr": cagr,
        }

    def get_market_context(self) -> dict:
        """Retrieve current market-level valuation context (S&P 500)."""
        spy = yf.Ticker("SPY")
        info = spy.info or {}

        hist_5y = spy.history(period="5y", interval="1mo")
        hist_10y = spy.history(period="10y", interval="1mo")

        current_pe = _safe_float(info.get("trailingPE"))

        pe_5y_avg = None
        pe_10y_avg = None
        if not hist_5y.empty:
            try:
                returns_5y = (hist_5y["Close"].iloc[-1] / hist_5y["Close"].iloc[0]) - 1.0
            except Exception:
                returns_5y = 0.0
        else:
            returns_5y = 0.0

        return {
            "index": "SPY",
            "current_price": _safe_float(info.get("regularMarketPrice")),
            "trailing_pe": current_pe,
            "forward_pe": _safe_float(info.get("forwardPE")),
            "52w_high": _safe_float(info.get("fiftyTwoWeekHigh")),
            "52w_low": _safe_float(info.get("fiftyTwoWeekLow")),
            "5y_return": returns_5y,
            "as_of": date.today().isoformat(),
        }
