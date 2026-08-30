"""Vampire algorithm -- bi-directional micro-scalper.

Ported from the spec at notes/2026-08-29-vampire-algorithm-spec.md.
Bleeds micro-profits from every price oscillation on liquid tickers.
Trades both directions: buys dips, shorts rips.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from enum import Enum

from alpaca.trading.enums import OrderSide, TimeInForce

from src.core.alpaca_client import AlpacaClient
from src.core.market_data import MarketDataService, Quote
from src.core.position_tracker import PositionTracker

log = logging.getLogger(__name__)

SESSION_START = dt_time(9, 30)
SESSION_END = dt_time(15, 55)


class VampireState(str, Enum):
    IDLE = "idle"
    WATCHING = "watching"
    STOPPED = "stopped"


@dataclass
class BleedRecord:
    timestamp: datetime
    symbol: str
    action: str
    qty: float
    delta: float
    pnl_estimate: float


@dataclass
class VampireConfig:
    symbol: str = "SPY"
    tick_threshold: float = 0.02
    position_size: int = 10
    max_position: int = 100
    bleed_window_seconds: int = 5
    max_daily_loss: float = 50.0
    max_trades_per_min: int = 20


class VampireEngine:
    """Core vampire tick logic with bi-directional trading."""

    def __init__(
        self,
        client: AlpacaClient,
        data: MarketDataService,
        tracker: PositionTracker,
        config: VampireConfig | None = None,
    ):
        self._client = client
        self._data = data
        self._tracker = tracker
        self.cfg = config or VampireConfig()

        self._state = VampireState.IDLE
        self._net_position: int = 0
        self._daily_pnl: float = 0.0
        self._bleeds: list[BleedRecord] = []
        self._trade_timestamps: deque = deque(maxlen=100)
        self._last_fill_price: float | None = None
        self._avg_entry: float | None = None
        self._realized_pnl: float = 0.0

    @property
    def state(self) -> VampireState:
        return self._state

    @property
    def net_position(self) -> int:
        return self._net_position

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def bleeds(self) -> list[BleedRecord]:
        return list(self._bleeds)

    def _is_market_hours(self) -> bool:
        now = datetime.now().time()
        return SESSION_START <= now <= SESSION_END

    def _check_rate_limit(self) -> bool:
        now = time.time()
        cutoff = now - 60
        while self._trade_timestamps and self._trade_timestamps[0] < cutoff:
            self._trade_timestamps.popleft()
        return len(self._trade_timestamps) < self.cfg.max_trades_per_min

    def _open_lot(self, qty: int, price: float, long: bool):
        """Fold a new lot into the running average entry price."""
        prior_qty = abs(self._net_position)
        if self._avg_entry is None or prior_qty == 0:
            self._avg_entry = price
        else:
            self._avg_entry = ((self._avg_entry * prior_qty) + (price * qty)) / (prior_qty + qty)

    def _close_lot(self, qty: int, price: float, long: bool) -> float:
        """Realize P&L against the average entry. Returns the realized amount.

        This is the correction that matters. The previous accounting credited
        `qty * abs(delta)` on every exit, which is unconditionally positive:
        it recorded the size of the move that triggered the exit, never the
        difference between the exit price and what the position actually cost.
        A losing round trip was booked as a gain, and daily P&L could only rise.
        """
        entry = self._avg_entry if self._avg_entry is not None else price
        realized = qty * (price - entry) if long else qty * (entry - price)
        self._realized_pnl += realized
        self._daily_pnl += realized
        return realized

    def _record_bleed(self, qty: float, delta: float, action: str, pnl: float = 0.0):
        self._bleeds.append(
            BleedRecord(
                timestamp=datetime.now(),
                symbol=self.cfg.symbol,
                action=action,
                qty=qty,
                delta=delta,
                pnl_estimate=pnl,
            )
        )
        self._trade_timestamps.append(time.time())

    def tick(self, current_price: float, vwap: float | None = None):
        """Process one price tick. Core bi-directional logic from the spec."""
        if self._state == VampireState.STOPPED:
            return

        if not self._is_market_hours():
            if self._net_position != 0:
                self._flatten_all("session_end")
            self._state = VampireState.IDLE
            return

        self._state = VampireState.WATCHING

        ref = vwap if vwap is not None else (self._last_fill_price or current_price)
        delta = current_price - ref

        if not self._check_rate_limit():
            return

        if delta >= self.cfg.tick_threshold:
            if self._net_position > 0:
                qty = min(self._net_position, self.cfg.position_size)
                self._sell(qty, current_price)
                realized = self._close_lot(qty, current_price, long=True)
                self._net_position -= qty
                if self._net_position == 0:
                    self._avg_entry = None
                self._record_bleed(qty, delta, "long_exit", realized)

            elif abs(self._net_position) < self.cfg.max_position:
                qty = self.cfg.position_size
                self._sell_short(qty, current_price)
                self._open_lot(qty, current_price, long=False)
                self._net_position -= qty
                self._record_bleed(qty, delta, "short_entry")

        elif delta <= -self.cfg.tick_threshold:
            if self._net_position < 0:
                qty = min(abs(self._net_position), self.cfg.position_size)
                self._buy_to_cover(qty, current_price)
                realized = self._close_lot(qty, current_price, long=False)
                self._net_position += qty
                if self._net_position == 0:
                    self._avg_entry = None
                self._record_bleed(qty, abs(delta), "short_exit", realized)

            elif self._net_position < self.cfg.max_position:
                qty = self.cfg.position_size
                self._buy(qty, current_price)
                self._open_lot(qty, current_price, long=True)
                self._net_position += qty
                self._record_bleed(qty, abs(delta), "long_entry")

        marked = self.total_pnl(current_price)
        if marked <= -self.cfg.max_daily_loss:
            log.warning(
                "Circuit breaker hit: mark-to-market $%.2f (realized $%.2f, "
                "unrealized $%.2f, net %+d)",
                marked, self._realized_pnl, self.unrealized_pnl(current_price),
                self._net_position,
            )
            self._flatten_all("circuit_breaker")
            self._state = VampireState.STOPPED

    def _buy(self, qty: int, price: float):
        try:
            self._client.market_order(self.cfg.symbol, qty, OrderSide.BUY, TimeInForce.IOC)
            self._last_fill_price = price
            self._tracker.record_trade(self.cfg.symbol, "buy", qty, price, "vampire")
            log.debug("BUY %d %s @ %.2f", qty, self.cfg.symbol, price)
        except Exception:
            log.exception("Buy failed")

    def _sell(self, qty: int, price: float):
        try:
            self._client.market_order(self.cfg.symbol, qty, OrderSide.SELL, TimeInForce.IOC)
            self._last_fill_price = price
            self._tracker.record_trade(self.cfg.symbol, "sell", qty, price, "vampire")
            log.debug("SELL %d %s @ %.2f", qty, self.cfg.symbol, price)
        except Exception:
            log.exception("Sell failed")

    def _sell_short(self, qty: int, price: float):
        try:
            self._client.market_order(self.cfg.symbol, qty, OrderSide.SELL, TimeInForce.IOC)
            self._last_fill_price = price
            self._tracker.record_trade(self.cfg.symbol, "sell_short", qty, price, "vampire")
            log.debug("SHORT %d %s @ %.2f", qty, self.cfg.symbol, price)
        except Exception:
            log.exception("Short sell failed")

    def _buy_to_cover(self, qty: int, price: float):
        try:
            self._client.market_order(self.cfg.symbol, qty, OrderSide.BUY, TimeInForce.IOC)
            self._last_fill_price = price
            self._tracker.record_trade(self.cfg.symbol, "buy_to_cover", qty, price, "vampire")
            log.debug("COVER %d %s @ %.2f", qty, self.cfg.symbol, price)
        except Exception:
            log.exception("Cover failed")

    def _flatten_all(self, reason: str):
        log.info("Flattening all vampire positions: %s (net=%d)", reason, self._net_position)
        try:
            if self._net_position != 0:
                self._client.close_position(self.cfg.symbol)
                self._net_position = 0
        except Exception:
            log.exception("Flatten failed")

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    def unrealized_pnl(self, current_price: float) -> float:
        """Mark the open position to the current price.

        Signed by net position, so it is correct for both sides: a short is a
        negative net, and a falling price yields a positive number.
        """
        if self._net_position == 0 or self._avg_entry is None:
            return 0.0
        return self._net_position * (current_price - self._avg_entry)

    def total_pnl(self, current_price: float) -> float:
        """Session P&L marked to market: realized so far today plus the open book.

        Uses the daily counter rather than the lifetime one because the limit it
        feeds (max_daily_loss) is a per-session limit and reset_daily() zeroes it.
        """
        return self._daily_pnl + self.unrealized_pnl(current_price)

    @property
    def avg_entry(self) -> float | None:
        return self._avg_entry

    def reset_daily(self):
        self._daily_pnl = 0.0
        self._realized_pnl = 0.0
        self._avg_entry = None
        self._bleeds.clear()
        self._trade_timestamps.clear()
        if self._state == VampireState.STOPPED:
            self._state = VampireState.IDLE

    async def run(self):
        """Main loop: subscribe to real-time quotes and feed ticks to the engine."""
        log.info("Vampire starting on %s (threshold=%.3f)", self.cfg.symbol, self.cfg.tick_threshold)

        async def on_quote(quote: Quote):
            vwap = self._data.get_vwap(quote.symbol, self.cfg.bleed_window_seconds)
            self.tick(quote.mid, vwap)

        await self._data.subscribe_quotes([self.cfg.symbol], on_quote)
        await self._data.subscribe_trades([self.cfg.symbol], lambda _: asyncio.sleep(0))
        await self._data.run_stream()
