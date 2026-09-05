"""Tradier broker adapter for the Vampire, duck-typed to AlpacaClient.

The engine (src/strategies/vampire_engine.py) calls five methods and reads
four attributes off whatever they return. This implements that surface against
Tradier's REST API so the strategy itself needs no change.

Two Tradier semantics differ from Alpaca in ways that matter, both verified
against the sandbox on 2026-09-04 rather than assumed:

IOC DOES NOT EXIST. Tradier accepts duration day, gtc, pre or post; "ioc"
returns 400 "Invalid parameter, duration: is not valid." A market order fills
immediately on a liquid ETF either way, so IOC maps to day, and _submit's
caller still polls to a terminal state. Any order that has not filled by the
end of that poll is CANCELLED here rather than left resting, which is the part
of IOC that actually matters to a scalper: no unintended overnight or
late-session exposure from an order the engine has stopped tracking.

SIDE IS FOUR-VALUED, NOT TWO. Alpaca's SELL both closes a long and opens a
short; Tradier splits these into sell / sell_short and buy / buy_to_cover, and
picking wrong is the failure mode that has cost real money on this desk before
(prediction-market-arb, 2026-06-26: a side chosen from a lagging position read
turned a hedge into a doubled directional bet). The side is therefore resolved
from the BROKER's own position, never from the engine's local counter, with a
short TTL cache so a burst of ticks does not exhaust the rate limit. A stale
read cannot silently invert a trade: Tradier rejects sell without a long and
buy_to_cover without a short, and any such rejection forces an immediate
re-sync before the next attempt.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

try:
    import certifi
    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:                                    # pragma: no cover
    _SSL_CTX = None


PRODUCTION = "https://api.tradier.com/v1"
SANDBOX = "https://sandbox.tradier.com/v1"

# Tradier's published limit is ~120 requests/minute for market data. The side
# resolver caches positions for this long so a burst of ticks costs one read.
POSITION_TTL_SECONDS = 2.0

TERMINAL = ("filled", "canceled", "cancelled", "rejected", "expired", "error")


@dataclass
class TradierOrder:
    """The attribute surface vampire_engine reads: id, status, filled_qty, symbol."""

    id: str | None
    status: str
    filled_qty: float
    symbol: str
    side: str = ""
    qty: float = 0.0


class TradierError(RuntimeError):
    """Carries the venue's own message so _reject_facts can parse it."""

    def __init__(self, message: str, body: str = ""):
        super().__init__(message)
        self.body = body

    @property
    def response(self):
        return type("R", (), {"text": self.body})()


class TradierClient:
    def __init__(self, token: str | None = None, account_id: str | None = None,
                 sandbox: bool = True, timeout: float = 10.0):
        env_token = "TRADIER_SANDBOX_TOKEN" if sandbox else "TRADIER_TOKEN"
        env_acct = "TRADIER_SANDBOX_ACCOUNT_ID" if sandbox else "TRADIER_INDIVIDUAL_ACCOUNT_ID"
        self._token = token or os.environ.get(env_token) or ""
        self._account = account_id or os.environ.get(env_acct) or ""
        if not self._token or not self._account:
            raise ValueError(f"Tradier needs {env_token} and {env_acct}")
        self._base = SANDBOX if sandbox else PRODUCTION
        self.sandbox = sandbox
        self._timeout = timeout
        self._pos_cache: dict[str, float] = {}
        self._pos_at = 0.0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- HTTP

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        url = self._base + path
        body = urllib.parse.urlencode(params).encode() if params else None
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=_SSL_CTX) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")[:600]
            raise TradierError(f"Tradier {exc.code}: {text}", body=text) from exc

    # ------------------------------------------------------------ account

    def get_account(self):
        b = (self._request("GET", f"/accounts/{self._account}/balances").get("balances") or {})
        return type("Account", (), {
            "equity": b.get("total_equity"), "cash": b.get("total_cash"),
            "last_equity": b.get("total_equity"), "raw": b,
        })()

    def get_positions(self) -> list:
        raw = (self._request("GET", f"/accounts/{self._account}/positions") or {}).get("positions")
        if not raw or raw == "null":
            return []
        p = raw.get("position") if isinstance(raw, dict) else raw
        p = [p] if isinstance(p, dict) else (p or [])
        return [type("Position", (), {
            "symbol": x.get("symbol"), "qty": float(x.get("quantity", 0) or 0),
            "avg_entry_price": float(x.get("cost_basis", 0) or 0) / (float(x.get("quantity")) or 1),
            "raw": x,
        })() for x in p]

    def get_position(self, symbol: str):
        for p in self.get_positions():
            if str(p.symbol).upper() == symbol.upper():
                return p
        return None

    def working_quantity(self, symbol: str) -> float:
        """Signed shares committed by orders that are still working.

        Tradier validates a side against filled position PLUS anything in
        flight. The sandbox proved it on 2026-09-04: with a buy still pending
        and /positions returning null, a sell_short came back rejected with
        "Sell short order cannot be placed while you have a current long
        position, please check open orders." A resolver that reads only filled
        positions therefore picks a side the venue will refuse, and on a
        strategy that fires every few seconds an in-flight order is the normal
        case, not the edge case.
        """
        try:
            orders = self.get_orders("open")
        except Exception:
            log.warning("could not read working orders for %s", symbol, exc_info=True)
            return 0.0
        net = 0.0
        for o in orders:
            if str(o.symbol).upper() != symbol.upper():
                continue
            remaining = max(0.0, float(o.qty) - float(o.filled_qty))
            if str(o.side).lower() in ("buy", "buy_to_cover"):
                net += remaining
            else:
                net -= remaining
        return net

    def effective_position(self, symbol: str, force: bool = False) -> float:
        """What the VENUE thinks we hold: filled position plus working orders.

        This, not the filled position alone, is what the side must be resolved
        against, because this is what Tradier validates against.
        """
        return self.net_position(symbol, force=force) + self.working_quantity(symbol)

    def net_position(self, symbol: str, force: bool = False) -> float:
        """Signed FILLED share count from the BROKER, cached briefly.

        The engine's own counter is process-local and has been wrong before;
        this is the number the side resolver is allowed to trust. For choosing
        a side use effective_position, which also counts orders in flight.
        """
        with self._lock:
            fresh = (time.time() - self._pos_at) < POSITION_TTL_SECONDS
            if fresh and not force and symbol.upper() in self._pos_cache:
                return self._pos_cache[symbol.upper()]
        try:
            positions = self.get_positions()
        except Exception:
            log.warning("could not read Tradier positions for %s", symbol, exc_info=True)
            with self._lock:
                return self._pos_cache.get(symbol.upper(), 0.0)
        with self._lock:
            self._pos_cache = {str(p.symbol).upper(): float(p.qty) for p in positions}
            self._pos_at = time.time()
            return self._pos_cache.get(symbol.upper(), 0.0)

    def invalidate_positions(self) -> None:
        with self._lock:
            self._pos_at = 0.0

    # -------------------------------------------------------------- sides

    @staticmethod
    def resolve_side(direction: str, net: float) -> str:
        """Alpaca's two-valued side plus the current position -> Tradier's four.

        Pure and total, so it can be exhaustively tested without a broker.
        """
        buying = str(direction).lower().endswith("buy")
        if buying:
            return "buy_to_cover" if net < 0 else "buy"
        return "sell" if net > 0 else "sell_short"

    @staticmethod
    def _closing_qty(direction: str, net: float, qty: float) -> float:
        """Never ask to close more than exists: Tradier rejects the whole order."""
        buying = str(direction).lower().endswith("buy")
        if buying and net < 0:
            return min(qty, abs(net))
        if not buying and net > 0:
            return min(qty, net)
        return qty

    # ------------------------------------------------------------- orders

    def market_order(self, symbol: str, qty: float, side, time_in_force=None):
        """Submit a market order, choosing the Tradier side from the live book.

        time_in_force is accepted and ignored: Tradier has no IOC (verified,
        400 "duration: is not valid"). The caller polls to a terminal state and
        cancel_if_open() emulates the part of IOC that matters.
        """
        direction = getattr(side, "value", side)
        # Refuse to stack an order on an unsettled book. Tradier validates a
        # side against filled position PLUS anything working, so submitting
        # while one of our own orders is in flight is how you get "Sell short
        # order cannot be placed while you have a current long position"
        # (observed in the sandbox, 2026-09-04) or, worse, a side that is
        # accepted and wrong. With nothing working, filled IS the effective
        # position and the side below is unambiguous.
        working = self.working_quantity(symbol)
        if working:
            raise TradierError(
                f"{symbol}: {working:+.0f} shares already working; not stacking "
                f"a {direction} on an unsettled book",
                body=json.dumps({"available": "0", "message": "order already working"}),
            )
        net = self.net_position(symbol)
        tradier_side = self.resolve_side(direction, net)
        send_qty = int(self._closing_qty(direction, net, float(qty)))
        if send_qty < 1:
            raise TradierError(
                f"nothing to do for {symbol}: {direction} {qty} against net {net}",
                body=json.dumps({"available": "0", "existing_qty": str(int(abs(net))),
                                 "message": "no position to close"}),
            )
        try:
            r = self._request("POST", f"/accounts/{self._account}/orders", {
                "class": "equity", "symbol": symbol, "side": tradier_side,
                "quantity": str(send_qty), "type": "market", "duration": "day",
            })
        except TradierError:
            # A side-related refusal means our position read was stale.
            self.invalidate_positions()
            raise
        o = r.get("order") or {}
        self.invalidate_positions()
        log.debug("%s %s %d -> order %s", tradier_side, symbol, send_qty, o.get("id"))
        return TradierOrder(id=str(o.get("id")) if o.get("id") is not None else None,
                            status=str(o.get("status") or "open"), filled_qty=0.0,
                            symbol=symbol, side=tradier_side, qty=float(send_qty))

    def get_order(self, order_id: str):
        r = self._request("GET", f"/accounts/{self._account}/orders/{order_id}")
        o = r.get("order") or {}
        return TradierOrder(
            id=str(o.get("id")) if o.get("id") is not None else str(order_id),
            status=str(o.get("status") or "open"),
            filled_qty=float(o.get("exec_quantity") or 0),
            symbol=str(o.get("symbol") or ""), side=str(o.get("side") or ""),
            qty=float(o.get("quantity") or 0),
        )

    def get_orders(self, status: str = "open"):
        raw = (self._request("GET", f"/accounts/{self._account}/orders") or {}).get("orders")
        if not raw or raw == "null":
            return []
        o = raw.get("order") if isinstance(raw, dict) else raw
        o = [o] if isinstance(o, dict) else (o or [])
        out = [TradierOrder(
            id=str(x.get("id")), status=str(x.get("status") or ""),
            filled_qty=float(x.get("exec_quantity") or 0), symbol=str(x.get("symbol") or ""),
            side=str(x.get("side") or ""), qty=float(x.get("quantity") or 0)) for x in o]
        if status == "open":
            return [x for x in out if x.status.lower() not in TERMINAL]
        return out

    def cancel_order(self, order_id: str):
        return self._request("DELETE", f"/accounts/{self._account}/orders/{order_id}")

    def cancel_if_open(self, order_id: str) -> bool:
        """The half of IOC that matters: leave nothing resting we stopped watching."""
        try:
            if self.get_order(order_id).status.lower() in TERMINAL:
                return False
            self.cancel_order(order_id)
            log.info("cancelled unfilled order %s (Tradier has no IOC)", order_id)
            return True
        except Exception:
            log.warning("could not cancel order %s", order_id, exc_info=True)
            return False

    def close_position(self, symbol: str):
        """Flatten a symbol. Tradier has no close-position endpoint."""
        net = self.net_position(symbol, force=True)
        if not net:
            return None
        side = "sell" if net > 0 else "buy_to_cover"
        r = self._request("POST", f"/accounts/{self._account}/orders", {
            "class": "equity", "symbol": symbol, "side": side,
            "quantity": str(int(abs(net))), "type": "market", "duration": "day",
        })
        self.invalidate_positions()
        return r
