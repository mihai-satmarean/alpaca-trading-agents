"""Offline backtesting using historical Alpaca data.

Replays historical bars through the vampire tick logic to estimate
performance without placing real orders.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

from src.core.alpaca_client import load_config

load_dotenv()
log = logging.getLogger(__name__)


def backtest_vampire(
    symbol: str = "SPY",
    days: int = 5,
    tick_threshold: float = 0.02,
    position_size: int = 10,
    max_position: int = 100,
):
    cfg = load_config()
    data_client = StockHistoricalDataClient(api_key=cfg.api_key, secret_key=cfg.secret_key)

    end = datetime.now()
    start = end - timedelta(days=days)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
    )
    bars = data_client.get_stock_bars(req)
    bar_list = bars[symbol] if symbol in bars else []

    net_position = 0
    pnl = 0.0
    trades = 0
    last_price = None
    wins = 0
    losses = 0

    for bar in bar_list:
        price = float(bar.close)
        if last_price is None:
            last_price = price
            continue

        delta = price - last_price

        if delta >= tick_threshold:
            if net_position > 0:
                qty = min(net_position, position_size)
                trade_pnl = qty * delta
                pnl += trade_pnl
                net_position -= qty
                trades += 1
                if trade_pnl > 0:
                    wins += 1
                else:
                    losses += 1
            elif abs(net_position) < max_position:
                net_position -= position_size
                trades += 1

        elif delta <= -tick_threshold:
            if net_position < 0:
                qty = min(abs(net_position), position_size)
                trade_pnl = qty * abs(delta)
                pnl += trade_pnl
                net_position += qty
                trades += 1
                if trade_pnl > 0:
                    wins += 1
                else:
                    losses += 1
            elif net_position < max_position:
                net_position += position_size
                trades += 1

        last_price = price

    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0

    print(f"\n=== Vampire Backtest: {symbol} ({days} days) ===")
    print(f"Tick threshold: ${tick_threshold}")
    print(f"Total trades: {trades}")
    print(f"Wins: {wins} | Losses: {losses} | Win rate: {win_rate:.1f}%")
    print(f"Gross P&L: ${pnl:.2f}")
    print(f"Final net position: {net_position}")
    print(f"Bars processed: {len(bar_list)}")


def main():
    parser = argparse.ArgumentParser(description="Backtest vampire algorithm")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.02)
    parser.add_argument("--size", type=int, default=10)
    parser.add_argument("--max-pos", type=int, default=100)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    backtest_vampire(args.symbol, args.days, args.threshold, args.size, args.max_pos)


if __name__ == "__main__":
    main()
