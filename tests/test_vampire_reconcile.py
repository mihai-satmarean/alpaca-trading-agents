"""The engine must know the position it actually has.

_net_position is a process-local counter. Every restart reset it to zero while
the broker still held the shares, so the notional cap measured a position that
had stopped being real. Ten restarts in one morning took a $20,000 sleeve to
$137,000 of exposure without the cap ever failing a check: it was checking the
wrong number.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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
        # A terminal status matters: without it _submit polls to timeout, which
        # is correct behaviour and makes the suite wall-clock bound.
        o.status = "filled"
        o.filled_qty = str(qty if fill is None else fill)
        o.id = "test-order"
        return o

    c.market_order.side_effect = _order
    return c



def _engine(max_notional=6_667.0, max_position=100):
    cfg = VampireConfig(symbol="SPY", tick_threshold=0.02, position_size=10,
                        max_position=max_position, max_daily_loss=1e9,
                        max_notional=max_notional)
    e = VampireEngine(_filling_client(), MagicMock(), MagicMock(), cfg)
    e._is_market_hours = lambda: True
    return e


class TestReconcile:
    def test_adopts_a_long_broker_position(self):
        e = _engine()
        e.reconcile(qty=25, avg_entry=700.0)
        assert e.net_position == 25 and e.avg_entry == 700.0

    def test_adopts_a_short_broker_position(self):
        e = _engine()
        e.reconcile(qty=-47, avg_entry=714.0)
        assert e.net_position == -47

    def test_flat_broker_clears_the_basis(self):
        e = _engine()
        e.reconcile(qty=25, avg_entry=700.0)
        e.reconcile(qty=0, avg_entry=None)
        assert e.net_position == 0 and e.avg_entry is None

    def test_missing_avg_entry_keeps_the_position(self):
        e = _engine()
        e.reconcile(qty=10, avg_entry=None)
        assert e.net_position == 10


class TestCapHoldsAcrossARestart:
    """The regression that cost us $137,000 of unintended exposure."""

    def test_a_restart_that_adopts_the_position_refuses_to_add(self):
        """Already past the cap, so the only correct number of new shorts is
        zero. The position cannot shrink by adding to it."""
        e = _engine(max_notional=6_667.0)
        e.reconcile(qty=-47, avg_entry=714.0)          # what the broker holds
        for _ in range(20):
            e.tick(714.0, vwap=713.0)                  # would open more shorts
        assert e.net_position == -47

    def test_without_reconciling_a_fresh_engine_opens_a_whole_new_cap(self):
        """The defect, exactly: a fresh process believes it is flat and is
        therefore entitled to the full cap again. Ten restarts bought ten caps,
        which is how SPY reached -80 against an 8-share limit."""
        e = _engine(max_notional=6_667.0)
        e.cfg.position_size = 8                        # what sizing sets at $767
        assert e.net_position == 0                     # the false premise
        for _ in range(20):
            e.tick(767.0, vwap=766.0)
        assert e.net_position == -8                    # one full cap, from nothing
        assert abs(e.net_position) * 767.0 <= 6_667.0  # and it stops there

    def test_an_already_oversized_position_opens_nothing(self):
        e = _engine(max_notional=6_667.0)
        e.reconcile(qty=-80, avg_entry=766.0)          # far past the cap
        before = e.net_position
        for _ in range(10):
            e.tick(766.0, vwap=765.0)
        assert e.net_position == before                # no new shorts

    def test_an_oversized_position_can_still_be_reduced(self):
        """A cap that blocks risk reduction is worse than no cap."""
        e = _engine(max_notional=6_667.0)
        e.reconcile(qty=-80, avg_entry=766.0)
        e.tick(765.0, vwap=766.0)                      # cover direction
        assert e.net_position > -80


class TestAgentReconcilesOnStart:
    def _agent(self, broker_positions):
        from src.agents.vampire import VampireAgent

        client, data, tracker, breaker, allocator = (MagicMock() for _ in range(5))
        data.get_latest_quote.return_value = MagicMock(mid=700.0)
        allocator.get_budget.return_value = MagicMock(vampire_budget=20_000.0)
        tracker.get_snapshot.return_value = MagicMock(positions=broker_positions)
        return VampireAgent(client, data, tracker, breaker, allocator,
                            symbols=["SPY", "QQQ"])

    def test_engines_adopt_the_broker_position_at_start(self):
        a = self._agent({"SPY": {"qty": -80, "avg_entry_price": 766.0,
                                 "market_value": -61_280.0}})
        a._apply_sleeve_limits()
        assert a._engines["SPY"].net_position == -80

    def test_a_symbol_with_no_position_starts_flat(self):
        a = self._agent({"SPY": {"qty": -80, "avg_entry_price": 766.0,
                                 "market_value": -61_280.0}})
        a._apply_sleeve_limits()
        assert a._engines["QQQ"].net_position == 0

    def test_an_unreadable_book_does_not_crash_startup(self):
        a = self._agent({})
        a._tracker.get_snapshot.side_effect = RuntimeError("broker down")
        a._apply_sleeve_limits()
        assert a._engines["SPY"].net_position == 0
