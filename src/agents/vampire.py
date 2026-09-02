"""Vampire Agent: wraps the vampire engine for the coordinator."""

from __future__ import annotations

import asyncio
import logging
import statistics
import threading
import time

from src.core.alpaca_client import AlpacaClient
from src.core.market_data import MarketDataService, Quote
from src.core.position_tracker import PositionTracker
from src.risk.allocation import AllocationManager
from src.risk.circuit_breakers import CircuitBreaker
from src.strategies.regime_advisor import RegimeAdvisor
from src.strategies.vampire_engine import VampireConfig, VampireEngine, VampireState

log = logging.getLogger(__name__)

# A round trip pays the spread twice. Below about 2x there is no room for the
# move to cover the cost, let alone to profit.
SPREAD_MULTIPLE = 2.5

# Never let a threshold be set below this, whatever the sampled spread says. A
# single tight quote on an unstable book (PLTR read 0.04 at startup and traded
# 0.915 wide twenty minutes later) would otherwise arm the engine to fire on
# quote flicker all session.
MIN_TICK_THRESHOLD = 0.02
SPREAD_SAMPLES = 5

# A quote wider than this fraction of the price is not the book this
# strategy trades against. HOOD read a median spread of $3.87 on a $104
# stock at 15:25 on 2026-08-31, 3.7% wide against its usual 4 cents, and
# the derived trigger came out at $9.67: a 9.3% move required before the
# symbol would ever act. There was a floor on the threshold and no
# ceiling, so a bad book silently retires a symbol instead of failing.
MAX_SPREAD_FRACTION = 0.005


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
        regime_advisor: RegimeAdvisor | None = None,
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
        self._advisor = regime_advisor
        self._regime_stop = threading.Event()
        self._regime_thread: threading.Thread | None = None
        for sym in symbols:
            cfg = VampireConfig(symbol=sym, **overrides)
            if regime_advisor is not None:
                cfg.entry_gate = self._entry_gate_for(sym)
            self._engines[sym] = VampireEngine(client, data, tracker, cfg)

    # -- LLM regime gate -----------------------------------------------------

    def _entry_gate_for(self, symbol: str):
        """One closure per symbol, built here rather than inline in the loop:
        a lambda written inside the loop closes over the loop variable and
        every engine ends up asking about the last symbol."""
        advisor = self._advisor
        return lambda: advisor.entry_allowed(symbol)

    def _refresh_regimes(self) -> None:
        """Ask the advisor about every symbol, off the quote thread.

        A verdict is 17 to 19 seconds of model time on dell4-chat. Run inside
        tick() it would stall the quote loop for the duration and every engine
        would miss every tick in it, so it lives on its own thread.
        """
        if self._advisor is None:
            return
        for sym in list(self._engines):
            try:
                bars = self._data.get_recent_minute_bars(
                    sym, minutes=self._advisor.bars_needed * 3)
            except Exception:
                log.warning("%s: could not read bars for the regime advisor; "
                            "entries stay closed", sym, exc_info=True)
                bars = []
            self._advisor.refresh(sym, bars)

    def _regime_loop(self) -> None:
        while not self._regime_stop.is_set():
            try:
                self._refresh_regimes()
            except Exception:
                log.warning("regime refresh failed; entries stay closed until "
                            "the next pass", exc_info=True)
            self._regime_stop.wait(self._advisor.window_seconds)

    def _start_regime_loop(self) -> None:
        if self._advisor is None or self._regime_thread is not None:
            return
        self._regime_thread = threading.Thread(
            target=self._regime_loop, name="vampire-regime", daemon=True)
        self._regime_thread.start()

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

    def _apply_spread_thresholds(self) -> None:
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
                time.sleep(0.2)
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
            if self._advisor is not None:
                status[sym]["regime"] = self._advisor.status().get(sym)
        return status

    async def run(self):
        """Subscribe all symbols on one stream and dispatch to engines."""
        if not self._breaker.check():
            log.warning("Circuit breaker active, vampire agent not starting")
            return

        # The advisor runs whether or not the sleeve is funded. At 0% this is
        # a shadow mode: every verdict is journalled and shown on the dashboard
        # and nothing trades, which is how a gate earns the right to be funded.
        self._start_regime_loop()

        budget = self._allocator.get_budget()
        if budget.vampire_budget <= 0:
            log.warning("Vampire allocation is zero; engines idle, regime advisor "
                        "in shadow mode")
            return
        if budget.vampire_available < 500:
            log.warning("Insufficient vampire budget ($%.0f), not starting", budget.vampire_available)
            return

        self._apply_sleeve_limits()
        self._drop_unshortable()
        self._apply_spread_thresholds()

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
        self._regime_stop.set()
        for sym, engine in self._engines.items():
            engine._flatten_all("agent_stop")
            engine._state = VampireState.STOPPED
            log.info("Stopped vampire on %s", sym)

    def reset_daily(self):
        for engine in self._engines.values():
            engine.reset_daily()
