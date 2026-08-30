"""Options contract discovery, filtering, and chain utilities."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import AssetStatus, ContractType

from src.core.alpaca_client import AlpacaClient

log = logging.getLogger(__name__)


@dataclass
class OptionCandidate:
    symbol: str
    underlying: str
    contract_type: str
    strike_price: float
    expiration: date
    open_interest: int | None
    premium_estimate: float | None
    days_to_expiry: int


class OptionsChain:
    """Fetches and filters option contracts from Alpaca."""

    def __init__(self, client: AlpacaClient):
        self._trading = client.trading

    def get_puts(
        self,
        underlying: str,
        min_dte: int = 7,
        max_dte: int = 45,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        limit: int = 100,
    ) -> list[OptionCandidate]:
        return self._fetch_contracts(
            underlying=underlying,
            contract_type=ContractType.PUT,
            min_dte=min_dte,
            max_dte=max_dte,
            strike_gte=strike_gte,
            strike_lte=strike_lte,
            limit=limit,
        )

    def get_calls(
        self,
        underlying: str,
        min_dte: int = 7,
        max_dte: int = 30,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        limit: int = 100,
    ) -> list[OptionCandidate]:
        return self._fetch_contracts(
            underlying=underlying,
            contract_type=ContractType.CALL,
            min_dte=min_dte,
            max_dte=max_dte,
            strike_gte=strike_gte,
            strike_lte=strike_lte,
            limit=limit,
        )

    def _fetch_contracts(
        self,
        underlying: str,
        contract_type: ContractType,
        min_dte: int,
        max_dte: int,
        strike_gte: float | None,
        strike_lte: float | None,
        limit: int,
    ) -> list[OptionCandidate]:
        today = date.today()
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status=AssetStatus.ACTIVE,
            type=contract_type,
            expiration_date_gte=str(today + timedelta(days=min_dte)),
            expiration_date_lte=str(today + timedelta(days=max_dte)),
            strike_price_gte=str(strike_gte) if strike_gte else None,
            strike_price_lte=str(strike_lte) if strike_lte else None,
            limit=limit,
        )

        try:
            result = self._trading.get_option_contracts(req)
        except Exception:
            log.exception("Failed to fetch option contracts for %s", underlying)
            return []

        candidates = []
        contracts = result.option_contracts if hasattr(result, "option_contracts") else []

        for c in contracts:
            exp = c.expiration_date if isinstance(c.expiration_date, date) else date.fromisoformat(str(c.expiration_date))
            dte = (exp - today).days

            candidates.append(
                OptionCandidate(
                    symbol=c.symbol,
                    underlying=underlying,
                    contract_type=contract_type.value,
                    strike_price=float(c.strike_price),
                    expiration=exp,
                    open_interest=int(c.open_interest) if c.open_interest else None,
                    premium_estimate=None,
                    days_to_expiry=dte,
                )
            )

        return candidates

    def filter_by_otm_pct(
        self,
        candidates: list[OptionCandidate],
        current_price: float,
        max_otm_pct: float = 0.10,
    ) -> list[OptionCandidate]:
        """Keep only contracts that are OTM within a percentage of current price."""
        filtered = []
        for c in candidates:
            if c.contract_type == "put":
                otm_pct = (current_price - c.strike_price) / current_price
            else:
                otm_pct = (c.strike_price - current_price) / current_price

            if 0 < otm_pct <= max_otm_pct:
                filtered.append(c)

        return filtered

    def filter_by_open_interest(
        self,
        candidates: list[OptionCandidate],
        min_oi: int = 100,
    ) -> list[OptionCandidate]:
        return [c for c in candidates if c.open_interest and c.open_interest >= min_oi]

    def select_best_expiry(
        self,
        candidates: list[OptionCandidate],
        target_dte: int = 30,
    ) -> list[OptionCandidate]:
        """Group by expiry and pick the one closest to target DTE."""
        if not candidates:
            return []

        by_expiry: dict[date, list[OptionCandidate]] = {}
        for c in candidates:
            by_expiry.setdefault(c.expiration, []).append(c)

        best_exp = min(by_expiry.keys(), key=lambda d: abs((d - date.today()).days - target_dte))
        return by_expiry[best_exp]
