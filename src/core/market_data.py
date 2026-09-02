"""Real-time and historical market data via Alpaca."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

from src.core.alpaca_client import AlpacaClient


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    mid: float
    timestamp: datetime


@dataclass
class VWAPTracker:
    """Rolling VWAP over a configurable window."""

    window_seconds: int = 5
    _prices: deque = field(default_factory=deque)
    _volumes: deque = field(default_factory=deque)
    _timestamps: deque = field(default_factory=deque)

    def update(self, price: float, volume: float, timestamp: datetime):
        self._prices.append(price)
        self._volumes.append(volume)
        self._timestamps.append(timestamp)
        self._trim(timestamp)

    def _trim(self, now: datetime):
        cutoff = now - timedelta(seconds=self.window_seconds)
        while self._timestamps and self._timestamps[0] < cutoff:
            self._prices.popleft()
            self._volumes.popleft()
            self._timestamps.popleft()

    @property
    def value(self) -> float | None:
        if not self._prices:
            return None
        total_vol = sum(self._volumes)
        if total_vol == 0:
            return self._prices[-1]
        return sum(p * v for p, v in zip(self._prices, self._volumes)) / total_vol


class MarketDataService:
    """Provides historical bars, latest quotes, and real-time streaming."""

    def __init__(self, client: AlpacaClient):
        self._data = client.data
        self._stream = client.stream
        self._vwaps: dict[str, VWAPTracker] = {}
        self._on_quote = None
        self._quote_symbols: set[str] = set()

    def get_latest_quote(self, symbol: str) -> Quote | None:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        result = self._data.get_stock_latest_quote(req)
        q = result.get(symbol)
        if q is None:
            return None
        return Quote(
            symbol=symbol,
            bid=float(q.bid_price),
            ask=float(q.ask_price),
            mid=(float(q.bid_price) + float(q.ask_price)) / 2,
            timestamp=q.timestamp,
        )

    def get_bars(
        self,
        symbol: str,
        timeframe: TimeFrame = TimeFrame.Minute,
        days_back: int = 5,
    ):
        end = datetime.now()
        start = end - timedelta(days=days_back)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
        )
        return self._data.get_stock_bars(req)

    def get_vwap(self, symbol: str, window_seconds: int = 5) -> float | None:
        tracker = self._vwaps.get(symbol)
        if tracker is None:
            return None
        return tracker.value

    async def subscribe_quotes(
        self,
        symbols: list[str],
        on_quote: Callable,
    ):
        """Subscribe to real-time quote updates via WebSocket."""
        self._on_quote = on_quote
        self._quote_symbols.update(symbols)
        for sym in symbols:
            if sym not in self._vwaps:
                self._vwaps[sym] = VWAPTracker(window_seconds=5)

        async def _handler(data):
            symbol = data.symbol
            mid = (float(data.bid_price) + float(data.ask_price)) / 2
            tracker = self._vwaps.get(symbol)
            if tracker:
                tracker.update(mid, float(getattr(data, "bid_size", 1)), data.timestamp)

            quote = Quote(
                symbol=symbol,
                bid=float(data.bid_price),
                ask=float(data.ask_price),
                mid=mid,
                timestamp=data.timestamp,
            )
            await on_quote(quote)

        self._stream.subscribe_quotes(_handler, *symbols)

    async def extend_quotes(self, symbols: list[str]) -> None:
        """Add symbols to an already-running quote stream."""
        new = [s for s in symbols if s not in self._quote_symbols]
        if not new or self._on_quote is None:
            return
        await self.subscribe_quotes(new, self._on_quote)

    async def subscribe_trades(
        self,
        symbols: list[str],
        on_trade: Callable,
    ):
        """Subscribe to real-time trade updates for VWAP tracking."""
        for sym in symbols:
            if sym not in self._vwaps:
                self._vwaps[sym] = VWAPTracker(window_seconds=5)

        async def _handler(data):
            symbol = data.symbol
            tracker = self._vwaps.get(symbol)
            if tracker:
                tracker.update(float(data.price), float(data.size), data.timestamp)
            await on_trade(data)

        self._stream.subscribe_trades(_handler, *symbols)

    async def run_stream(self):
        await self._stream._run_forever()
