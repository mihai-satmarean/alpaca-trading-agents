"""Cash-Secured Put strategy: scan, score, and place put-selling orders."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

from src.core.alpaca_client import AlpacaClient
from src.core.config import get_config
from src.core.market_data import MarketDataService
from src.core.options_chain import OptionCandidate, OptionsChain
from src.core.position_tracker import PositionTracker
from src.strategies.csp_scoring import QuotedPut, ScoringConfig, rank

log = logging.getLogger(__name__)

# Fallback only. The live universe comes from config/strategies.yml so the
# symbol list can be sized to the sleeve without a code change; a hardcoded
# list here silently outranked the config and had the scanner spending every
# cycle on names whose collateral exceeds the whole allocation.
CSP_SYMBOLS = ["CLF", "NIO", "F", "SOFI", "T"]


def default_symbols() -> list[str]:
    try:
        return get_config().options_symbols or CSP_SYMBOLS
    except Exception:
        log.warning("Falling back to built-in CSP symbols", exc_info=True)
        return CSP_SYMBOLS


@dataclass
class CSPOpportunity:
    """bid is attached post-construction by scan() so the order can be priced
    at it rather than sent to market."""
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
        self.max_delta = max_delta
        self._quote_provider = quote_provider
        self._allocator = allocator
        self._breaker = breaker

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
        symbols = symbols or default_symbols()

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
                opp = CSPOpportunity(
                    candidate=match,
                    current_price=quote.mid,
                    cash_required=ev.collateral,
                    premium_pct=ev.return_on_capital,
                    annualized_return=ev.annualized,
                    score=ev.score,
                )
                opp.bid = ev.put.bid
                opportunities.append(opp)

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
                # Sell at the bid rather than at market. Hitting the bid is
                # marketable so it fills like a market order, but it puts a
                # floor under the fill: option spreads on these names run wide,
                # and a market sell can give up much of the premium the whole
                # strategy exists to collect.
                limit = getattr(opp, "bid", None)
                if not limit or limit <= 0:
                    log.warning("Skipping %s: no bid to price the limit against", sym)
                    continue
                order = self._client.trading.submit_order(
                    LimitOrderRequest(
                        symbol=sym,
                        qty=1,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                        limit_price=round(float(limit), 2),
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
