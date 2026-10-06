"""Bull Spread adapter -- multi-leg vertical spreads via Alpaca.

Scans configured symbols for bull put credit and bull call debit spread
opportunities, scores them, and submits multi-leg orders through the
Alpaca MLEG order class.

No symbol is hardcoded anywhere.  The adapter reads its list from
config/strategies.yml and will work on any ticker that has an options
chain with sufficient liquidity.

Configuration example::

    adapters:
      bull_spread:
        enabled: true
        dry_run: true
        capital_pct: 0.05
        symbols: [QBTS, SLV, SPY]
        spread_types: [bull_put_credit, bull_call_debit]
        contracts_per_spread: 1
        max_concurrent_spreads: 5
        wing_width: 5.0
        dte_min: 20
        dte_max: 45
        target_short_delta: 0.20
        min_open_interest: 50
        max_bid_ask_spread_pct: 0.20
        min_net_credit: 0.10
        min_score: 40.0
        scan_interval_seconds: 600
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any

from src.core.options_chain import OptionsChain
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
from src.strategies.spreads.bull_call_debit import (
    BullCallDebitConfig,
    build_bull_call_debit,
)
from src.strategies.spreads.bull_put_credit import (
    BullPutCreditConfig,
    build_bull_put_credit,
)
from src.strategies.spreads.models import SpreadCandidate, SpreadType

log = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL = 600


class BullSpreadAdapter(StrategyAdapter):
    """Adapter that builds and executes vertical bull spreads."""

    def __init__(self, config: AdapterConfig, client, data, tracker, breaker, allocator):
        super().__init__(config, client, data, tracker, breaker, allocator)

        extra = config.extra
        self._symbols: list[str] = extra.get("symbols", [])
        self._spread_types: list[str] = extra.get(
            "spread_types", ["bull_put_credit", "bull_call_debit"]
        )
        self._contracts_per_spread: int = int(extra.get("contracts_per_spread", 1))
        self._max_concurrent: int = int(extra.get("max_concurrent_spreads", 5))
        self._min_score: float = float(extra.get("min_score", 40.0))
        self._scan_interval: int = int(extra.get("scan_interval_seconds", DEFAULT_SCAN_INTERVAL))

        # Build spread configs from adapter extra
        self._put_cfg = BullPutCreditConfig(
            target_short_delta=float(extra.get("target_short_delta", 0.20)),
            wing_width=float(extra.get("wing_width", 5.0)),
            dte_min=int(extra.get("dte_min", 20)),
            dte_max=int(extra.get("dte_max", 45)),
            max_bid_ask_spread_pct=float(extra.get("max_bid_ask_spread_pct", 0.20)),
            min_open_interest=int(extra.get("min_open_interest", 50)),
            min_net_credit=float(extra.get("min_net_credit", 0.10)),
        )
        self._call_cfg = BullCallDebitConfig(
            wing_width=float(extra.get("wing_width", 5.0)),
            dte_min=int(extra.get("dte_min", 20)),
            dte_max=int(extra.get("dte_max", 45)),
            max_bid_ask_spread_pct=float(extra.get("max_bid_ask_spread_pct", 0.20)),
            min_open_interest=int(extra.get("min_open_interest", 50)),
        )

        self._chain = OptionsChain(client)
        self._last_scan: float = 0.0
        self._errors_today: int = 0
        self._signals_today: int = 0
        self._last_signal_time: datetime | None = None
        self._active_spreads: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health_check(self) -> AdapterHealth:
        if not self._symbols:
            return AdapterHealth(
                status=AdapterStatus.DEGRADED,
                message="No symbols configured",
            )
        return AdapterHealth(
            status=AdapterStatus.HEALTHY,
            message=f"{len(self._symbols)} symbols, {len(self._active_spreads)} active spreads",
            last_signal_time=self._last_signal_time,
            signals_today=self._signals_today,
            errors_today=self._errors_today,
        )

    # ------------------------------------------------------------------
    # Evaluate
    # ------------------------------------------------------------------

    def evaluate(self) -> list[Signal]:
        now = time.monotonic()
        if now - self._last_scan < self._scan_interval:
            return []
        self._last_scan = now

        if len(self._active_spreads) >= self._max_concurrent:
            log.info("bull_spread: at max concurrent spreads (%d), skipping scan",
                     self._max_concurrent)
            return []

        signals: list[Signal] = []
        for symbol in self._symbols:
            try:
                candidates = self._scan_symbol(symbol)
                for cand in candidates:
                    if cand.score >= self._min_score:
                        sig = self._candidate_to_signal(cand)
                        signals.append(sig)
            except Exception:
                log.exception("bull_spread: error scanning %s", symbol)
                self._errors_today += 1

        if signals:
            self._signals_today += len(signals)
            self._last_signal_time = datetime.now()
            log.info("bull_spread: %d candidates across %d symbols",
                     len(signals), len(self._symbols))

        return signals

    def _scan_symbol(self, symbol: str) -> list[SpreadCandidate]:
        """Fetch option chain and build spread candidates for one symbol."""
        candidates: list[SpreadCandidate] = []

        # Get current price
        try:
            price = self._get_underlying_price(symbol)
        except Exception:
            log.warning("bull_spread: no price for %s, skipping", symbol)
            return []

        if price <= 0:
            return []

        # Bull Put Credit
        if "bull_put_credit" in self._spread_types:
            puts = self._fetch_chain_as_dicts(symbol, "put")
            if puts:
                cand = build_bull_put_credit(puts, price, symbol, self._put_cfg)
                if cand:
                    candidates.append(cand)

        # Bull Call Debit
        if "bull_call_debit" in self._spread_types:
            calls = self._fetch_chain_as_dicts(symbol, "call")
            if calls:
                cand = build_bull_call_debit(calls, price, symbol, self._call_cfg)
                if cand:
                    candidates.append(cand)

        return candidates

    def _get_underlying_price(self, symbol: str) -> float:
        """Get current price from market data service."""
        try:
            quote = self._data.get_latest_quote(symbol)
            ask = float(quote.get("ask_price", 0) if isinstance(quote, dict) else getattr(quote, "ask_price", 0))
            bid = float(quote.get("bid_price", 0) if isinstance(quote, dict) else getattr(quote, "bid_price", 0))
            if ask > 0 and bid > 0:
                return (ask + bid) / 2.0
            return ask or bid
        except Exception:
            pass

        try:
            bar = self._data.get_latest_bar(symbol)
            return float(bar.get("close", 0) if isinstance(bar, dict) else getattr(bar, "close", 0))
        except Exception:
            return 0.0

    def _fetch_chain_as_dicts(self, symbol: str, contract_type: str) -> list[dict[str, Any]]:
        """Fetch option contracts and return as plain dicts for the builders."""
        try:
            if contract_type == "put":
                raw = self._chain.get_puts(
                    symbol,
                    min_dte=self._put_cfg.dte_min,
                    max_dte=self._put_cfg.dte_max,
                    limit=200,
                )
            else:
                raw = self._chain.get_calls(
                    symbol,
                    min_dte=self._call_cfg.dte_min,
                    max_dte=self._call_cfg.dte_max,
                    limit=200,
                )
        except Exception:
            log.exception("bull_spread: chain fetch failed for %s %s", symbol, contract_type)
            return []

        # Convert OptionCandidate to plain dict
        # Note: OptionCandidate from our chain module lacks bid/ask/delta.
        # For now we include what we have; a richer chain source would add Greeks.
        return [
            {
                "symbol": c.symbol,
                "strike_price": c.strike_price,
                "expiration": c.expiration,
                "open_interest": c.open_interest,
                "bid": None,        # TODO: enrich from options quotes API
                "ask": None,
                "delta": None,
            }
            for c in raw
        ]

    def _candidate_to_signal(self, cand: SpreadCandidate) -> Signal:
        """Wrap a SpreadCandidate into a Signal for the coordinator."""
        return Signal(
            symbol=cand.underlying,
            direction=SignalDirection.LONG,
            asset_type=SignalAssetType.OPTION,
            qty=self._contracts_per_spread,
            confidence=cand.score / 100.0,
            reason=cand.rationale,
            metadata={
                "spread_type": cand.spread_type.value,
                "short_symbol": cand.short_leg.option_symbol,
                "long_symbol": cand.long_leg.option_symbol,
                "short_strike": cand.short_leg.strike,
                "long_strike": cand.long_leg.strike,
                "expiration": str(cand.expiration),
                "wing_width": cand.wing_width,
                "net_credit": cand.net_credit,
                "net_debit": cand.net_debit,
                "max_profit": cand.max_profit,
                "max_loss": cand.max_loss,
                "breakeven": cand.breakeven,
                "score": cand.score,
            },
        )

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def execute(self, signals: list[Signal]) -> list[OrderResult]:
        results: list[OrderResult] = []

        for signal in signals:
            risk_reason = self._check_risk(signal)
            if risk_reason:
                results.append(OrderResult(
                    signal=signal,
                    outcome=OrderOutcome.GATED,
                    error=risk_reason,
                ))
                continue

            if len(self._active_spreads) >= self._max_concurrent:
                results.append(OrderResult(
                    signal=signal,
                    outcome=OrderOutcome.GATED,
                    error=f"max concurrent spreads ({self._max_concurrent}) reached",
                ))
                continue

            meta = signal.metadata

            if self.is_dry_run:
                log.info(
                    "bull_spread DRY_RUN: %s %s/%s on %s "
                    "credit=$%.2f debit=$%.2f max_loss=$%.2f score=%.1f",
                    meta.get("spread_type"),
                    meta.get("long_strike"),
                    meta.get("short_strike"),
                    signal.symbol,
                    meta.get("net_credit", 0),
                    meta.get("net_debit", 0),
                    meta.get("max_loss", 0),
                    meta.get("score", 0),
                )
                results.append(OrderResult(
                    signal=signal,
                    outcome=OrderOutcome.DRY_RUN,
                    filled_qty=signal.qty,
                ))
                self._active_spreads.append({
                    "symbol": signal.symbol,
                    "spread_type": meta.get("spread_type"),
                    "opened_at": datetime.now().isoformat(),
                    "dry_run": True,
                })
                continue

            # Live execution via Alpaca MLEG order
            try:
                order = self._submit_mleg_order(signal)
                results.append(OrderResult(
                    signal=signal,
                    outcome=OrderOutcome.FILLED,
                    filled_qty=signal.qty,
                    filled_price=0.0,
                ))
                self._active_spreads.append({
                    "symbol": signal.symbol,
                    "spread_type": meta.get("spread_type"),
                    "order_id": getattr(order, "id", None),
                    "opened_at": datetime.now().isoformat(),
                    "short_symbol": meta.get("short_symbol"),
                    "long_symbol": meta.get("long_symbol"),
                })
                log.info("bull_spread FILLED: %s %s on %s",
                         meta.get("spread_type"), signal.symbol,
                         meta.get("expiration"))
            except Exception as exc:
                self._errors_today += 1
                log.exception("bull_spread: MLEG order failed for %s", signal.symbol)
                results.append(OrderResult(
                    signal=signal,
                    outcome=OrderOutcome.ERROR,
                    error=str(exc),
                ))

        return results

    def _submit_mleg_order(self, signal: Signal):
        """Submit a multi-leg order to Alpaca."""
        from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce, PositionIntent
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

        meta = signal.metadata
        spread_type = meta["spread_type"]

        # Build legs
        if spread_type == SpreadType.BULL_PUT_CREDIT.value:
            # Credit spread: net limit price is the credit we want
            limit_price = meta["net_credit"]
            legs = [
                OptionLegRequest(
                    symbol=meta["short_symbol"],
                    ratio_qty=1,
                    position_intent=PositionIntent.SELL_TO_OPEN,
                ),
                OptionLegRequest(
                    symbol=meta["long_symbol"],
                    ratio_qty=1,
                    position_intent=PositionIntent.BUY_TO_OPEN,
                ),
            ]
        else:
            # Debit spread
            limit_price = meta["net_debit"]
            legs = [
                OptionLegRequest(
                    symbol=meta["long_symbol"],
                    ratio_qty=1,
                    position_intent=PositionIntent.BUY_TO_OPEN,
                ),
                OptionLegRequest(
                    symbol=meta["short_symbol"],
                    ratio_qty=1,
                    position_intent=PositionIntent.SELL_TO_OPEN,
                ),
            ]

        req = LimitOrderRequest(
            qty=signal.qty,
            limit_price=limit_price,
            order_class=OrderClass.MLEG,
            time_in_force=TimeInForce.DAY,
            type=OrderType.LIMIT,
            legs=legs,
        )

        return self._client.trading.submit_order(req)

    # ------------------------------------------------------------------
    # Flatten
    # ------------------------------------------------------------------

    def flatten(self) -> None:
        """Close all active spreads. No LLM, no delay."""
        if self.is_dry_run:
            count = len(self._active_spreads)
            self._active_spreads.clear()
            log.info("bull_spread: flatten (dry run, cleared %d spreads)", count)
            return

        for spread in list(self._active_spreads):
            try:
                # Close each leg individually
                for leg_key in ("short_symbol", "long_symbol"):
                    sym = spread.get(leg_key)
                    if sym:
                        try:
                            self._client.close_position(sym)
                            log.info("bull_spread: closed leg %s", sym)
                        except Exception:
                            log.debug("bull_spread: leg %s already closed or not found", sym)
            except Exception:
                log.exception("bull_spread: error flattening spread on %s", spread.get("symbol"))

        self._active_spreads.clear()
