"""Order accounting, audited after three breaches in one session.

Alpaca returns a market order with filled_qty="0" and status "new"; the fill
lands roughly 85 to 100 milliseconds later. Reading filled_qty off the submit
response therefore always saw zero. The engine concluded nothing had filled,
left its counter at zero, and bought again on the next tick while every one of
those orders filled. That is how AAPL reached 271 shares against a 21-share cap.

The rule this encodes: an unknown fill must be assumed FILLED. Under-counting
accumulates without bound.

CORRECTION, 2026-08-31 afternoon. This docstring originally justified the rule
with "over-counting only makes the engine trade less." That is true only while
opening a position. While closing one it is false and dangerous: an over-stated
position asks the venue to buy back more than exists, the venue refuses, and no
retry makes the extra share appear. It produced 4,700 refused orders in 29
minutes and rate-limited the account. Over-counting is bounded on entry and a
deadlock on exit. See tests/test_vampire_reject_recovery.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from alpaca.trading.enums import OrderSide

from src.strategies.vampire_engine import VampireConfig, VampireEngine


def _order(status="new", filled="0", oid="o1"):
    o = MagicMock()
    o.status, o.filled_qty, o.id = status, filled, oid
    return o


def _engine(submit_order, poll_sequence=None):
    cfg = VampireConfig(symbol="SPY", tick_threshold=0.02, position_size=10,
                        max_position=21, max_daily_loss=1e9)
    client = MagicMock()
    client.market_order.return_value = submit_order
    if poll_sequence is not None:
        client.get_order.side_effect = poll_sequence
    e = VampireEngine(client, MagicMock(), MagicMock(), cfg)
    e._is_market_hours = lambda: True
    return e, client


class TestFillIsResolvedNotGuessed:
    async def test_a_pending_order_is_polled_until_it_fills(self):
        """The exact defect: filled_qty is 0 at submit and 10 a moment later."""
        e, client = _engine(_order("new", "0"),
                            [_order("new", "0"), _order("filled", "10")])
        assert await e._submit(10, 100.0, OrderSide.BUY) == 10

    async def test_an_order_already_filled_at_submit_needs_no_poll(self):
        e, client = _engine(_order("filled", "10"))
        assert await e._submit(10, 100.0, OrderSide.BUY) == 10
        client.get_order.assert_not_called()

    async def test_a_cancelled_ioc_reports_what_it_actually_filled(self):
        e, _ = _engine(_order("new", "0"), [_order("canceled", "4")])
        assert await e._submit(10, 100.0, OrderSide.BUY) == 4

    async def test_a_fully_unfilled_cancel_reports_zero(self):
        e, _ = _engine(_order("new", "0"), [_order("canceled", "0")])
        assert await e._submit(10, 100.0, OrderSide.BUY) == 0

    async def test_a_rejected_order_reports_zero(self):
        e, _ = _engine(_order("new", "0"), [_order("rejected", "0")])
        assert await e._submit(10, 100.0, OrderSide.BUY) == 0


class TestUnknownMeansFilled:
    """The direction that matters. Assuming unfilled is what let it run away."""

    async def test_a_poll_that_never_resolves_assumes_the_full_quantity(self):
        e, _ = _engine(_order("new", "0"), [_order("new", "0")] * 200)
        e.POLL_TIMEOUT = 0.15
        assert await e._submit(10, 100.0, OrderSide.BUY) == 10

    async def test_an_unreadable_poll_assumes_the_full_quantity(self):
        e, client = _engine(_order("new", "0"))
        client.get_order.side_effect = RuntimeError("broker down")
        assert await e._submit(10, 100.0, OrderSide.BUY) == 10

    async def test_an_order_with_no_id_assumes_the_full_quantity(self):
        o = _order("new", "0"); o.id = None
        e, _ = _engine(o)
        assert await e._submit(10, 100.0, OrderSide.BUY) == 10

    async def test_a_rejected_submission_is_the_one_case_that_is_zero(self):
        """Nothing reached the venue, so nothing can fill."""
        e, client = _engine(_order())
        client.market_order.side_effect = RuntimeError("rejected at submit")
        assert await e._submit(10, 100.0, OrderSide.BUY) == 0


class TestTheBreachCannotRecur:
    async def test_repeated_pending_responses_still_cap_the_position(self):
        """Replays the failure: every submit returns filled_qty 0 and every
        order fills. The cap must hold anyway."""
        e, client = _engine(_order("new", "0"))
        # A venue cannot fill more than was requested; a mock that does hides
        # the clamp being tested.
        asked = {}
        def _submit_order(symbol, qty, side, tif=None):
            asked["qty"] = qty
            return _order("new", "0")
        client.market_order.side_effect = _submit_order
        client.get_order.side_effect = lambda _: _order("filled", str(asked["qty"]))
        e.POLL_INTERVAL = 0.001
        for _ in range(60):
            await e.tick(99.0, vwap=100.0)
        assert e.net_position <= e.cfg.max_position, "the cap is a ceiling, not a trigger"
