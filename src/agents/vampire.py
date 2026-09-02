"""Vampire Agent: wraps the vampire engine for the coordinator.

Integrates the VampireSymbolPicker for data-driven symbol selection and
mid-session rotation via bleed budgets.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from src.core.alpaca_client import AlpacaClient
from src.core.decision_log import record
from src.core.market_data import MarketDataService, Quote
from src.core.position_tracker import PositionTracker
from src.risk.allocation import AllocationManager
from src.risk.circuit_breakers import CircuitBreaker
from src.strategies.vampire_engine import VampireConfig, VampireEngine, VampireState
from src.strategies.vampire_symbol_picker import (
    HARD_EXCLUDE,
    PickerConfig,
    VampireSymbolPicker,
)

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

SPREAD_MULTIPLE = 2.5
MIN_TICK_THRESHOLD = 0.02
SPREAD_SAMPLES = 5

MAX_SPREAD_FRACTION = 0.005

HEALTH_CHECK_INTERVAL = 300


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
        enable_picker: bool = True,
        picker_config: PickerConfig | None = None,
    ):
        self._client = client
        self._data = data
        self._tracker = tracker
        self._breaker = breaker
        self._allocator = allocator

        overrides = config_overrides or {}
        self._overrides = overrides
        self._engines: dict[str, VampireEngine] = {}
        self._picker: VampireSymbolPicker | None = None
        self._last_health_check: float = 0.0
        self._last_lineup: list[dict] = []

        if enable_picker:
            budget = allocator.get_budget().vampire_budget
            self._picker = VampireSymbolPicker(
                client=client, data=data,
                sleeve_budget=budget,
                config=picker_config or PickerConfig(llm_hunt=True),
            )

        if symbols:
            self._symbols = list(symbols)
        else:
            self._symbols = ["SPY"]

        for sym in self._symbols:
            cfg = VampireConfig(symbol=sym, **overrides)
            self._engines[sym] = VampireEngine(client, data, tracker, cfg)

    def _reconcile_engines(self) -> None:
        """Point each engine at the position the broker reports for its symbol."""
        try:
            positions = self._tracker.get_snapshot().positions or {}
        except Exception:
            log.warning("could not read the book; engines stay at their own count",
                        exc_info=True)
            return

        for sym, engine in self._engines.items():
            row = positions.get(sym) or positions.get(sym.upper()) or {}
            try:
                qty = int(float(row.get("qty", 0) or 0))
                avg = row.get("avg_entry_price")
                engine.reconcile(qty, float(avg) if avg else None)
            except Exception:
                log.warning("could not reconcile %s", sym, exc_info=True)

    def _drop_unshortable(self) -> None:
        """Refuse to run a bi-directional engine on a symbol that cannot be shorted.

        The scalper trades both directions, so a symbol with no borrow can only
        ever half-work: every short signal is refused by the venue and the engine
        retries it. SOXL was selected on 2026-08-31 for its move-to-spread ratio
        with no borrow check at all, and shipped that way.

        Borrow is a property of the symbol, not of the strategy, and the venue
        will state it on request. Asking once at startup costs one call per
        symbol and removes a whole class of guaranteed refusal.
        """
        for sym in list(self._engines):
            try:
                asset = self._client.trading.get_asset(sym)
            except Exception:
                log.warning("could not read borrow status for %s; keeping it", sym)
                continue
            if not getattr(asset, "shortable", True):
                log.error(
                    "%s is not shortable; dropping it from the scalper "
                    "(a bi-directional engine cannot run on it)", sym,
                )
                # Flatten BEFORE dropping. stop_all and the end-of-day flatten
                # both iterate self._engines, so a position whose engine has
                # been removed is unreachable by every exit path there is and
                # would carry overnight with nothing able to close it. Removing
                # SOXL from config on 2026-08-31 orphaned 12 shares exactly this
                # way; they had to be closed by hand.
                engine = self._engines.get(sym)
                if engine is not None:
                    try:
                        engine._flatten_all("not_shortable")
                    except Exception:
                        log.exception(
                            "%s: could not flatten before dropping; KEEPING the "
                            "engine so the position stays reachable", sym,
                        )
                        continue
                self._engines.pop(sym, None)

    async def _apply_spread_thresholds(self) -> None:
        """Set each engine's trigger from its own spread, not a flat number.

        A round trip crosses the spread on entry and again on exit, so a fixed
        $0.02 trigger is below the spread on most liquid names and every trade
        it fires is negative before it starts. Measured live on 2026-08-31, the
        average 2.5-second move divided by the spread was 1.00 for PLTR, 0.84
        for SPY and 0.81 for QQQ, and below 0.6 for everything else sampled.

        Setting the trigger to a multiple of the spread makes the engine trade
        rarely and only when the move on offer exceeds what it costs to take it.
        """
        for sym, engine in self._engines.items():
            spreads: list[float] = []
            mids: list[float] = []
            for _ in range(SPREAD_SAMPLES):
                try:
                    quote = self._data.get_latest_quote(sym)
                    if quote:
                        bid, ask = float(quote.bid), float(quote.ask)
                        sp = ask - bid
                        if sp > 0:
                            spreads.append(sp)
                            mids.append((bid + ask) / 2)
                except Exception:
                    log.warning("quote read failed for %s", sym, exc_info=True)
                await asyncio.sleep(0.2)
            price = statistics.median(mids) if mids else 0.0
            if not spreads:
                log.warning("no usable quotes for %s; keeping its configured threshold", sym)
                continue
            # Median across several reads, not one read: a single quote can
            # catch a momentarily tight book on a name whose spread regime is
            # 10x wider, and the threshold it produces fires on flicker.
            usable = [sp for sp in spreads if sp <= price * MAX_SPREAD_FRACTION] \
                if price and price > 0 else spreads
            if not usable:
                log.warning(
                    "%s: every spread read was wider than %.1f%% of price "
                    "(median %.3f on a %.2f book); keeping its configured "
                    "threshold %.4f rather than deriving one from a book this "
                    "strategy does not trade",
                    sym, MAX_SPREAD_FRACTION * 100, statistics.median(spreads),
                    price, engine.cfg.tick_threshold,
                )
                continue
            if len(usable) < len(spreads):
                log.info("%s: discarded %d of %d spread reads as unrepresentative",
                         sym, len(spreads) - len(usable), len(spreads))

            spread = statistics.median(usable)
            engine.cfg.tick_threshold = round(
                max(spread * SPREAD_MULTIPLE, MIN_TICK_THRESHOLD), 4
            )
            log.info("%s median spread %.3f (%d of %d reads) -> tick_threshold %.4f",
                     sym, spread, len(usable), len(spreads), engine.cfg.tick_threshold)

    def _apply_sleeve_limits(self) -> None:
        """Split the vampire sleeve across its symbols and cap each engine.

        Without this the sleeve is advisory: the budget is read once at startup
        and the engines then accumulate independently to max_position.
        """
        # Adopt the real book before sizing anything. A fresh process believes
        # it is flat, and sizing against that belief is what let the sleeve be
        # breached almost sevenfold across restarts.
        self._reconcile_engines()

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

    def run_pre_market_scan(self) -> list[str]:
        """Use the VampireSymbolPicker to select symbols before the open.

        Replaces the static list with data-driven selection.  If the picker
        is disabled or fails, falls back to the existing symbols.
        """
        if self._picker is None:
            log.info("Picker disabled, keeping static symbols: %s", list(self._engines.keys()))
            record(
                "vampire_picker", "lineup",
                thought="picker disabled; using static symbols",
                decision="static",
                symbols=list(self._engines.keys()),
            )
            return list(self._engines.keys())

        try:
            keep = {
                s for s, e in self._engines.items()
                if e.daily_pnl > 0 and s.upper() not in HARD_EXCLUDE
            }
            result = self._picker.pick(keep_symbols=keep)
        except Exception:
            log.exception("Pre-market scan failed; keeping current symbols")
            return list(self._engines.keys())

        if not result.symbols:
            log.warning("Picker returned no symbols; keeping current list")
            return list(self._engines.keys())

        old = set(self._engines.keys())
        new = set(result.symbols)

        for sym in old - new:
            engine = self._engines.get(sym)
            # Staging 2026-09-01: a hunt veto of the whole universe would have
            # rotated QQQ out. QQQ was the only scalper name with a believable
            # session edge. Names the engine has already made money on stay.
            # HOOD/SPY are excluded even if the ledger claims a profit: that
            # number was a lot-accounting lie.
            if (
                engine is not None
                and engine.daily_pnl > 0
                and sym.upper() not in HARD_EXCLUDE
            ):
                log.info(
                    "Keeping %s through picker rotation (session edge $%.2f)",
                    sym, engine.daily_pnl,
                )
                continue
            engine = self._engines.pop(sym, None)
            if engine:
                try:
                    engine._flatten_all("picker_rotation")
                except Exception:
                    log.exception("Failed to flatten %s during rotation", sym)

        for sym in new - old:
            cfg = VampireConfig(symbol=sym, **self._overrides)
            self._engines[sym] = VampireEngine(
                self._client, self._data, self._tracker, cfg,
            )
            log.info("Added victim %s from pre-market scan", sym)

        self._symbols = list(self._engines.keys())
        log.info("Post-scan lineup: %s", self._symbols)
        lineup = []
        for sym in result.symbols:
            metrics = result.metrics.get(sym)
            budget = result.bleed_budgets.get(sym)
            lineup.append({
                "symbol": sym,
                "score": None if metrics is None else round(metrics.score, 4),
                "spread": None if metrics is None else round(metrics.spread, 4),
                "atr_pct": None if metrics is None else round(metrics.atr_pct, 4),
                "profit_target": None if budget is None else budget.profit_target,
                "loss_limit": None if budget is None else budget.loss_limit,
            })
        record(
            "vampire_picker", "lineup",
            thought=f"selected {result.symbols}",
            decision="hunt",
            lineup=lineup,
        )
        self._last_lineup = lineup
        return self._symbols

    async def check_and_rotate(self) -> list[dict]:
        """Mid-session health check: retire exhausted symbols, add replacements.

        Called periodically during the session (every HEALTH_CHECK_INTERVAL seconds).
        Returns a list of rotation events for logging/notification.
        """
        if self._picker is None:
            return []

        now = time.time()
        if now - self._last_health_check < HEALTH_CHECK_INTERVAL:
            return []
        self._last_health_check = now

        events: list[dict] = []

        retirements = self._picker.check_health(self._engines)

        for sym, reason in retirements:
            log.info("Retiring %s: %s", sym, reason)
            engine = self._engines.get(sym)
            if engine:
                try:
                    engine._flatten_all("rotation_" + reason.split(":")[0])
                    engine._state = VampireState.STOPPED
                except Exception:
                    log.exception("Failed to flatten %s during rotation", sym)

            self._picker.retire_symbol(sym, reason)
            events.append({"action": "retire", "symbol": sym, "reason": reason})
            record("vampire_picker", "rotate", symbol=sym, thought=reason, decision="retire")

        if retirements:
            current = [s for s in self._engines if self._engines[s].state != VampireState.STOPPED]
            replacements = self._picker.find_replacements(current, count=len(retirements))

            for sym in replacements:
                cfg = VampireConfig(symbol=sym, **self._overrides)
                engine = VampireEngine(self._client, self._data, self._tracker, cfg)
                self._engines[sym] = engine

                await self._calibrate_single(sym, engine)
                events.append({"action": "add", "symbol": sym, "reason": "replacement"})
                log.info("Fresh victim: %s replaces retired symbol", sym)
                record(
                    "vampire_picker", "rotate",
                    symbol=sym, thought="replacement for a retired symbol",
                    decision="add",
                )

            for retired_sym, _ in retirements:
                if retired_sym in self._engines and self._engines[retired_sym].state == VampireState.STOPPED:
                    self._engines.pop(retired_sym, None)

            self._apply_sleeve_limits()
            self._symbols = list(self._engines.keys())

        return events

    async def _calibrate_single(self, sym: str, engine: VampireEngine) -> None:
        """Apply spread threshold to a single engine (used for mid-session replacements)."""
        spreads: list[float] = []
        mids: list[float] = []
        for _ in range(SPREAD_SAMPLES):
            try:
                quote = self._data.get_latest_quote(sym)
                if quote:
                    bid, ask = float(quote.bid), float(quote.ask)
                    sp = ask - bid
                    if sp > 0:
                        spreads.append(sp)
                        mids.append((bid + ask) / 2)
            except Exception:
                pass
            await asyncio.sleep(0.2)

        price = statistics.median(mids) if mids else 0.0
        if not spreads:
            return
        usable = [sp for sp in spreads if sp <= price * MAX_SPREAD_FRACTION] if price > 0 else spreads
        if not usable:
            return
        spread = statistics.median(usable)
        engine.cfg.tick_threshold = round(max(spread * SPREAD_MULTIPLE, MIN_TICK_THRESHOLD), 4)
        log.info("%s calibrated: spread %.3f -> threshold %.4f", sym, spread, engine.cfg.tick_threshold)

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
                "threshold": engine.cfg.tick_threshold,
                "last_thought": getattr(engine, "last_thought", {}),
            }
        return status

    def picker_status(self) -> dict:
        hunts = list(getattr(self._picker, "last_hunts", []) or []) if self._picker else []
        return {
            "enabled": self._picker is not None,
            "llm_hunt": bool(self._picker and self._picker.cfg.llm_hunt),
            "symbols": list(self._engines.keys()),
            "lineup": list(self._last_lineup),
            "hunts": hunts,
        }

    async def run(self):
        """Subscribe all symbols on one stream and dispatch to engines."""
        if not self._breaker.check():
            log.warning("Circuit breaker active, vampire agent not starting")
            return

        budget = self._allocator.get_budget()
        if budget.vampire_budget <= 0:
            log.warning("Vampire allocation is zero; agent will not start")
            return
        if budget.vampire_available < 500:
            log.warning("Insufficient vampire budget ($%.0f), not starting", budget.vampire_available)
            return

        self.run_pre_market_scan()
        self._apply_sleeve_limits()
        self._drop_unshortable()
        await self._apply_spread_thresholds()

        all_symbols = list(self._engines.keys())
        log.info("Vampire Agent starting with %d symbols: %s", len(all_symbols), all_symbols)

        async def on_quote(quote: Quote):
            engine = self._engines.get(quote.symbol)
            if engine:
                vwap = self._data.get_vwap(quote.symbol, engine.cfg.bleed_window_seconds)
                engine.tick(quote.mid, vwap)

            await self.check_and_rotate()

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
