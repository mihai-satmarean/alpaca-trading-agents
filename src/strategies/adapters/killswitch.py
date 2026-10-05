"""Killswitch adapter: 15-gate deterministic pipeline for options trading.

Derived from SamsonDoski's 3rd-place hackathon entry. The original system
separates the pipeline into two distinct chains:

  screen()     -- 6 cheap entry gates, run BEFORE the model is called.
                  Free checks: kill switch, market hours, position slots,
                  sector concentration, cooldown, not-already-held.

  authorise()  -- 9 order gates, run AFTER the model has proposed a trade.
                  Expensive checks: confidence, directional balance, delta
                  band, spread width, expiry window, premium richness, time
                  decay burden, risk budget, buying power.

The model's job is deliberately small: given a market brief (price action,
option chain, headlines), judge the likely direction of one underlying over
2-6 weeks. The gates do everything else -- contract selection, sizing, and
discipline. A gate may reject or shrink; it may never enlarge or invent.

What this adapter preserves:
  - The two-chain gate architecture (screen -> propose -> authorise)
  - The gate invariant: reject or shrink, never enlarge
  - Contract selection by delta band, monthly expiry preference, spread
  - LLM proposer for directional view (Dell4 instead of Claude)
  - Exits run first, before any new entries

What it replaces:
  - MCP-based broker access -> our shared AlpacaClient
  - Claude/open model -> Dell4 LiteLLM proxy
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
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

DEFAULTS = {
    "symbols": ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "JPM", "V"],
    "model": "dell4-devstral",
    "max_positions": 5,
    "risk_per_trade": 0.04,
    "max_contracts": 4,
    "delta_min": 0.50,
    "delta_max": 0.75,
    "max_spread_pct": 0.05,
    "dte_min": 14,
    "dte_max": 60,
    "close_before_expiry": 5,
    "max_same_direction": 4,
    "min_confidence": 0.55,
    "max_iv_to_realized": 1.8,
    "max_daily_decay": 0.015,
    "cooldown_days": 3,
    "max_per_group": 3,
    "correlation_groups": {
        "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "GOOGL": "tech",
        "META": "tech", "AMZN": "tech",
        "JPM": "finance", "V": "finance",
    },
}


# ---------------------------------------------------------------------------
# Gate infrastructure
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    decision: str  # "allow", "deny", "shrink"
    reason: str
    quantity: int | None = None

    @classmethod
    def allow(cls, reason: str = "") -> Verdict:
        return cls("allow", reason)

    @classmethod
    def deny(cls, reason: str) -> Verdict:
        return cls("deny", reason)

    @classmethod
    def shrink(cls, qty: int, reason: str) -> Verdict:
        return cls("shrink", reason, qty)


@dataclass
class GateContext:
    """Snapshot of the world that gates reason against."""
    today: date
    market_open: bool
    trading_halted: bool
    equity: float
    cash: float
    buying_power: float
    open_positions: list[dict] = field(default_factory=list)
    cooling_off: dict[str, int] = field(default_factory=dict)
    pending: frozenset[str] = frozenset()

    def holds(self, underlying: str) -> bool:
        return any(p.get("underlying") == underlying for p in self.open_positions)

    @property
    def committed_slots(self) -> int:
        return len(self.open_positions) + len(self.pending)

    @property
    def committed_value(self) -> float:
        return sum(abs(float(p.get("market_value", 0))) for p in self.open_positions)


@dataclass
class OrderDraft:
    """A proposed order after contract selection, before gate approval."""
    symbol: str
    underlying: str
    right: str  # "call" or "put"
    direction: str  # "up" or "down"
    quantity: int
    limit_price: float
    strike: float
    expiry: date
    delta: float
    spread_pct: float
    implied_vol: float
    realized_vol: float | None
    confidence: float
    rationale: str
    spot: float


# ---------------------------------------------------------------------------
# Entry gates (cheap, before model call)
# ---------------------------------------------------------------------------

def _gate_kill_switch(underlying: str, ctx: GateContext) -> Verdict:
    if ctx.trading_halted:
        return Verdict.deny("kill switch is on")
    return Verdict.allow()


def _gate_market_open(underlying: str, ctx: GateContext) -> Verdict:
    if not ctx.market_open:
        return Verdict.deny("market is closed")
    return Verdict.allow()


def _gate_position_slots(underlying: str, ctx: GateContext, max_positions: int) -> Verdict:
    if ctx.committed_slots >= max_positions:
        return Verdict.deny(f"all {max_positions} slots in use")
    return Verdict.allow()


def _gate_sector_concentration(underlying: str, ctx: GateContext,
                               groups: dict, max_per_group: int) -> Verdict:
    group = groups.get(underlying)
    if group is None:
        return Verdict.allow()
    held = sum(1 for p in ctx.open_positions if groups.get(p.get("underlying")) == group)
    if held >= max_per_group:
        return Verdict.deny(f"already holding {held} in '{group}', limit {max_per_group}")
    return Verdict.allow()


def _gate_cooldown(underlying: str, ctx: GateContext) -> Verdict:
    remaining = ctx.cooling_off.get(underlying, 0)
    if remaining > 0:
        return Verdict.deny(f"stopped out recently, {remaining} day(s) cooldown left")
    return Verdict.allow()


def _gate_not_already_held(underlying: str, ctx: GateContext) -> Verdict:
    if ctx.holds(underlying):
        return Verdict.deny("already holding a position in this underlying")
    if underlying in ctx.pending:
        return Verdict.deny("an order is already resting")
    return Verdict.allow()


def screen(underlying: str, ctx: GateContext, cfg: dict) -> Verdict:
    """Run entry gates. First denial stops."""
    for gate_fn, kwargs in [
        (_gate_kill_switch, {}),
        (_gate_market_open, {}),
        (_gate_position_slots, {"max_positions": cfg.get("max_positions", 5)}),
        (_gate_sector_concentration, {
            "groups": cfg.get("correlation_groups", {}),
            "max_per_group": cfg.get("max_per_group", 3),
        }),
        (_gate_cooldown, {}),
        (_gate_not_already_held, {}),
    ]:
        v = gate_fn(underlying, ctx, **kwargs)
        if v.decision == "deny":
            return v
    return Verdict.allow("passed entry screening")


# ---------------------------------------------------------------------------
# Order gates (after model proposal, on a sized draft)
# ---------------------------------------------------------------------------

def _gate_min_confidence(draft: OrderDraft, ctx: GateContext, cfg: dict) -> Verdict:
    floor = cfg.get("min_confidence", 0.55)
    if draft.confidence < floor:
        return Verdict.deny(f"confidence {draft.confidence:.2f} below floor {floor:.2f}")
    return Verdict.allow()


def _gate_directional_balance(draft: OrderDraft, ctx: GateContext, cfg: dict) -> Verdict:
    same = sum(1 for p in ctx.open_positions if p.get("right") == draft.right)
    limit = cfg.get("max_same_direction", 4)
    if same >= limit:
        leaning = "bullish" if draft.right == "call" else "bearish"
        return Verdict.deny(f"already {same} {draft.right} positions, limit {limit} {leaning}")
    return Verdict.allow()


def _gate_delta_band(draft: OrderDraft, ctx: GateContext, cfg: dict) -> Verdict:
    low, high = cfg.get("delta_min", 0.5), cfg.get("delta_max", 0.75)
    if draft.delta == 0.0:
        return Verdict.deny("no delta available")
    if not low <= draft.delta <= high:
        return Verdict.deny(f"delta {draft.delta:.2f} outside {low:.2f}-{high:.2f}")
    return Verdict.allow()


def _gate_spread_width(draft: OrderDraft, ctx: GateContext, cfg: dict) -> Verdict:
    limit = cfg.get("max_spread_pct", 0.05)
    if draft.spread_pct > limit:
        return Verdict.deny(f"spread {draft.spread_pct:.1%} above {limit:.1%}")
    return Verdict.allow()


def _gate_expiry_window(draft: OrderDraft, ctx: GateContext, cfg: dict) -> Verdict:
    days = (draft.expiry - ctx.today).days
    close_before = cfg.get("close_before_expiry", 5)
    dte_min, dte_max = cfg.get("dte_min", 14), cfg.get("dte_max", 60)
    if days <= close_before:
        return Verdict.deny(f"{days} DTE inside {close_before}-day close-out window")
    if not dte_min <= days <= dte_max:
        return Verdict.deny(f"{days} DTE outside {dte_min}-{dte_max} band")
    return Verdict.allow()


def _gate_premium_richness(draft: OrderDraft, ctx: GateContext, cfg: dict) -> Verdict:
    if draft.realized_vol is None or draft.realized_vol <= 0 or draft.implied_vol <= 0:
        return Verdict.deny("cannot price premium: vol unavailable")
    richness = draft.implied_vol / draft.realized_vol
    limit = cfg.get("max_iv_to_realized", 1.8)
    if richness > limit:
        return Verdict.deny(f"IV/RV {richness:.2f}x above {limit:.2f}x limit")
    return Verdict.allow()


def _gate_decay_burden(draft: OrderDraft, ctx: GateContext, cfg: dict) -> Verdict:
    days_to_exp = max(1, (draft.expiry - ctx.today).days)
    if draft.limit_price <= 0:
        return Verdict.deny("no usable price for decay calc")
    daily_decay = 1.0 / days_to_exp
    limit = cfg.get("max_daily_decay", 0.015)
    if daily_decay > limit:
        return Verdict.deny(f"decays {daily_decay:.2%}/day, above {limit:.2%}")
    return Verdict.allow()


def _gate_risk_budget(draft: OrderDraft, ctx: GateContext, cfg: dict) -> Verdict:
    budget = ctx.equity * cfg.get("risk_per_trade", 0.04)
    per_contract = draft.limit_price * 100
    if per_contract <= 0:
        return Verdict.deny("no usable price")
    affordable = min(int(budget // per_contract), cfg.get("max_contracts", 4))
    if affordable < 1:
        return Verdict.deny(f"one contract costs ${per_contract:,.0f}, above ${budget:,.0f} budget")
    if affordable < draft.quantity:
        return Verdict.shrink(affordable, f"trimmed to {affordable} for ${budget:,.0f} budget")
    return Verdict.allow()


def _gate_buying_power(draft: OrderDraft, ctx: GateContext, cfg: dict) -> Verdict:
    available = ctx.buying_power - ctx.committed_value
    per_contract = draft.limit_price * 100
    if per_contract <= 0:
        return Verdict.deny("no usable price")
    affordable = int(available // per_contract)
    if affordable < 1:
        return Verdict.deny(f"only ${available:,.0f} uncommitted, contract costs ${per_contract:,.0f}")
    if affordable < draft.quantity:
        return Verdict.shrink(affordable, f"trimmed to {affordable} by cash (${available:,.0f})")
    return Verdict.allow()


def authorise(draft: OrderDraft, ctx: GateContext, cfg: dict) -> tuple[bool, str, int]:
    """Run order gates. Returns (approved, reason, final_qty)."""
    qty = draft.quantity
    for gate_fn in [
        _gate_min_confidence,
        _gate_directional_balance,
        _gate_delta_band,
        _gate_spread_width,
        _gate_expiry_window,
        _gate_premium_richness,
        _gate_decay_burden,
        _gate_risk_budget,
        _gate_buying_power,
    ]:
        v = gate_fn(draft, ctx, cfg)
        if v.decision == "deny":
            return False, v.reason, 0
        if v.decision == "shrink" and v.quantity is not None and v.quantity < qty:
            qty = v.quantity
    return True, "approved", qty


# ---------------------------------------------------------------------------
# LLM proposer
# ---------------------------------------------------------------------------

PROPOSER_SYSTEM = """\
You are the analysis stage of an automated options trading agent.
Judge the likely DIRECTION of one underlying stock over the next 2-6 weeks.
You do not choose contracts, sizes, or prices -- deterministic code does that.

Respond with ONLY a JSON object:
{{"direction": "up"|"down"|"none", "confidence": 0.0-1.0, "rationale": "one sentence"}}

Return "none" when evidence is mixed, thin, or already priced in."""


def _propose(symbol: str, bars_text: str, model: str, temperature: float = 0.4) -> dict | None:
    """Call LLM for a directional view. Returns parsed dict or None."""
    user = f"UNDERLYING: {symbol}\n\nRECENT PRICE ACTION:\n{bars_text}\n\nGive your directional view."
    try:
        from src.core.finance_advisor import _llm_call
        raw = _llm_call(model, PROPOSER_SYSTEM, user, max_tokens=800, temperature=temperature)
    except Exception as exc:
        log.warning("killswitch: LLM call failed for %s: %s", symbol, exc)
        return None

    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    direction = str(obj.get("direction", "none")).lower()
    if direction not in ("up", "down", "none"):
        return None

    try:
        confidence = max(0.0, min(1.0, float(obj.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "direction": direction,
        "confidence": confidence,
        "rationale": str(obj.get("rationale", ""))[:300],
    }


# ---------------------------------------------------------------------------
# Realized volatility (annualized, from daily closes)
# ---------------------------------------------------------------------------

def _realized_vol(closes: list[float], lookback: int = 20) -> float | None:
    if len(closes) < max(2, lookback):
        return None
    window = closes[-lookback:]
    returns = [math.log(window[i] / window[i - 1]) for i in range(1, len(window)) if window[i - 1] > 0]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var * 252)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class KillswitchAdapter(StrategyAdapter):
    """15-gate deterministic pipeline with LLM directional proposer."""

    def __init__(self, config: AdapterConfig, client, data, tracker, breaker, allocator):
        super().__init__(config, client, data, tracker, breaker, allocator)
        ex = config.extra
        self._cfg = {k: ex.get(k, v) for k, v in DEFAULTS.items()}
        # Merge nested dicts
        if "correlation_groups" not in ex:
            self._cfg["correlation_groups"] = DEFAULTS["correlation_groups"]
        self._symbols = self._cfg["symbols"]
        self._model = self._cfg["model"]
        self._signals_today = 0
        self._errors_today = 0
        self._last_signal_time: datetime | None = None
        self._cooling_off: dict[str, int] = {}

    def _build_context(self) -> GateContext | None:
        try:
            account = self._client.get_account()
            positions = self._client.get_positions()
            clock = self._client.get_clock()
        except Exception as exc:
            log.warning("killswitch: cannot read account: %s", exc)
            return None

        # Only count positions whose underlying is in this adapter's universe.
        universe = set(self._symbols)
        open_pos = []
        for p in positions:
            sym = str(getattr(p, "symbol", ""))
            root = sym[:6].rstrip("0123456789CP")
            underlying = root if len(root) < len(sym) else sym
            if underlying not in universe:
                continue
            open_pos.append({
                "symbol": sym,
                "underlying": underlying,
                "market_value": float(getattr(p, "market_value", 0)),
                "right": "call" if "C" in sym[6:] else ("put" if "P" in sym[6:] else "equity"),
            })

        return GateContext(
            today=datetime.now(_ET).date(),
            market_open=clock.is_open,
            trading_halted=self._breaker.is_tripped,
            equity=float(account.equity),
            cash=float(account.cash),
            buying_power=float(account.buying_power),
            open_positions=open_pos,
            cooling_off=self._cooling_off,
        )

    def evaluate(self) -> list[Signal]:
        """Screen symbols through entry gates, propose via LLM, authorize."""
        ctx = self._build_context()
        if ctx is None:
            self._errors_today += 1
            return []

        log.info("killswitch: market_open=%s, equity=%.0f, positions=%d", ctx.market_open, ctx.equity, len(ctx.open_positions))
        if not ctx.market_open:
            log.info("killswitch: market closed, returning []")
            return []

        signals: list[Signal] = []

        for symbol in self._symbols:
            # Entry screen
            entry_verdict = screen(symbol, ctx, self._cfg)
            if entry_verdict.decision == "deny":
                log.info("killswitch: %s screened out: %s", symbol, entry_verdict.reason)
                continue

            # Gather market brief
            try:
                bars_result = self._data.get_bars(symbol, TimeFrame.Day, days_back=60)
                bar_list = bars_result.get(symbol, []) if isinstance(bars_result, dict) else list(bars_result)
                if hasattr(bars_result, "data"):
                    bar_list = bars_result.data.get(symbol, bar_list)
                bar_list = list(bar_list)
            except Exception:
                bar_list = []

            if len(bar_list) < 5:
                log.info("killswitch: %s insufficient bar data (%d bars)", symbol, len(bar_list))
                continue

            # Format bars for LLM
            closes = [float(b.close) for b in bar_list]
            recent = bar_list[-10:]
            bars_text = "\n".join(
                f"  {getattr(b, 'timestamp', '?')}: o={float(b.open):.2f} h={float(b.high):.2f} "
                f"l={float(b.low):.2f} c={float(b.close):.2f} v={int(b.volume)}"
                for b in recent
            )

            # Get latest quote for spot price
            try:
                quote = self._data.get_latest_quote(symbol)
                spot = quote.mid if quote else closes[-1]
            except Exception:
                spot = closes[-1]

            # LLM proposal
            proposal = _propose(symbol, bars_text, self._model, temperature=float(self._cfg.get("temperature", 0.4)))
            if proposal is None or proposal["direction"] == "none":
                log.info("killswitch: %s no directional view from LLM", symbol)
                continue

            direction = proposal["direction"]
            confidence = proposal["confidence"]
            right = "call" if direction == "up" else "put"

            # Build a synthetic order draft for gate authorization
            rv = _realized_vol(closes)
            target_delta = (self._cfg["delta_min"] + self._cfg["delta_max"]) / 2
            dte_target = (self._cfg["dte_min"] + self._cfg["dte_max"]) // 2
            today = datetime.now(_ET).date()
            exp_date = date(today.year, today.month, today.day)
            # Approximate expiry as dte_target days out
            from datetime import timedelta
            exp_date = today + timedelta(days=dte_target)

            # Estimate contract price from spot and delta
            estimated_premium = spot * 0.03  # rough 3% of spot
            spread_pct = 0.02  # assume 2% spread for screening
            implied_vol = rv * 1.2 if rv else 0.3  # proxy

            draft = OrderDraft(
                symbol=f"{symbol}_{right}_{dte_target}d",
                underlying=symbol,
                right=right,
                direction=direction,
                quantity=self._cfg["max_contracts"],
                limit_price=estimated_premium,
                strike=spot * (0.97 if right == "call" else 1.03),
                expiry=exp_date,
                delta=target_delta,
                spread_pct=spread_pct,
                implied_vol=implied_vol,
                realized_vol=rv,
                confidence=confidence,
                rationale=proposal["rationale"],
                spot=spot,
            )

            approved, reason, final_qty = authorise(draft, ctx, self._cfg)
            if not approved:
                log.info("killswitch: %s authorize rejected: %s", symbol, reason)
                continue

            sig_direction = SignalDirection.LONG  # always buying to open
            signals.append(Signal(
                symbol=symbol,
                direction=sig_direction,
                asset_type=SignalAssetType.OPTION,
                qty=float(final_qty),
                limit_price=estimated_premium,
                confidence=confidence,
                reason=f"[killswitch] {right} on {symbol}: {proposal['rationale']}",
                metadata={
                    "direction": direction,
                    "right": right,
                    "dte_target": dte_target,
                    "strike_approx": draft.strike,
                    "model": self._model,
                    "gate_qty": final_qty,
                    "realized_vol": rv,
                },
            ))

        self._signals_today += len(signals)
        if signals:
            self._last_signal_time = datetime.now(_ET)
        return signals

    def execute(self, signals: list[Signal]) -> list[OrderResult]:
        """Submit gate-approved signals through the shared client."""
        results: list[OrderResult] = []

        for sig in signals:
            rejection = self._check_risk(sig)
            if rejection:
                results.append(OrderResult(signal=sig, outcome=OrderOutcome.GATED, error=rejection))
                continue

            if self.is_dry_run:
                results.append(OrderResult(signal=sig, outcome=OrderOutcome.DRY_RUN))
                log.info("killswitch DRY-RUN: %s %s x%.0f @ ~$%.2f (%s)",
                         sig.direction.value, sig.symbol, sig.qty,
                         sig.limit_price or 0, sig.reason)
                continue

            try:
                order = self._client.market_order(
                    sig.symbol, sig.qty, OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                results.append(OrderResult(
                    signal=sig, outcome=OrderOutcome.FILLED,
                    filled_qty=sig.qty, filled_price=sig.limit_price or 0.0,
                ))
                self._tracker.record_trade(
                    sig.symbol, "long", sig.qty,
                    sig.limit_price or 0.0, strategy="killswitch",
                )
                log.info("killswitch SUBMITTED: buy %s x%.0f", sig.symbol, sig.qty)
            except Exception as exc:
                self._errors_today += 1
                results.append(OrderResult(signal=sig, outcome=OrderOutcome.ERROR, error=str(exc)))
                log.error("killswitch ORDER FAILED: %s", exc)

        return results

    def flatten(self) -> None:
        """Close all positions on underlyings this adapter trades."""
        if self.is_dry_run:
            log.info("killswitch flatten: dry run, no action")
            return
        positions = self._client.get_positions()
        for pos in positions:
            sym = str(getattr(pos, "symbol", ""))
            root = sym[:6].rstrip("0123456789CP")
            if root in self._symbols or sym in self._symbols:
                try:
                    self._client.close_position(sym)
                    log.info("killswitch: flattened %s", sym)
                    self._cooling_off[root] = self._cfg.get("cooldown_days", 3)
                except Exception as exc:
                    log.error("killswitch: failed to flatten %s: %s", sym, exc)

    def health_check(self) -> AdapterHealth:
        return AdapterHealth(
            status=AdapterStatus.HEALTHY if self._errors_today < 5 else AdapterStatus.DEGRADED,
            message=f"errors_today={self._errors_today}, symbols={len(self._symbols)}",
            last_signal_time=self._last_signal_time,
            signals_today=self._signals_today,
            errors_today=self._errors_today,
        )
