"""Should-AI Buy adapter: multi-stage discovery pipeline with AI council.

Ported from nabielnovelkhubran's 2nd-place hackathon entry (TypeScript/Next.js).
The original system runs an 18-step autonomous trading cycle with a 5-stage
candidate discovery pipeline, multi-agent AI council, deterministic position
sizing, and an authoritative risk gate.

Pipeline stages (all deterministic except stage 5):
  1. Universe & session filter - skip equities when market is closed
  2. Liquidity & spread filter - minimum dollar liquidity, max spread bps
  3. Market regime classification - trending/range-bound/volatile from price data
  4. Multi-factor opportunity scoring - momentum, volume, volatility, RSI, R:R
  5. AI decision engine - multi-agent synthesis (quant + intelligence + red team)

What this adapter preserves:
  - The 5-stage progressive filter pipeline
  - Multi-factor scoring (momentum score, RSI, relative volume, risk/reward)
  - Market regime classification from price action
  - Deterministic position sizing with risk constraints
  - The fail-closed invariant: any LLM failure defaults to PASS (no trade)

What it replaces:
  - Featherless API -> Dell4 LLM cluster
  - TypeScript runtime -> pure Python
  - Next.js API routes -> direct adapter calls
  - Paper trading service -> our shared AlpacaClient

All numeric thresholds match the original TypeScript constants.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
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
    "universe": ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "JPM"],
    "model": "dell4-devstral",
    "scan_limit": 5,
    "max_open_positions": 5,
    "min_opportunity_score": 55,
    "min_confidence_score": 60,
    "min_risk_reward_ratio": 1.5,
    "min_liquidity_usd": 500_000,
    "max_spread_bps": 50,
    "max_position_size_pct": 0.05,
    "max_gross_exposure_pct": 0.60,
    "risk_profile": "MODERATE",
}


# ---------------------------------------------------------------------------
# Stage 1-4: Deterministic pipeline filters
# ---------------------------------------------------------------------------

@dataclass
class MarketSnapshot:
    symbol: str
    price: float
    open_price: float
    high: float
    low: float
    volume_24h: float
    change_24h: float  # percentage
    rsi_14: float
    momentum_score: float
    relative_volume: float
    volume_acceleration: float
    realized_volatility: float
    liquidity_usd: float
    spread_bps: float


@dataclass
class MarketRegime:
    regime: str  # "TRENDING_UP", "TRENDING_DOWN", "RANGE_BOUND", "VOLATILE"
    confidence: int
    trend_direction: str  # "BULLISH", "BEARISH", "NEUTRAL"


@dataclass
class MultiFactorScore:
    opportunity_score: float
    risk_reward_ratio: float
    recommended_strategy: str
    factors: dict[str, float]
    is_eligible: bool
    warnings: list[str]


@dataclass
class PipelineCandidate:
    symbol: str
    rank: int
    snapshot: MarketSnapshot
    regime: MarketRegime
    score: MultiFactorScore


def _compute_rsi(closes: list[float], period: int = 14) -> float:
    """Relative Strength Index from a list of closing prices."""
    if len(closes) < period + 1:
        return 50.0  # neutral default
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-(period):]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0.001
    avg_loss = sum(losses) / period if losses else 0.001
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _momentum_score(closes: list[float]) -> float:
    """Score 0-100 based on short/medium trend alignment."""
    if len(closes) < 20:
        return 50.0
    price = closes[-1]
    sma5 = sum(closes[-5:]) / 5
    sma20 = sum(closes[-20:]) / 20

    score = 50.0
    # Above SMA5: short trend up
    if price > sma5:
        score += 15
    else:
        score -= 10

    # Above SMA20: medium trend up
    if price > sma20:
        score += 15
    else:
        score -= 10

    # SMA5 > SMA20: bullish crossover
    if sma5 > sma20:
        score += 10
    else:
        score -= 5

    # Position in 20-day range
    high20 = max(closes[-20:])
    low20 = min(closes[-20:])
    if high20 > low20:
        range_pos = (price - low20) / (high20 - low20)
        score += (range_pos - 0.5) * 20

    return max(0.0, min(100.0, score))


def _realized_vol(closes: list[float], lookback: int = 20) -> float:
    """Annualized realized volatility as percentage."""
    if len(closes) < max(2, lookback):
        return 20.0
    window = closes[-lookback:]
    returns = [math.log(window[i] / window[i - 1]) for i in range(1, len(window)) if window[i - 1] > 0]
    if len(returns) < 2:
        return 20.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var * 252) * 100


def _classify_regime(snapshot: MarketSnapshot) -> MarketRegime:
    """Classify market regime from snapshot metrics."""
    momentum = snapshot.momentum_score
    vol = snapshot.realized_volatility
    rvol = snapshot.relative_volume

    if vol > 40:
        regime = "VOLATILE"
        direction = "NEUTRAL"
        confidence = min(90, int(vol))
    elif momentum > 70 and rvol > 1.2:
        regime = "TRENDING_UP"
        direction = "BULLISH"
        confidence = min(85, int(momentum))
    elif momentum < 30 and rvol > 1.2:
        regime = "TRENDING_DOWN"
        direction = "BEARISH"
        confidence = min(85, int(100 - momentum))
    else:
        regime = "RANGE_BOUND"
        direction = "NEUTRAL"
        confidence = 70

    return MarketRegime(regime=regime, confidence=confidence, trend_direction=direction)


def _score_opportunity(snapshot: MarketSnapshot, regime: MarketRegime,
                       min_score: float, min_rr: float) -> MultiFactorScore:
    """Multi-factor opportunity scoring matching the TypeScript evaluateMultiFactorOpportunity."""
    factors: dict[str, float] = {}
    warnings: list[str] = []

    # Momentum (0-30 points)
    factors["momentum"] = min(30.0, snapshot.momentum_score * 0.3)

    # Volume (0-20 points)
    vol_score = min(20.0, snapshot.relative_volume * 10)
    if snapshot.volume_acceleration > 1.5:
        vol_score = min(20.0, vol_score + 5)
    factors["volume"] = vol_score

    # Volatility (0-20 points) -- moderate vol is best
    vol_pct = snapshot.realized_volatility
    if 15 < vol_pct < 35:
        factors["volatility"] = 15.0
    elif 10 < vol_pct <= 15 or 35 <= vol_pct < 50:
        factors["volatility"] = 10.0
    else:
        factors["volatility"] = 5.0

    # RSI signal (0-15 points) -- oversold = opportunity
    rsi = snapshot.rsi_14
    if rsi < 30:
        factors["rsi"] = 15.0  # oversold, strong buy signal
    elif rsi < 40:
        factors["rsi"] = 10.0
    elif 40 <= rsi <= 60:
        factors["rsi"] = 7.0   # neutral
    elif rsi > 70:
        factors["rsi"] = 3.0   # overbought, weak
    else:
        factors["rsi"] = 5.0

    # Regime compatibility (0-15 points)
    if regime.regime == "TRENDING_UP":
        factors["regime"] = 15.0
    elif regime.regime == "RANGE_BOUND":
        factors["regime"] = 10.0
    elif regime.regime == "VOLATILE":
        factors["regime"] = 5.0
    else:
        factors["regime"] = 3.0

    opportunity_score = sum(factors.values())

    # Estimate risk/reward from momentum and volatility
    stop_pct = max(0.015, min(0.08, vol_pct * 0.0012))
    rr = max(1.0, (snapshot.momentum_score / 40) * min_rr) if snapshot.momentum_score > 40 else 1.0

    is_eligible = opportunity_score >= min_score and rr >= min_rr
    if not is_eligible:
        if opportunity_score < min_score:
            warnings.append(f"Score {opportunity_score:.0f} below threshold {min_score}")
        if rr < min_rr:
            warnings.append(f"R:R {rr:.1f} below {min_rr}")

    return MultiFactorScore(
        opportunity_score=round(opportunity_score, 1),
        risk_reward_ratio=round(rr, 2),
        recommended_strategy="MOMENTUM_BREAKOUT" if snapshot.momentum_score > 60 else "MEAN_REVERSION",
        factors=factors,
        is_eligible=is_eligible,
        warnings=warnings,
    )


def _build_snapshot(symbol: str, bars: list, quote_mid: float) -> MarketSnapshot | None:
    """Build a MarketSnapshot from Alpaca bar data."""
    if not bars or len(bars) < 5:
        return None

    closes = [float(b.close) for b in bars]
    volumes = [float(b.volume) for b in bars]
    price = quote_mid if quote_mid > 0 else closes[-1]

    # Change 24h
    if len(closes) >= 2 and closes[-2] > 0:
        change_24h = ((closes[-1] - closes[-2]) / closes[-2]) * 100
    else:
        change_24h = 0.0

    # Relative volume (today vs 20-day average)
    avg_vol = sum(volumes[-20:]) / len(volumes[-20:]) if len(volumes) >= 20 else sum(volumes) / len(volumes)
    relative_volume = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    # Volume acceleration (last 5 vs prior 5)
    if len(volumes) >= 10:
        recent_avg = sum(volumes[-5:]) / 5
        prior_avg = sum(volumes[-10:-5]) / 5
        vol_accel = recent_avg / prior_avg if prior_avg > 0 else 1.0
    else:
        vol_accel = 1.0

    return MarketSnapshot(
        symbol=symbol,
        price=price,
        open_price=float(bars[-1].open),
        high=max(float(b.high) for b in bars[-5:]),
        low=min(float(b.low) for b in bars[-5:]),
        volume_24h=volumes[-1],
        change_24h=round(change_24h, 2),
        rsi_14=round(_compute_rsi(closes), 1),
        momentum_score=round(_momentum_score(closes), 1),
        relative_volume=round(relative_volume, 2),
        volume_acceleration=round(vol_accel, 2),
        realized_volatility=round(_realized_vol(closes), 1),
        liquidity_usd=price * volumes[-1] if volumes else 0,
        spread_bps=10.0,  # updated from live quote if available
    )


# ---------------------------------------------------------------------------
# Stage 5: AI decision engine (multi-agent synthesis)
# ---------------------------------------------------------------------------

AI_SYSTEM_PROMPT = """\
You are an institutional trading council synthesis model. Analyze the
provided market evidence and quantitative scores to produce a trading
decision.

Respond ONLY with a valid JSON object:
{{
  "decision": "BUY" | "HOLD" | "PASS",
  "confidence": <number 0-100>,
  "thesis": "<investment hypothesis>",
  "invalidation": "<what would disprove the thesis>",
  "risks": ["<risk 1>", "<risk 2>"],
  "risk_reward_ratio": <number>
}}

This is a PAPER trading account for a hackathon. Deploy capital actively.
BUY when the multi-factor score and regime are favorable.
Only PASS when the evidence is clearly negative (bearish regime + poor scores).
Today is {today}.""".format(today=__import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("America/New_York")).strftime("%Y-%m-%d"))


def _ai_evaluate(symbol: str, snapshot: MarketSnapshot, score: MultiFactorScore,
                 regime: MarketRegime, model: str, temperature: float = 0.4) -> dict | None:
    """Call LLM for trading decision. Returns parsed dict or None on failure."""
    user_prompt = json.dumps({
        "symbol": symbol,
        "price": snapshot.price,
        "change_24h": snapshot.change_24h,
        "rsi_14": snapshot.rsi_14,
        "momentum_score": snapshot.momentum_score,
        "relative_volume": snapshot.relative_volume,
        "realized_volatility": snapshot.realized_volatility,
        "regime": regime.regime,
        "trend_direction": regime.trend_direction,
        "opportunity_score": score.opportunity_score,
        "risk_reward_ratio": score.risk_reward_ratio,
        "strategy": score.recommended_strategy,
        "factor_scores": score.factors,
    }, indent=2)

    try:
        from src.core.finance_advisor import _llm_call
        raw = _llm_call(model, AI_SYSTEM_PROMPT, user_prompt, max_tokens=800, temperature=temperature)
    except Exception as exc:
        log.warning("shouldai: LLM evaluation failed for %s: %s", symbol, exc)
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

    decision = str(obj.get("decision", "PASS")).upper()
    if decision not in ("BUY", "HOLD", "PASS", "SELL"):
        decision = "PASS"

    try:
        confidence = max(0, min(100, float(obj.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0

    return {
        "decision": decision,
        "confidence": confidence,
        "thesis": str(obj.get("thesis", "")),
        "invalidation": str(obj.get("invalidation", "")),
        "risk_reward_ratio": float(obj.get("risk_reward_ratio", 1.0)),
    }


# ---------------------------------------------------------------------------
# Position sizing (deterministic)
# ---------------------------------------------------------------------------

def _calculate_position_size(
    price: float,
    confidence: float,
    opportunity_score: float,
    equity: float,
    available_cash: float,
    gross_exposure: float,
    cfg: dict,
) -> tuple[int, float, list[str]]:
    """Returns (qty, position_value_usd, violations)."""
    violations: list[str] = []
    max_pct = cfg.get("max_position_size_pct", 0.05)
    max_gross = cfg.get("max_gross_exposure_pct", 0.60)

    # Base allocation from confidence
    confidence_mult = max(0.5, min(1.0, confidence / 100))
    base_size = equity * max_pct * confidence_mult

    # Reduce if approaching gross exposure limit
    remaining_exposure = equity * max_gross - gross_exposure
    if remaining_exposure <= 0:
        violations.append(f"Gross exposure at {max_gross:.0%} limit")
        return 0, 0.0, violations

    position_size = min(base_size, remaining_exposure, available_cash * 0.9)

    if price <= 0:
        violations.append("Price is zero or negative")
        return 0, 0.0, violations

    qty = int(position_size / price)
    if qty <= 0:
        violations.append(f"Position size ${position_size:.0f} cannot buy one share at ${price:.2f}")
        return 0, 0.0, violations

    return qty, qty * price, violations


# ---------------------------------------------------------------------------
# Risk gate (deterministic, never LLM)
# ---------------------------------------------------------------------------

def _risk_gate(
    symbol: str,
    opportunity_score: float,
    liquidity_usd: float,
    position_value: float,
    available_cash: float,
    cfg: dict,
) -> tuple[bool, list[str]]:
    """Authoritative deterministic risk gate. Returns (passed, violations)."""
    violations: list[str] = []

    min_score = cfg.get("min_opportunity_score", 55)
    if opportunity_score < min_score:
        violations.append(f"Score {opportunity_score:.0f} below {min_score}")

    min_liq = cfg.get("min_liquidity_usd", 500_000)
    if liquidity_usd < min_liq:
        violations.append(f"Liquidity ${liquidity_usd:,.0f} below ${min_liq:,.0f}")

    if position_value > available_cash * 0.95:
        violations.append(f"Position ${position_value:,.0f} exceeds 95% of cash ${available_cash:,.0f}")

    return len(violations) == 0, violations


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ShouldAIAdapter(StrategyAdapter):
    """Multi-stage discovery pipeline with AI council and deterministic risk gate."""

    def __init__(self, config: AdapterConfig, client, data, tracker, breaker, allocator):
        super().__init__(config, client, data, tracker, breaker, allocator)
        ex = config.extra
        self._cfg = {k: ex.get(k, v) for k, v in DEFAULTS.items()}
        self._universe = self._cfg["universe"]
        self._model = self._cfg["model"]
        self._signals_today = 0
        self._errors_today = 0
        self._last_signal_time: datetime | None = None

    def evaluate(self) -> list[Signal]:
        """Run the 5-stage pipeline and produce trade signals."""
        now = datetime.now(_ET)

        # Read account state
        try:
            account = self._client.get_account()
            positions = self._client.get_positions()
            clock = self._client.get_clock()
        except Exception as exc:
            log.warning("shouldai: cannot read account: %s", exc)
            self._errors_today += 1
            return []

        if not clock.is_open:
            return []

        equity = float(account.equity)
        cash = float(account.cash)
        # Only count positions in this adapter's universe -- other sleeves'
        # positions were filling held_symbols and tripping max_positions=5.
        universe_set = set(self._universe)
        own_positions = [p for p in positions
                         if str(getattr(p, "symbol", "")).upper() in universe_set]
        held_symbols = {str(getattr(p, "symbol", "")).upper() for p in own_positions}
        gross_exposure = sum(abs(float(getattr(p, "market_value", 0))) for p in own_positions)

        candidates: list[PipelineCandidate] = []
        min_score = self._cfg["min_opportunity_score"]
        min_rr = self._cfg["min_risk_reward_ratio"]

        for symbol in self._universe:
            # Stage 1: Session filter (equities need open market -- already checked above)

            # Stage 2: Fetch data and check liquidity/spread
            try:
                bars_result = self._data.get_bars(symbol, TimeFrame.Day, days_back=60)
                bar_list = bars_result.get(symbol, []) if isinstance(bars_result, dict) else list(bars_result)
                if hasattr(bars_result, "data"):
                    bar_list = bars_result.data.get(symbol, bar_list)
                bar_list = list(bar_list)
            except Exception:
                bar_list = []

            # Get live quote for spread
            try:
                quote = self._data.get_latest_quote(symbol)
                quote_mid = quote.mid if quote else 0
                if quote and quote.bid > 0 and quote.ask > 0:
                    spread_bps = ((quote.ask - quote.bid) / quote.mid) * 10_000
                else:
                    spread_bps = 10.0
            except Exception:
                quote_mid = 0
                spread_bps = 10.0

            snapshot = _build_snapshot(symbol, bar_list, quote_mid)
            if snapshot is None:
                continue
            snapshot.spread_bps = spread_bps

            # Liquidity filter
            if snapshot.liquidity_usd < self._cfg["min_liquidity_usd"]:
                log.debug("shouldai: %s liquidity $%.0f below threshold", symbol, snapshot.liquidity_usd)
                continue

            # Spread filter
            if snapshot.spread_bps > self._cfg["max_spread_bps"]:
                log.debug("shouldai: %s spread %.0f bps above threshold", symbol, snapshot.spread_bps)
                continue

            # Stage 3: Market regime
            regime = _classify_regime(snapshot)

            # Stage 4: Multi-factor scoring
            score = _score_opportunity(snapshot, regime, min_score, min_rr)
            if not score.is_eligible:
                log.debug("shouldai: %s score %.0f not eligible: %s",
                          symbol, score.opportunity_score, "; ".join(score.warnings))
                continue

            candidates.append(PipelineCandidate(
                symbol=symbol,
                rank=0,
                snapshot=snapshot,
                regime=regime,
                score=score,
            ))

        # Rank by opportunity score descending
        candidates.sort(key=lambda c: c.score.opportunity_score, reverse=True)
        for i, c in enumerate(candidates):
            c.rank = i + 1

        # Cap at scan_limit
        top = candidates[:self._cfg["scan_limit"]]

        log.info("shouldai: pipeline produced %d candidates from %d universe, held=%d/%d",
                 len(top), len(self._universe), len(held_symbols), self._cfg["max_open_positions"])
        # Stage 5: AI evaluation for each candidate
        signals: list[Signal] = []
        max_positions = self._cfg["max_open_positions"]

        for candidate in top:
            symbol = candidate.symbol

            # Max positions check
            if len(held_symbols) + len(signals) >= max_positions:
                log.debug("shouldai: max positions reached")
                break

            # Already held check
            if symbol in held_symbols:
                log.debug("shouldai: %s already held", symbol)
                continue

            # AI decision
            ai_result = _ai_evaluate(
                symbol, candidate.snapshot, candidate.score,
                candidate.regime, self._model,
                temperature=float(self._cfg.get("temperature", 0.4)),
            )

            if ai_result is None or ai_result["decision"] != "BUY":
                action = ai_result["decision"] if ai_result else "ERROR"
                log.info("shouldai: %s AI says %s", symbol, action)
                continue

            confidence = ai_result["confidence"]
            min_conf = self._cfg["min_confidence_score"]
            if confidence < min_conf:
                log.debug("shouldai: %s confidence %.0f below %s", symbol, confidence, min_conf)
                continue

            # Deterministic position sizing
            qty, pos_value, size_violations = _calculate_position_size(
                price=candidate.snapshot.price,
                confidence=confidence,
                opportunity_score=candidate.score.opportunity_score,
                equity=equity,
                available_cash=cash,
                gross_exposure=gross_exposure,
                cfg=self._cfg,
            )

            if size_violations:
                log.debug("shouldai: %s sizing rejected: %s", symbol, "; ".join(size_violations))
                continue

            # Risk gate
            passed, gate_violations = _risk_gate(
                symbol=symbol,
                opportunity_score=candidate.score.opportunity_score,
                liquidity_usd=candidate.snapshot.liquidity_usd,
                position_value=pos_value,
                available_cash=cash,
                cfg=self._cfg,
            )

            if not passed:
                log.debug("shouldai: %s risk gate rejected: %s", symbol, "; ".join(gate_violations))
                continue

            # Build signal
            signals.append(Signal(
                symbol=symbol,
                direction=SignalDirection.LONG,
                asset_type=SignalAssetType.EQUITY,
                qty=float(qty),
                confidence=confidence / 100.0,
                reason=(f"[shouldai] {candidate.score.recommended_strategy} on {symbol}: "
                        f"score={candidate.score.opportunity_score:.0f}, "
                        f"R:R={candidate.score.risk_reward_ratio:.1f}, "
                        f"regime={candidate.regime.regime}"),
                metadata={
                    "opportunity_score": candidate.score.opportunity_score,
                    "risk_reward_ratio": candidate.score.risk_reward_ratio,
                    "regime": candidate.regime.regime,
                    "ai_confidence": confidence,
                    "ai_thesis": ai_result.get("thesis", ""),
                    "strategy": candidate.score.recommended_strategy,
                    "model": self._model,
                    "factors": candidate.score.factors,
                },
            ))

            gross_exposure += pos_value
            cash -= pos_value

        self._signals_today += len(signals)
        if signals:
            self._last_signal_time = now
        return signals

    def execute(self, signals: list[Signal]) -> list[OrderResult]:
        """Submit pipeline-approved signals through the shared client."""
        results: list[OrderResult] = []

        for sig in signals:
            rejection = self._check_risk(sig)
            if rejection:
                results.append(OrderResult(signal=sig, outcome=OrderOutcome.GATED, error=rejection))
                continue

            if self.is_dry_run:
                results.append(OrderResult(signal=sig, outcome=OrderOutcome.DRY_RUN))
                log.info("shouldai DRY-RUN: buy %s x%.0f (score=%.0f, conf=%.0f%%) %s",
                         sig.symbol, sig.qty,
                         sig.metadata.get("opportunity_score", 0),
                         sig.confidence * 100, sig.reason)
                continue

            try:
                order = self._client.market_order(
                    sig.symbol, sig.qty, OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                results.append(OrderResult(
                    signal=sig, outcome=OrderOutcome.FILLED,
                    filled_qty=sig.qty, filled_price=0.0,
                ))
                self._tracker.record_trade(
                    sig.symbol, "long", sig.qty, 0.0, strategy="shouldai",
                )
                log.info("shouldai SUBMITTED: buy %s x%.0f", sig.symbol, sig.qty)
            except Exception as exc:
                self._errors_today += 1
                results.append(OrderResult(signal=sig, outcome=OrderOutcome.ERROR, error=str(exc)))
                log.error("shouldai ORDER FAILED: %s", exc)

        return results

    def flatten(self) -> None:
        """Close all positions on underlyings in the universe."""
        if self.is_dry_run:
            log.info("shouldai flatten: dry run, no action")
            return
        positions = self._client.get_positions()
        for pos in positions:
            sym = str(getattr(pos, "symbol", "")).upper()
            if sym in self._universe:
                try:
                    self._client.close_position(sym)
                    log.info("shouldai: flattened %s", sym)
                except Exception as exc:
                    log.error("shouldai: failed to flatten %s: %s", sym, exc)

    def health_check(self) -> AdapterHealth:
        return AdapterHealth(
            status=AdapterStatus.HEALTHY if self._errors_today < 5 else AdapterStatus.DEGRADED,
            message=f"errors_today={self._errors_today}, universe={len(self._universe)}",
            last_signal_time=self._last_signal_time,
            signals_today=self._signals_today,
            errors_today=self._errors_today,
        )
