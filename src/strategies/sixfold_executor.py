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
                 max_concurrent: int = MAX_CONCURRENT):
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

    def _held(self) -> dict[str, float]:
        snap = self._tracker.get_snapshot()
        return {s.upper(): abs(float(p.get("market_value", 0.0)))
                for s, p in snap.positions.items() if len(s) <= 6}

    def _underlyings_with_short_calls(self) -> set[str]:
        """Roots of every short call in the account. Never raises."""
        from src.risk.allocation import parse_occ
        out: set[str] = set()
        try:
            snap = self._tracker.get_snapshot()
            for s, p in snap.positions.items():
                occ = parse_occ(str(s).upper())
                if occ is None or float(p.get("qty", 0) or 0) >= 0:
                    continue
                if str(getattr(occ, "contract_type", "")).lower().startswith("c"):
                    out.add(str(occ.root).upper())
        except Exception:
            log.warning("could not read short calls; treating none as covered", exc_info=True)
        return out

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

    def run_disposals(self) -> list[dict]:
        """Exit names the analyst no longer rates as holdable.

        The analyst has always computed these (action "dispose" below 50, or
        "avoid" below 40) and nothing ever consumed them, so the executor
        could open a position and never close one. A scoring system whose
        sell signal is unreachable is a buy-only system wearing a score.

        Scope is deliberately just the disposition bands. SPEC 3.8 (exit) is
        tagged [UNKNOWN. Second most important gap], and the spec's own
        convention says [UNKNOWN] means implementation is blocked on it;
        every rule inside it is [P], "proposed default that Tashi must
        confirm or replace." So the +35% target, the gap-close exit and the
        6-month time stop are NOT implemented here - they are the document
        author's placeholders, not Tashi's rules, and building an automatic
        seller on them would invent an authority the spec explicitly
        disclaims. The bands below are different: they are the analyst's own
        thresholds, already in this codebase and already driving the buy
        side, so consuming them is self-consistent rather than invented.

        SPEC 3.7's -30% single-name rail is also absent on purpose: the spec
        says it "forces a logged human decision", not an automatic sale.

        Exits are NOT gated on the advisory council. The council is a buy
        gate; making an exit wait for AI approval would mean a cluster
        outage silently blocks the system from leaving a deteriorating
        position, which inverts the safety it exists to provide.
        """
        try:
            flagged = {s.upper() for s in self._analyst.get_disposal_candidates()}
        except Exception:
            log.exception("SIXFOLD analyst unavailable for disposals")
            return []
        if not flagged:
            return []

        held = self._held()
        covered = self._underlyings_with_short_calls()
        sold: list[dict] = []

        for sym in sorted(flagged & set(held)):
            if sym in self._excluded:
                # Another sleeve owns this ticker; selling it here would close
                # a position this strategy never opened.
                self._reject(sym, "flagged for disposal but owned by another sleeve")
                continue
            if sym in covered:
                # A short call is written against these shares. Selling them
                # turns a covered call into a naked one, which this account
                # cannot hold and which has unlimited loss. The call has to be
                # bought back first, and that is a human decision, not a
                # scoring outcome; a fundamentals feed that returns a blank
                # and scores the name 0 must not be able to trigger it.
                self._reject(sym, "has a covered call open; selling the shares would leave it naked")
                continue

            score_obj = None
            try:
                score_obj = self._analyst.scores.get(sym)
            except Exception:
                # Reporting only: the sale is driven by the analyst's action
                # band, not by this number, so an unreadable score must not
                # block the exit. Logged rather than swallowed.
                log.warning("%s: score unreadable for the disposal record",
                            sym, exc_info=True)
            composite = float(getattr(score_obj, "composite_score", 0.0) or 0.0)

            try:
                self._client.close_position(sym)
            except Exception:
                log.exception("SIXFOLD disposal failed for %s", sym)
                self._reject(sym, "broker rejected the disposal")
                continue

            entry = {"strategy": "sixfold", "symbol": sym, "side": "sell",
                     "notional": round(held[sym], 2), "score": composite,
                     "reason": f"SIXFOLD disposal: score {composite:.1f} below the hold band"}
            sold.append(entry)
            self.last_orders.append(entry)
            log.info("SIXFOLD disposed %s (score %.1f, $%.0f)",
                     sym, composite, held[sym])

        return sold

    def run_cycle(self) -> dict:
        self.last_orders, self.last_rejections = [], []

        if not self._breaker.check():
            return {"status": "breaker_active", "orders": []}

        # Disposals run before buys: a name the analyst has just downgraded
        # must not be bought back in the same cycle, and the freed capital
        # should be available to the buy pass that follows.
        disposed = self.run_disposals()

        sleeve = float(getattr(self._allocator.get_budget(), "sixfold_budget", 0.0))
        if sleeve <= 0:
            # Disposals already happened and must still be reported: an exit
            # is not conditional on there being budget left to buy with.
            return {"status": "no_sleeve", "orders": [], "disposals": disposed}

        try:
            candidates = self._analyst.get_buy_candidates()
        except Exception:
            log.exception("SIXFOLD analyst unavailable")
            return {"status": "analyst_error", "orders": [], "disposals": disposed}

        held = self._held()
        # Count only this sleeve's own positions against its concurrency
        # limit. _held() returns every equity position in the account, so
        # counting it raw let the scalper's 2-4 open names consume SIXFOLD's
        # 10-position budget: 8 held here plus 4 there is 12, so every new
        # candidate was rejected as "at the limit" and the sleeve sat at $38K
        # of its $50K with KO and HD both scoring above the buy threshold.
        # committed() already draws this boundary for dollars; the count has
        # to draw the same one.
        own_held = {k for k in held if k not in self._excluded}
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
            if len(placed) + len(own_held) >= self._max_concurrent:
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

            # ROIC is the framework's central question and its heaviest lens.
            # Where it cannot be computed at all -- a bank filing no classified
            # balance sheet, a filer tagging no operating income -- the lens now
            # scores a neutral 50 rather than a 0 that reads as "destroying
            # value". That correction is right for the ranking and wrong for the
            # buy list on its own: a neutral 50 on 25 points can carry a name
            # over the buy threshold on the strength of the five lenses that
            # happened to be computable. Not being able to answer the central
            # question is a reason to pass on a name, not a reason to rank it
            # low, so it is filtered here rather than penalised there.
            #
            # A score object that is missing or unreadable also fails this gate:
            # the default is False, so absence of evidence does not buy.
            if not bool(getattr(score_obj, "roic_measured", False)):
                self._reject(sym, "ROIC could not be measured; the framework's "
                                  "central question is unanswered")
                continue

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

        return {"status": "ok", "orders": placed, "disposals": disposed,
                "rejections": self.last_rejections}

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
