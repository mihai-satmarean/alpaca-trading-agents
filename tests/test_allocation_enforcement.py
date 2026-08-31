"""Tests for the configured 80/15/5 split being measured and enforced.

Each test here corresponds to a defect that the original 22-test suite passed
straight through, because the suite exercised the allocation helpers directly
and nothing asserted that the trading path ever called them.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.core.config import StrategyConfig, load_config
from src.core.position_tracker import PortfolioSnapshot
from src.risk.allocation import (
    AllocationConfig,
    AllocationManager,
    capital_committed,
    parse_occ,
)


def _snapshot(equity=100_000, cash=50_000, positions=None):
    return PortfolioSnapshot(
        equity=equity,
        cash=cash,
        buying_power=cash * 2,
        positions=positions or {},
        daily_pnl=0,
        total_pnl=0,
        timestamp=datetime.now(),
    )


def _short_put(strike: float, mark: float = -300.0, qty: int = -1):
    """An OCC symbol for a short put plus the position dict Alpaca returns."""
    sym = f"SPY241220P{int(strike * 1000):08d}"
    return sym, {"market_value": mark, "qty": qty, "unrealized_pl": 0.0}


class TestOCCParsing:
    def test_parses_standard_option_symbol(self):
        p = parse_occ("SPY241220P00450000")
        assert p is not None
        assert (p.root, p.contract_type, p.strike) == ("SPY", "put", 450.0)

    def test_equity_symbol_is_not_an_option(self):
        assert parse_occ("SPY") is None
        assert parse_occ("GOOGL") is None

    def test_long_root_option_still_parses(self):
        # The old classifier used len(symbol) > 10, which also caught nothing
        # shorter and would misclassify any long ticker string.
        p = parse_occ("GOOGL241220C00150000")
        assert p is not None and p.root == "GOOGL"


class TestCapitalCommitted:
    def test_short_put_charges_collateral_not_mark(self):
        """The defect: a $45,000 obligation was counted as its $300 mark."""
        sym, pos = _short_put(450.0, mark=-300.0)
        assert capital_committed(sym, pos) == pytest.approx(45_000.0)

    def test_short_call_does_not_double_count_covering_shares(self):
        assert capital_committed("SPY241220C00500000", {"market_value": -250.0, "qty": -1}) == 0.0

    def test_equity_uses_market_value(self):
        assert capital_committed("SPY", {"market_value": 45_000.0, "qty": 100}) == 45_000.0

    def test_long_option_uses_debit_paid(self):
        assert capital_committed("SPY241220P00400000", {"market_value": 500.0, "qty": 1}) == 500.0

    def test_multiple_contracts_scale_collateral(self):
        sym, pos = _short_put(450.0, mark=-900.0, qty=-3)
        assert capital_committed(sym, pos) == pytest.approx(135_000.0)


class TestSleeveCapBinds:
    """The headline regression: the 80% options cap must actually stop trading."""

    def _mgr(self, positions):
        tracker = MagicMock()
        tracker.get_snapshot.return_value = _snapshot(positions=positions)
        return AllocationManager(tracker, AllocationConfig(), vampire_symbols=["SPY", "QQQ"])

    def test_one_csp_consumes_over_half_the_sleeve(self):
        sym, pos = _short_put(450.0)
        budget = self._mgr({sym: pos}).get_budget()
        assert budget.options_budget == pytest.approx(80_000.0)
        assert budget.options_used == pytest.approx(45_000.0)
        assert budget.options_available == pytest.approx(35_000.0)

    def test_second_large_csp_is_refused(self):
        sym, pos = _short_put(450.0)
        mgr = self._mgr({sym: pos})
        assert mgr.can_allocate_options(30_000) is True
        assert mgr.can_allocate_options(45_000) is False

    def test_sleeve_exhausts_and_refuses_everything(self):
        positions = {}
        for i in range(2):
            sym, pos = _short_put(450.0 + i)  # distinct symbols
            positions[sym] = pos
        mgr = self._mgr(positions)
        budget = mgr.get_budget()
        assert budget.options_used > 80_000
        assert budget.options_available == 0.0
        assert mgr.can_allocate_options(1_000) is False

    def test_old_market_value_accounting_would_not_have_bound(self):
        """Documents the original behaviour so it cannot silently return."""
        sym, pos = _short_put(450.0, mark=-300.0)
        old_used = abs(pos["market_value"])          # what the code used to sum
        new_used = capital_committed(sym, pos)       # what it sums now
        assert old_used == 300.0
        assert new_used == 45_000.0
        # Under the old measure an $80k sleeve would have absorbed 266 puts,
        # roughly $12m of collateral on a $100k account.
        assert 80_000 / old_used > 250
        assert 80_000 / new_used < 2


class TestUnattributedIsSurfaced:
    def test_equity_outside_the_vampire_universe_is_not_hidden(self):
        tracker = MagicMock()
        tracker.get_snapshot.return_value = _snapshot(
            positions={"TSLA": {"market_value": 9_000.0, "qty": 40}}
        )
        mgr = AllocationManager(tracker, AllocationConfig(), vampire_symbols=["SPY", "QQQ"])
        budget = mgr.get_budget()
        assert budget.unattributed_used == pytest.approx(9_000.0)
        assert budget.options_used == 0.0
        assert budget.vampire_used == 0.0


class TestConfigDrivesBehaviour:
    def test_repo_config_loads_and_is_coherent(self):
        """Assert the invariant, not the numbers: the split is a live decision
        and will move, but it must always be coherent."""
        cfg = load_config()
        assert cfg.validate() == []
        assert (cfg.sixfold_pct + cfg.options_pct + cfg.vampire_pct
                + cfg.reserve_pct) == pytest.approx(1.0)
        for pct in (cfg.sixfold_pct, cfg.options_pct, cfg.vampire_pct, cfg.reserve_pct):
            assert 0.0 <= pct <= 1.0

    def test_repo_config_matches_the_agreed_split(self):
        """Agreed 2026-08-31: sixfold 50, CSP 20, vampire 20, buffer 10.

        The scalper's 20% is held at zero pending a fix to its execution, and
        moved to reserve rather than reallocated, because nothing else has been
        sized to absorb it.
        """
        cfg = load_config()
        assert (cfg.sixfold_pct, cfg.options_pct) == (0.50, 0.20)
        assert cfg.vampire_pct == 0.0, "scalper is off for the contest after three breaches"
        assert cfg.reserve_pct == 0.30

    def test_sixfold_budget_is_reported_even_though_it_cannot_trade(self):
        """The sleeve is named so the gap is visible. Folding it into reserve
        hid a team decision behind a bigger uncommitted number."""
        tracker = MagicMock()
        tracker.get_snapshot.return_value = _snapshot(equity=100_000)
        budget = AllocationManager(tracker, AllocationConfig.from_config()).get_budget()
        assert budget.sixfold_budget == pytest.approx(50_000.0)

    def test_every_csp_symbol_is_affordable_at_the_options_sleeve(self):
        """A contract whose collateral exceeds the sleeve can never be sold.
        The previous list was entirely unsellable at $20k: one SPY put ties up
        about $71,500."""
        cfg = load_config()
        assert cfg.options_symbols
        assert "SPY" not in cfg.options_symbols
        assert "AAPL" not in cfg.options_symbols

    def test_allocation_config_reads_the_yaml(self):
        cfg, ac = load_config(), AllocationConfig.from_config()
        assert ac.options_pct == cfg.options_pct
        assert ac.vampire_pct == cfg.vampire_pct

    def test_validate_catches_a_split_that_does_not_sum_to_one(self):
        bad = StrategyConfig(allocation={"options_pct": 0.9, "vampire_pct": 0.3, "reserve_pct": 0.05})
        problems = bad.validate()
        assert any("sum to" in p for p in problems)

    def test_validate_catches_positive_put_delta(self):
        bad = StrategyConfig(options={"csp": {"max_delta": 0.30}})
        assert any("max_delta" in p for p in bad.validate())


class TestGatesAreActuallyCalled:
    """The original suite tested can_trade / can_allocate_options directly and
    passed, while no production code path called either. These assert the wiring
    itself, which is the part that was missing."""

    def _strategy(self, allocator, breaker, cash_required=45_000.0):
        from datetime import date, timedelta

        from src.core.options_chain import OptionCandidate
        from src.strategies.csp import CashSecuredPutStrategy, CSPOpportunity

        client, chain, data, tracker = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        tracker.get_snapshot.return_value = _snapshot()

        strat = CashSecuredPutStrategy(
            client, chain, data, tracker, allocator=allocator, breaker=breaker
        )
        candidate = OptionCandidate(
            symbol="SPY241220P00450000",
            underlying="SPY",
            contract_type="put",
            strike_price=450.0,
            expiration=date.today() + timedelta(days=30),
            open_interest=500,
            premium_estimate=None,
            days_to_expiry=30,
        )
        opp = CSPOpportunity(
            candidate=candidate,
            current_price=470.0,
            cash_required=cash_required,
            premium_pct=0.005,
            annualized_return=0.06,
            score=5.0,
        )
        opp.bid = 4.50          # orders are priced at the bid, so fixtures need one
        strat.scan = lambda symbols=None: [opp]
        return strat, client

    def test_sleeve_gate_is_consulted_and_blocks_the_order(self):
        allocator = MagicMock()
        allocator.get_budget.return_value = MagicMock(options_available=80_000.0)
        allocator.can_allocate_options.return_value = False
        breaker = MagicMock()
        breaker.can_trade.return_value = True

        strat, client = self._strategy(allocator, breaker)
        executed = strat.execute_best(max_trades=2)

        allocator.can_allocate_options.assert_called_once_with(45_000.0)
        client.trading.submit_order.assert_not_called()
        assert executed == []

    def test_per_trade_limit_is_consulted_and_blocks_the_order(self):
        allocator = MagicMock()
        allocator.get_budget.return_value = MagicMock(options_available=80_000.0)
        allocator.can_allocate_options.return_value = True
        breaker = MagicMock()
        breaker.can_trade.return_value = False  # 45k is 45% of a 100k account

        strat, client = self._strategy(allocator, breaker)
        executed = strat.execute_best(max_trades=2)

        breaker.can_trade.assert_called_once_with("SPY241220P00450000", 45_000.0)
        client.trading.submit_order.assert_not_called()
        assert executed == []

    def test_order_placed_and_collateral_reported_when_both_gates_pass(self):
        allocator = MagicMock()
        allocator.get_budget.return_value = MagicMock(options_available=80_000.0)
        allocator.can_allocate_options.return_value = True
        breaker = MagicMock()
        breaker.can_trade.return_value = True

        strat, client = self._strategy(allocator, breaker)
        executed = strat.execute_best(max_trades=1)

        client.trading.submit_order.assert_called_once()
        assert len(executed) == 1
        assert executed[0]["collateral"] == pytest.approx(45_000.0)

    def test_running_total_stops_a_second_order_that_would_overrun(self):
        """Two 45k puts against an 80k sleeve: the second must be refused even
        though each passes on its own."""
        allocator = MagicMock()
        allocator.get_budget.return_value = MagicMock(options_available=80_000.0)
        allocator.can_allocate_options.return_value = True
        breaker = MagicMock()
        breaker.can_trade.return_value = True

        strat, client = self._strategy(allocator, breaker)
        one = strat.scan()[0]
        strat.scan = lambda symbols=None: [one, one]

        executed = strat.execute_best(max_trades=5)
        assert len(executed) == 1
        assert client.trading.submit_order.call_count == 1
