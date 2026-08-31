"""Thin wrapper around alpaca-py TradingClient for paper trading."""

from __future__ import annotations

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


def load_config() -> AlpacaConfig:
    return AlpacaConfig(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=True,
    )


class AlpacaClient:
    """Unified access to Alpaca trading, data, and streaming APIs."""

    def __init__(self, config: AlpacaConfig | None = None):
        cfg = config or load_config()
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
        return self.trading.submit_order(req)

    def close_position(self, symbol: str):
        return self.trading.close_position(symbol)

    def close_all_positions(self):
        return self.trading.close_all_positions(cancel_orders=True)

    def cancel_all_orders(self):
        return self.trading.cancel_orders()

    def cancel_order(self, order_id: str):
        return self.trading.cancel_order_by_id(order_id)

    def get_clock(self):
        return self.trading.get_clock()
