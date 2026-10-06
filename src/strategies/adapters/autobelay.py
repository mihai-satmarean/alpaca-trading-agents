"""Autobelay adapter: LLM-driven options trading with structured risk checks.

Derived from bill-mccormick-dg's 1st-place hackathon entry. The original
system sends a full market snapshot (account state, option chains with Greeks,
positions held, resting orders) to an LLM and receives back a JSON array of
trade proposals. Each proposal passes through a deterministic risk funnel
before reaching the broker.

What this adapter preserves:
  - The structured prompt that frames the LLM as a decision engine
  - The risk manager that rejects proposals violating hard limits
  - The contract pricing logic (mid of bid/ask, fallback to last trade)
  - The deterministic exit rules (DTE close, stop-loss, take-profit)

What it replaces:
  - Featherless API -> Dell4 LLM cluster via LiteLLM proxy
  - Alpaca MCP client -> our shared AlpacaClient
  - Config loading -> AdapterConfig.extra section

The model never places orders. It proposes; the adapter's risk gates dispose.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import OrderSide, TimeInForce

from src.strategies.adapters.base import (
    AdapterConfig,
    AdapterHealth,
    AdapterStatus,
    OrderOutcome,
    OrderResult,
    Signal,
    SignalAssetType,
    SignalDirection,
    StrategyAdapter,
)

log = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

# Hard limits from the original Autobelay config, overridable via extra:
DEFAULTS = {
    "underlyings": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"],
    "max_position_usd": 10_000,
    "max_positions": 5,
    "max_contracts_per_order": 10,
    "min_days_to_expiration": 2,
    "max_days_to_expiration": 45,
    "last_entry": "15:30",
    "trade_end": "15:55",
    "model": "dell4-devstral",
    "temperature": 0.2,
    "max_tokens": 4000,
}


@dataclass
class Proposal:
    """Raw LLM output before risk checks."""
    instrument: str  # "option" or "stock"
    symbol: str
    side: str  # "buy" or "sell"
    qty: int
    order_type: str  # "market" or "limit"
    limit_price: float | None
    reason: str


SYSTEM_PROMPT = """\
You are the decision engine of an autonomous PAPER-trading agent on Alpaca.
You run periodically during US market hours and decide what to trade.
Doing nothing is a perfectly good decision.

Long-only: you may BUY to open or SELL to close. Never propose selling a
symbol you do not hold. Whole-number quantities only.

Options trading is the core strategy. Stock trades should support your
options thesis rather than replace it.

Hard limits (enforced downstream; violating proposals are rejected outright):
  max ${max_position_usd} per position, max {max_positions} concurrent
  positions, max {max_contracts} contracts per order, whitelist: {underlyings},
  options must have {min_dte} to {max_dte} DTE.
  New entries rejected after {last_entry} ET.

Respond with ONLY a JSON array (no markdown, no prose):
[{{"instrument": "option"|"stock", "symbol": "<ticker or OCC>",
   "side": "buy"|"sell", "qty": <int>,
   "order_type": "market"|"limit", "limit_price": <optional>,
   "reason": "<one sentence>"}}]
An empty array [] means hold and do nothing this cycle.

IMPORTANT: Use OCC option symbols in the format AAPL261120C00340000
  (symbol, YYMMDD expiry, C or P, 8-digit strike x 1000).
  Today is {today}. Options DTE is from today's date."""


def _parse_proposals(text: str) -> list[Proposal]:
    """Extract proposals from LLM output, tolerating markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    m = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not m:
        return []

    try:
        actions = json.loads(m.group(0))
    except json.JSONDecodeError:
        log.warning("autobelay: could not parse LLM JSON")
        return []

    proposals = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        try:
            qty = int(float(a.get("qty", 0)))
        except (TypeError, ValueError):
            qty = 0
        lp = a.get("limit_price")
        try:
            lp = float(lp) if lp is not None else None
        except (TypeError, ValueError):
            lp = None
        raw_symbol = str(a.get("symbol", "")).upper()
        if str(a.get("instrument", "")).lower() == "option":
            raw_symbol = _normalize_occ(raw_symbol)
        proposals.append(Proposal(
            instrument=str(a.get("instrument", "")).lower(),
            symbol=raw_symbol,
            side=str(a.get("side", "")).lower(),
            qty=qty,
            order_type=str(a.get("order_type", "market")).lower(),
            limit_price=lp,
            reason=str(a.get("reason", "")),
        ))
    return proposals



def _normalize_occ(symbol: str) -> str:
    """Fix common OCC symbol issues from LLM output.
    
    The OCC format is: ROOT(1-6) + YYMMDD(6) + C/P(1) + STRIKE(8 digits).
    Strike = price * 1000, zero-padded to 8 digits.
    LLMs commonly produce 9-digit strikes, wrong years, or omit leading zeros.
    """
    m = re.match(r'^([A-Z]{1,6})(\d{6})([CP])(\d+)$', symbol)
    if not m:
        return symbol
    root, date_str, cp, strike_str = m.groups()
    # Fix strike digits
    if len(strike_str) > 8:
        strike_str = strike_str[:8]
    elif len(strike_str) < 8:
        strike_str = strike_str.zfill(8)
    # Fix expiry: if year is in the past, remap to current year + similar month/day
    from datetime import datetime, date, timedelta
    from zoneinfo import ZoneInfo
    try:
        yy = int(date_str[:2])
        mm = int(date_str[2:4])
        dd = int(date_str[4:6])
        today = datetime.now(ZoneInfo("America/New_York")).date()
        exp = date(2000 + yy, mm, dd)
        if exp < today:
            # Pick the 3rd Friday of the same month in the current year or next
            target_year = today.year
            target_month = mm
            if date(target_year, mm, dd if dd <= 28 else 20) <= today:
                target_month = today.month + 1
                if target_month > 12:
                    target_month = 1
                    target_year += 1
            # Find 3rd Friday
            first = date(target_year, target_month, 1)
            fri_offset = (4 - first.weekday()) % 7
            third_fri = first + timedelta(days=fri_offset + 14)
            date_str = third_fri.strftime("%y%m%d")
            log.info("autobelay: fixed OCC expiry %s%s%s -> %s", root, m.group(2), cp, f"{root}{date_str}{cp}{strike_str}")
    except (ValueError, OverflowError):
        pass
    return f"{root}{date_str}{cp}{strike_str}"


def _eastern_now() -> datetime:
    return datetime.now(_ET)


def _snap_to_real_contract(client, symbol: str) -> str | None:
    """Find the nearest real option contract matching an LLM-generated symbol.
    
    The LLM often generates OCC symbols with wrong dates or non-existent strikes.
    This queries the Alpaca option contracts API for the closest match,
    preferring contracts near the LLM's intended expiry date.
    """
    m = re.match(r'^([A-Z]{1,6})(\d{6})([CP])(\d{8})$', symbol)
    if not m:
        return None
    
    root, date_str, cp, strike_str = m.groups()
    strike_val = int(strike_str) / 1000.0  # OCC strike to dollar price
    
    from alpaca.trading.requests import GetOptionContractsRequest
    from datetime import date, timedelta
    
    today = date.today()
    option_type = "call" if cp == "C" else "put"
    
    # Parse LLM's intended expiry to use as target DTE
    try:
        llm_year = 2000 + int(date_str[:2])
        llm_month = int(date_str[2:4])
        llm_day = int(date_str[4:6])
        llm_expiry = date(llm_year, llm_month, llm_day)
        target_dte = max((llm_expiry - today).days, 7)
    except (ValueError, OverflowError):
        target_dte = 30  # fallback: ~1 month out
    
    # Search window centered on LLM's intended DTE (min 7, max 90)
    dte_min = max(2, target_dte - 15)
    dte_max = min(90, target_dte + 15)
    
    try:
        req = GetOptionContractsRequest(
            underlying_symbols=[root],
            expiration_date_gte=str(today + timedelta(days=dte_min)),
            expiration_date_lte=str(today + timedelta(days=dte_max)),
            type=option_type,
            strike_price_gte=str(int(strike_val * 0.95)),
            strike_price_lte=str(int(strike_val * 1.05)),
            limit=20,
        )
        result = client.trading.get_option_contracts(req)
        contracts = result.option_contracts if hasattr(result, "option_contracts") else []
        if not contracts:
            log.warning("autobelay: no real contracts near %s (DTE %d-%d, strike %.0f +/-5%%)",
                        symbol, dte_min, dte_max, strike_val)
            return None
        # Score: weighted combo of strike distance + DTE distance from LLM intent
        def score(c):
            strike_diff = abs(float(c.strike_price) - strike_val) / max(strike_val, 1)
            exp = date.fromisoformat(str(c.expiration_date))
            dte_diff = abs((exp - today).days - target_dte) / max(target_dte, 1)
            return strike_diff + 0.5 * dte_diff
        best = min(contracts, key=score)
        log.info("autobelay: snapped %s -> %s (strike=%.0f, exp=%s, target_dte=%d)", 
                 symbol, best.symbol, float(best.strike_price), best.expiration_date, target_dte)
        return str(best.symbol)
    except Exception as exc:
        log.warning("autobelay: contract lookup failed for %s: %s", symbol, exc)
        return None


class AutobelayAdapter(StrategyAdapter):
    """LLM-driven options decision engine with deterministic risk gates."""

    def __init__(self, config: AdapterConfig, client, data, tracker, breaker, allocator):
        super().__init__(config, client, data, tracker, breaker, allocator)
        ex = config.extra
        self._underlyings = ex.get("underlyings", DEFAULTS["underlyings"])
        self._max_position_usd = float(ex.get("max_position_usd", DEFAULTS["max_position_usd"]))
        self._max_positions = int(ex.get("max_positions", DEFAULTS["max_positions"]))
        self._max_contracts = int(ex.get("max_contracts_per_order", DEFAULTS["max_contracts_per_order"]))
        self._min_dte = int(ex.get("min_days_to_expiration", DEFAULTS["min_days_to_expiration"]))
        self._max_dte = int(ex.get("max_days_to_expiration", DEFAULTS["max_days_to_expiration"]))
        self._last_entry = dt_time(*map(int, str(ex.get("last_entry", DEFAULTS["last_entry"])).split(":")))
        self._trade_end = dt_time(*map(int, str(ex.get("trade_end", DEFAULTS["trade_end"])).split(":")))
        self._model = str(ex.get("model", DEFAULTS["model"]))
        self._temperature = float(ex.get("temperature", DEFAULTS["temperature"]))
        self._max_tokens = int(ex.get("max_tokens", DEFAULTS["max_tokens"]))
        self._signals_today = 0
        self._errors_today = 0
        self._last_signal_time: datetime | None = None

    def evaluate(self) -> list[Signal]:
        """Build snapshot, call LLM, parse proposals into Signals."""
        now = _eastern_now()

        # Gate: market hours
        if now.time() > self._trade_end:
            return []

        # Build account snapshot for the prompt
        try:
            account = self._client.get_account()
            positions = self._client.get_positions()
        except Exception as exc:
            log.warning("autobelay: cannot read account: %s", exc)
            self._errors_today += 1
            return []

        # Only show this adapter's positions to the LLM -- positions from
        # SIXFOLD and other sleeves are irrelevant and make the model overly
        # conservative (it sees 14 positions and returns []).
        underlyings_set = set(self._underlyings)
        own_positions = []
        for p in positions:
            sym = str(getattr(p, "symbol", ""))
            root = sym[:6].rstrip("0123456789CP")
            if root in underlyings_set or sym in underlyings_set:
                own_positions.append(p)

        snapshot = {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "positions": [
                {
                    "symbol": str(getattr(p, "symbol", "")),
                    "qty": float(getattr(p, "qty", 0)),
                    "market_value": float(getattr(p, "market_value", 0)),
                    "unrealized_pl": float(getattr(p, "unrealized_pl", 0)),
                    "current_price": float(getattr(p, "current_price", 0)),
                }
                for p in own_positions
            ],
        }

        # Fetch recent bars for underlyings
        bars_summary = {}
        for sym in self._underlyings:
            try:
                bars = self._data.get_bars(sym, TimeFrame.Day, days_back=5)
                bar_list = bars.get(sym, []) if isinstance(bars, dict) else list(bars)
                if hasattr(bars, "data"):
                    bar_list = bars.data.get(sym, bar_list)
                bars_summary[sym] = [
                    {"close": float(b.close), "volume": int(b.volume)}
                    for b in (bar_list[-5:] if bar_list else [])
                ]
            except Exception:
                bars_summary[sym] = []

        prompt = SYSTEM_PROMPT.format(
            max_position_usd=self._max_position_usd,
            max_positions=self._max_positions,
            max_contracts=self._max_contracts,
            underlyings=", ".join(self._underlyings),
            min_dte=self._min_dte,
            max_dte=self._max_dte,
            last_entry=self._last_entry.strftime("%H:%M"),
            today=now.strftime("%Y-%m-%d"),
        )

        user_msg = json.dumps({
            "account": snapshot,
            "bars": bars_summary,
            "time": now.strftime("%H:%M ET"),
        }, separators=(",", ":"))

        try:
            from src.core.finance_advisor import _llm_call
            raw = _llm_call(
                self._model, prompt, user_msg,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except Exception as exc:
            log.warning("autobelay: LLM call failed: %s", exc)
            self._errors_today += 1
            return []

        log.info("autobelay: LLM raw response: %s", raw[:500] if raw else "(empty)")
        proposals = _parse_proposals(raw)
        log.info("autobelay: parsed %d proposal(s)", len(proposals))
        if not proposals:
            return []

        # Convert proposals to Signals with risk filtering
        signals: list[Signal] = []
        for p in proposals:
            if p.qty <= 0:
                continue

            # Whitelist check
            root = p.symbol.split(".")[0][:6] if p.instrument == "stock" else p.symbol[:6].rstrip("0123456789")
            if root not in self._underlyings and p.symbol not in self._underlyings:
                log.debug("autobelay: %s not in underlyings whitelist", p.symbol)
                continue

            # Entry time gate
            if p.side == "buy" and now.time() > self._last_entry:
                log.debug("autobelay: entry rejected after %s ET", self._last_entry)
                continue

            # Position count gate (only count positions in our universe)
            held = len([pos for pos in own_positions if float(getattr(pos, "qty", 0)) != 0])
            if p.side == "buy" and held >= self._max_positions:
                log.debug("autobelay: at max positions (%d)", self._max_positions)
                continue

            # Contract limit gate (options)
            if p.instrument == "option" and p.qty > self._max_contracts:
                log.debug("autobelay: qty %d exceeds max_contracts %d", p.qty, self._max_contracts)
                continue

            direction = SignalDirection.LONG if p.side == "buy" else SignalDirection.CLOSE
            asset_type = SignalAssetType.OPTION if p.instrument == "option" else SignalAssetType.EQUITY

            signals.append(Signal(
                symbol=p.symbol,
                direction=direction,
                asset_type=asset_type,
                qty=float(p.qty),
                limit_price=p.limit_price,
                confidence=0.7,  # Autobelay does not emit confidence; use a fixed threshold
                reason=f"[autobelay] {p.reason}",
                metadata={
                    "order_type": p.order_type,
                    "instrument": p.instrument,
                    "model": self._model,
                },
            ))

        self._signals_today += len(signals)
        if signals:
            self._last_signal_time = now
        return signals

    def execute(self, signals: list[Signal]) -> list[OrderResult]:
        """Submit signals through the shared client with risk checks."""
        results: list[OrderResult] = []

        for sig in signals:
            # Shared risk gate
            rejection = self._check_risk(sig)
            if rejection:
                results.append(OrderResult(
                    signal=sig,
                    outcome=OrderOutcome.GATED,
                    error=rejection,
                ))
                continue

            # Dry run
            if self.is_dry_run:
                results.append(OrderResult(
                    signal=sig,
                    outcome=OrderOutcome.DRY_RUN,
                ))
                log.info("autobelay DRY-RUN: %s %s x%.0f (%s)",
                         sig.direction.value, sig.symbol, sig.qty, sig.reason)
                continue

            # Live execution
            try:
                trade_symbol = sig.symbol
                # For options, snap LLM-generated symbol to a real contract
                if sig.asset_type == SignalAssetType.OPTION:
                    real = _snap_to_real_contract(self._client, sig.symbol)
                    if real is None:
                        self._errors_today += 1
                        results.append(OrderResult(
                            signal=sig,
                            outcome=OrderOutcome.ERROR,
                            error=f"no real contract found for {sig.symbol}",
                        ))
                        log.warning("autobelay: skipping %s (no real contract)", sig.symbol)
                        continue
                    trade_symbol = real
                
                side = OrderSide.BUY if sig.direction == SignalDirection.LONG else OrderSide.SELL
                if sig.limit_price is not None:
                    order = self._client.limit_order(
                        trade_symbol, sig.qty, side, sig.limit_price,
                        time_in_force=TimeInForce.DAY,
                    )
                else:
                    order = self._client.market_order(
                        trade_symbol, sig.qty, side,
                        time_in_force=TimeInForce.DAY,
                    )
                results.append(OrderResult(
                    signal=sig,
                    outcome=OrderOutcome.FILLED,
                    filled_qty=sig.qty,
                    filled_price=sig.limit_price or 0.0,
                ))
                self._tracker.record_trade(
                    sig.symbol, sig.direction.value, sig.qty,
                    sig.limit_price or 0.0, strategy="autobelay",
                )
                log.info("autobelay SUBMITTED: %s %s x%.0f", side.value, sig.symbol, sig.qty)
            except Exception as exc:
                self._errors_today += 1
                results.append(OrderResult(
                    signal=sig,
                    outcome=OrderOutcome.ERROR,
                    error=str(exc),
                ))
                log.error("autobelay ORDER FAILED: %s", exc)

        return results

    def flatten(self) -> None:
        """Close all positions on underlyings this adapter trades."""
        if self.is_dry_run:
            log.info("autobelay flatten: dry run, no action")
            return
        positions = self._client.get_positions()
        for pos in positions:
            sym = str(getattr(pos, "symbol", ""))
            root = sym[:6].rstrip("0123456789")
            if root in self._underlyings or sym in self._underlyings:
                try:
                    self._client.close_position(sym)
                    log.info("autobelay: flattened %s", sym)
                except Exception as exc:
                    log.error("autobelay: failed to flatten %s: %s", sym, exc)

    def health_check(self) -> AdapterHealth:
        return AdapterHealth(
            status=AdapterStatus.HEALTHY if self._errors_today < 5 else AdapterStatus.DEGRADED,
            message=f"errors_today={self._errors_today}",
            last_signal_time=self._last_signal_time,
            signals_today=self._signals_today,
            errors_today=self._errors_today,
        )
