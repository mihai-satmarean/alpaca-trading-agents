"""Two defects found in the live log on 2026-09-02, both pre-existing.

The EOD flatten referenced an attribute the constructor never set, so it
raised on its first line every evening and closed nothing; the scalper
carried positions overnight for the life of the project. Had it run, its
cancel_all_orders() would have wiped the options sleeve's resting orders.
And the order renderer fell through to dict.get() on real Order objects
whenever an attribute was falsy, dropping market orders from every report.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.agents.risk_manager import RiskManagerAgent
from src.core.strategy_report import describe_orders


def _agent(positions, open_orders):
    client = MagicMock()
    client.get_positions.return_value = positions
    client.get_orders.return_value = open_orders
    a = RiskManagerAgent(client, MagicMock(), MagicMock(), MagicMock())
    a._intraday_symbols = {"QQQ", "TQQQ"}
    return a, client


def _pos(sym): p = MagicMock(); p.symbol = sym; return p
def _ord(sym, oid): o = MagicMock(); o.symbol = sym; o.id = oid; return o


class TestEodFlattenActuallyRuns:
    def test_it_no_longer_raises_and_closes_the_intraday_sleeve(self):
        a, client = _agent([_pos("QQQ"), _pos("AAPL"), _pos("CLF260918P00011500")], [])
        closed = a.flatten_intraday()
        assert closed == ["QQQ"]
        client.close_position.assert_called_once_with("QQQ")
        assert a._last_flatten_date is not None, "the once-per-session latch must set"

    def test_it_cancels_only_the_intraday_sleeves_orders(self):
        """cancel_all_orders() would have forfeited CSP and SIXFOLD resting
        limits every evening. Only the scalper's orders may be cancelled."""
        a, client = _agent([], [_ord("TQQQ", "t1"), _ord("CLF260918P00011500", "c1"),
                                _ord("AMZN", "s1")])
        a.flatten_intraday()
        client.cancel_all_orders.assert_not_called()
        client.cancel_order.assert_called_once_with("t1")

    def test_options_and_other_sleeves_are_never_closed(self):
        a, client = _agent([_pos("AMZN"), _pos("TLT"), _pos("MARA260911P00010000")], [])
        assert a.flatten_intraday() == []
        client.close_position.assert_not_called()


class TestOrderReportRendersRealOrders:
    def test_a_market_order_with_no_limit_price_is_rendered_not_dropped(self):
        o = MagicMock(spec=["symbol", "side", "qty", "limit_price", "status"])
        o.symbol, o.side, o.qty, o.limit_price, o.status = "TQQQ", "sell", "100", None, "accepted"
        lines = describe_orders([o])
        assert len(lines) == 1 and "TQQQ" in lines[0]

    def test_dicts_still_work(self):
        lines = describe_orders([{"symbol": "AMZN", "side": "buy", "qty": 18,
                                  "limit_price": 230.5, "status": "new"}])
        assert len(lines) == 1 and "230.50" in lines[0]
