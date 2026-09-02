"""Pull the IEX quote stream and one-minute bars for Vampire replay and validation.

    python scripts/fetch_vampire_data.py --data ./data/vampire --symbols QQQ,TQQQ --days 2026-09-01,2026-09-02

IEX is the feed the live engine's quotes come from, so a replay on it sees the
same triggers. It is NOT the book the paper broker fills against (the NBBO), which
is why scripts/validate_vampire_gates.py judges gates on real fills instead.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockQuotesRequest
from alpaca.data.timeframe import TimeFrame

ET = ZoneInfo("America/New_York")


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--symbols", default="QQQ,TQQQ")
    ap.add_argument("--days", required=True)
    a = ap.parse_args()
    os.makedirs(a.data, exist_ok=True)
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    for day in a.days.split(","):
        y, m, d = map(int, day.split("-"))
        start = dt.datetime(y, m, d, 9, 30, tzinfo=ET)
        end = dt.datetime(y, m, d, 16, 0, tzinfo=ET)
        for sym in a.symbols.split(","):
            quotes = client.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=sym, start=start, end=end, feed=DataFeed.IEX, limit=None))[sym]
            with open(f"{a.data}/{sym}_{day}_quotes.csv", "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["ts", "bid", "ask", "bid_size", "ask_size"])
                for q in quotes:
                    w.writerow([q.timestamp.isoformat(), q.bid_price, q.ask_price, q.bid_size, q.ask_size])
            bars = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame.Minute, start=start, end=end, feed=DataFeed.IEX))[sym]
            with open(f"{a.data}/{sym}_{day}_1min.csv", "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["ts", "open", "high", "low", "close", "volume", "vwap"])
                for b in bars:
                    w.writerow([b.timestamp.isoformat(), b.open, b.high, b.low, b.close, b.volume, b.vwap])
            print(f"{sym} {day}: {len(quotes)} quotes, {len(bars)} bars", flush=True)


if __name__ == "__main__":
    main()
