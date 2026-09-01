"""Pendulum agent -- runs the mean-reversion signal once a day and acts on it.

Tashi's spec is written as signals-only: compute on the close, act at the next
open, place the order by hand. This runs the same rules unattended, which is
the one liberty taken with the document, and it is taken because the account
is paper.

Everything else follows the spec literally, including the parts that make it
trade rarely. The regime filter is the reason the strategy survives a 2022,
and loosening it to manufacture activity would remove the only thing standing
between this sleeve and a twelve-month drawdown.
"""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from alpaca.data.enums import Adjustment
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from src.strategies.pendulum import (
    PendulumParams, Position, Signal, compute_indicators, decide, stop_price,
)

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
LIMIT_SLIPPAGE = 0.002      # cross by 20bp: a marketable limit, not a market order
HISTORY_DAYS = 420          # enough for the 200-day regime SMA plus slack


class PendulumAgent:
    def __init__(self, client, data, tracker, breaker, allocator,
                 symbol: str = "TLT", params: PendulumParams | None = None,
                 risk_per_trade: float = 0.01, first_tranche: float = 0.6,
                 run_after: dt.time = dt.time(9, 35),
                 history_client=None):
        self._client = client
        self._data = data
        self._tracker = tracker
        self._breaker = breaker
        self._allocator = allocator
        self.symbol = symbol.upper()
        self.p = params or PendulumParams()
        self._risk_per_trade = risk_per_trade
        self._first_tranche = first_tranche
        self._run_after = run_after
        self._history = history_client
        self._last_run_date: dt.date | None = None
        self._added = False
        self._entry_date: dt.date | None = None
        self.last_signal: dict | None = None

    # ---------------------------------------------------------------- data
    def _daily_bars(self) -> list:
        """Completed daily bars only.

        Today's bar exists from the opening print onward and is partial until
        the close. Feeding it to the indicators would compute a 'close' that
        is really a mid-morning price, so today is dropped explicitly rather
        than trusted to be absent.
        """
        client = self._history or getattr(self._data, "_client", None) or self._data
        req = StockBarsRequest(
            symbol_or_symbols=self.symbol, timeframe=TimeFrame.Day,
            start=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=HISTORY_DAYS),
            adjustment=Adjustment.ALL, feed="sip",
        )
        bars = list(client.get_stock_bars(req).data.get(self.symbol, []))
        today = dt.datetime.now(ET).date()
        return [b for b in bars if b.timestamp.astimezone(ET).date() < today]

    def _broker_position(self) -> Position | None:
        """Read the position from the broker, never from memory.

        The scalper's worst incident came from a process-local counter that
        survived a restart while the shares did not. This asks the account.
        """
        try:
            snap = self._tracker.get_snapshot()
            pos = snap.positions.get(self.symbol) or snap.positions.get(self.symbol.upper())
        except Exception:
            log.exception("%s: could not read positions", self.symbol)
            raise
        if not pos:
            return None
        qty = int(float(pos.get("qty", 0)))
        if qty <= 0:
            return None
        held = 0
        if self._entry_date:
            held = sum(1 for d in _weekdays_between(self._entry_date, dt.datetime.now(ET).date()))
        return Position(entry_price=float(pos.get("avg_entry_price", 0.0) or 0.0),
                        shares=qty, bars_held=held,
                        tranches=2 if self._added else 1)

    # ---------------------------------------------------------------- sizing
    def _size(self, price: float, ind, is_add: bool) -> int:
        """Risk-based size, capped by the sleeve.

        Per the spec the stop distance sets the size, so a wider stop buys
        fewer shares and every trade risks about the same dollars. The sleeve
        is then a hard ceiling on top of that, because a risk calculation that
        is allowed to exceed its allocation is not a limit.
        """
        equity = float(self._tracker.get_snapshot().equity)
        sleeve = float(getattr(self._allocator.get_budget(), "pendulum_budget", 0.0))
        if sleeve <= 0 or price <= 0:
            return 0
        stop = stop_price(price, ind.atr, self.p)
        distance = max(price - stop, price * 0.005)     # never divide by ~0
        by_risk = (equity * self._risk_per_trade) / distance
        share_of_sleeve = sleeve * ((1 - self._first_tranche) if is_add else self._first_tranche)
        by_sleeve = share_of_sleeve / price
        used = float(getattr(self._allocator.get_budget(), "pendulum_used", 0.0))
        headroom = max(0.0, sleeve - used) / price
        return int(min(by_risk, by_sleeve, headroom))

    # ---------------------------------------------------------------- cycle
    def should_run(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(ET)
        if now.weekday() >= 5:
            return False
        if now.time() < self._run_after:
            return False
        return self._last_run_date != now.date()

    def run_cycle(self, now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now(ET)
        if not self._breaker.check():
            return {"status": "breaker_active"}

        bars = self._daily_bars()
        if len(bars) < self.p.regime_lookback + 5:
            return {"status": "insufficient_history", "bars": len(bars)}

        highs = [float(b.high) for b in bars]
        lows = [float(b.low) for b in bars]
        closes = [float(b.close) for b in bars]
        ind = compute_indicators(highs, lows, closes, self.p)
        pos = self._broker_position()
        sig, why = decide(ind, pos, self.p)

        self._last_run_date = now.date()
        self.last_signal = {
            "date": str(bars[-1].timestamp.astimezone(ET).date()),
            "symbol": self.symbol, "signal": sig.value, "reason": why,
            "close": round(ind.close, 2),
            "sma20": round(ind.sma, 2) if ind.sma else None,
            "z": round(ind.z, 2) if ind.z is not None else None,
            "rsi2": round(ind.rsi, 1) if ind.rsi is not None else None,
            "sma200": round(ind.sma_regime, 2) if ind.sma_regime else None,
            "atr": round(ind.atr, 2) if ind.atr else None,
            "position": pos.shares if pos else 0,
            "stop": round(stop_price(pos.entry_price, ind.atr, self.p), 2) if pos else None,
        }
        log.info("PENDULUM %s %s: %s", self.symbol, sig.value, why)

        if sig in (Signal.HOLD, Signal.NO_TRADE):
            return {"status": "ok", **self.last_signal, "action": "none"}

        quote = self._quote()
        if not quote or quote <= 0:
            return {"status": "no_quote", **self.last_signal}

        if sig is Signal.EXIT:
            return {**self._exit(), **self.last_signal}
        return {**self._enter(quote, ind, is_add=(sig is Signal.ADD)), **self.last_signal}

    def _enter(self, price: float, ind, is_add: bool) -> dict:
        qty = self._size(price, ind, is_add)
        if qty < 1:
            return {"status": "size_zero", "action": "none"}
        notional = qty * price
        if not self._breaker.can_trade(self.symbol, notional):
            return {"status": "risk_blocked", "action": "none"}
        limit = round(price * (1 + LIMIT_SLIPPAGE), 2)
        try:
            order = self._client.trading.submit_order(LimitOrderRequest(
                symbol=self.symbol, qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, limit_price=limit))
        except Exception:
            log.exception("PENDULUM entry failed for %s", self.symbol)
            return {"status": "order_failed", "action": "none"}
        if is_add:
            self._added = True
        else:
            self._entry_date = dt.datetime.now(ET).date()
        self._tracker.record_trade(symbol=self.symbol, side="buy", qty=qty,
                                   price=limit, strategy="pendulum")
        log.info("PENDULUM bought %d %s at %.2f ($%.0f)",
                 qty, self.symbol, limit, notional)
        return {"status": "ok", "action": "add" if is_add else "buy",
                "qty": qty, "limit": limit, "notional": round(notional, 2),
                "order_id": str(getattr(order, "id", ""))}

    def _exit(self) -> dict:
        try:
            self._client.close_position(self.symbol)
        except Exception:
            log.exception("PENDULUM exit failed for %s", self.symbol)
            return {"status": "exit_failed", "action": "none"}
        self._entry_date = None
        self._added = False
        return {"status": "ok", "action": "exit"}

    def _quote(self) -> float | None:
        try:
            q = self._data.get_latest_quote(self.symbol)
        except Exception:
            log.warning("PENDULUM quote failed for %s", self.symbol, exc_info=True)
            return None
        return float(q.mid) if q and getattr(q, "mid", None) else None


def _weekdays_between(a: dt.date, b: dt.date):
    d = a
    while d < b:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            yield d
