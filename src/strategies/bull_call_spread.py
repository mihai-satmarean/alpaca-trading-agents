"""Bull Call Spread strategy: express SIXFOLD bullish thesis with defined risk.

A bull call spread buys a lower-strike call and sells a higher-strike call
on the same underlying and expiration.  The debit paid is the maximum loss;
the spread width minus the debit is the maximum gain.

This strategy is triggered by SIXFOLD buy candidates: instead of (or alongside)
buying shares, the executor can open a bull call spread to achieve leveraged
upside with a hard cap on downside.

Alpaca Level 3 multi-leg orders are used: order_class=MLEG, two OptionLegRequest
items per spread.  Paper accounts have Level 3 by default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, OptionLegRequest

from src.core.alpaca_client import AlpacaClient
from src.core.market_data import MarketDataService
from src.core.options_chain import OptionsChain, OptionCandidate
from src.core.position_tracker import PositionTracker

log = logging.getLogger(__name__)


@dataclass
class SpreadCandidate:
    underlying: str
    long_call: OptionCandidate
    short_call: OptionCandidate
    spread_width: float
    max_debit: float
    max_profit: float
    breakeven: float
    days_to_expiry: int
    score: float


@dataclass
class SpreadConfig:
    min_dte: int = 14
    max_dte: int = 45
    target_dte: int = 30
    spread_width_pct: float = 0.05
    max_otm_long_pct: float = 0.03
    min_open_interest: int = 50
    max_debit_per_spread: float = 500.0
    max_spreads_per_symbol: int = 2
    max_total_spreads: int = 5


class BullCallSpreadStrategy:
    """Scans for bull call spread opportunities on SIXFOLD-recommended symbols."""

    def __init__(
        self,
        client: AlpacaClient,
        chain: OptionsChain,
        data: MarketDataService,
        tracker: PositionTracker,
        config: SpreadConfig | None = None,
        allocator=None,
        breaker=None,
    ):
        self._client = client
        self._chain = chain
        self._data = data
        self._tracker = tracker
        self.cfg = config or SpreadConfig()
        self._allocator = allocator
        self._breaker = breaker
        self.last_orders: list[dict] = []
        self.last_rejections: list[dict] = []

    def scan(self, symbols: list[str]) -> list[SpreadCandidate]:
        """Find bull call spread candidates for the given symbols."""
        candidates: list[SpreadCandidate] = []

        for sym in symbols:
            quote = self._data.get_latest_quote(sym)
            if not quote or not quote.mid or quote.mid <= 0:
                continue

            price = quote.mid
            strike_low = price * (1 - self.cfg.max_otm_long_pct)
            strike_high = price * (1 + self.cfg.spread_width_pct + self.cfg.max_otm_long_pct)

            calls = self._chain.get_calls(
                underlying=sym,
                min_dte=self.cfg.min_dte,
                max_dte=self.cfg.max_dte,
                strike_gte=strike_low,
                strike_lte=strike_high,
            )

            if len(calls) < 2:
                continue

            calls = self._chain.select_best_expiry(calls, target_dte=self.cfg.target_dte)
            calls.sort(key=lambda c: c.strike_price)

            for spread in self._build_spreads(sym, calls, price):
                candidates.append(spread)

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    def _build_spreads(
        self, sym: str, calls: list[OptionCandidate], price: float
    ) -> list[SpreadCandidate]:
        """Pair calls into spreads: lower strike = long, higher strike = short."""
        spreads: list[SpreadCandidate] = []

        for i, long_call in enumerate(calls):
            if long_call.strike_price > price * (1 + self.cfg.max_otm_long_pct):
                continue

            for short_call in calls[i + 1:]:
                width = short_call.strike_price - long_call.strike_price
                width_pct = width / price

                if width_pct < self.cfg.spread_width_pct * 0.5:
                    continue
                if width_pct > self.cfg.spread_width_pct * 2.0:
                    break

                estimated_debit = width * 0.55
                max_profit = width - estimated_debit

                if estimated_debit > self.cfg.max_debit_per_spread:
                    continue
                if estimated_debit <= 0 or max_profit <= 0:
                    continue

                breakeven = long_call.strike_price + estimated_debit
                score = self._score(width_pct, estimated_debit, max_profit,
                                    long_call.days_to_expiry, price, breakeven)

                spreads.append(SpreadCandidate(
                    underlying=sym,
                    long_call=long_call,
                    short_call=short_call,
                    spread_width=width,
                    max_debit=estimated_debit * 100,
                    max_profit=max_profit * 100,
                    breakeven=breakeven,
                    days_to_expiry=long_call.days_to_expiry,
                    score=score,
                ))

        return spreads

    def _score(
        self,
        width_pct: float,
        debit: float,
        max_profit: float,
        dte: int,
        price: float,
        breakeven: float,
    ) -> float:
        """Higher is better. Favors high reward/risk, near-ATM, 20-35 DTE."""
        reward_risk = max_profit / debit if debit > 0 else 0
        rr_score = min(reward_risk * 2, 5.0)

        breakeven_distance = (breakeven - price) / price
        proximity_score = max(0, 3.0 - breakeven_distance * 50)

        dte_score = 2.0 if 20 <= dte <= 35 else 1.0

        return rr_score + proximity_score + dte_score

    def execute(
        self,
        symbols: list[str],
        budget: float | None = None,
        max_spreads: int | None = None,
    ) -> list[dict]:
        """Scan and place bull call spread orders via Alpaca multi-leg API."""
        self.last_orders, self.last_rejections = [], []
        max_spreads = max_spreads or self.cfg.max_total_spreads

        if budget is None and self._allocator is not None:
            budget = float(getattr(self._allocator.get_budget(), "sixfold_budget", 0.0)) * 0.20
        budget = budget or 5000.0

        candidates = self.scan(symbols)
        placed: list[dict] = []
        committed = 0.0
        by_symbol: dict[str, int] = {}

        for c in candidates:
            if len(placed) >= max_spreads:
                break
            if by_symbol.get(c.underlying, 0) >= self.cfg.max_spreads_per_symbol:
                self._reject(c.underlying, "max spreads per symbol reached")
                continue
            if committed + c.max_debit > budget:
                self._reject(c.underlying, f"${c.max_debit:.0f} debit exceeds "
                             f"${budget - committed:.0f} remaining budget")
                continue

            if self._breaker is not None and not self._breaker.can_trade(
                c.underlying, c.max_debit
            ):
                self._reject(c.underlying, "blocked by risk limits")
                continue

            order = self._place_spread(c)
            if order:
                committed += c.max_debit
                by_symbol[c.underlying] = by_symbol.get(c.underlying, 0) + 1
                placed.append(order)
                self.last_orders.append(order)

        return placed

    def _place_spread(self, c: SpreadCandidate) -> dict | None:
        """Submit a multi-leg bull call spread order to Alpaca."""
        try:
            legs = [
                OptionLegRequest(
                    symbol=c.long_call.symbol,
                    side=OrderSide.BUY,
                    ratio_qty=1,
                ),
                OptionLegRequest(
                    symbol=c.short_call.symbol,
                    side=OrderSide.SELL,
                    ratio_qty=1,
                ),
            ]

            req = MarketOrderRequest(
                qty=1,
                order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY,
                legs=legs,
            )

            order = self._client.trading.submit_order(req)

            log.info(
                "Bull call spread placed: %s long %s / short %s, "
                "width $%.2f, max debit $%.0f",
                c.underlying, c.long_call.symbol, c.short_call.symbol,
                c.spread_width, c.max_debit,
            )

            self._tracker.record_trade(
                symbol=f"{c.underlying}-spread",
                side="bull_call_spread",
                qty=1,
                price=c.max_debit / 100,
                strategy="sixfold_spread",
            )

            return {
                "strategy": "bull_call_spread",
                "underlying": c.underlying,
                "long_call": c.long_call.symbol,
                "short_call": c.short_call.symbol,
                "strike_low": c.long_call.strike_price,
                "strike_high": c.short_call.strike_price,
                "spread_width": c.spread_width,
                "max_debit": c.max_debit,
                "max_profit": c.max_profit,
                "breakeven": c.breakeven,
                "dte": c.days_to_expiry,
                "order_id": str(getattr(order, "id", "")),
                "score": c.score,
            }

        except Exception:
            log.exception("Failed to place bull call spread for %s", c.underlying)
            self._reject(c.underlying, "broker rejected the spread order")
            return None

    def _reject(self, symbol: str, reason: str) -> None:
        if len(self.last_rejections) < 20:
            self.last_rejections.append({"symbol": symbol, "reason": reason})
