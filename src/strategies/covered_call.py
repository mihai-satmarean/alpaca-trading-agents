"""Covered Call strategy: sell calls against existing long stock positions."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from src.core.alpaca_client import AlpacaClient
from src.core.options_chain import OptionsChain, OptionCandidate
from src.core.market_data import MarketDataService
from src.core.position_tracker import PositionTracker

log = logging.getLogger(__name__)


@dataclass
class CoveredCallOpportunity:
    candidate: OptionCandidate
    underlying_qty: float
    current_price: float
    premium_pct: float
    upside_to_strike_pct: float
    contracts_possible: int
    score: float


class CoveredCallStrategy:
    """Identifies long stock positions eligible for covered calls and places them.

    Covered call: own >= 100 shares of a stock, sell a call option against them.
    Profit = premium collected. Risk = shares get called away if price exceeds strike
    (but you still profit from the premium + price appreciation up to strike).
    """

    def __init__(
        self,
        client: AlpacaClient,
        chain: OptionsChain,
        data: MarketDataService,
        tracker: PositionTracker,
        min_premium_pct: float = 0.003,
        max_dte: int = 30,
        min_dte: int = 7,
        target_delta: float = 0.30,
        max_upside_pct: float = 0.05,
    ):
        self._client = client
        self._chain = chain
        self._data = data
        self._tracker = tracker
        self.min_premium_pct = min_premium_pct
        self.max_dte = max_dte
        self.min_dte = min_dte
        self.target_delta = target_delta
        self.max_upside_pct = max_upside_pct

    def scan(self) -> list[CoveredCallOpportunity]:
        """Find positions with >= 100 shares and scan for call-selling opportunities."""
        snapshot = self._tracker.get_snapshot()
        opportunities: list[CoveredCallOpportunity] = []

        # Shares already backing a short call are not free to be written
        # against again. Without this, a position that reached 100 shares
        # with one call open was re-scanned every five minutes and a second
        # call submitted against the same shares: a naked call, at market.
        from src.risk.allocation import parse_occ
        short_calls: dict[str, int] = {}
        for s, p in snapshot.positions.items():
            occ = parse_occ(str(s).upper())
            if occ is None or occ.contract_type != "call":
                continue
            q = float(p.get("qty", 0) or 0)
            if q < 0:
                short_calls[occ.root] = short_calls.get(occ.root, 0) + int(abs(q))

        for sym, pos in snapshot.positions.items():
            if parse_occ(str(sym).upper()) is not None:
                continue
            qty = abs(pos["qty"])
            side = pos["side"]

            free_shares = qty - 100 * short_calls.get(str(sym).upper(), 0)
            if side != "long" or free_shares < 100:
                continue

            contracts_possible = int(free_shares // 100)
            current_price = pos["current_price"]

            calls = self._chain.get_calls(
                underlying=sym,
                min_dte=self.min_dte,
                max_dte=self.max_dte,
                strike_gte=current_price * 1.02,
                strike_lte=current_price * (1 + self.max_upside_pct),
            )

            calls = self._chain.select_best_expiry(calls, target_dte=21)

            for c in calls:
                upside_pct = (c.strike_price - current_price) / current_price
                estimated_premium = current_price * self.min_premium_pct
                premium_pct = estimated_premium / current_price

                score = self._score(premium_pct, upside_pct, c.days_to_expiry)

                opportunities.append(
                    CoveredCallOpportunity(
                        candidate=c,
                        underlying_qty=qty,
                        current_price=current_price,
                        premium_pct=premium_pct,
                        upside_to_strike_pct=upside_pct,
                        contracts_possible=contracts_possible,
                        score=score,
                    )
                )

        opportunities.sort(key=lambda o: o.score, reverse=True)
        return opportunities

    def _score(
        self,
        premium_pct: float,
        upside_pct: float,
        dte: int,
    ) -> float:
        """Score: balance premium income vs upside potential."""
        premium_score = min(premium_pct * 1000, 5.0)
        upside_score = min(upside_pct * 20, 3.0)
        dte_penalty = 0.5 if dte < 10 else 0.0
        return premium_score + upside_score - dte_penalty

    def execute_best(self, max_trades: int = 3) -> list[dict]:
        """Execute top covered call opportunities."""
        opportunities = self.scan()
        executed = []

        for opp in opportunities[:max_trades]:
            try:
                order = self._client.trading.submit_order(
                    MarketOrderRequest(
                        symbol=opp.candidate.symbol,
                        qty=opp.contracts_possible,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                    )
                )
                log.info(
                    "CC order placed: %s x%d at strike $%.2f",
                    opp.candidate.symbol,
                    opp.contracts_possible,
                    opp.candidate.strike_price,
                )
                self._tracker.record_trade(
                    symbol=opp.candidate.symbol,
                    side="sell_to_open",
                    qty=opp.contracts_possible,
                    price=opp.candidate.strike_price,
                    strategy="covered_call",
                )
                executed.append({
                    "symbol": opp.candidate.symbol,
                    "strike": opp.candidate.strike_price,
                    "expiry": str(opp.candidate.expiration),
                    "contracts": opp.contracts_possible,
                    "order_id": str(order.id),
                })
            except Exception:
                log.exception("Failed to place CC order for %s", opp.candidate.symbol)

        return executed
