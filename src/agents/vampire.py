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

        self._symbols = list(symbols)
        self._overrides = overrides
        self._engines: dict[str, VampireEngine] = {}
        for sym in symbols:
            cfg = VampireConfig(symbol=sym, **overrides)
            self._engines[sym] = VampireEngine(client, data, tracker, cfg)

    def _apply_sleeve_limits(self) -> None:
        """Split the vampire sleeve across its symbols and cap each engine.

        Without this the sleeve is advisory: the budget is read once at startup
        and the engines then accumulate independently to max_position.
        """
        budget = self._allocator.get_budget().vampire_budget
        if not budget or not self._engines:
            return
        per_symbol = budget / len(self._engines)

        for sym, engine in self._engines.items():
            engine.cfg.max_notional = per_symbol
            try:
                quote = self._data.get_latest_quote(sym)
                price = quote.mid if quote else None
            except Exception:
                price = None
            if price and price > 0:
                shares = int(per_symbol // price)
                engine.cfg.max_position = max(0, min(engine.cfg.max_position, shares))
                engine.cfg.position_size = max(
                    1, min(engine.cfg.position_size, engine.cfg.max_position or 1)
                )
            log.info(
                "%s sleeve cap $%.0f -> max_position %d, position_size %d",
                sym, per_symbol, engine.cfg.max_position, engine.cfg.position_size,
            )

    def activity_summary(self) -> list[dict]:
        """Per-symbol view of what the scalper actually did.

        The engines hold this individually and nothing asked them, so reports
        covered the options sleeve and silently omitted a fifth of the account.
        Never raises: a report that dies takes the session's only visibility
        with it.
        """
        rows: list[dict] = []
        for sym, engine in self._engines.items():
            try:
                rows.append({
                    "symbol": sym,
                    "trades": len(engine.bleeds),
                    "net_position": engine.net_position,
                    "realized_pnl": round(engine.daily_pnl, 2),
                    "state": engine.state.value,
                })
            except Exception:
                log.warning("could not read vampire engine for %s", sym, exc_info=True)
                rows.append({"symbol": sym, "trades": 0, "net_position": 0,
                             "realized_pnl": 0.0, "state": "unknown"})
        return rows

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

        self._apply_sleeve_limits()

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
