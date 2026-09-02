#!/usr/bin/env python3
"""Diagnostic: run the Vampire's pre-market symbol scan and show results.

Does NOT trade. Only reads market data (quotes + daily bars) to score
symbols and display what the Vampire would pick for today's session.
"""
from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

from src.core.alpaca_client import AlpacaClient, load_config
from src.core.market_data import MarketDataService
from src.strategies.vampire_symbol_picker import (
    VampireSymbolPicker, PickerConfig, UNIVERSE,
)

ET = ZoneInfo("America/New_York")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vampire_scan")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vampire pre-market scan (read-only)")
    parser.add_argument(
        "--staging",
        action="store_true",
        help="Use ALPACA_STAGING_* keys. Never touches the contest account.",
    )
    return parser.parse_args(argv)


def main():
    args = parse_args()
    if args.staging:
        os.environ["ALPACA_ENV"] = "staging"

    now_et = datetime.now(ET)
    log.info("Vampire Pre-Market Scan -- %s ET", now_et.strftime("%Y-%m-%d %H:%M"))
    log.info("Universe: %d symbols", len(UNIVERSE))

    cfg = load_config(staging=args.staging)
    log.info("Scan env=%s key=%s...", cfg.environment, cfg.api_key[:6])
    client = AlpacaClient(config=cfg, dry_run=True)
    data = MarketDataService(client)

    account = client.get_account()
    equity = float(account.equity)
    log.info("Account equity: $%,.2f", equity)

    vampire_pct = 0.10
    sleeve_budget = equity * vampire_pct
    log.info("Vampire sleeve budget (%.0f%%): $%,.2f", vampire_pct * 100, sleeve_budget)

    picker = VampireSymbolPicker(
        client=client,
        data=data,
        sleeve_budget=sleeve_budget,
        config=PickerConfig(
            target_count=6,
            profit_target_per_symbol=50.0,
            loss_limit_per_symbol=25.0,
        ),
    )

    result = picker.pick()

    print("\n" + "=" * 70)
    print("VAMPIRE SYMBOL SELECTION RESULTS")
    print("=" * 70)

    print(f"\nSelected {len(result.symbols)} symbols: {result.symbols}")

    print("\n--- Selected Symbol Details ---")
    print(f"{'Symbol':<8} {'Price':>8} {'Spread':>8} {'Sprd%':>7} {'ATR%':>7} {'Score':>7}")
    print("-" * 55)
    for sym in result.symbols:
        m = result.metrics.get(sym)
        if m:
            print(f"{m.symbol:<8} ${m.price:>7.2f} ${m.spread:>6.3f} "
                  f"{m.spread_pct:>6.3%} {m.atr_pct:>6.3%} {m.score:>7.3f}")

    print("\n--- Bleed Budgets ---")
    print(f"{'Symbol':<8} {'Target':>10} {'Loss Limit':>12} {'Status':<12}")
    print("-" * 50)
    for sym in result.symbols:
        b = result.bleed_budgets.get(sym)
        if b:
            print(f"{b.symbol:<8} ${b.profit_target:>8.2f} ${b.loss_limit:>10.2f} "
                  f"{'ACTIVE' if b.is_active else 'RETIRED':<12}")

    all_scored = sorted(result.metrics.values(), key=lambda m: m.score)
    print("\n--- Full Ranking (all scored symbols) ---")
    print(f"{'#':<4} {'Symbol':<8} {'Price':>8} {'Sprd%':>7} {'ATR%':>7} {'Score':>7} {'Pick?':<5}")
    print("-" * 55)
    for i, m in enumerate(all_scored[:20], 1):
        picked = "<<<" if m.symbol in result.symbols else ""
        print(f"{i:<4} {m.symbol:<8} ${m.price:>7.2f} {m.spread_pct:>6.3%} "
              f"{m.atr_pct:>6.3%} {m.score:>7.3f} {picked}")

    filtered_out = [s for s in UNIVERSE if s not in result.metrics]
    if filtered_out:
        print(f"\n--- Filtered Out ({len(filtered_out)} symbols) ---")
        print(", ".join(sorted(filtered_out)))

    print("\n--- Strategy Summary ---")
    print(f"Total sleeve budget: ${sleeve_budget:,.2f}")
    print(f"Per-symbol capital: ${sleeve_budget / max(len(result.symbols), 1):,.2f}")
    total_target = sum(b.profit_target for b in result.bleed_budgets.values())
    total_limit = sum(b.loss_limit for b in result.bleed_budgets.values())
    print(f"Combined profit target: ${total_target:,.2f}")
    print(f"Combined loss limit: ${total_limit:,.2f}")
    print(f"Target R:R ratio: {total_target / max(total_limit, 0.01):.1f}:1")

    print("\nThe Vampire will:")
    print("  1. Subscribe to real-time quotes for selected symbols")
    print("  2. Track VWAP and tick movements for entry signals")
    print("  3. Execute bi-directional micro-scalps (buy+short)")
    print("  4. Retire symbols that hit profit target or loss limit")
    print("  5. Replace retired symbols with fresh victims mid-session")


if __name__ == "__main__":
    main()
