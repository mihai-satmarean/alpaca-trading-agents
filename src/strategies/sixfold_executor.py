"""Turns SIXFOLD's recommendations into orders.

The analyst scores names and stops there, so half the account could not be
deployed. This is the missing half: it sizes candidates against the sixfold
sleeve and routes every order through the same gates as the other strategies.

The gates are not optional here. This is the largest sleeve and the only
strategy in the system whose signal has never placed an order, so it gets the
strictest treatment rather than the most trusting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from src.core.finance_advisor import evaluate_equity_buy
from src.strategies.bull_call_spread import BullCallSpreadStrategy, SpreadConfig

log = logging.getLogger(__name__)

# Buying the same ticker the scalper trades makes the resulting position
# unattributable between two sleeves, so the overlap is simply not traded here.
MAX_CONCURRENT = 10
LIMIT_SLIPPAGE = 0.002       # cross by 20bp so a marketable limit actually fills


@dataclass
class SixfoldOrder:
    symbol: str
    qty: int
    limit_price: float
    notional: float
    score: float
    reason: str


class SixfoldExecutor:
    def __init__(self, client, data, tracker, breaker, allocator, analyst,
                 excluded: set[str] | None = None,
                 max_concurrent: int = MAX_CONCURRENT,
                 chain=None,
                 enable_spreads: bool = True):
        self._client = client
        self._data = data
        self._tracker = tracker
        self._breaker = breaker
        self._allocator = allocator
        self._analyst = analyst
        self._excluded = {s.upper() for s in (excluded or set())}
        self._max_concurrent = max_concurrent
        self.last_orders: list[dict] = []
        self.last_rejections: list[dict] = []
        self._spread_strategy: BullCallSpreadStrategy | None = None
        if enable_spreads and chain is not None:
            self._spread_strategy = BullCallSpreadStrategy(
                client=client, chain=chain, data=data, tracker=tracker,
                allocator=allocator, breaker=breaker,
            )

    def _held(self) -> dict[str, float]:
        snap = self._tracker.get_snapshot()
        return {s.upper(): abs(float(p.get("market_value", 0.0)))
                for s, p in snap.positions.items() if len(s) <= 6}

    def position_budget(self) -> float:
        """What one name may consume: the sleeve split N ways, capped by the
        portfolio's own per-trade limit, whichever is smaller."""
        sleeve = float(getattr(self._allocator.get_budget(), "sixfold_budget", 0.0))
        per_name = sleeve / self._max_concurrent if self._max_concurrent else 0.0
        equity = self._tracker.get_snapshot().equity
        cap = equity * getattr(self._breaker.limits, "max_single_trade_pct", 0.05)
        return max(0.0, min(per_name, cap))

    def committed(self) -> float:
        held = self._held()
        return sum(v for k, v in held.items() if k not in self._excluded)

    def run_cycle(self) -> dict:
        self.last_orders, self.last_rejections = [], []

        if not self._breaker.check():
            return {"status": "breaker_active", "orders": []}

        sleeve = float(getattr(self._allocator.get_budget(), "sixfold_budget", 0.0))
        if sleeve <= 0:
            return {"status": "no_sleeve", "orders": []}

        try:
            candidates = self._analyst.get_buy_candidates()
        except Exception:
            log.exception("SIXFOLD analyst unavailable")
            return {"status": "analyst_error", "orders": []}

        held = self._held()
        budget_each = self.position_budget()
        room = sleeve - self.committed()
        placed: list[dict] = []

        for symbol in candidates:
            sym = symbol.upper()
            if sym in self._excluded:
                self._reject(sym, "traded by another sleeve; would be unattributable")
                continue
            if sym in held:
                self._reject(sym, "already held")
                continue
            if len(placed) + len(held) >= self._max_concurrent:
                self._reject(sym, f"at the {self._max_concurrent}-position limit")
                continue

            quote = self._quote(sym)
            if not quote or quote <= 0:
                self._reject(sym, "no usable quote")
                continue

            qty = int(budget_each // quote)
            if qty < 1:
                self._reject(sym, f"one share (${quote:,.2f}) exceeds the "
                                  f"${budget_each:,.0f} per-name budget")
                continue

            notional = qty * quote
            if notional > room:
                self._reject(sym, f"${notional:,.0f} exceeds ${room:,.0f} of sleeve left")
                continue
            if not self._breaker.can_trade(sym, notional):
                self._reject(sym, "blocked by portfolio risk limits")
                continue

            # SixfoldScore is a dataclass whose field is composite_score; the
            # previous dict-style read silently produced 0.0 for every real
            # call, so each advisor was told the quant system scored the name
            # 0/100 while recommending it, which invites a veto of everything.
            try:
                score_obj = self._analyst.scores.get(sym)
            except Exception:
                score_obj = None
            composite = float(getattr(score_obj, "composite_score", 0.0) or 0.0)
            council = evaluate_equity_buy(sym, composite)
            if not council.approved:
                reasons = "; ".join(
                    f"{o.role}: {o.reasoning[:60]}"
                    for o in council.opinions if o.verdict == "reject"
                )
                self._reject(sym, f"Council rejected ({council.summary}): {reasons[:120]}")
                continue

            limit = round(quote * (1 + LIMIT_SLIPPAGE), 2)
            try:
                order = self._client.trading.submit_order(
                    LimitOrderRequest(symbol=sym, qty=qty, side=OrderSide.BUY,
                                      time_in_force=TimeInForce.DAY, limit_price=limit)
                )
            except Exception:
                log.exception("SIXFOLD order failed for %s", sym)
                self._reject(sym, "broker rejected the order")
                continue

            room -= notional
            council_detail = [
                {"role": o.role, "verdict": o.verdict, "reasoning": o.reasoning[:120]}
                for o in council.opinions if o.responded
            ]
            entry = {"strategy": "sixfold", "symbol": sym, "side": "buy", "qty": qty,
                     "limit_price": limit, "notional": round(notional, 2),
                     "order_id": str(getattr(order, "id", "")),
                     "reason": f"SIXFOLD buy, council {council.summary}",
                     "council": council_detail}
            placed.append(entry)
            self.last_orders.append(entry)
            self._tracker.record_trade(symbol=sym, side="buy", qty=qty,
                                       price=limit, strategy="sixfold")
            log.info("SIXFOLD bought %d %s at %.2f (%s)", qty, sym, limit, f"${notional:,.0f}")

        spread_orders = self._run_spreads(candidates)

        return {
            "status": "ok",
            "orders": placed,
            "spread_orders": spread_orders,
            "rejections": self.last_rejections,
        }

    def _run_spreads(self, candidates: list) -> list[dict]:
        """Open bull call spreads on SIXFOLD buy candidates.

        Allocates up to 20% of the SIXFOLD sleeve to spreads. This gives
        leveraged upside exposure with defined max loss (the debit paid),
        complementing the direct equity positions.
        """
        if self._spread_strategy is None:
            return []

        symbols = [s.upper() for s in candidates
                   if s.upper() not in self._excluded]
        if not symbols:
            return []

        try:
            orders = self._spread_strategy.execute(symbols)
            for o in orders:
                self.last_orders.append(o)
            return orders
        except Exception:
            log.exception("Bull call spread execution failed")
            return []

    def _quote(self, symbol: str) -> float | None:
        try:
            q = self._data.get_latest_quote(symbol)
        except Exception:
            log.warning("quote failed for %s", symbol, exc_info=True)
            return None
        return float(q.mid) if q and getattr(q, "mid", None) else None

    def _reject(self, symbol: str, reason: str) -> None:
        if len(self.last_rejections) < 20:
            self.last_rejections.append({"symbol": symbol, "reason": reason})
