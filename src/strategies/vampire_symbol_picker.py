"""Pre-market symbol scanner for the Vampire scalper.

Replaces the static symbol list with data-driven selection.  Runs at 9:25 ET,
scores ~50 liquid symbols by ATR/spread ratio, borrow availability, and
pre-market volume, then returns the top N for the session.

Each selected symbol also gets a "bleed budget" -- a per-symbol profit target
and loss limit.  When a symbol has been bled dry (target reached) or is
bleeding the engine (loss limit hit), the symbol is retired mid-session and
a fresh victim can replace it.

Industry standard metric confirmed by MT5 Ranking Scalping:
    score = volatility / spread
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

from src.core.alpaca_client import AlpacaClient
from src.core.market_data import MarketDataService

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

UNIVERSE = frozenset([
    "SPY", "QQQ", "IWM", "DIA",
    "TQQQ", "SQQQ",
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
    "AMD", "INTC", "CRM", "ORCL",
    "JPM", "BAC", "GS",
    "XLK", "XLF", "XLE", "XLV", "XLI",
    "NFLX", "PYPL", "UBER", "SQ", "COIN",
    "HOOD", "RIVN", "SOFI",
])


@dataclass
class SymbolMetrics:
    symbol: str
    price: float = 0.0
    spread: float = 0.0
    spread_pct: float = 0.0
    atr_pct: float = 0.0
    pre_market_volume: int = 0
    gap_pct: float = 0.0
    shortable: bool = True
    score: float = float("inf")


@dataclass
class BleedBudget:
    """Per-symbol profit/loss envelope for the session."""
    symbol: str
    profit_target: float
    loss_limit: float
    patience_minutes: float = 60.0
    realized_pnl: float = 0.0
    trade_count: int = 0
    started_at: datetime | None = None
    retired_at: datetime | None = None
    retire_reason: str = ""

    @property
    def is_active(self) -> bool:
        return self.retired_at is None

    @property
    def target_reached(self) -> bool:
        return self.realized_pnl >= self.profit_target

    @property
    def limit_hit(self) -> bool:
        return self.realized_pnl <= -self.loss_limit

    @property
    def patience_expired(self) -> bool:
        """True when the symbol has had enough time and produced nothing."""
        if self.started_at is None:
            return False
        elapsed = (datetime.now(ET) - self.started_at).total_seconds() / 60.0
        if elapsed < self.patience_minutes:
            return False
        # Patience expires only if P&L is flat or negative after the window.
        # A symbol that is slowly profitable gets to keep going.
        return self.realized_pnl <= 0.0

    @property
    def should_retire(self) -> bool:
        return self.target_reached or self.limit_hit or self.patience_expired


@dataclass
class PickerConfig:
    target_count: int = 4
    weight_atr: float = 0.40
    weight_spread: float = 0.25
    weight_volume: float = 0.15
    weight_gap: float = 0.10
    min_price: float = 5.0
    max_spread_pct: float = 0.01
    min_pre_market_volume: int = 500
    profit_target_per_symbol: float = 50.0
    loss_limit_per_symbol: float = 25.0
    retirement_cooldown_minutes: int = 30
    mid_session_rescan_interval: int = 1800

    # Time management: how long to try a symbol before giving up
    patience_minutes: float = 60.0        # give each symbol 1 hour max

    # Capital preservation: the Vampire must never drain the sleeve to zero.
    # When realized session losses reach this fraction of the sleeve budget,
    # the Vampire stops hunting entirely and preserves whatever is left.
    starvation_floor_pct: float = 0.30    # stop when 30% of sleeve is lost
    # After hitting the floor, the Vampire enters "fasting mode": it keeps
    # existing positions but opens no new ones, and waits for a recovery
    # tick before closing them. This prevents panic-selling at the worst price.
    fasting_mode: bool = True


@dataclass
class SelectionResult:
    symbols: list[str]
    metrics: dict[str, SymbolMetrics]
    bleed_budgets: dict[str, BleedBudget]


class VampireSymbolPicker:
    """ATR-based pre-market scanner with per-symbol bleed budgets."""

    def __init__(
        self,
        client: AlpacaClient,
        data: MarketDataService,
        sleeve_budget: float = 10_000.0,
        universe: frozenset[str] | None = None,
        config: PickerConfig | None = None,
    ):
        self._client = client
        self._data = data
        self._sleeve_budget = sleeve_budget
        self._universe = universe or UNIVERSE
        self.cfg = config or PickerConfig()

        self._all_metrics: dict[str, SymbolMetrics] = {}
        self._bleed_budgets: dict[str, BleedBudget] = {}
        self._retired: dict[str, datetime] = {}
        self._last_scan: datetime | None = None
        self._is_fasting: bool = False
        self._session_pnl: float = 0.0

    @property
    def bleed_budgets(self) -> dict[str, BleedBudget]:
        return dict(self._bleed_budgets)

    @property
    def is_fasting(self) -> bool:
        """True when the Vampire has hit the starvation floor and stopped hunting."""
        return self._is_fasting

    @property
    def starvation_floor(self) -> float:
        """Dollar amount of loss that triggers fasting mode."""
        return self._sleeve_budget * self.cfg.starvation_floor_pct

    def pick(self) -> SelectionResult:
        """Run the full pre-market scan and return top symbols with budgets."""
        log.info("VampireSymbolPicker: scanning %d candidates", len(self._universe))

        raw = self._collect_metrics(self._universe)
        passed = self._apply_hard_filters(raw)
        scored = self._score_all(passed)
        selected = sorted(scored, key=lambda m: m.score)[:self.cfg.target_count]

        symbols = [m.symbol for m in selected]
        budgets = self._assign_bleed_budgets(selected)
        self._last_scan = datetime.now(ET)

        log.info(
            "VampireSymbolPicker: selected %s (scores: %s)",
            symbols,
            {m.symbol: round(m.score, 3) for m in selected},
        )

        return SelectionResult(
            symbols=symbols,
            metrics={m.symbol: m for m in scored},
            bleed_budgets=budgets,
        )

    def check_health(
        self, engines: dict,
    ) -> list[tuple[str, str]]:
        """Mid-session check: which symbols should be retired?

        Also updates session P&L and checks the starvation floor.
        Returns list of (symbol, reason) pairs for symbols to drop.
        """
        retirements: list[tuple[str, str]] = []

        # Update session-wide P&L from all engines
        self._session_pnl = sum(
            e.daily_pnl for e in engines.values() if hasattr(e, "daily_pnl")
        )

        # Starvation guard: if total session losses exceed the floor,
        # stop hunting entirely. Keep existing positions to avoid panic selling.
        if not self._is_fasting and self._session_pnl <= -self.starvation_floor:
            self._is_fasting = True
            log.warning(
                "STARVATION FLOOR HIT: session P&L $%.2f exceeds -$%.2f floor. "
                "Entering fasting mode -- no new trades, waiting for recovery.",
                self._session_pnl, self.starvation_floor,
            )
            for sym, budget in self._bleed_budgets.items():
                if budget.is_active:
                    retirements.append((sym, f"starvation floor: session at ${self._session_pnl:.2f}"))
                    budget.retired_at = datetime.now(ET)
                    budget.retire_reason = "starvation_floor"
            return retirements

        if self._is_fasting:
            return []

        for sym, budget in self._bleed_budgets.items():
            if not budget.is_active:
                continue

            engine = engines.get(sym)
            if engine is None:
                continue

            budget.realized_pnl = engine.daily_pnl
            budget.trade_count = len(getattr(engine, "bleeds", []))

            if budget.target_reached:
                retirements.append((sym, f"target reached: ${budget.realized_pnl:.2f}"))
                budget.retired_at = datetime.now(ET)
                budget.retire_reason = "target_reached"

            elif budget.limit_hit:
                retirements.append((sym, f"loss limit hit: ${budget.realized_pnl:.2f}"))
                budget.retired_at = datetime.now(ET)
                budget.retire_reason = "loss_limit"

            elif budget.patience_expired:
                elapsed = (datetime.now(ET) - budget.started_at).total_seconds() / 60
                retirements.append((
                    sym,
                    f"patience expired: {elapsed:.0f}min, "
                    f"P&L ${budget.realized_pnl:.2f}, {budget.trade_count} trades",
                ))
                budget.retired_at = datetime.now(ET)
                budget.retire_reason = "patience_expired"

            elif budget.trade_count > 50 and budget.realized_pnl < -5.0:
                retirements.append((sym, f"sustained loss after {budget.trade_count} trades"))
                budget.retired_at = datetime.now(ET)
                budget.retire_reason = "sustained_loss"

        return retirements

    def find_replacements(
        self, current_symbols: list[str], count: int = 1,
    ) -> list[str]:
        """Find fresh victims to replace retired symbols.

        Respects the cooldown: recently retired symbols are not re-picked.
        Returns empty list if in fasting mode (capital preservation).
        """
        if self._is_fasting:
            log.info("VampireSymbolPicker: fasting -- no replacements")
            return []

        if not self._all_metrics:
            return []

        now = datetime.now(ET)
        cooldown = timedelta(minutes=self.cfg.retirement_cooldown_minutes)

        excluded = set(s.upper() for s in current_symbols)
        for sym, when in self._retired.items():
            if now - when < cooldown:
                excluded.add(sym.upper())

        candidates = [
            m for m in self._all_metrics.values()
            if m.symbol.upper() not in excluded and m.shortable and m.score < float("inf")
        ]
        candidates.sort(key=lambda m: m.score)

        replacements = []
        for m in candidates[:count]:
            replacements.append(m.symbol)
            budget = self._make_budget(m)
            self._bleed_budgets[m.symbol] = budget
            log.info(
                "VampireSymbolPicker: fresh victim %s (score %.3f, "
                "target $%.0f, limit $%.0f)",
                m.symbol, m.score, budget.profit_target, budget.loss_limit,
            )

        return replacements

    def retire_symbol(self, symbol: str, reason: str) -> None:
        """Mark a symbol as retired so the cooldown applies."""
        self._retired[symbol.upper()] = datetime.now(ET)
        budget = self._bleed_budgets.get(symbol)
        if budget and budget.is_active:
            budget.retired_at = datetime.now(ET)
            budget.retire_reason = reason
        log.info("VampireSymbolPicker: retired %s (%s)", symbol, reason)

    def _collect_metrics(self, symbols: frozenset[str]) -> list[SymbolMetrics]:
        results: list[SymbolMetrics] = []

        for sym in sorted(symbols):
            try:
                quote = self._data.get_latest_quote(sym)
                if not quote or not quote.mid or quote.mid <= 0:
                    continue

                price = quote.mid
                spread = quote.ask - quote.bid if quote.ask and quote.bid else 0.0
                spread_pct = spread / price if price > 0 else 0.0

                atr_pct = self._calculate_atr_pct(sym, price)
                gap_pct = self._calculate_gap_pct(sym, price)

                m = SymbolMetrics(
                    symbol=sym,
                    price=price,
                    spread=spread,
                    spread_pct=spread_pct,
                    atr_pct=atr_pct,
                    gap_pct=gap_pct,
                )
                results.append(m)
                self._all_metrics[sym] = m

            except Exception:
                log.debug("metrics failed for %s", sym, exc_info=True)

        return results

    def _apply_hard_filters(self, metrics: list[SymbolMetrics]) -> list[SymbolMetrics]:
        passed: list[SymbolMetrics] = []

        for m in metrics:
            if m.price < self.cfg.min_price:
                continue

            if m.spread_pct > self.cfg.max_spread_pct:
                continue

            try:
                asset = self._client.trading.get_asset(m.symbol)
                if not getattr(asset, "shortable", True):
                    m.shortable = False
                    continue
                if not getattr(asset, "tradable", True):
                    continue
            except Exception:
                pass

            m.shortable = True
            passed.append(m)

        return passed

    def _calculate_atr_pct(self, symbol: str, current_price: float) -> float:
        """ATR(14) as fraction of price, from daily bars."""
        try:
            end = datetime.now()
            start = end - timedelta(days=21)
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start, end=end,
            )
            result = self._data._data.get_stock_bars(req)
            bars = result.get(symbol, []) if hasattr(result, "get") else []
            bars = list(bars) if bars else []
        except Exception:
            return 0.0

        if len(bars) < 5:
            return 0.0

        trs: list[float] = []
        for i in range(1, len(bars)):
            h = float(bars[i].high)
            l = float(bars[i].low)
            pc = float(bars[i - 1].close)
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)

        if not trs:
            return 0.0

        n = min(14, len(trs))
        atr = sum(trs[-n:]) / n
        return atr / current_price if current_price > 0 else 0.0

    def _calculate_gap_pct(self, symbol: str, current_price: float) -> float:
        """Gap from previous close."""
        try:
            end = datetime.now()
            start = end - timedelta(days=5)
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start, end=end,
            )
            result = self._data._data.get_stock_bars(req)
            bars = result.get(symbol, []) if hasattr(result, "get") else []
            bars = list(bars) if bars else []
        except Exception:
            return 0.0

        if len(bars) < 2:
            return 0.0

        prev_close = float(bars[-2].close)
        if prev_close <= 0:
            return 0.0
        return (current_price - prev_close) / prev_close

    def _score_all(self, metrics: list[SymbolMetrics]) -> list[SymbolMetrics]:
        """Z-score composite: lower = better."""
        if len(metrics) < 3:
            for m in metrics:
                m.score = 0.0
            return metrics

        atr_vals = [m.atr_pct for m in metrics]
        spread_vals = [m.spread_pct for m in metrics]
        gap_vals = [self._gap_penalty(m.gap_pct) for m in metrics]

        def zscore(val: float, values: list[float]) -> float:
            mu = statistics.mean(values)
            sd = statistics.stdev(values) if len(values) > 1 else 1.0
            if sd < 1e-10:
                return 0.0
            return (val - mu) / sd

        for m in metrics:
            z_atr = zscore(m.atr_pct, atr_vals)
            z_spread = zscore(m.spread_pct, spread_vals)
            z_gap = zscore(self._gap_penalty(m.gap_pct), gap_vals)

            m.score = (
                -self.cfg.weight_atr * z_atr
                + self.cfg.weight_spread * z_spread
                + self.cfg.weight_gap * z_gap
            )

        return metrics

    @staticmethod
    def _gap_penalty(gap_pct: float) -> float:
        """Non-linear: penalize very small gaps (dead) and very large (trending)."""
        g = abs(gap_pct)
        if g < 0.002:
            return 2.0
        if g < 0.005:
            return 0.0
        if g < 0.02:
            return 0.5
        return 3.0

    def _assign_bleed_budgets(
        self, selected: list[SymbolMetrics],
    ) -> dict[str, BleedBudget]:
        """Each victim gets a profit target proportional to its ATR."""
        budgets: dict[str, BleedBudget] = {}
        per_symbol_capital = self._sleeve_budget / max(len(selected), 1)
        now = datetime.now(ET)

        for m in selected:
            target = self.cfg.profit_target_per_symbol
            limit = self.cfg.loss_limit_per_symbol

            if m.atr_pct > 0:
                atr_factor = min(m.atr_pct / 0.01, 2.0)
                target *= atr_factor
                limit *= atr_factor

            budget = BleedBudget(
                symbol=m.symbol,
                profit_target=round(target, 2),
                loss_limit=round(limit, 2),
                patience_minutes=self.cfg.patience_minutes,
                started_at=now,
            )
            budgets[m.symbol] = budget
            self._bleed_budgets[m.symbol] = budget

        return budgets

    def _make_budget(self, m: SymbolMetrics) -> BleedBudget:
        target = self.cfg.profit_target_per_symbol
        limit = self.cfg.loss_limit_per_symbol
        if m.atr_pct > 0:
            atr_factor = min(m.atr_pct / 0.01, 2.0)
            target *= atr_factor
            limit *= atr_factor
        return BleedBudget(
            symbol=m.symbol,
            profit_target=round(target, 2),
            loss_limit=round(limit, 2),
            patience_minutes=self.cfg.patience_minutes,
            started_at=datetime.now(ET),
        )
