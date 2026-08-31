"""Tests for the Vampire engine tick logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from datetime import datetime, time as dt_time

import pytest

from src.strategies.vampire_engine import VampireEngine, VampireConfig, VampireState


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



@pytest.fixture
def mock_deps():
    client = MagicMock()
    data = MagicMock()
    tracker = MagicMock()
    return client, data, tracker


@pytest.fixture
def engine(mock_deps):
    client, data, tracker = mock_deps
    config = VampireConfig(
        symbol="SPY",
        tick_threshold=0.02,
        position_size=10,
        max_position=100,
        max_daily_loss=50.0,
    )
    return VampireEngine(_filling_client(), data, tracker, config)


def _patch_market_hours(engine):
    engine._is_market_hours = lambda: True


class TestVampireTick:
    def test_buy_on_dip(self, engine):
        _patch_market_hours(engine)
        engine._last_fill_price = 100.00
        engine.tick(99.97, vwap=100.00)
        assert engine.net_position == 10

    def test_sell_on_rip_when_long(self, engine):
        _patch_market_hours(engine)
        engine._net_position = 10
        engine._last_fill_price = 100.00
        engine.tick(100.03, vwap=100.00)
        assert engine.net_position == 0

    def test_short_on_rip_when_flat(self, engine):
        _patch_market_hours(engine)
        engine._last_fill_price = 100.00
        engine.tick(100.03, vwap=100.00)
        assert engine.net_position == -10

    def test_cover_on_dip_when_short(self, engine):
        _patch_market_hours(engine)
        engine._net_position = -10
        engine._last_fill_price = 100.00
        engine.tick(99.97, vwap=100.00)
        assert engine.net_position == 0

    def test_max_position_long(self, engine):
        _patch_market_hours(engine)
        engine._net_position = 100
        engine._last_fill_price = 100.00
        engine.tick(99.97, vwap=100.00)
        assert engine.net_position == 100

    def test_max_position_short(self, engine):
        _patch_market_hours(engine)
        engine._net_position = -100
        engine._last_fill_price = 100.00
        engine.tick(100.03, vwap=100.00)
        assert engine.net_position == -100

    def test_no_trade_within_threshold(self, engine):
        _patch_market_hours(engine)
        engine._last_fill_price = 100.00
        engine.tick(100.01, vwap=100.00)
        assert engine.net_position == 0

    def test_circuit_breaker_stops_trading(self, engine):
        _patch_market_hours(engine)
        engine._daily_pnl = -51.0
        engine._last_fill_price = 100.00
        engine.tick(99.97, vwap=100.00)
        assert engine.state == VampireState.STOPPED

    def test_outside_market_hours_flattens(self, engine):
        engine._is_market_hours = lambda: False
        engine._net_position = 10
        engine.tick(100.00, vwap=100.00)
        assert engine.state == VampireState.IDLE


class TestVampireReset:
    def test_reset_clears_state(self, engine):
        engine._daily_pnl = -30.0
        engine._state = VampireState.STOPPED
        engine.reset_daily()
        assert engine.daily_pnl == 0.0
        assert engine.state == VampireState.IDLE
