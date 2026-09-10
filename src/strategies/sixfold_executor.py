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
import time
from dataclasses import dataclass

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from src.core import sixfold_registry as registry
from src.core.finance_advisor import evaluate_equity_buy
from src.core.notify import notify
from src.strategies.sixfold_exits import DEFAULT_CFG, ExitCfg, exit_decision

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
                 exit_cfg: ExitCfg = DEFAULT_CFG,
                 reentry_lockout_days: float = 56.0):
        self._client = client
        self._data = data
        self._tracker = tracker
        self._breaker = breaker
        self._allocator = allocator
        self._analyst = analyst
        self._excluded = {s.upper() for s in (excluded or set())}
        self._max_concurrent = max_concurrent
        self._exit_cfg = exit_cfg
        self._reentry_lockout_days = reentry_lockout_days
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
        """Exit names that should no longer be held, on any of three rules.

        1. Score-dispose: the analyst's own action band (below the hold
           threshold). This is the ORIGINAL rule and its scope is
           unchanged: any held name the analyst currently flags, whether or
           not this process registered opening it. See the reasoning this
           docstring used to carry for why the two rules below were once
           deliberately left out.

        2 & 3. Time stop (default 183 days) and take-profit (default +35%),
           ported from the live Tradier deployment of this same framework
           2026-09-10, scoped to symbols src/core/sixfold_registry.py
           recorded THIS process opening. A CSP-assignment share, a
           pre-existing position, or a buy whose fill was never confirmed
           has no recorded age and cannot trigger either rule -- only the
           score-dispose rule above can sell it, exactly as before this
           change. Frank made the call to port these after reviewing that
           the live Tradier side already runs them; see the plan file for
           the full reasoning this docstring used to hold.

        Iteration is deliberately over held names that are EITHER
        score-flagged OR registry-tracked, not score-flagged alone: a
        winning position by definition will not be score-flagged (a good
        score is why it was bought), so gating the loop on the analyst's
        disposal set alone would make the time stop and take-profit dead
        code for the scenario they exist to handle.

        registry.reconcile() runs first and clears any registry entry for a
        symbol no longer actually held -- the self-healing half of the
        optimistic open-date write at buy time (see run_cycle): an order
        that never filled or was canceled leaves at most one cycle's worth
        of phantom entry before this clears it, rather than a synchronous
        fill-confirmation poll added to the buy loop's hot path.

        Exits are NOT gated on the advisory council. The council is a buy
        gate; making an exit wait for AI approval would mean a cluster
        outage silently blocks the system from leaving a deteriorating
        position, which inverts the safety it exists to provide.
        """
        held = self._held()
        registry.reconcile(set(held))

        try:
            flagged = {s.upper() for s in self._analyst.get_disposal_candidates()}
        except Exception:
            log.exception("SIXFOLD analyst unavailable for disposals")
            flagged = set()

        registry_tracked = registry.open_symbols()
        candidates = (flagged | registry_tracked) & set(held)
        if not candidates:
            return []

        covered = self._underlyings_with_short_calls()
        sold: list[dict] = []

        for sym in sorted(candidates):
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
                log.warning("%s: score unreadable for the disposal record",
                            sym, exc_info=True)
            composite = getattr(score_obj, "composite_score", None)
            composite = float(composite) if composite is not None else None

            plpc = None
            try:
                snap = self._tracker.get_snapshot()
                plpc = float(snap.positions.get(sym, {}).get("unrealized_plpc"))
            except (TypeError, ValueError, KeyError):
                plpc = None
            age = registry.age_days(sym)
            is_tracked = sym in registry_tracked

            decision = exit_decision(plpc, sym in flagged, age, is_tracked, self._exit_cfg)
            if not decision.exit:
                continue

            try:
                self._client.close_position(sym)
            except Exception:
                log.exception("SIXFOLD disposal failed for %s", sym)
                self._reject(sym, "broker rejected the disposal")
                continue

            # The score itself is reporting only, same as before this change:
            # the disposal DECISION already came from the analyst's action
            # band (sym in flagged) or the registry-scoped rules above, so an
            # unreadable score enriches this string but never blocks the sale.
            reason = decision.reason
            if reason == "score below the hold band" and composite is not None:
                reason = f"score deteriorated to {composite:.1f}"

            registry.record_disposal(sym, reason, self._reentry_lockout_days)
            registry.clear(sym)

            entry = {"strategy": "sixfold", "symbol": sym, "side": "sell",
                     "notional": round(held[sym], 2), "score": composite,
                     "reason": f"SIXFOLD disposal: {reason}"}
            sold.append(entry)
            self.last_orders.append(entry)
            log.info("SIXFOLD disposed %s (%s, $%.0f)", sym, reason, held[sym])

            if is_tracked and ("time stop" in decision.reason or "profit target" in decision.reason):
                # New triggers only. The pre-existing score-dispose path has
                # never alerted, and retrofitting that is a separate
                # decision from porting these two rules; see the plan file.
                notify(
                    f"SIXFOLD disposed {sym}",
                    f"{decision.reason}. ${held[sym]:,.0f}, "
                    f"{self._reentry_lockout_days:.0f}-day re-entry lockout applied.",
                    severity="default",
                )

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

        # Read once for the whole cycle, not per candidate: the live universe
        # is the S&P 400, and re-parsing a file per candidate would run
        # hundreds of times per 10-minute cycle. Fails CLOSED, deliberately:
        # an unreadable lockout table must not read as "nothing is locked
        # out", the one check whose purpose is not immediately rebuying a
        # name just sold. See sixfold_registry.load_lockouts().
        try:
            lockouts = registry.load_lockouts()
        except Exception:
            log.exception("SIXFOLD lockout table unreadable; skipping the buy pass entirely")
            return {"status": "lockouts_unreadable", "orders": [], "disposals": disposed}

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
            if sym in lockouts:
                self._reject(sym, f"re-entry locked out until "
                                  f"{time.strftime('%Y-%m-%d', time.gmtime(lockouts[sym]))}")
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
            # Optimistic: this DAY limit order may not fill. run_disposals()'s
            # registry.reconcile() clears this entry on the next cycle if the
            # symbol never actually ends up held, so an unfilled or canceled
            # order leaves at most one cycle's phantom entry rather than
            # needing a synchronous fill-confirmation poll here.
            registry.record_open(sym)
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
