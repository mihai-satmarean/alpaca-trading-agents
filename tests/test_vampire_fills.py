"""The engine must count what filled, not what it asked for.

_buy/_sell fire IOC market orders, swallow exceptions, and the caller then
updated _net_position as though every order filled. IOC orders frequently do
not fill. The counter stayed inside an 8-share cap all session while the broker
position reached 96, because the engine believed it had sold what it still held
and bought again.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.strategies.vampire_engine import VampireConfig, VampireEngine


def _engine(fill_map=None):
    cfg = VampireConfig(symbol="SPY", tick_threshold=0.02, position_size=10,
                        max_position=100, max_daily_loss=1e9, max_notional=None)
    client = MagicMock()
    e = VampireEngine(client, MagicMock(), MagicMock(), cfg)
    e._is_market_hours = lambda: True
    e._fills = list(fill_map or [])
    async def fake(qty, price, side):
        return e._fills.pop(0) if e._fills else qty
    e._submit = fake
    return e


class TestCounterFollowsFills:
    async def test_a_full_fill_moves_the_counter_fully(self):
        e = _engine([10])
        await e.tick(99.0, vwap=100.0)
        assert e.net_position == 10

    async def test_a_zero_fill_does_not_move_the_counter(self):
        """The defect: an unfilled IOC used to count as a completed trade."""
        e = _engine([0])
        await e.tick(99.0, vwap=100.0)
        assert e.net_position == 0

    async def test_a_partial_fill_moves_the_counter_partially(self):
        e = _engine([4])
        await e.tick(99.0, vwap=100.0)
        assert e.net_position == 4

    async def test_an_unfilled_exit_leaves_the_position_open(self):
        """This is the exact path that grew the book: the engine thought it had
        sold, so it was free to buy again while still holding the shares."""
        e = _engine([10, 0])
        await e.tick(99.0, vwap=100.0)          # buy 10, fills
        assert e.net_position == 10
        await e.tick(101.0, vwap=100.0)         # sell 10, does NOT fill
        assert e.net_position == 10       # still held

    async def test_short_entry_respects_the_fill(self):
        e = _engine([0])
        await e.tick(101.0, vwap=100.0)
        assert e.net_position == 0

    async def test_cover_respects_the_fill(self):
        e = _engine([10, 0])
        await e.tick(101.0, vwap=100.0)         # short 10
        assert e.net_position == -10
        await e.tick(99.0, vwap=100.0)          # cover, no fill
        assert e.net_position == -10


class TestRepeatedNoFillsCannotGrowTheBook:
    async def test_a_never_filling_venue_never_moves_the_counter(self):
        e = _engine([0] * 50)
        for i in range(50):
            await e.tick(99.0 if i % 2 else 101.0, vwap=100.0)
        assert e.net_position == 0

    async def test_buys_filling_while_sells_do_not_still_respects_the_cap(self):
        """The real failure shape: entries fill, exits do not."""
        e = _engine()
        e.cfg.max_position = 8
        fills = []
        for _ in range(40):
            fills += [8, 0]               # buy fills, sell does not
        e._fills = fills
        for i in range(80):
            await e.tick(99.0 if i % 2 == 0 else 101.0, vwap=100.0)
        assert e.net_position <= 8


class TestSubmitReportsTheTruth:
    """_submit itself, not a stub of it.

    Superseded in part by test_vampire_submit.py after the audit: an order that
    is still pending at submit is now POLLED rather than counted as unfilled,
    and an unknown outcome is assumed FILLED. Reading filled_qty off the submit
    response is exactly what caused the third breach.
    """

    def _engine_with(self, order):
        from unittest.mock import MagicMock
        cfg = VampireConfig(symbol="SPY", tick_threshold=0.02, position_size=10,
                            max_position=100, max_daily_loss=1e9)
        client = MagicMock()
        client.market_order.return_value = order
        return VampireEngine(client, MagicMock(), MagicMock(), cfg), client

    async def test_reports_the_filled_quantity_of_a_resolved_order(self):
        from unittest.mock import MagicMock
        from alpaca.trading.enums import OrderSide
        o = MagicMock(); o.status, o.filled_qty = "filled", "7"
        e, _ = self._engine_with(o)
        assert await e._submit(10, 100.0, OrderSide.BUY) == 7

    async def test_a_resolved_order_that_filled_nothing_reports_zero(self):
        from unittest.mock import MagicMock
        from alpaca.trading.enums import OrderSide
        o = MagicMock(); o.status, o.filled_qty = "canceled", "0"
        e, _ = self._engine_with(o)
        assert await e._submit(10, 100.0, OrderSide.BUY) == 0

    async def test_a_broker_exception_reports_zero(self):
        from unittest.mock import MagicMock
        from alpaca.trading.enums import OrderSide
        e, client = self._engine_with(MagicMock())
        client.market_order.side_effect = RuntimeError("rejected")
        assert await e._submit(10, 100.0, OrderSide.BUY) == 0

    async def test_an_unreadable_fill_assumes_the_full_quantity(self):
        """Corrected by the audit. Assuming UNFILLED is what let the book run
        away: the counter never moved, so the engine kept buying. Believing we
        hold more than we do only makes it trade less."""
        from unittest.mock import MagicMock
        from alpaca.trading.enums import OrderSide
        o = MagicMock(); o.status, o.filled_qty = "filled", "not-a-number"
        e, _ = self._engine_with(o)
        assert await e._submit(10, 100.0, OrderSide.BUY) == 10
