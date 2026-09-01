"""Vampire algorithm -- bi-directional micro-scalper.

Ported from the spec at notes/2026-08-29-vampire-algorithm-spec.md.
Bleeds micro-profits from every price oscillation on liquid tickers.
Trades both directions: buys dips, shorts rips.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from enum import Enum
from zoneinfo import ZoneInfo

from alpaca.trading.enums import OrderSide, TimeInForce

from src.core.alpaca_client import AlpacaClient
from src.core.market_data import MarketDataService
from src.core.position_tracker import PositionTracker

log = logging.getLogger(__name__)

SESSION_START = dt_time(9, 30)
SESSION_END = dt_time(15, 55)

# 2026-09-01, the first live session on EC2: 28 orders exceeded the 2.0s poll
# and fell through to "assume filled." Every one of them landed between
# 09:30:33 and 09:37:15 - two dense clusters in the opening minutes, zero
# anywhere in the 25+ minutes that followed. Opening order flow is
# congested in a way the rest of the session isn't; the fix is a wider
# window there, not a permanently longer one, since a longer poll blocks
# tick processing for every order that takes it, and that cost is worst
# exactly when the market is moving fastest.
OPENING_WINDOW_END = dt_time(9, 40)


class VampireState(str, Enum):
    IDLE = "idle"
    WATCHING = "watching"
    STOPPED = "stopped"


@dataclass
class BleedRecord:
    timestamp: datetime
    symbol: str
    action: str
    qty: float
    delta: float
    pnl_estimate: float


@dataclass
class VampireConfig:
    symbol: str = "SPY"
    tick_threshold: float = 0.02
    position_size: int = 10
    max_position: int = 100
    bleed_window_seconds: int = 5
    max_daily_loss: float = 50.0
    max_trades_per_min: int = 20
    max_notional: float | None = None   # hard cap on |position| x price
    # ISO date (ET). While today is before it, the engine flattens and idles.
    # A pause that needs a human to lift it is a pause that gets left on, so
    # this one carries its own expiry: set the date the strategy should trade
    # again and nothing further is required. Fail-safe by construction - a bug
    # here can only stop trading, never start it.
    paused_until: str | None = None


class VampireEngine:
    """Core vampire tick logic with bi-directional trading."""

    def __init__(
        self,
        client: AlpacaClient,
        data: MarketDataService,
        tracker: PositionTracker,
        config: VampireConfig | None = None,
    ):
        self._client = client
        self._data = data
        self._tracker = tracker
        self.cfg = config or VampireConfig()

        self._state = VampireState.IDLE
        self._net_position: int = 0
        self._daily_pnl: float = 0.0
        self._bleeds: list[BleedRecord] = []
        self._trade_timestamps: deque = deque(maxlen=100)
        self._last_fill_price: float | None = None
        self._avg_entry: float | None = None
        self._realized_pnl: float = 0.0
        self._reject_streak: int = 0
        self._reject_cooldown_until: float = 0.0

    @property
    def state(self) -> VampireState:
        return self._state

    @property
    def net_position(self) -> int:
        return self._net_position

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def bleeds(self) -> list[BleedRecord]:
        return list(self._bleeds)

    def _is_paused(self) -> bool:
        """True while today (ET) is before the configured resume date.

        Halted on 2026-09-01 after the scalper posted a negative expectancy on
        both remaining symbols: TQQQ -$1.03/trade over 142 closed round trips
        with a 0% win rate, QQQ -$0.13/trade. The pause carries its own expiry
        so the strategy resumes on its own rather than depending on someone
        remembering to switch it back on.

        An unparseable date does NOT pause. A typo that silently halted the
        strategy indefinitely would be the same "left off by accident" failure
        this is built to avoid, and it would be invisible - the engine would
        just quietly never trade.
        """
        if not self.cfg.paused_until:
            return False
        try:
            resume = date.fromisoformat(str(self.cfg.paused_until))
        except ValueError:
            log.warning("%s: paused_until %r is not an ISO date; not pausing",
                        self.cfg.symbol, self.cfg.paused_until)
            return False
        return datetime.now(ZoneInfo("America/New_York")).date() < resume

    def _is_market_hours(self) -> bool:
        now = datetime.now(ZoneInfo("America/New_York")).time()
        return SESSION_START <= now <= SESSION_END

    def reconcile(self, qty: int, avg_entry: float | None) -> None:
        """Adopt the position the broker actually reports.

        _net_position is process-local. Every restart reset it to zero while the
        broker still held the shares, so the notional cap kept measuring a
        position that had stopped being real: ten restarts in a morning took a
        $20,000 sleeve to $137,000 of exposure without a single check failing,
        because each check was asking about the wrong number.
        """
        self._net_position = int(qty)
        self._avg_entry = float(avg_entry) if (avg_entry and qty) else (
            self._avg_entry if qty else None
        )
        if qty:
            log.info("%s: adopted broker position %+d at %s",
                     self.cfg.symbol, qty, self._avg_entry)

    def _would_breach_notional(self, qty: int, price: float) -> bool:
        """True if adding qty would take this engine past its notional cap.

        The agent checked the sleeve budget once at startup and never again, so
        each engine could accumulate to max_position independently: three
        symbols at 100 shares of a ~$700 name is roughly $180k of exposure
        against a $20k sleeve on a $100k account. Sizing is bounded here rather
        than by a check in the tick loop so it holds regardless of caller.
        """
        if not self.cfg.max_notional:
            return False
        projected = (abs(self._net_position) + qty) * price
        if projected > self.cfg.max_notional:
            log.debug(
                "%s: %d + %d shares @ %.2f = $%.0f would breach the $%.0f cap",
                self.cfg.symbol, abs(self._net_position), qty, price,
                projected, self.cfg.max_notional,
            )
            return True
        return False

    def _check_rate_limit(self) -> bool:
        now = time.time()
        cutoff = now - 60
        while self._trade_timestamps and self._trade_timestamps[0] < cutoff:
            self._trade_timestamps.popleft()
        return len(self._trade_timestamps) < self.cfg.max_trades_per_min

    def _open_lot(self, qty: int, price: float, long: bool):
        """Fold a new lot into the running average entry price."""
        prior_qty = abs(self._net_position)
        if self._avg_entry is None or prior_qty == 0:
            self._avg_entry = price
        else:
            self._avg_entry = ((self._avg_entry * prior_qty) + (price * qty)) / (prior_qty + qty)

    def _close_lot(self, qty: int, price: float, long: bool) -> float:
        """Realize P&L against the average entry. Returns the realized amount.

        This is the correction that matters. The previous accounting credited
        `qty * abs(delta)` on every exit, which is unconditionally positive:
        it recorded the size of the move that triggered the exit, never the
        difference between the exit price and what the position actually cost.
        A losing round trip was booked as a gain, and daily P&L could only rise.
        """
        entry = self._avg_entry if self._avg_entry is not None else price
        realized = qty * (price - entry) if long else qty * (entry - price)
        self._realized_pnl += realized
        self._daily_pnl += realized
        return realized

    def _record_bleed(self, qty: float, delta: float, action: str, pnl: float = 0.0):
        self._bleeds.append(
            BleedRecord(
                timestamp=datetime.now(),
                symbol=self.cfg.symbol,
                action=action,
                qty=qty,
                delta=delta,
                pnl_estimate=pnl,
            )
        )
        self._trade_timestamps.append(time.time())

    REJECT_STREAK_TRIP = 5
    REJECT_BACKOFF_BASE = 2.0
    REJECT_BACKOFF_MAX = 300.0

    @staticmethod
    def _reject_body(exc: Exception) -> str:
        """Pull the venue's own error text out of whatever the SDK raised.

        A bare traceback tells us an order was refused, not why. Alpaca puts the
        reason in the response body, and the reason decides the fix: a wash-trade
        block needs order spacing, a borrow failure needs the symbol dropped, and
        insufficient buying power needs sizing. Without the body all three look
        identical and the engine just retries forever.
        """
        resp = getattr(exc, "response", None)
        for src in (getattr(resp, "text", None), str(exc)):
            if src:
                return str(src)[:400]
        return type(exc).__name__

    @staticmethod
    def _reject_facts(exc: Exception) -> dict:
        """Parse the venue's structured rejection into broker truth.

        Alpaca refuses an oversized reducing order with a body that states the
        real position and what may be sent right now:

            {"available":"9","existing_qty":"9","held_for_orders":"0",
             "message":"insufficient qty available for order (requested: 10, ...)"}

        existing_qty is the true position; available is existing_qty minus the
        shares already reserved by resting orders. They differ: one TQQQ refusal
        read existing_qty 17, held_for_orders 13, available 4. Position state is
        reconciled from the first, order size clamped by the second.
        """
        body = VampireEngine._reject_body(exc)
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            raw = json.loads(body[start:end + 1])
        except (ValueError, TypeError):
            return {}
        facts = {}
        for key in ("available", "existing_qty"):
            try:
                facts[key] = int(float(raw[key]))
            except (KeyError, TypeError, ValueError):
                pass
        return facts

    def _note_reject(self, exc: Exception, side) -> None:
        """Record a refused submission and back off if they keep coming.

        Rejects previously bypassed the rate limiter entirely, because only
        _record_bleed appended to _trade_timestamps and a refused order never
        reaches it. A symbol the venue refuses on every attempt therefore retried
        at the full tick rate: 4,700 refusals in 29 minutes on 2026-08-31, which
        rate-limited the whole account and blinded the watchdog and the reports.
        Refusals now cost the same budget as fills, and a persistent streak
        parks the symbol instead of hammering the venue.
        """
        self._reject_streak += 1
        self._trade_timestamps.append(time.time())
        log.warning(
            "%s submit rejected for %s (streak %d): %s",
            side, self.cfg.symbol, self._reject_streak, self._reject_body(exc),
        )
        if self._reject_streak >= self.REJECT_STREAK_TRIP:
            over = self._reject_streak - self.REJECT_STREAK_TRIP
            backoff = min(self.REJECT_BACKOFF_BASE * (2 ** over), self.REJECT_BACKOFF_MAX)
            self._reject_cooldown_until = time.time() + backoff
            log.error(
                "%s: %d consecutive rejects, pausing %.0fs",
                self.cfg.symbol, self._reject_streak, backoff,
            )

    def _clear_rejects(self) -> None:
        self._reject_streak = 0
        self._reject_cooldown_until = 0.0

    def tick(self, current_price: float, vwap: float | None = None):
        """Process one price tick. Core bi-directional logic from the spec."""
        if self._state == VampireState.STOPPED:
            return

        if time.time() < self._reject_cooldown_until:
            return

        if self._is_paused():
            if self._net_position != 0:
                self._flatten_all("paused")
            self._state = VampireState.IDLE
            return

        if not self._is_market_hours():
            if self._net_position != 0:
                self._flatten_all("session_end")
            self._state = VampireState.IDLE
            return

        self._state = VampireState.WATCHING

        ref = vwap if vwap is not None else (self._last_fill_price or current_price)
        delta = current_price - ref

        if not self._check_rate_limit():
            return

        if delta >= self.cfg.tick_threshold:
            if self._net_position > 0:
                want = min(self._net_position, self.cfg.position_size)
                qty = self._submit(want, current_price, OrderSide.SELL)
                if qty:
                    self._last_fill_price = current_price
                    self._tracker.record_trade(self.cfg.symbol, "sell", qty,
                                               current_price, "vampire")
                    realized = self._close_lot(qty, current_price, long=True)
                    self._net_position -= qty
                    if self._net_position == 0:
                        self._avg_entry = None
                    self._record_bleed(qty, delta, "long_exit", realized)

            elif abs(self._net_position) < self.cfg.max_position:
                room = self.cfg.max_position - abs(self._net_position)
                want = min(self.cfg.position_size, room)
                if want < 1 or self._would_breach_notional(want, current_price):
                    want = 0
                qty = self._submit(want, current_price, OrderSide.SELL) if want else 0
                if qty:
                    self._last_fill_price = current_price
                    self._tracker.record_trade(self.cfg.symbol, "sell_short", qty,
                                               current_price, "vampire")
                    self._open_lot(qty, current_price, long=False)
                    self._net_position -= qty
                    self._record_bleed(qty, delta, "short_entry")

        elif delta <= -self.cfg.tick_threshold:
            if self._net_position < 0:
                want = min(abs(self._net_position), self.cfg.position_size)
                qty = self._submit(want, current_price, OrderSide.BUY)
                if qty:
                    self._last_fill_price = current_price
                    self._tracker.record_trade(self.cfg.symbol, "buy_to_cover", qty,
                                               current_price, "vampire")
                    realized = self._close_lot(qty, current_price, long=False)
                    self._net_position += qty
                    if self._net_position == 0:
                        self._avg_entry = None
                    self._record_bleed(qty, abs(delta), "short_exit", realized)

            elif self._net_position < self.cfg.max_position:
                room = self.cfg.max_position - self._net_position
                want = min(self.cfg.position_size, room)
                if want < 1 or self._would_breach_notional(want, current_price):
                    want = 0
                qty = self._submit(want, current_price, OrderSide.BUY) if want else 0
                if qty:
                    self._last_fill_price = current_price
                    self._tracker.record_trade(self.cfg.symbol, "buy", qty,
                                               current_price, "vampire")
                    self._open_lot(qty, current_price, long=True)
                    self._net_position += qty
                    self._record_bleed(qty, abs(delta), "long_entry")

        marked = self.total_pnl(current_price)
        if marked <= -self.cfg.max_daily_loss:
            log.warning(
                "Circuit breaker hit: mark-to-market $%.2f (realized $%.2f, "
                "unrealized $%.2f, net %+d)",
                marked, self._realized_pnl, self.unrealized_pnl(current_price),
                self._net_position,
            )
            self._flatten_all("circuit_breaker")
            self._state = VampireState.STOPPED

    POLL_TIMEOUT = 2.0        # IOC resolves in ~100ms; this is a generous ceiling
    OPENING_POLL_TIMEOUT = 4.0   # doubled; only applies through OPENING_WINDOW_END
    POLL_INTERVAL = 0.05

    def _submit(self, qty: int, price: float, side: OrderSide) -> int:
        """Place an IOC order and return the quantity that actually filled.

        Alpaca returns the order with filled_qty "0" and status "new"; the fill
        lands roughly 85 to 100 milliseconds later. Reading filled_qty off the
        submit response therefore always saw zero, so the engine concluded
        nothing had filled, left its counter at zero, and bought again on the
        next tick while every one of those orders filled. That is how one symbol
        reached 271 shares against a 21-share cap.

        An UNKNOWN fill is assumed to be a FULL fill. Under-counting accumulates
        without bound; over-counting only makes the engine trade less. The one
        case that returns zero is a submission the venue never accepted, where
        nothing can fill because nothing arrived.
        """
        order = None
        for attempt in (0, 1):
            try:
                order = self._client.market_order(
                    self.cfg.symbol, qty, side, TimeInForce.IOC
                )
                break
            except Exception as exc:
                self._note_reject(exc, side)
                facts = self._reject_facts(exc)

                # The refusal carries the truth this engine got wrong. Adopt it
                # rather than retrying the same oversized order: an over-stated
                # position asks for more than exists and is refused every time,
                # which is a permanent deadlock, not a transient failure.
                existing = facts.get("existing_qty")
                if existing is not None and abs(self._net_position) != existing:
                    corrected = existing if self._net_position >= 0 else -existing
                    log.warning(
                        "%s: counter said %d, venue says %d; adopting venue",
                        self.cfg.symbol, self._net_position, corrected,
                    )
                    self._net_position = corrected

                available = facts.get("available")
                if attempt == 0 and available is not None and 1 <= available < qty:
                    qty = available
                    continue
                return 0

        self._clear_rejects()

        if self._is_terminal(getattr(order, "status", None)):
            return self._filled_or(order, qty)

        oid = getattr(order, "id", None)
        if oid is None:
            log.warning("%s: order carries no id; assuming it filled", self.cfg.symbol)
            return qty

        # Captured once, not re-read on every loop iteration: a poll that starts
        # inside the opening window and finishes after it must not have its
        # deadline or its log message disagree about which budget applied.
        poll_timeout = self._current_poll_timeout()
        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            time.sleep(self.POLL_INTERVAL)
            try:
                polled = self._client.get_order(str(oid))
            except Exception:
                log.warning("%s: cannot read order %s; assuming it filled",
                            self.cfg.symbol, oid, exc_info=True)
                return qty
            if self._is_terminal(getattr(polled, "status", None)):
                return self._filled_or(polled, qty)

        log.warning("%s: order %s did not resolve in %.1fs; assuming it filled",
                    self.cfg.symbol, oid, poll_timeout)
        return qty

    def _current_poll_timeout(self) -> float:
        """The fill-confirmation budget for a submission starting right now."""
        now = datetime.now(ZoneInfo("America/New_York")).time()
        if SESSION_START <= now < OPENING_WINDOW_END:
            return self.OPENING_POLL_TIMEOUT
        return self.POLL_TIMEOUT

    @staticmethod
    def _is_terminal(status) -> bool:
        text = str(getattr(status, "value", status) or "").lower()
        return any(k in text for k in
                   ("filled", "canceled", "cancelled", "rejected", "expired", "done"))

    @staticmethod
    def _filled_or(order, fallback: int) -> int:
        """Read filled_qty from a resolved order. Unreadable means assume filled."""
        try:
            return int(float(getattr(order, "filled_qty", None)))
        except (TypeError, ValueError):
            return fallback

    def _buy(self, qty: int, price: float):
        try:
            self._client.market_order(self.cfg.symbol, qty, OrderSide.BUY, TimeInForce.IOC)
            self._last_fill_price = price
            self._tracker.record_trade(self.cfg.symbol, "buy", qty, price, "vampire")
            log.debug("BUY %d %s @ %.2f", qty, self.cfg.symbol, price)
        except Exception:
            log.exception("Buy failed")

    def _sell(self, qty: int, price: float):
        try:
            self._client.market_order(self.cfg.symbol, qty, OrderSide.SELL, TimeInForce.IOC)
            self._last_fill_price = price
            self._tracker.record_trade(self.cfg.symbol, "sell", qty, price, "vampire")
            log.debug("SELL %d %s @ %.2f", qty, self.cfg.symbol, price)
        except Exception:
            log.exception("Sell failed")

    def _sell_short(self, qty: int, price: float):
        try:
            self._client.market_order(self.cfg.symbol, qty, OrderSide.SELL, TimeInForce.IOC)
            self._last_fill_price = price
            self._tracker.record_trade(self.cfg.symbol, "sell_short", qty, price, "vampire")
            log.debug("SHORT %d %s @ %.2f", qty, self.cfg.symbol, price)
        except Exception:
            log.exception("Short sell failed")

    def _buy_to_cover(self, qty: int, price: float):
        try:
            self._client.market_order(self.cfg.symbol, qty, OrderSide.BUY, TimeInForce.IOC)
            self._last_fill_price = price
            self._tracker.record_trade(self.cfg.symbol, "buy_to_cover", qty, price, "vampire")
            log.debug("COVER %d %s @ %.2f", qty, self.cfg.symbol, price)
        except Exception:
            log.exception("Cover failed")

    def _flatten_all(self, reason: str):
        log.info("Flattening all vampire positions: %s (net=%d)", reason, self._net_position)
        if self._net_position == 0:
            return
        try:
            self._cancel_resting_orders()
            self._client.close_position(self.cfg.symbol)
            self._net_position = 0
        except Exception:
            log.exception("Flatten failed")

    def _cancel_resting_orders(self) -> None:
        """Clear this symbol's own resting orders before a close attempt.

        close_position submits a new market order, and Alpaca refuses it
        outright with a wash-trade rejection when an opposing order is
        already resting on the same symbol. HOOD hit exactly this on
        2026-09-01: the flatten call raised, the position was never
        closed, and it rode through to the next process via startup
        adoption instead - safe only because that adoption path exists.

        Best-effort: a failure to read or cancel must not stop the close
        attempt that follows. If the real conflict is something other
        than our own resting order, cancelling ours won't have fixed it,
        and close_position raising again is the honest outcome; silently
        giving up here would only remove one legitimate attempt.
        """
        try:
            orders = self._client.get_orders(status="open")
        except Exception:
            log.warning("%s: could not read open orders before flatten",
                        self.cfg.symbol, exc_info=True)
            return
        for order in orders:
            if getattr(order, "symbol", None) != self.cfg.symbol:
                continue
            oid = getattr(order, "id", None)
            if oid is None:
                continue
            try:
                self._client.cancel_order(str(oid))
            except Exception:
                log.warning("%s: could not cancel resting order %s before flatten",
                            self.cfg.symbol, oid, exc_info=True)

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    def unrealized_pnl(self, current_price: float) -> float:
        """Mark the open position to the current price.

        Signed by net position, so it is correct for both sides: a short is a
        negative net, and a falling price yields a positive number.
        """
        if self._net_position == 0 or self._avg_entry is None:
            return 0.0
        return self._net_position * (current_price - self._avg_entry)

    def total_pnl(self, current_price: float) -> float:
        """Session P&L marked to market: realized so far today plus the open book.

        Uses the daily counter rather than the lifetime one because the limit it
        feeds (max_daily_loss) is a per-session limit and reset_daily() zeroes it.
        """
        return self._daily_pnl + self.unrealized_pnl(current_price)

    @property
    def avg_entry(self) -> float | None:
        return self._avg_entry

    def reset_daily(self):
        self._daily_pnl = 0.0
        self._realized_pnl = 0.0
        self._avg_entry = None
        self._bleeds.clear()
        self._trade_timestamps.clear()
        if self._state == VampireState.STOPPED:
            self._state = VampireState.IDLE

    def start(self):
        log.info("Vampire engine ready on %s (threshold=%.3f)", self.cfg.symbol, self.cfg.tick_threshold)
