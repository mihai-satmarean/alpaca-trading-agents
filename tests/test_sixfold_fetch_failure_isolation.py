"""A fundamentals fetch that fails must not become a score of zero.

No production code changed for this file. It locks in behaviour this build
already had, because the same framework running on live money did NOT have it
and the consequence was severe: there, a failed fetch produced a placeholder
score of 0, the disposal rule read 0 as "score collapsed", and one SEC EDGAR
outage would have market-sold every position in the book and locked each one
out of re-entry for eight weeks.

This build is immune by construction -- SixfoldAnalystAgent.scan skips a symbol
it could not fetch, so the symbol is never scored and never reaches the
disposal set. That immunity is load-bearing and easy to lose to a well-meaning
refactor that "handles" the exception by appending a placeholder, so it is
asserted here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.agents.sixfold_analyst import SixfoldAnalystAgent
from src.core.financial_data import BalanceSheet, FundamentalData, IncomeStatement


def _healthy(symbol: str) -> FundamentalData:
    return FundamentalData(
        symbol=symbol,
        sector="Technology",
        income_statements=[IncomeStatement(
            revenue=200_000_000_000,
            gross_profit=140_000_000_000,
            operating_income=110_000_000_000,
            net_income=80_000_000_000,
            tax_provision=15_000_000_000,
        )],
        balance_sheets=[BalanceSheet(
            stockholders_equity=200_000_000_000,
            cash_and_equivalents=70_000_000_000,
            current_assets=150_000_000_000,
            current_liabilities=100_000_000_000,
            net_ppe=100_000_000_000,
        )],
    )


def _agent_with_one_broken_symbol():
    agent = SixfoldAnalystAgent(MagicMock(), universe=["BROKEN", "MSFT"])

    def fetch(symbol, force_refresh=False):
        if symbol == "BROKEN":
            raise RuntimeError("EDGAR outage")
        return _healthy(symbol)

    provider = MagicMock()
    provider.get_fundamentals.side_effect = fetch
    agent._data_provider = provider
    return agent


class TestAFailedFetchIsNotAScoreOfZero:

    def test_the_symbol_is_skipped_not_scored(self):
        agent = _agent_with_one_broken_symbol()

        agent.scan()

        assert "BROKEN" not in agent.scores

    def test_it_never_reaches_the_disposal_set(self):
        """The consequence that matters: a name nobody could score must not be
        proposed for sale."""
        agent = _agent_with_one_broken_symbol()

        agent.scan()

        assert "BROKEN" not in agent.get_disposal_candidates()

    def test_the_healthy_symbols_are_unaffected(self):
        """One bad symbol must not take the scan down with it."""
        agent = _agent_with_one_broken_symbol()

        recommendations = agent.scan()

        assert [r.symbol for r in recommendations] == ["MSFT"]
