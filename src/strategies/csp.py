"""Cash-Secured Put strategy: scan, score, and place put-selling orders."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from src.core.alpaca_client import AlpacaClient
from src.core.market_data import MarketDataService
from src.core.options_chain import OptionCandidate, OptionsChain
from src.core.position_tracker import PositionTracker

log = logging.getLogger(__name__)

CSP_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "AMZN", "NVDA", "META", "GOOGL"]


@dataclass
class CSPOpportunity:
    candidate: OptionCandidate
    current_price: float
    cash_required: float
    premium_pct: float
    annualized_return: float
    score: float


class CashSecuredPutStrategy:
    """Scans for high-premium OTM puts, scores them, and places sell-to-open orders.

    Cash-secured put: sell a put option and hold enough cash to buy the shares
    if assigned. Profit = premium collected if the put expires worthless (price
    stays above strike).
    """

    def __init__(
        self,
        client: AlpacaClient,
        chain: OptionsChain,
        data: MarketDataService,
        tracker: PositionTracker,
        min_premium_pct: float = 0.005,
        max_dte: int = 45,
        min_dte: int = 7,
        max_otm_pct: float = 0.08,
        min_open_interest: int = 100,
        max_allocation_per_trade: float = 0.10,
        allocator=None,
        breaker=None,
    ):
        self._client = client
        self._chain = chain
        self._data = data
        self._tracker = tracker
        self.min_premium_pct = min_premium_pct
        self.max_dte = max_dte
        self.min_dte = min_dte
        self.max_otm_pct = max_otm_pct
        self.min_open_interest = min_open_interest
        self.max_allocation_per_trade = max_allocation_per_trade
        self._allocator = allocator
        self._breaker = breaker

    def scan(self, symbols: list[str] | None = None) -> list[CSPOpportunity]:
        """Scan multiple symbols for CSP opportunities and return scored list."""
        symbols = symbols or CSP_SYMBOLS
        opportunities: list[CSPOpportunity] = []

        for sym in symbols:
            quote = self._data.get_latest_quote(sym)
            if not quote:
                log.warning("No quote for %s, skipping", sym)
                continue

            puts = self._chain.get_puts(
                underlying=sym,
                min_dte=self.min_dte,
                max_dte=self.max_dte,
            )

            puts = self._chain.filter_by_otm_pct(puts, quote.mid, self.max_otm_pct)
            puts = self._chain.filter_by_open_interest(puts, self.min_open_interest)
            puts = self._chain.select_best_expiry(puts, target_dte=30)

            for p in puts:
                cash_required = p.strike_price * 100
                estimated_premium = quote.mid * self.min_premium_pct
                premium_pct = estimated_premium / p.strike_price if p.strike_price else 0
                annualized = (premium_pct / p.days_to_expiry * 365) if p.days_to_expiry > 0 else 0

                score = self._score(premium_pct, annualized, p.days_to_expiry, p.open_interest or 0)

                opportunities.append(
                    CSPOpportunity(
                        candidate=p,
                        current_price=quote.mid,
                        cash_required=cash_required,
                        premium_pct=premium_pct,
                        annualized_return=annualized,
                        score=score,
                    )
                )

        opportunities.sort(key=lambda o: o.score, reverse=True)
        return opportunities

    def _score(
        self,
        premium_pct: float,
        annualized: float,
        dte: int,
        open_interest: int,
    ) -> float:
        """Composite score: higher premium + reasonable DTE + liquidity."""
        premium_score = min(annualized * 10, 5.0)
        dte_score = 1.0 if 20 <= dte <= 35 else 0.5
        liquidity_score = min(open_interest / 500, 2.0)
        return premium_score + dte_score + liquidity_score

    def execute_best(self, max_trades: int = 3, budget: float | None = None) -> list[dict]:
        """Execute top CSP opportunities within the options sleeve budget.

        Every order is gated twice before it is sent: once against the sleeve
        budget (the configured 80% split) and once against the per-trade and
        per-position limits in the circuit breaker. Both gates already existed
        and neither was called from this path, so a cash-secured put worth 45%
        of the account could pass a 5% per-trade limit unnoticed.
        """
        snapshot = self._tracker.get_snapshot()

        if budget is not None:
            available = budget
        elif self._allocator is not None:
            available = self._allocator.get_budget().options_available
        else:
            available = snapshot.cash * self.max_allocation_per_trade

        opportunities = self.scan()
        executed: list[dict] = []
        committed = 0.0

        for opp in opportunities:
            if len(executed) >= max_trades:
                break

            need = opp.cash_required
            sym = opp.candidate.symbol

            if committed + need > available:
                log.info(
                    "Skipping %s: needs $%.0f, $%.0f of $%.0f sleeve budget left",
                    sym, need, available - committed, available,
                )
                continue

            # Sleeve gate: does the options budget still have room for this?
            if self._allocator is not None and not self._allocator.can_allocate_options(need):
                log.info("Skipping %s: options sleeve budget exhausted", sym)
                continue

            # Per-trade and per-position gate.
            if self._breaker is not None and not self._breaker.can_trade(sym, need):
                log.info("Skipping %s: blocked by risk limits ($%.0f notional)", sym, need)
                continue

            try:
                order = self._client.trading.submit_order(
                    MarketOrderRequest(
                        symbol=sym,
                        qty=1,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                    )
                )
                log.info(
                    "CSP order placed: %s strike $%.2f, collateral $%.0f",
                    sym, opp.candidate.strike_price, need,
                )
                self._tracker.record_trade(
                    symbol=sym,
                    side="sell_to_open",
                    qty=1,
                    price=opp.candidate.strike_price,
                    strategy="csp",
                )
                committed += need
                executed.append({
                    "symbol": sym,
                    "strike": opp.candidate.strike_price,
                    "expiry": str(opp.candidate.expiration),
                    "collateral": need,
                    "order_id": str(order.id),
                })
            except Exception:
                log.exception("Failed to place CSP order for %s", sym)

        return executed
