"""Live option quotes for the CSP scanner, sourced through Alpaca's MCP server.

The scanner refuses to trade a contract it cannot price (see csp_scoring), so
this is what turns it from safe-and-idle into safe-and-trading. Kept separate
from the strategy so the strategy stays testable without a broker.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.core.mcp_client import AlpacaMCPClient

log = logging.getLogger(__name__)

# The MCP server accepts a comma-joined list; keep batches modest so one bad
# symbol cannot spoil a large request.
BATCH = 25


def _coerce(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _rows(payload: Any) -> dict[str, dict]:
    """Normalise the server's reply into {symbol: {bid, ask, delta}}.

    The tool returns either a mapping keyed by symbol or a list of quote
    objects depending on how many symbols were asked for. Anything we cannot
    read is omitted rather than defaulted, because a fabricated zero here
    becomes a fabricated premium downstream.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("Option quote reply was not JSON: %.120s", payload)
            return {}

    if isinstance(payload, dict):
        inner = payload.get("quotes") if "quotes" in payload else payload
        if isinstance(inner, dict):
            items = inner.items()
        elif isinstance(inner, list):
            items = ((q.get("symbol"), q) for q in inner if isinstance(q, dict))
        else:
            return {}
    elif isinstance(payload, list):
        items = ((q.get("symbol"), q) for q in payload if isinstance(q, dict))
    else:
        return {}

    out: dict[str, dict] = {}
    for symbol, q in items:
        if not symbol or not isinstance(q, dict):
            continue
        bid = _coerce(q.get("bid_price", q.get("bid", q.get("bp"))))
        ask = _coerce(q.get("ask_price", q.get("ask", q.get("ap"))))
        if bid is None:
            continue
        row: dict[str, Any] = {"bid": bid, "ask": ask if ask is not None else bid}
        greeks = q.get("greeks") or {}
        delta = _coerce(greeks.get("delta") if isinstance(greeks, dict) else None)
        if delta is None:
            delta = _coerce(q.get("delta"))
        if delta is not None:
            row["delta"] = delta
        out[str(symbol)] = row
    return out


def mcp_quote_provider(client: AlpacaMCPClient):
    """Build the callable the CSP scanner expects.

    Returns {} on any failure. The scanner treats an unpriced contract as
    untradeable, so an outage costs us trades rather than costing us a blind one.
    """

    def provider(symbols: list[str]) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        for i in range(0, len(symbols), BATCH):
            chunk = symbols[i:i + BATCH]
            try:
                merged.update(_rows(client.option_quote(chunk)))
            except Exception:
                log.exception("Option quote batch failed (%d symbols)", len(chunk))
        if not merged:
            log.warning("No option quotes resolved for %d symbols", len(symbols))
        return merged

    return provider
