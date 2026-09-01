"""The poll budget widens for the opening minutes only.

2026-09-01, the first live session on EC2: 28 orders exceeded the 2.0s poll
and fell through to "assume filled." Every one landed between 09:30:33 and
09:37:15 - two dense clusters in the opening minutes - and zero occurred in
the 25+ minutes that followed. Opening order flow is congested in a way the
rest of the session isn't.

A flat, permanently longer timeout would fix the open at the cost of
blocking tick processing for every slow order all day, worst exactly when
the market moves fastest. The budget widens only through 09:40 ET and
reverts for the rest of the session.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import src.strategies.vampire_engine as ve
from src.strategies.vampire_engine import VampireConfig, VampireEngine


class _FakeNow:
    """Pins datetime.now(tz) inside vampire_engine to a fixed wall-clock time."""

    def __init__(self, hh, mm, ss=0):
        self._hh, self._mm, self._ss = hh, mm, ss

    def now(self, tz=None):
        return datetime(2026, 9, 1, self._hh, self._mm, self._ss, tzinfo=tz)


def _engine():
    cfg = VampireConfig(symbol="HOOD", tick_threshold=0.02, position_size=10,
                        max_position=100, max_daily_loss=1e9)
    return VampireEngine(MagicMock(), MagicMock(), MagicMock(), cfg)


class TestTheOpeningWindowWidensThePollBudget:
    def test_at_the_open_the_wider_budget_applies(self, monkeypatch):
        monkeypatch.setattr(ve, "datetime", _FakeNow(9, 30, 33))
        e = _engine()
        assert e._current_poll_timeout() == VampireEngine.OPENING_POLL_TIMEOUT

    def test_mid_cluster_still_widened(self, monkeypatch):
        """09:37:15 - the last observed timeout - must still be covered."""
        monkeypatch.setattr(ve, "datetime", _FakeNow(9, 37, 15))
        e = _engine()
        assert e._current_poll_timeout() == VampireEngine.OPENING_POLL_TIMEOUT

    def test_the_window_closes_at_the_boundary(self, monkeypatch):
        monkeypatch.setattr(ve, "datetime", _FakeNow(9, 40, 0))
        e = _engine()
        assert e._current_poll_timeout() == VampireEngine.POLL_TIMEOUT

    def test_one_second_before_the_boundary_is_still_widened(self, monkeypatch):
        monkeypatch.setattr(ve, "datetime", _FakeNow(9, 39, 59))
        e = _engine()
        assert e._current_poll_timeout() == VampireEngine.OPENING_POLL_TIMEOUT

    def test_the_rest_of_the_session_is_unwidened(self, monkeypatch):
        """10:02 ET, when the false-alarm report was checked: normal budget."""
        monkeypatch.setattr(ve, "datetime", _FakeNow(10, 2, 46))
        e = _engine()
        assert e._current_poll_timeout() == VampireEngine.POLL_TIMEOUT

    def test_the_wider_budget_is_strictly_wider(self):
        """A regression that sets them equal would pass every other test here
        while silently undoing the fix."""
        assert VampireEngine.OPENING_POLL_TIMEOUT > VampireEngine.POLL_TIMEOUT


class TestThePollLoopActuallyUsesTheWiderBudget:
    """_current_poll_timeout being correct is necessary but not sufficient -
    _submit has to spend it, not just compute it.

    Drives a real, honest fake clock: each loop iteration advances wall
    time by exactly one POLL_INTERVAL, so the number of get_order polls
    made before give-up is a direct, unambiguous measurement of which
    deadline the loop actually used.
    """

    def _order(self, status="new", filled="0", oid="o1"):
        o = MagicMock()
        o.status, o.filled_qty, o.id = status, filled, oid
        return o

    def _poll_count_before_giveup(self, monkeypatch, at_hh_mm_ss):
        monkeypatch.setattr(ve, "datetime", _FakeNow(*at_hh_mm_ss))
        e = _engine()
        e._client.market_order.return_value = self._order("new", "0")
        e._client.get_order.return_value = self._order("new", "0")  # never resolves

        clock = {"t": 1_000_000.0}
        monkeypatch.setattr(ve.time, "sleep", lambda s: None)
        monkeypatch.setattr(ve.time, "time", lambda: clock["t"])

        polls = {"n": 0}
        never_resolves = self._order("new", "0")

        def counting_get_order(oid):
            polls["n"] += 1
            clock["t"] += VampireEngine.POLL_INTERVAL
            return never_resolves

        e._client.get_order.side_effect = counting_get_order

        from alpaca.trading.enums import OrderSide
        e._submit(10, 100.0, OrderSide.BUY)
        return polls["n"]

    def test_at_the_open_it_polls_for_the_full_4s_budget(self, monkeypatch):
        n = self._poll_count_before_giveup(monkeypatch, (9, 31, 0))
        expected = round(VampireEngine.OPENING_POLL_TIMEOUT / VampireEngine.POLL_INTERVAL)
        assert n == expected, f"expected ~{expected} polls (4.0s budget), got {n}"

    def test_outside_the_window_it_only_polls_for_2s(self, monkeypatch):
        n = self._poll_count_before_giveup(monkeypatch, (10, 2, 46))
        expected = round(VampireEngine.POLL_TIMEOUT / VampireEngine.POLL_INTERVAL)
        assert n == expected, f"expected ~{expected} polls (2.0s budget), got {n}"
