"""The scalper must book what it actually made, and must be able to stop.

Previously `_record_bleed` credited `qty * abs(delta)` on every exit -- the size
of the move that triggered the exit, never the difference between the exit price
and what the position cost. That quantity is unconditionally positive, so daily
P&L could only rise and the max_daily_loss breaker was unreachable by
construction.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.strategies.vampire_engine import VampireConfig, VampireEngine, VampireState

def _filling_client(fill=None):
    """A client whose IOC orders report filling what was asked.

    Tests that predate fill confirmation assumed this implicitly. Making it
    explicit is the point: a MagicMock order has no readable filled_qty, and the
    engine correctly treats an unreadable fill as zero.
    """
    from unittest.mock import MagicMock

    c = MagicMock()

    def order(symbol, qty, side, tif=None):
        o = MagicMock()
        o.status = "filled"
        o.filled_qty = str(qty if fill is None else fill)
        o.id = "test-order"
        return o

    c.market_order.side_effect = order
    return c



def _engine(**kw):
    cfg = VampireConfig(
        symbol="SPY", tick_threshold=0.02, position_size=10,
        max_position=100, max_daily_loss=50.0, **kw
    )
    e = VampireEngine(_filling_client(), MagicMock(), MagicMock(), cfg)
    e._is_market_hours = lambda: True
    return e


class TestRealizedPnl:
    def test_losing_round_trip_is_booked_as_a_loss(self):
        """The headline regression: this used to report +0.50 on a -9.00 trade."""
        e = _engine()
        e.tick(499.95, vwap=500.00)   # long entry @ 499.95
        e.tick(499.05, vwap=499.00)   # long exit  @ 499.05
        assert e.daily_pnl == pytest.approx(-9.00)
        assert e.realized_pnl == pytest.approx(-9.00)

    def test_winning_round_trip(self):
        e = _engine()
        e.tick(499.00, vwap=500.00)
        e.tick(500.00, vwap=499.00)
        assert e.daily_pnl == pytest.approx(10.00)

    def test_short_round_trip_profit(self):
        e = _engine()
        e.tick(500.10, vwap=500.00)   # short entry @ 500.10
        e.tick(499.50, vwap=500.10)   # cover       @ 499.50
        assert e.daily_pnl == pytest.approx(6.00)

    def test_short_round_trip_loss(self):
        """A rising price adds to the short by design, so the loss shows up when
        the book is covered above its average entry."""
        e = _engine()
        e.tick(500.10, vwap=500.00)   # short 10 @ 500.10
        e.tick(500.80, vwap=500.10)   # rising: shorts 10 more @ 500.80
        assert e.net_position == -20
        assert e.avg_entry == pytest.approx(500.45)
        e.tick(500.60, vwap=500.80)   # falling: covers 10 @ 500.60, above avg
        assert e.daily_pnl == pytest.approx(-1.50)

    def test_entries_alone_realize_nothing(self):
        e = _engine()
        e.tick(499.00, vwap=500.00)
        assert e.realized_pnl == 0.0
        assert e.net_position == 10

    def test_average_entry_across_two_lots(self):
        e = _engine()
        e.tick(499.00, vwap=500.00)   # 10 @ 499.00
        e.tick(497.00, vwap=499.00)   # 10 @ 497.00 -> avg 498.00
        assert e.avg_entry == pytest.approx(498.00)
        e.tick(498.50, vwap=497.00)   # sell 10 @ 498.50 against avg 498.00
        assert e.daily_pnl == pytest.approx(5.00)

    def test_flat_position_clears_the_average(self):
        e = _engine()
        e.tick(499.00, vwap=500.00)
        e.tick(500.00, vwap=499.00)
        assert e.net_position == 0
        assert e.avg_entry is None


class TestSideFlipDoesNotInventPnl:
    def test_overfill_through_zero_resets_the_basis(self):
        """The HOOD +$10k-on-5-shares lie: a cover that filled more than the
        short flipped the signed qty while keeping the old average."""
        e = _engine()
        e.tick(500.10, vwap=500.00)          # short 10 @ 500.10
        assert e.net_position == -10
        e._submit = lambda qty, price, side: 20   # cover 10 and flip long 10
        e.tick(499.50, vwap=500.10)
        assert e.net_position == 10
        assert e.avg_entry == pytest.approx(499.50)
        # Closed the 10-share short for +6.00; the new long has no realized P&L.
        assert e.daily_pnl == pytest.approx(6.00)
        assert abs(e.daily_pnl) < 100

    def test_opposite_side_open_does_not_blend_averages(self):
        e = _engine()
        e._net_position = 5
        e._avg_entry = 100.0
        e._open_lot(5, 90.0, long=False)
        assert e.avg_entry == pytest.approx(90.0)


class TestMarkToMarket:
    def test_unrealized_is_signed_correctly_for_a_long(self):
        e = _engine()
        e.tick(499.00, vwap=500.00)          # long 10 @ 499
        assert e.unrealized_pnl(498.00) == pytest.approx(-10.00)
        assert e.unrealized_pnl(500.00) == pytest.approx(10.00)

    def test_unrealized_is_signed_correctly_for_a_short(self):
        e = _engine()
        e.tick(500.10, vwap=500.00)          # short 10 @ 500.10
        assert e.unrealized_pnl(499.10) == pytest.approx(10.00)
        assert e.unrealized_pnl(501.10) == pytest.approx(-10.00)

    def test_flat_book_has_no_unrealized(self):
        assert _engine().unrealized_pnl(500.0) == 0.0


class TestCircuitBreakerIsReachable:
    def test_accumulating_loser_trips_the_breaker(self):
        """A one-way downtrend: the scalper keeps buying dips and never exits,
        so realized P&L stays zero. Marking to market is what makes the limit
        enforceable at all."""
        e = _engine()
        px, ref = 500.0, 500.0
        for _ in range(300):
            e.tick(px, vwap=ref)
            ref, px = px, px - 0.05
            if e.state is VampireState.STOPPED:
                break
        assert e.state is VampireState.STOPPED
        assert e.net_position == 0          # flattened on trip

    def test_breaker_stays_off_for_a_profitable_book(self):
        e = _engine()
        e.tick(499.00, vwap=500.00)
        e.tick(500.00, vwap=499.00)
        assert e.state is not VampireState.STOPPED

    def test_stopped_engine_ignores_further_ticks(self):
        e = _engine()
        e._state = VampireState.STOPPED
        e.tick(400.0, vwap=500.0)
        assert e.net_position == 0

    def test_old_accounting_could_never_trip(self):
        """Documents the defect so it cannot silently return: the previous
        measure was sum(qty * abs(delta)) over exits, which is >= 0 always."""
        e = _engine()
        for i in range(200):
            e.tick(500.0 + (i % 2) * 0.5, vwap=500.0)
        old_style = sum(b.qty * abs(b.delta) for b in e.bleeds if b.action.endswith("_exit"))
        assert old_style >= 0
        assert not (old_style <= -e.cfg.max_daily_loss)


class TestResetDaily:
    def test_reset_clears_pnl_and_position_basis(self):
        e = _engine()
        e.tick(499.00, vwap=500.00)
        e.tick(500.00, vwap=499.00)
        e.reset_daily()
        assert e.daily_pnl == 0.0
        assert e.realized_pnl == 0.0
        assert e.avg_entry is None
