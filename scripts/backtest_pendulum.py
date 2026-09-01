"""Backtest Pendulum on real daily bars.

Calls the SAME decide() the live engine calls. Signals are computed on a
close and filled at the NEXT open, in both places, because a backtest whose
timing differs from the live path is measuring a different strategy.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from alpaca.data.enums import Adjustment  # noqa: E402
from alpaca.data.historical import StockHistoricalDataClient  # noqa: E402
from alpaca.data.requests import StockBarsRequest  # noqa: E402
from alpaca.data.timeframe import TimeFrame  # noqa: E402

from src.strategies.pendulum import (  # noqa: E402
    PendulumParams, Position, Signal, compute_indicators, decide, stop_price,
)


def load_bars(symbol: str, start: dt.datetime, feed: str = "sip"):
    c = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    r = c.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start,
        adjustment=Adjustment.ALL, feed=feed))
    return list(r.data.get(symbol, []))


@dataclass
class Trade:
    entry_date: dt.date
    exit_date: dt.date
    entry: float
    exit: float
    shares: int
    reason: str
    bars: int

    @property
    def pnl(self) -> float:
        return (self.exit - self.entry) * self.shares

    @property
    def ret(self) -> float:
        return (self.exit / self.entry) - 1.0


def run(bars, p: PendulumParams, capital: float = 100_000.0,
        slip_bps: float = 2.0, first_tranche: float = 0.6, verbose: bool = False):
    """Event-driven, next-open fills.

    slip_bps is charged on every fill, each way. TLT's typical half-spread is
    about 1bp, so 2bp per side is the doubled estimate the risk discipline
    asks for rather than the optimistic one.
    """
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    closes = [float(b.close) for b in bars]
    opens = [float(b.open) for b in bars]
    dates = [b.timestamp.date() for b in bars]

    cash, pos, trades = capital, None, []
    # (signal, why, below_regime_at_signal). The regime flag rides along
    # because aggressive mode halves size below the 200-day, and a backtest
    # that sizes differently from the live agent is measuring a different
    # strategy -- the exact drift this file exists to prevent.
    pending: tuple[Signal, str, bool] | None = None
    equity_curve, signal_log = [], []
    entry_date = None

    for i in range(len(bars)):
        # ---- fill yesterday's decision at today's open, before deciding again
        if pending is not None:
            sig, why, below = pending
            px_raw = opens[i]
            if sig in (Signal.BUY, Signal.ADD):
                px = px_raw * (1 + slip_bps / 10_000)
                budget = (capital * first_tranche) if sig is Signal.BUY else (capital * (1 - first_tranche))
                if below:
                    budget *= p.below_regime_size_mult
                budget = min(budget, cash)
                qty = int(budget // px)
                if qty > 0:
                    cash -= qty * px
                    if pos is None:
                        pos = Position(entry_price=px, shares=qty, bars_held=0, tranches=1)
                        entry_date = dates[i]
                    else:
                        tot = pos.shares + qty
                        pos.entry_price = (pos.entry_price * pos.shares + px * qty) / tot
                        pos.shares = tot
                        pos.tranches += 1
            elif sig is Signal.EXIT and pos is not None:
                px = px_raw * (1 - slip_bps / 10_000)
                cash += pos.shares * px
                trades.append(Trade(entry_date, dates[i], pos.entry_price, px,
                                    pos.shares, why, pos.bars_held))
                pos, entry_date = None, None
            pending = None

        equity_curve.append((dates[i], cash + (pos.shares * closes[i] if pos else 0.0)))

        # ---- decide on today's close, for tomorrow's open
        ind = compute_indicators(highs[:i + 1], lows[:i + 1], closes[:i + 1], p)
        sig, why = decide(ind, pos, p)
        if pos is not None:
            pos.bars_held += 1
        if sig in (Signal.BUY, Signal.ADD, Signal.EXIT):
            below_now = bool(ind.sma_regime is not None and ind.close < ind.sma_regime)
            pending = (sig, why, below_now)
            signal_log.append((dates[i], sig.value, why))

    if pos is not None:   # mark the open position out at the last close
        trades.append(Trade(entry_date, dates[-1], pos.entry_price, closes[-1],
                            pos.shares, "open at end of test", pos.bars_held))
        cash += pos.shares * closes[-1]

    return {"trades": trades, "equity": equity_curve, "final": cash,
            "capital": capital, "signals": signal_log,
            "bh": (closes[-1] / opens[0] - 1.0), "dates": dates, "closes": closes}


def max_dd(curve) -> float:
    peak, worst = -1e18, 0.0
    for _, v in curve:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1.0)
    return worst


def report(name: str, r, p: PendulumParams):
    t = r["trades"]
    wins = [x for x in t if x.pnl > 0]
    losses = [x for x in t if x.pnl <= 0]
    tot = r["final"] / r["capital"] - 1.0
    yrs = (r["dates"][-1] - r["dates"][0]).days / 365.25
    cagr = (r["final"] / r["capital"]) ** (1 / yrs) - 1 if yrs > 0 else 0.0
    aw = sum(x.pnl for x in wins) / len(wins) if wins else 0.0
    al = abs(sum(x.pnl for x in losses) / len(losses)) if losses else 0.0
    wr = len(wins) / len(t) if t else 0.0
    exp = wr * aw - (1 - wr) * al
    pf = (sum(x.pnl for x in wins) / abs(sum(x.pnl for x in losses))) if losses and sum(x.pnl for x in losses) else float("inf")

    print(f"\n{'='*74}\n{name}\n{'='*74}")
    print(f"  window        {r['dates'][0]} to {r['dates'][-1]}  ({yrs:.1f} years)")
    print(f"  trades        {len(t)}   ({len(t)/yrs:.1f}/yr)")
    print(f"  total return  {tot*100:+.2f}%      CAGR {cagr*100:+.2f}%")
    print(f"  buy & hold    {r['bh']*100:+.2f}%      (same window, same instrument)")
    print(f"  max drawdown  {max_dd(r['equity'])*100:.2f}%")
    print(f"  win rate      {wr*100:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"  avg win       ${aw:,.2f}      avg loss  ${al:,.2f}")
    print(f"  expectancy    ${exp:,.2f}/trade      profit factor {pf:.2f}")
    if t:
        print(f"  best / worst  {max(x.ret for x in t)*100:+.2f}% / {min(x.ret for x in t)*100:+.2f}%")
        print(f"  avg hold      {sum(x.bars for x in t)/len(t):.1f} trading days")
    by_year: dict[int, list] = {}
    for x in t:
        by_year.setdefault(x.exit_date.year, []).append(x)
    if by_year:
        print("\n  by year (exit date):")
        for y in sorted(by_year):
            xs = by_year[y]
            w = len([x for x in xs if x.pnl > 0])
            print(f"    {y}   {len(xs):>2} trades  {w}W/{len(xs)-w}L   P&L ${sum(x.pnl for x in xs):>+10,.2f}")
    return {"trades": len(t), "ret": tot, "cagr": cagr, "dd": max_dd(r["equity"]),
            "wr": wr, "exp": exp, "pf": pf}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="TLT,SPTL")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--slip-bps", type=float, default=2.0)
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()
    start = dt.datetime.fromisoformat(a.start)

    for sym in a.symbols.split(","):
        bars = load_bars(sym, start)
        if not bars:
            print(f"  {sym}: no data"); continue
        for label, p in (("conservative (no entries below the 200-day)", PendulumParams()),
                         ("aggressive (entries below the 200-day, half size)",
                          PendulumParams(allow_below_regime=True))):
            r = run(bars, p, a.capital, a.slip_bps)
            report(f"{sym} -- {label}", r, p)
            if a.detail:
                print("\n  trades:")
                for x in r["trades"]:
                    print(f"    {x.entry_date} -> {x.exit_date}  {x.entry:7.2f} -> {x.exit:7.2f}  "
                          f"{x.ret*100:+6.2f}%  ${x.pnl:>+9,.2f}  {x.reason}")
