"""Tradier market data for the Vampire, duck-typed to MarketDataService.

Tradier's streaming endpoint needs a scope this account's token does not carry
(verified 2026-09-04: POST /markets/events/session returns 401 "Required
scope(s): scope-stream.scopeSet"), so quotes are POLLED rather than streamed.
That is a real degradation and is stated plainly rather than hidden: the
Alpaca build reacts to every tick, this one reacts at the poll interval. Both
symbols are fetched in a single request because /markets/quotes accepts a
comma-separated list, so the poll costs one call per interval, not one per
symbol, which keeps a two-symbol book inside Tradier's ~120 requests/minute.

The spread calibration deliberately matches the Alpaca fix that preceded it
(PR #97): measure a WINDOW of the book, never a handful of reads taken at one
moment, because the agent starts near the opening bell and the auction book is
15x the width the session actually trades at. Here the window is accumulated
from the poll loop itself, so it costs no extra requests, and the engine
refuses to trade a symbol until the window is large enough to trust.
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
from collections import deque
from datetime import datetime, timedelta, timezone

from src.core.market_data import Quote
from src.core.tradier_client import PRODUCTION, SANDBOX, TradierError

log = logging.getLogger(__name__)

try:
    import certifi
    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:                                    # pragma: no cover
    _SSL_CTX = None


MIN_SPREAD_SAMPLES = 120          # ~2 minutes at a 1s poll
SPREAD_WINDOW_SECONDS = 20 * 60


class TradierMarketData:
    def __init__(self, token: str | None = None, sandbox: bool = True,
                 poll_seconds: float = 1.0, timeout: float = 10.0):
        self._token = token or os.environ.get(
            "TRADIER_SANDBOX_TOKEN" if sandbox else "TRADIER_TOKEN") or ""
        if not self._token:
            raise ValueError("Tradier market data needs a token")
        self._base = SANDBOX if sandbox else PRODUCTION
        self._timeout = timeout
        self.poll_seconds = poll_seconds
        self._symbols: list[str] = []
        self._on_quote = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._history: dict[str, deque] = {}
        self._last: dict[str, Quote] = {}

    # ---------------------------------------------------------------- HTTP

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(
            self._base + path,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=_SSL_CTX) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")[:400]
            raise TradierError(f"Tradier {exc.code}: {text}", body=text) from exc

    # -------------------------------------------------------------- quotes

    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """One request for every symbol. Bad or crossed quotes are dropped."""
        q = self._get("/markets/quotes?symbols=" + urllib.parse.quote(",".join(symbols)))
        raw = (q.get("quotes") or {}).get("quote") or []
        raw = [raw] if isinstance(raw, dict) else raw
        out: dict[str, Quote] = {}
        now = datetime.now(timezone.utc)
        for x in raw:
            try:
                bid, ask = float(x.get("bid") or 0), float(x.get("ask") or 0)
                sym = str(x.get("symbol") or "").upper()
            except (TypeError, ValueError):
                continue
            if not sym or bid <= 0 or ask <= bid:
                continue
            out[sym] = Quote(symbol=sym, bid=bid, ask=ask, mid=(bid + ask) / 2, timestamp=now)
        with self._lock:
            for sym, quote in out.items():
                self._last[sym] = quote
                self._history.setdefault(sym, deque(maxlen=4000)).append(
                    (time.time(), quote.ask - quote.bid, quote.mid))
        return out

    def get_latest_quote(self, symbol: str) -> Quote | None:
        got = self.fetch_quotes([symbol])
        return got.get(symbol.upper())

    def recent_spread(self, symbol: str, minutes: int = 20,
                      max_age_minutes: int = 24 * 60) -> dict | None:
        """Median spread over the accumulated poll window, or None.

        None means "not enough of the book seen yet", and the caller keeps its
        configured threshold. That is the safe direction: a stale wide trigger
        only stops a symbol trading, while one that is too narrow trades
        continuously at negative edge.
        """
        cutoff = time.time() - min(minutes, max_age_minutes) * 60
        with self._lock:
            rows = [(sp, mid) for ts, sp, mid in self._history.get(symbol.upper(), ())
                    if ts >= cutoff and sp > 0]
        if len(rows) < MIN_SPREAD_SAMPLES:
            return None
        spreads = sorted(r[0] for r in rows)
        mids = sorted(r[1] for r in rows)
        n = len(spreads)
        return {"n": n, "median": spreads[n // 2], "p90": spreads[min(n - 1, int(n * 0.9))],
                "price": mids[n // 2], "window_minutes": minutes}

    # ---------------------------------------------------------------- bars

    def get_recent_minute_bars(self, symbol: str, minutes: int = 90) -> list:
        """1-minute bars from /markets/timesales, shaped for the regime advisor."""
        end = datetime.now()
        start = end - timedelta(minutes=minutes)
        fmt = "%Y-%m-%d %H:%M"
        path = (f"/markets/timesales?symbol={urllib.parse.quote(symbol)}&interval=1min"
                f"&start={urllib.parse.quote(start.strftime(fmt))}"
                f"&end={urllib.parse.quote(end.strftime(fmt))}")
        data = (self._get(path).get("series") or {}).get("data") or []
        data = [data] if isinstance(data, dict) else data
        out = []
        for b in data:
            try:
                out.append(type("Bar", (), {
                    "timestamp": datetime.fromtimestamp(int(b["timestamp"]), timezone.utc),
                    "open": float(b["open"]), "high": float(b["high"]),
                    "low": float(b["low"]), "close": float(b["close"]),
                    "volume": float(b.get("volume") or 0),
                })())
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def get_vwap(self, symbol: str, window_seconds: int = 5) -> float | None:
        """Mid-price VWAP proxy over the poll window.

        Tradier's poll gives no per-tick size, so this is an unweighted mean of
        the mids in the window rather than a true volume weighting. The engine
        uses it only as the reference price a move is measured against, and it
        is the same quantity for every tick, so the asymmetry does not favour
        either side of the trade.
        """
        cutoff = time.time() - window_seconds
        with self._lock:
            mids = [mid for ts, _sp, mid in self._history.get(symbol.upper(), ()) if ts >= cutoff]
        return sum(mids) / len(mids) if mids else None

    # ------------------------------------------------------------- polling

    async def subscribe_quotes(self, symbols: list[str], on_quote):
        self._symbols = [s.upper() for s in symbols]
        self._on_quote = on_quote

    async def subscribe_trades(self, symbols: list[str], on_trade):
        return None

    async def run_stream(self):
        """Poll in place of a stream, dispatching each quote to the engine."""
        import asyncio
        log.info("Tradier poll loop starting for %s every %.1fs (no streaming scope on this token)",
                 self._symbols, self.poll_seconds)
        while not self._stop.is_set():
            started = time.time()
            try:
                for quote in self.fetch_quotes(self._symbols).values():
                    if self._on_quote:
                        await self._on_quote(quote)
            except Exception:
                log.warning("quote poll failed; retrying next interval", exc_info=True)
            await asyncio.sleep(max(0.0, self.poll_seconds - (time.time() - started)))

    def stop(self):
        self._stop.set()
