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


SECTOR_MAP = {
    "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "GOOGL": "tech",
    "AMZN": "consumer", "META": "tech", "TSLA": "consumer",
    "AMD": "tech", "INTC": "tech", "CRM": "tech", "ORCL": "tech",
    "JPM": "financials", "BAC": "financials", "GS": "financials",
    "NFLX": "consumer", "PYPL": "financials", "UBER": "consumer",
    "SQ": "financials", "COIN": "financials",
    "HOOD": "financials", "RIVN": "consumer", "SOFI": "financials",
    "SPY": "index", "QQQ": "index", "IWM": "index", "DIA": "index",
}


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
    sector: str = ""


@dataclass
class SpreadConfig:
    min_dte: int = 7
    max_dte: int = 21
    target_dte: int = 14
    spread_width_pct: float = 0.03
    max_otm_long_pct: float = 0.02
    min_open_interest: int = 50
    max_debit_per_spread: float = 500.0
    max_spreads_per_symbol: int = 2
    max_total_spreads: int = 5
    max_per_sector: int = 2
    take_profit_pct: float = 0.50
    stop_loss_pct: float = 0.50


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

    def _estimate_debit(
        self, long_call: OptionCandidate, short_call: OptionCandidate, width: float,
    ) -> float:
        """Net debit per share: long premium minus short premium.

        Uses premium_estimate from the options chain when available. Falls back
        to the midpoint heuristic (width * 0.55) when real quotes are missing,
        but this is a last resort -- the whole point of this fix is to stop
        relying on the heuristic that Day 1 proved inaccurate.
        """
        long_prem = long_call.premium_estimate
        short_prem = short_call.premium_estimate
        if long_prem is not None and short_prem is not None and long_prem > short_prem:
            return long_prem - short_prem
        return width * 0.55

    def _build_spreads(
        self, sym: str, calls: list[OptionCandidate], price: float
    ) -> list[SpreadCandidate]:
        """Pair calls into spreads: lower strike = long, higher strike = short."""
        spreads: list[SpreadCandidate] = []
        sector = SECTOR_MAP.get(sym.upper(), "other")

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

                net_debit = self._estimate_debit(long_call, short_call, width)
                max_profit = width - net_debit

                if net_debit > self.cfg.max_debit_per_spread / 100:
                    continue
                if net_debit <= 0 or max_profit <= 0:
                    continue

                breakeven = long_call.strike_price + net_debit
                score = self._score(width_pct, net_debit, max_profit,
                                    long_call.days_to_expiry, price, breakeven)

                spreads.append(SpreadCandidate(
                    underlying=sym,
                    long_call=long_call,
                    short_call=short_call,
                    spread_width=width,
                    max_debit=net_debit * 100,
                    max_profit=max_profit * 100,
                    breakeven=breakeven,
                    days_to_expiry=long_call.days_to_expiry,
                    score=score,
                    sector=sector,
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
        by_sector: dict[str, int] = {}

        for c in candidates:
            if len(placed) >= max_spreads:
                break
            if by_symbol.get(c.underlying, 0) >= self.cfg.max_spreads_per_symbol:
                self._reject(c.underlying, "max spreads per symbol reached")
                continue
            sector = c.sector or SECTOR_MAP.get(c.underlying.upper(), "other")
            if by_sector.get(sector, 0) >= self.cfg.max_per_sector:
                self._reject(c.underlying, f"sector {sector} at limit ({self.cfg.max_per_sector})")
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
                by_sector[sector] = by_sector.get(sector, 0) + 1
                placed.append(order)
                self.last_orders.append(order)

        return placed

    def check_exits(self, open_spreads: list[dict]) -> list[dict]:
        """Check open spreads for take-profit or stop-loss conditions.

        Returns a list of exit actions taken.
        """
        exits: list[dict] = []

        for spread in open_spreads:
            entry_debit = float(spread.get("max_debit", 0))
            max_profit = float(spread.get("max_profit", 0))
            order_id = spread.get("order_id", "")
            underlying = spread.get("underlying", "")

            if not entry_debit or not order_id:
                continue

            current_value = self._get_spread_value(spread)
            if current_value is None:
                continue

            pnl = current_value - entry_debit

            if max_profit > 0 and pnl >= max_profit * self.cfg.take_profit_pct:
                log.info(
                    "Bull spread %s: take profit at $%.0f (%.0f%% of max $%.0f)",
                    underlying, pnl,
                    (pnl / max_profit) * 100, max_profit,
                )
                exits.append({
                    "action": "take_profit", "underlying": underlying,
                    "pnl": round(pnl, 2), "order_id": order_id,
                })

            elif pnl <= -(entry_debit * self.cfg.stop_loss_pct):
                log.info(
                    "Bull spread %s: stop loss at $%.0f (%.0f%% of debit $%.0f)",
                    underlying, pnl,
                    abs(pnl / entry_debit) * 100, entry_debit,
                )
                exits.append({
                    "action": "stop_loss", "underlying": underlying,
                    "pnl": round(pnl, 2), "order_id": order_id,
                })

        return exits

    def _get_spread_value(self, spread: dict) -> float | None:
        """Estimate current spread value from underlying price movement."""
        underlying = spread.get("underlying", "")
        if not underlying:
            return None

        try:
            quote = self._data.get_latest_quote(underlying)
            if not quote or not quote.mid:
                return None
        except Exception:
            return None

        price = quote.mid
        strike_low = float(spread.get("strike_low", 0))
        strike_high = float(spread.get("strike_high", 0))
        entry_debit = float(spread.get("max_debit", 0))

        if strike_low <= 0 or strike_high <= strike_low:
            return None

        width = strike_high - strike_low
        if price <= strike_low:
            intrinsic = 0.0
        elif price >= strike_high:
            intrinsic = width * 100
        else:
            intrinsic = (price - strike_low) * 100

        time_value_fraction = max(0.1, float(spread.get("dte", 14)) / 30.0)
        return intrinsic + (entry_debit - intrinsic) * time_value_fraction * 0.5

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

            order = self._client.submit_order(req)

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
