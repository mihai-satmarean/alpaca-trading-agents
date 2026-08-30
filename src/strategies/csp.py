"""Cash-Secured Put strategy: scan, score, and place put-selling orders."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from src.core.alpaca_client import AlpacaClient
from src.core.options_chain import OptionsChain, OptionCandidate
from src.core.market_data import MarketDataService
from src.core.position_tracker import PositionTracker
from src.strategies.csp_scoring import QuotedPut, ScoringConfig, rank

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
        max_delta: float = -0.30,
        quote_provider: Callable[[list[str]], dict[str, dict]] | None = None,
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
        self.max_delta = max_delta
        self._quote_provider = quote_provider

    def scan(self, symbols: list[str] | None = None) -> list[CSPOpportunity]:
        """Scan for sellable puts, priced on what each contract actually bids.

        The previous implementation derived premium as
        `underlying_price * min_premium_pct`, which never read the contract and,
        because it divided by strike, ranked cheaper far-OTM puts highest. It
        also meant the strategy sold puts without knowing what they paid.

        Without a quote provider this returns nothing. Refusing to trade is the
        only safe response to not knowing the price; the old code's answer was
        to invent one.
        """
        symbols = symbols or CSP_SYMBOLS

        if self._quote_provider is None:
            log.error("No option quote provider configured; refusing to scan CSPs")
            return []

        cfg = ScoringConfig(
            min_premium_pct=self.min_premium_pct,
            min_open_interest=self.min_open_interest,
            max_delta=getattr(self, "max_delta", -0.30),
            min_dte=self.min_dte,
            max_dte=self.max_dte,
        )

        opportunities: list[CSPOpportunity] = []

        for sym in symbols:
            quote = self._data.get_latest_quote(sym)
            if not quote:
                log.warning("No underlying quote for %s, skipping", sym)
                continue

            puts = self._chain.get_puts(sym, min_dte=self.min_dte, max_dte=self.max_dte)
            puts = self._chain.filter_by_otm_pct(puts, quote.mid, self.max_otm_pct)
            if not puts:
                continue

            try:
                quotes = self._quote_provider([p.symbol for p in puts])
            except Exception:
                log.exception("Option quote fetch failed for %s; skipping", sym)
                continue

            quoted: list[QuotedPut] = []
            for p in puts:
                q = quotes.get(p.symbol)
                if not q:
                    continue
                quoted.append(QuotedPut(
                    symbol=p.symbol,
                    strike=float(p.strike_price),
                    days_to_expiry=p.days_to_expiry,
                    bid=float(q.get("bid") or 0.0),
                    ask=float(q.get("ask") or 0.0),
                    open_interest=int(p.open_interest or 0),
                    delta=q.get("delta"),
                ))

            if not quoted:
                log.info("No priced contracts for %s", sym)
                continue

            for ev in rank(quoted, cfg):
                match = next((p for p in puts if p.symbol == ev.put.symbol), None)
                if match is None:
                    continue
                opportunities.append(CSPOpportunity(
                    candidate=match,
                    current_price=quote.mid,
                    cash_required=ev.collateral,
                    premium_pct=ev.return_on_capital,
                    annualized_return=ev.annualized,
                    score=ev.score,
                ))

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
        """Execute top CSP opportunities within budget."""
        snapshot = self._tracker.get_snapshot()
        available = budget or (snapshot.cash * self.max_allocation_per_trade)

        opportunities = self.scan()
        executed = []

        for opp in opportunities[:max_trades]:
            if opp.cash_required > available:
                log.info("Skipping %s: needs $%.0f, have $%.0f", opp.candidate.symbol, opp.cash_required, available)
                continue

            try:
                order = self._client.trading.submit_order(
                    MarketOrderRequest(
                        symbol=opp.candidate.symbol,
                        qty=1,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                    )
                )
                log.info("CSP order placed: %s at strike $%.2f", opp.candidate.symbol, opp.candidate.strike_price)
                self._tracker.record_trade(
                    symbol=opp.candidate.symbol,
                    side="sell_to_open",
                    qty=1,
                    price=opp.candidate.strike_price,
                    strategy="csp",
                )
                available -= opp.cash_required
                executed.append({
                    "symbol": opp.candidate.symbol,
                    "strike": opp.candidate.strike_price,
                    "expiry": str(opp.candidate.expiration),
                    "order_id": str(order.id),
                })
            except Exception:
                log.exception("Failed to place CSP order for %s", opp.candidate.symbol)

        return executed
