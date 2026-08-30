"""Tests for options chain filtering and strategy scoring."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from src.core.options_chain import OptionsChain, OptionCandidate


@pytest.fixture
def sample_puts():
    today = date.today()
    return [
        OptionCandidate(
            symbol="SPY260930P00520000",
            underlying="SPY",
            contract_type="put",
            strike_price=520.0,
            expiration=today + timedelta(days=30),
            open_interest=500,
            premium_estimate=None,
            days_to_expiry=30,
        ),
        OptionCandidate(
            symbol="SPY260930P00510000",
            underlying="SPY",
            contract_type="put",
            strike_price=510.0,
            expiration=today + timedelta(days=30),
            open_interest=200,
            premium_estimate=None,
            days_to_expiry=30,
        ),
        OptionCandidate(
            symbol="SPY260930P00490000",
            underlying="SPY",
            contract_type="put",
            strike_price=490.0,
            expiration=today + timedelta(days=30),
            open_interest=50,
            premium_estimate=None,
            days_to_expiry=30,
        ),
    ]


class TestOptionsChainFilter:
    def test_filter_by_otm_pct(self, sample_puts):
        chain = OptionsChain.__new__(OptionsChain)
        current_price = 550.0

        result = chain.filter_by_otm_pct(sample_puts, current_price, max_otm_pct=0.08)
        assert len(result) == 2
        strikes = [c.strike_price for c in result]
        assert 520.0 in strikes
        assert 510.0 in strikes
        assert 490.0 not in strikes

    def test_filter_by_open_interest(self, sample_puts):
        chain = OptionsChain.__new__(OptionsChain)
        result = chain.filter_by_open_interest(sample_puts, min_oi=100)
        assert len(result) == 2
        assert all(c.open_interest >= 100 for c in result)

    def test_select_best_expiry(self, sample_puts):
        chain = OptionsChain.__new__(OptionsChain)
        result = chain.select_best_expiry(sample_puts, target_dte=30)
        assert len(result) == 3

    def test_empty_candidates(self):
        chain = OptionsChain.__new__(OptionsChain)
        assert chain.filter_by_otm_pct([], 100, 0.1) == []
        assert chain.filter_by_open_interest([], 100) == []
        assert chain.select_best_expiry([], 30) == []
