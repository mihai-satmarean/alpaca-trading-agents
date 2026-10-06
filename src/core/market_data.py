"""Real-time and historical market data via Alpaca."""

from __future__ import annotations

import logging

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest, StockQuotesRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame

from src.core.alpaca_client import AlpacaClient

log = logging.getLogger(__name__)


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

    def get_recent_minute_bars(self, symbol: str, minutes: int = 90) -> list:
        """The last ``minutes`` of one-minute bars, oldest first, from IEX.

        The free plan refuses SIP data newer than 15 minutes, and IEX is the
        feed the scalper's quotes come from anyway, so the regime advisor
        judges the same tape the engine trades on.
        """
        end = datetime.now(timezone.utc)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=end - timedelta(minutes=minutes),
            end=end,
            feed=DataFeed.IEX,
        )
        result = self._data.get_stock_bars(req)
        try:
            bars = result[symbol]
        except (KeyError, TypeError):
            bars = (getattr(result, "data", None) or {}).get(symbol, [])
        return list(bars)

    def recent_spread(self, symbol: str, minutes: int = 20,
                      max_age_minutes: int = 24 * 60) -> dict | None:
        """Median quoted spread over a real window of the book, or None.

        Five REST reads a fifth of a second apart is one second of evidence,
        and the strategy pinned that second to the worst moment of the day:
        the agent starts within 30s of the opening bell and calibrated on the
        auction book. QQQ's IEX median spread on 2026-09-03 was $0.740 in the
        first minute, $0.110 by 09:35 and $0.050 by 11:10, so the trigger it
        froze for the session was 15x the width the book actually traded at.

        This reads a window of historical IEX quotes instead -- the same feed
        the engine's own stream uses -- and falls back to the previous session
        when the window is empty, which is exactly the case at the open. It
        returns the distribution, not just a number, so the caller can refuse
        a sample it should not trust.
        """
        end = datetime.now(timezone.utc)
        for lookback in (minutes, minutes * 6, max_age_minutes):
            try:
                req = StockQuotesRequest(
                    symbol_or_symbols=symbol,
                    start=end - timedelta(minutes=lookback),
                    end=end,
                    feed=DataFeed.IEX,
                )
                quotes = self._data.get_stock_quotes(req)[symbol]
            except Exception:
                log.warning("%s: spread window read failed", symbol, exc_info=True)
                return None
            spreads, mids = [], []
            for q in quotes:
                try:
                    bid, ask = float(q.bid_price), float(q.ask_price)
                except (TypeError, ValueError):
                    continue
                if bid > 0 and ask > bid:
                    spreads.append(ask - bid)
                    mids.append((bid + ask) / 2)
            if len(spreads) >= 200:
                spreads.sort()
                n = len(spreads)
                return {
                    "n": n,
                    "median": spreads[n // 2],
                    "p90": spreads[min(n - 1, int(n * 0.9))],
                    "price": sorted(mids)[len(mids) // 2],
                    "window_minutes": lookback,
                }
        return None

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
