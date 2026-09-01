"""Thin wrapper around alpaca-py TradingClient for paper trading.

Supports two environments via ALPACA_ENV (default "production"):
  - production: uses ALPACA_API_KEY / ALPACA_SECRET_KEY
  - staging: uses ALPACA_STAGING_API_KEY / ALPACA_STAGING_SECRET_KEY

Pass --staging on the CLI or set ALPACA_ENV=staging to switch.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


class OrderAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    SELL_SHORT = "sell_short"
    BUY_TO_COVER = "buy_to_cover"


@dataclass(frozen=True)
class AlpacaConfig:
    api_key: str
    secret_key: str
    paper: bool = True
    base_url: str | None = None
    environment: str = "production"


def load_config(staging: bool = False) -> AlpacaConfig:
    """Load Alpaca credentials for the requested environment.

    Priority: explicit `staging` arg > ALPACA_ENV env var > default production.
    """
    env = "staging" if staging else os.environ.get("ALPACA_ENV", "production")

    if env == "staging":
        key = os.environ.get("ALPACA_STAGING_API_KEY", "")
        secret = os.environ.get("ALPACA_STAGING_SECRET_KEY", "")
        if not key or not secret:
            raise ValueError(
                "Staging requested but ALPACA_STAGING_API_KEY / "
                "ALPACA_STAGING_SECRET_KEY are not set"
            )
        log.info("Using STAGING Alpaca account (key=%s...)", key[:6])
    else:
        key = os.environ["ALPACA_API_KEY"]
        secret = os.environ["ALPACA_SECRET_KEY"]
        log.info("Using PRODUCTION Alpaca account (key=%s...)", key[:6])

    return AlpacaConfig(
        api_key=key,
        secret_key=secret,
        paper=True,
        environment=env,
    )


class AlpacaClient:
    """Unified access to Alpaca trading, data, and streaming APIs.

    Set dry_run=True to log orders without submitting them to the broker.
    Read-only operations (account, positions, quotes) work normally in dry-run.
    """

    def __init__(self, config: AlpacaConfig | None = None, dry_run: bool = False):
        cfg = config or load_config()
        self._dry_run = dry_run
        self._environment = cfg.environment
        self.trading = TradingClient(
            api_key=cfg.api_key,
            secret_key=cfg.secret_key,
            paper=cfg.paper,
        )
        self.data = StockHistoricalDataClient(
            api_key=cfg.api_key,
            secret_key=cfg.secret_key,
        )
        self.stream = StockDataStream(
            api_key=cfg.api_key,
            secret_key=cfg.secret_key,
        )
        if dry_run:
            log.info("DRY-RUN mode: orders will be logged but NOT submitted")

    @property
    def is_dry_run(self) -> bool:
        return self._dry_run

    @property
    def environment(self) -> str:
        return self._environment

    def get_account(self):
        return self.trading.get_account()

    def get_positions(self):
        return self.trading.get_all_positions()

    def get_position(self, symbol: str):
        try:
            return self.trading.get_open_position(symbol)
        except Exception:
            return None

    def get_orders(self, status: str = "open"):
        req = GetOrdersRequest(status=QueryOrderStatus(status))
        return self.trading.get_orders(req)

    def get_order(self, order_id: str):
        """Read one order back by id.

        The scalper polls this to learn whether a market order actually filled,
        because Alpaca returns filled_qty "0" and status "new" at submit and
        lands the fill 85-100ms later. This method did not exist until
        2026-08-31: every poll raised AttributeError, was caught, and fell
        through to "assume it filled" - 101 times in one session, zero
        successful polls. The engine therefore counted every submission as a
        full fill, over-stated its position, and asked the venue to buy back
        more than it held.

        The tests did not catch it because they inject MagicMock, which invents
        any attribute asked of it. See test_client_contract.py.
        """
        return self.trading.get_order_by_id(order_id)

    def market_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ):
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            type=OrderType.MARKET,
            time_in_force=time_in_force,
        )
        if self._dry_run:
            log.info("[DRY-RUN] market_order %s %s qty=%s tif=%s", side, symbol, qty, time_in_force)
            return _dry_run_order(symbol, qty, side, "market")
        return self.trading.submit_order(req)

    def limit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        limit_price: float,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ):
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            type=OrderType.LIMIT,
            time_in_force=time_in_force,
            limit_price=limit_price,
        )
        if self._dry_run:
            log.info("[DRY-RUN] limit_order %s %s qty=%s @$%.2f tif=%s",
                     side, symbol, qty, limit_price, time_in_force)
            return _dry_run_order(symbol, qty, side, "limit", limit_price)
        return self.trading.submit_order(req)

    def close_position(self, symbol: str):
        if self._dry_run:
            log.info("[DRY-RUN] close_position %s", symbol)
            return None
        return self.trading.close_position(symbol)

    def close_all_positions(self):
        if self._dry_run:
            log.info("[DRY-RUN] close_all_positions")
            return []
        return self.trading.close_all_positions(cancel_orders=True)

    def cancel_all_orders(self):
        if self._dry_run:
            log.info("[DRY-RUN] cancel_all_orders")
            return []
        return self.trading.cancel_orders()

    def cancel_order(self, order_id: str):
        if self._dry_run:
            log.info("[DRY-RUN] cancel_order %s", order_id)
            return None
        return self.trading.cancel_order_by_id(order_id)

    def get_clock(self):
        return self.trading.get_clock()


def _dry_run_order(symbol: str, qty: float, side: OrderSide,
                   order_type: str, limit_price: float | None = None) -> dict:
    """Return a fake order dict for dry-run mode."""
    import uuid
    return {
        "id": str(uuid.uuid4()),
        "symbol": symbol,
        "qty": str(qty),
        "side": side.value if hasattr(side, "value") else str(side),
        "type": order_type,
        "limit_price": str(limit_price) if limit_price else None,
        "status": "dry_run",
        "filled_qty": "0",
    }
