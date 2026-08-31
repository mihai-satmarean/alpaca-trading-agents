"""Shutdown must not cancel the options book's resting orders.

It used to cancel everything, so every restart silently killed CSP limit orders
waiting to fill. Over a multi-day run that forfeits fills the strategy earned.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _coord(orders):
    from src.agents.coordinator import Coordinator

    c = Coordinator.__new__(Coordinator)     # skip __init__: no broker needed
    client = MagicMock()
    client.get_orders.return_value = orders
    c._client = client
    return c, client


def _order(symbol, oid="1"):
    o = MagicMock()
    o.symbol, o.id = symbol, oid
    return o


def test_option_orders_are_left_resting():
    c, client = _coord([_order("MARA260918P00010500"), _order("CLF261016P00011000")])
    assert c.cancel_intraday_orders() == []
    client.cancel_order.assert_not_called()


def test_equity_orders_are_cancelled():
    c, client = _coord([_order("SPY"), _order("QQQ")])
    assert sorted(c.cancel_intraday_orders()) == ["QQQ", "SPY"]
    assert client.cancel_order.call_count == 2


def test_mixed_book_cancels_only_the_equity_leg():
    c, client = _coord([_order("SPY", "a"), _order("MARA260918P00010500", "b")])
    assert c.cancel_intraday_orders() == ["SPY"]
    client.cancel_order.assert_called_once_with("a")


def test_cancel_all_orders_is_not_used():
    c, client = _coord([_order("SPY")])
    c.cancel_intraday_orders()
    client.cancel_all_orders.assert_not_called()


def test_one_failing_cancel_does_not_stop_the_rest():
    c, client = _coord([_order("SPY", "a"), _order("QQQ", "b")])
    client.cancel_order.side_effect = [RuntimeError("boom"), None]
    assert c.cancel_intraday_orders() == ["QQQ"]


def test_unreadable_order_list_returns_empty_rather_than_raising():
    c, client = _coord([])
    client.get_orders.side_effect = RuntimeError("broker down")
    assert c.cancel_intraday_orders() == []
