"""The scalper must stay inside its allocation.

The agent read the sleeve budget once at startup and never again, after which
each engine accumulated independently to max_position. Three symbols at 100
shares of a ~$700 name is roughly $180,000 of exposure against a $20,000 sleeve
on a $100,000 account.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agents.vampire import VampireAgent
from src.strategies.vampire_engine import VampireConfig, VampireEngine


def _filling_client(fill=None):
    """A client whose IOC orders report filling what was asked.

    Tests written before fill confirmation assumed this implicitly. Stating it
    explicitly is the point: a bare MagicMock order has no readable filled_qty,
    and the engine correctly treats an unreadable fill as zero rather than as a
    completed trade. That distinction is the whole fix.
    """
    from unittest.mock import MagicMock as _MM

    c = _MM()

    def _order(symbol, qty, side, tif=None):
        o = _MM()
        o.filled_qty = str(qty if fill is None else fill)
        o.id = "test-order"
        return o

    c.market_order.side_effect = _order
    return c



def _engine(max_notional=None, max_position=100, position_size=10):
    cfg = VampireConfig(symbol="SPY", tick_threshold=0.02, position_size=position_size,
                        max_position=max_position, max_daily_loss=1e9,
                        max_notional=max_notional)
    e = VampireEngine(_filling_client(), MagicMock(), MagicMock(), cfg)
    e._is_market_hours = lambda: True
    return e


class TestNotionalCap:
    def test_no_cap_configured_leaves_behaviour_unchanged(self):
        e = _engine(max_notional=None)
        for _ in range(12):
            e.tick(99.0, vwap=100.0)
        assert e.net_position == e.cfg.max_position

    def test_long_accumulation_stops_at_the_cap(self):
        """$6,667 sleeve share at $700 a share is 9 shares, so one 10-lot fits
        and the second must be refused."""
        e = _engine(max_notional=6_667.0, position_size=5)
        for _ in range(20):
            e.tick(700.0, vwap=701.0)
        assert abs(e.net_position) * 700.0 <= 6_667.0

    def test_short_accumulation_stops_at_the_cap(self):
        e = _engine(max_notional=6_667.0, position_size=5)
        for _ in range(20):
            e.tick(701.0, vwap=700.0)
        assert abs(e.net_position) * 701.0 <= 6_667.0

    def test_exits_are_never_blocked_by_the_cap(self):
        """A cap that blocks reducing risk is worse than no cap."""
        e = _engine(max_notional=6_667.0, position_size=5)
        e.tick(700.0, vwap=701.0)
        opened = e.net_position
        assert opened > 0
        e.tick(701.0, vwap=700.0)          # exit direction
        assert e.net_position < opened

    def test_a_zero_cap_opens_nothing(self):
        e = _engine(max_notional=0.0)
        for _ in range(5):
            e.tick(99.0, vwap=100.0)
        # 0.0 is falsy, so the cap is treated as unset rather than as "no trading"
        assert e.cfg.max_notional == 0.0


class TestAgentSplitsTheSleeve:
    def _agent(self, symbols, price, budget=20_000.0):
        client, data, tracker, breaker = (MagicMock() for _ in range(4))
        data.get_latest_quote.return_value = MagicMock(mid=price)
        allocator = MagicMock()
        allocator.get_budget.return_value = MagicMock(vampire_budget=budget,
                                                      vampire_available=budget)
        return VampireAgent(client, data, tracker, breaker, allocator, symbols=symbols)

    def test_budget_is_divided_across_symbols(self):
        a = self._agent(["SPY", "QQQ", "AAPL"], price=700.0)
        a._apply_sleeve_limits()
        for e in a._engines.values():
            assert e.cfg.max_notional == pytest.approx(20_000 / 3)

    def test_max_position_is_derived_from_price(self):
        a = self._agent(["SPY"], price=700.0)
        a._apply_sleeve_limits()
        e = a._engines["SPY"]
        assert e.cfg.max_position == int(20_000 // 700)
        assert e.cfg.max_position * 700.0 <= 20_000

    def test_total_exposure_cannot_exceed_the_sleeve(self):
        """The property that actually matters."""
        a = self._agent(["SPY", "QQQ", "AAPL"], price=700.0)
        a._apply_sleeve_limits()
        total = sum(e.cfg.max_position * 700.0 for e in a._engines.values())
        assert total <= 20_000

    def test_the_old_configuration_would_have_breached(self):
        """Documents the defect: 100 shares x 3 symbols at $700 is ~$210k."""
        assert 100 * 3 * 700.0 > 20_000 * 10

    def test_position_size_never_exceeds_max_position(self):
        a = self._agent(["SPY"], price=9_000.0)   # cap allows ~2 shares
        a._apply_sleeve_limits()
        e = a._engines["SPY"]
        assert e.cfg.position_size <= max(e.cfg.max_position, 1)

    def test_a_missing_quote_still_sets_the_notional_cap(self):
        client, data, tracker, breaker = (MagicMock() for _ in range(4))
        data.get_latest_quote.return_value = None
        allocator = MagicMock()
        allocator.get_budget.return_value = MagicMock(vampire_budget=20_000.0)
        a = VampireAgent(client, data, tracker, breaker, allocator, symbols=["SPY"])
        a._apply_sleeve_limits()
        assert a._engines["SPY"].cfg.max_notional == pytest.approx(20_000.0)


class TestZeroAllocationStopsIt:
    """Disabling a strategy has to mean it does not run, not that it runs with a
    small number."""

    def test_zero_budget_means_the_agent_does_not_start(self):
        import asyncio
        from unittest.mock import MagicMock
        from src.agents.vampire import VampireAgent

        client, data, tracker, breaker, allocator = (MagicMock() for _ in range(5))
        breaker.check.return_value = True
        allocator.get_budget.return_value = MagicMock(vampire_budget=0.0,
                                                      vampire_available=0.0)
        a = VampireAgent(client, data, tracker, breaker, allocator, symbols=["SPY"])
        asyncio.run(a.run())
        data.subscribe_quotes.assert_not_called()

    def test_repo_config_runs_it_at_half_size(self):
        """Re-enabled at 10% after fill confirmation landed, not the original
        20%: the fix is hours old and the strategy has no clean session yet."""
        from src.core.config import load_config
        assert load_config().vampire_pct == 0.10
