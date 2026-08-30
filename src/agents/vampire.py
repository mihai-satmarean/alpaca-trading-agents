"""Vampire Agent: wraps the vampire engine for the coordinator."""

from __future__ import annotations

import asyncio
import logging

from src.core.alpaca_client import AlpacaClient
from src.core.market_data import MarketDataService, Quote
from src.core.position_tracker import PositionTracker
from src.strategies.vampire_engine import VampireEngine, VampireConfig, VampireState
from src.risk.circuit_breakers import CircuitBreaker
from src.risk.allocation import AllocationManager

log = logging.getLogger(__name__)


class VampireAgent:
    """Manages one or more VampireEngine instances across symbols.

    Uses a single shared WebSocket stream and dispatches quotes
    to the appropriate engine by symbol.
    """

    def __init__(
        self,
        client: AlpacaClient,
        data: MarketDataService,
        tracker: PositionTracker,
        breaker: CircuitBreaker,
        allocator: AllocationManager,
        symbols: list[str] | None = None,
        config_overrides: dict | None = None,
    ):
        self._client = client
        self._data = data
        self._tracker = tracker
        self._breaker = breaker
        self._allocator = allocator

        symbols = symbols or ["SPY"]
        overrides = config_overrides or {}

        self._engines: dict[str, VampireEngine] = {}
        for sym in symbols:
            cfg = VampireConfig(symbol=sym, **overrides)
            self._engines[sym] = VampireEngine(client, data, tracker, cfg)

    def get_status(self) -> dict:
        status = {}
        for sym, engine in self._engines.items():
            status[sym] = {
                "state": engine.state.value,
                "net_position": engine.net_position,
                "daily_pnl": engine.daily_pnl,
                "bleed_count": len(engine.bleeds),
            }
        return status

    async def run(self):
        """Subscribe all symbols on one stream and dispatch to engines."""
        if not self._breaker.check():
            log.warning("Circuit breaker active, vampire agent not starting")
            return

        budget = self._allocator.get_budget()
        if budget.vampire_available < 500:
            log.warning("Insufficient vampire budget ($%.0f), not starting", budget.vampire_available)
            return

        all_symbols = list(self._engines.keys())
        log.info("Vampire Agent starting with %d symbols: %s", len(all_symbols), all_symbols)

        async def on_quote(quote: Quote):
            engine = self._engines.get(quote.symbol)
            if engine:
                vwap = self._data.get_vwap(quote.symbol, engine.cfg.bleed_window_seconds)
                engine.tick(quote.mid, vwap)

        await self._data.subscribe_quotes(all_symbols, on_quote)
        await self._data.subscribe_trades(all_symbols, lambda _: asyncio.sleep(0))
        await self._data.run_stream()

    def stop_all(self):
        for sym, engine in self._engines.items():
            engine._flatten_all("agent_stop")
            engine._state = VampireState.STOPPED
            log.info("Stopped vampire on %s", sym)

    def reset_daily(self):
        for engine in self._engines.values():
            engine.reset_daily()
