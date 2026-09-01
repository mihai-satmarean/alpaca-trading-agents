"""A flatten must clear its own resting orders before it tries to close.

2026-09-01: HOOD's shutdown-time flatten called close_position while a
resting order was still open on the same symbol, and Alpaca refused the
new closing order outright:

    {"code":40310000,"existing_order_id":"...",
     "message":"potential wash trade detected. use complex orders",
     "reject_reason":"opposite side market/stop order exists"}

The flatten raised, the position was never closed, and it survived only
because the next process's startup adoption happened to pick it up. That
is a lucky outcome, not a designed one - the coordinator's own shutdown
log said "Shutdown complete" while a real short was still open.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.strategies.vampire_engine import VampireConfig, VampireEngine


def _order(symbol, oid):
    o = MagicMock()
    o.symbol, o.id = symbol, oid
    return o


def _engine(symbol="HOOD", open_orders=None):
    cfg = VampireConfig(symbol=symbol, tick_threshold=0.02, position_size=10,
                        max_position=100, max_daily_loss=1e9)
    client = MagicMock()
    client.get_orders.return_value = open_orders or []
    e = VampireEngine(client, MagicMock(), MagicMock(), cfg)
    return e, client


class TestRestingOrdersAreClearedBeforeTheClose:
    def test_a_resting_order_on_the_same_symbol_is_cancelled_first(self):
        e, client = _engine(open_orders=[_order("HOOD", "resting-1")])
        e._net_position = -14
        e._flatten_all("agent_stop")
        client.cancel_order.assert_called_once_with("resting-1")

    def test_the_cancel_happens_before_the_close_is_attempted(self):
        """Order matters: closing first is exactly what produced the 403."""
        e, client = _engine(open_orders=[_order("HOOD", "resting-1")])
        e._net_position = -14
        calls = []
        client.cancel_order.side_effect = lambda oid: calls.append(("cancel", oid))
        client.close_position.side_effect = lambda sym: calls.append(("close", sym))
        e._flatten_all("agent_stop")
        assert calls == [("cancel", "resting-1"), ("close", "HOOD")]

    def test_a_resting_order_on_a_different_symbol_is_left_alone(self):
        """The scalper runs four engines; one flattening must not touch
        another symbol's book."""
        e, client = _engine(symbol="HOOD",
                             open_orders=[_order("TQQQ", "not-mine")])
        e._net_position = -14
        e._flatten_all("agent_stop")
        client.cancel_order.assert_not_called()

    def test_multiple_resting_orders_on_the_symbol_are_all_cancelled(self):
        e, client = _engine(open_orders=[_order("HOOD", "a"), _order("HOOD", "b")])
        e._net_position = -14
        e._flatten_all("agent_stop")
        assert {c.args[0] for c in client.cancel_order.call_args_list} == {"a", "b"}

    def test_no_resting_orders_still_closes_cleanly(self):
        e, client = _engine(open_orders=[])
        e._net_position = -14
        e._flatten_all("agent_stop")
        client.cancel_order.assert_not_called()
        client.close_position.assert_called_once_with("HOOD")

    def test_a_flat_engine_does_not_touch_orders_at_all(self):
        """No position, nothing to protect the close from - don't even read."""
        e, client = _engine()
        e._net_position = 0
        e._flatten_all("agent_stop")
        client.get_orders.assert_not_called()
        client.close_position.assert_not_called()


class TestTheCloseIsAttemptedEvenWhenCancellingFails:
    """A failure to clear the way must not be a reason to give up on
    closing the position - trying and failing again is strictly better
    than not trying."""

    def test_a_failed_cancel_does_not_prevent_the_close_attempt(self):
        e, client = _engine(open_orders=[_order("HOOD", "resting-1")])
        e._net_position = -14
        client.cancel_order.side_effect = RuntimeError("already filled")
        e._flatten_all("agent_stop")
        client.close_position.assert_called_once_with("HOOD")

    def test_an_unreadable_order_list_does_not_prevent_the_close_attempt(self):
        e, client = _engine()
        e._net_position = -14
        client.get_orders.side_effect = RuntimeError("timeout")
        e._flatten_all("agent_stop")
        client.close_position.assert_called_once_with("HOOD")

    def test_the_close_still_fully_flattens_the_counter_on_success(self):
        e, _ = _engine(open_orders=[_order("HOOD", "resting-1")])
        e._net_position = -14
        e._flatten_all("agent_stop")
        assert e._net_position == 0

    def test_a_close_that_still_fails_after_cancelling_does_not_raise(self):
        """The exact HOOD scenario if the conflict was never our own order:
        cancel our own resting order, attempt the close, it fails anyway.
        _flatten_all must swallow it, matching every other call site here."""
        e, client = _engine(open_orders=[_order("HOOD", "resting-1")])
        e._net_position = -14
        client.close_position.side_effect = RuntimeError("still rejected")
        e._flatten_all("agent_stop")   # must not raise
        assert e._net_position == -14, "counter must not be zeroed on failure"
