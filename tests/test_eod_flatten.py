"""The end-of-day flatten must not liquidate the options income book.

The options sleeve holds 7-45 DTE contracts and earns by carrying them. The
previous implementation called close_all_positions() -- everything, options
included -- every 30 seconds from 15:50 until midnight.
"""

from __future__ import annotations

from datetime import date, datetime, time as dt_time
from unittest.mock import MagicMock

import pytest

import src.agents.risk_manager as rm
from src.agents.risk_manager import RiskManagerAgent


class _FakeNow:
    """Pins datetime.now(tz) inside the module under test.

    The implementation asks for Eastern explicitly, so the fake has to accept
    the tzinfo argument and return an aware datetime.
    """

    def __init__(self, hh, mm, day=15):
        self._hh, self._mm, self._day = hh, mm, day

    def now(self, tz=None):
        return datetime(2026, 9, self._day, self._hh, self._mm, tzinfo=tz)


def _agent(positions=None, market_open=True):
    client = MagicMock()
    client.get_positions.return_value = positions or []
    client.get_clock.return_value = MagicMock(is_open=market_open)
    agent = RiskManagerAgent(client, MagicMock(), MagicMock(), MagicMock())
    agent._alpaca = client
    agent._client = client
    agent._intraday_symbols = {"SPY", "QQQ", "AAPL"}
    return agent, client


def _pos(symbol):
    p = MagicMock()
    p.symbol = symbol
    return p


class TestFlattenWindow:
    @pytest.mark.parametrize(
        "hh,mm,expected",
        [(9, 30, False), (12, 0, False), (15, 45, False),
         (15, 50, True), (15, 55, True), (16, 0, True),
         (16, 5, False), (20, 0, False), (23, 59, False)],
    )
    def test_only_fires_inside_the_window(self, hh, mm, expected, monkeypatch):
        agent, _ = _agent()
        monkeypatch.setattr(rm, "datetime", _FakeNow(hh, mm))
        assert agent.should_flatten_eod() is expected

    def test_fires_once_per_session_not_every_30_seconds(self, monkeypatch):
        agent, _ = _agent()
        monkeypatch.setattr(rm, "datetime", _FakeNow(15, 51))
        assert agent.should_flatten_eod() is True
        agent.flatten_intraday()
        assert agent.should_flatten_eod() is False

    def test_fires_again_the_next_session(self, monkeypatch):
        agent, _ = _agent()
        monkeypatch.setattr(rm, "datetime", _FakeNow(15, 51, day=15))
        agent.flatten_intraday()
        monkeypatch.setattr(rm, "datetime", _FakeNow(15, 51, day=16))
        assert agent.should_flatten_eod() is True


class TestFlattenScope:
    def test_options_positions_are_never_closed(self):
        agent, client = _agent([
            _pos("SPY241220P00450000"),   # short put, options sleeve
            _pos("AAPL241220C00230000"),  # covered call, options sleeve
        ])
        closed = agent.flatten_intraday()
        assert closed == []
        client.close_position.assert_not_called()

    def test_intraday_equity_is_closed(self):
        agent, client = _agent([_pos("SPY"), _pos("QQQ")])
        closed = agent.flatten_intraday()
        assert sorted(closed) == ["QQQ", "SPY"]
        assert client.close_position.call_count == 2

    def test_mixed_book_closes_only_the_scalper_leg(self):
        """The regression that mattered: shares get flattened, the put is held."""
        agent, client = _agent([_pos("SPY"), _pos("SPY241220P00450000")])
        closed = agent.flatten_intraday()
        assert closed == ["SPY"]
        client.close_position.assert_called_once_with("SPY")

    def test_equity_outside_the_intraday_universe_is_left_alone(self):
        """Shares assigned from a put are not the scalper's to close."""
        agent, client = _agent([_pos("TSLA")])
        assert agent.flatten_intraday() == []
        client.close_position.assert_not_called()

    def test_close_all_positions_is_not_used_by_the_eod_path(self):
        agent, client = _agent([_pos("SPY")])
        agent.flatten_intraday()
        client.close_all_positions.assert_not_called()

    def test_emergency_path_still_closes_everything(self):
        agent, client = _agent([_pos("SPY"), _pos("SPY241220P00450000")])
        agent.emergency_flatten_all()
        client.close_all_positions.assert_called_once()


class TestBrokerClockGate:
    """main's contribution: never flatten when the market is not open."""

    def test_closed_market_does_not_flatten(self, monkeypatch):
        agent, _ = _agent(market_open=False)
        monkeypatch.setattr(rm, "datetime", _FakeNow(15, 55))
        assert agent.should_flatten_eod() is False

    def test_open_market_inside_the_window_does_flatten(self, monkeypatch):
        agent, _ = _agent(market_open=True)
        monkeypatch.setattr(rm, "datetime", _FakeNow(15, 55))
        assert agent.should_flatten_eod() is True

    def test_clock_failure_falls_back_to_a_weekday_check(self, monkeypatch):
        agent, client = _agent()
        client.get_clock.side_effect = RuntimeError("broker down")
        monkeypatch.setattr(rm, "datetime", _FakeNow(15, 55, day=19))   # Saturday
        assert agent.should_flatten_eod() is False
        monkeypatch.setattr(rm, "datetime", _FakeNow(15, 55, day=15))   # Tuesday
        assert agent.should_flatten_eod() is True
