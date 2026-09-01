"""Pendulum -- mean-reversion on long-duration Treasuries.

Tashi's spec, v1.1. Buys the instrument when it is stretched far below its
20-day mean and sells when it reverts. It does not predict rates; it bets that
a sharp move over a few days more often partly reverses than continues.

That bet wins often and loses badly a few times, so everything structural here
exists to survive the few times: a 200-day regime filter, a hard ATR stop, and
a time stop on a thesis that has stalled.

The signal functions are pure and take a price series. The backtest and the
live engine both call THESE functions rather than each reimplementing the
rules, because a backtest that computes its signal differently from the live
path is measuring a strategy nobody is running. The spec makes the same point
about timing: signals are computed on the close and acted on at the next
open, in both places.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)


class Signal(str, Enum):
    BUY = "BUY"
    ADD = "ADD"
    HOLD = "HOLD"
    EXIT = "EXIT"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class PendulumParams:
    # A stop must sit far enough below the entry that ordinary noise cannot
    # reach it. Without this floor a near-zero ATR collapses the stop onto the
    # entry price -- max(entry - 1.5*0, pct_floor) is the entry itself -- and
    # the position is stopped out by the first down close, every time, forever.
    min_stop_pct: float = 0.005
    sma_lookback: int = 20
    entry_z: float = -2.0
    entry_rsi: float = 10.0
    add_z: float = -2.75
    exit_rsi: float = 70.0
    time_stop_days: int = 10
    atr_mult: float = 1.5
    hard_stop_pct: float = 0.05
    regime_lookback: int = 200
    rsi_period: int = 2
    atr_period: int = 14
    allow_below_regime: bool = False   # the spec's "aggressive" mode
    below_regime_size_mult: float = 0.5


@dataclass
class Indicators:
    close: float
    sma: float | None
    std: float | None
    z: float | None
    rsi: float | None
    sma_regime: float | None
    atr: float | None

    @property
    def ready(self) -> bool:
        return None not in (self.sma, self.std, self.z, self.rsi,
                            self.sma_regime, self.atr)


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def stdev(values: list[float], n: int) -> float | None:
    """Population standard deviation over the last n closes.

    ddof=0 to match the Bollinger convention the z-score inherits. With ddof=1
    every z would be scaled by sqrt(19/20) = 0.975, which shifts the -2.0
    entry threshold by about 2.5% of its own value. Small, but it decides
    whether a marginal day is a trade, so it is pinned rather than left to a
    library default.
    """
    if len(values) < n:
        return None
    w = values[-n:]
    m = sum(w) / n
    return (sum((x - m) ** 2 for x in w) / n) ** 0.5


def wilder_rsi(closes: list[float], period: int = 2) -> float | None:
    """RSI with Wilder smoothing, the convention Connors' RSI(2) assumes.

    A simple-average RSI gives materially different values at period 2 and
    would move the <10 entry gate, so the smoothing is stated rather than
    inherited.
    """
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def wilder_atr(highs: list[float], lows: list[float], closes: list[float],
               period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_indicators(highs: list[float], lows: list[float],
                       closes: list[float], p: PendulumParams) -> Indicators:
    """Everything the signal needs, from bars up to and including today's close.

    Every value is derived from closed bars only. Nothing here may read a bar
    later than the one being evaluated: that is the look-ahead the spec warns
    produces a suspiciously high win rate.
    """
    s = sma(closes, p.sma_lookback)
    sd = stdev(closes, p.sma_lookback)
    z = ((closes[-1] - s) / sd) if (s is not None and sd) else None
    return Indicators(
        close=closes[-1], sma=s, std=sd, z=z,
        rsi=wilder_rsi(closes, p.rsi_period),
        sma_regime=sma(closes, p.regime_lookback),
        atr=wilder_atr(highs, lows, closes, p.atr_period),
    )


@dataclass
class Position:
    entry_price: float
    shares: int
    bars_held: int = 0
    tranches: int = 1


def stop_price(entry: float, atr: float | None, p: PendulumParams) -> float:
    """The tighter of 1.5 x ATR and the percentage floor, per the spec.

    'whichever is tighter' means the HIGHER stop price for a long, since that
    is the one hit first. Taking the lower would widen the stop exactly when
    volatility spikes, which is backwards.
    """
    pct_stop = entry * (1 - p.hard_stop_pct)
    ceiling = entry * (1 - p.min_stop_pct)   # never tighter than this
    if atr is None:
        return min(pct_stop, ceiling)
    return min(max(entry - p.atr_mult * atr, pct_stop), ceiling)


def decide(ind: Indicators, pos: Position | None,
           p: PendulumParams) -> tuple[Signal, str]:
    """One of the five signal states, plus why.

    Exits are evaluated BEFORE the regime filter. A position opened while the
    regime was healthy must still be able to close after the regime turns;
    gating exits on the filter would strand a holding precisely in the
    downtrend the filter exists to respect.
    """
    if not ind.ready:
        return Signal.NO_TRADE, "insufficient history"

    if pos is not None:
        if ind.close >= ind.sma:
            return Signal.EXIT, f"reverted to mean (close {ind.close:.2f} >= SMA {ind.sma:.2f})"
        if ind.rsi > p.exit_rsi:
            return Signal.EXIT, f"overbought (RSI {ind.rsi:.1f} > {p.exit_rsi:.0f})"
        if pos.bars_held >= p.time_stop_days:
            return Signal.EXIT, f"time stop ({pos.bars_held} days without reverting)"
        sp = stop_price(pos.entry_price, ind.atr, p)
        if ind.close < sp:
            return Signal.EXIT, f"hard stop (close {ind.close:.2f} < {sp:.2f})"
        if ind.z <= p.add_z and pos.tranches < 2:
            return Signal.ADD, f"deeper weakness (z {ind.z:.2f} <= {p.add_z})"
        return Signal.HOLD, f"z {ind.z:.2f}, RSI {ind.rsi:.1f}"

    below_regime = ind.close < ind.sma_regime
    if below_regime and not p.allow_below_regime:
        return Signal.NO_TRADE, (f"below the {p.regime_lookback}-day SMA "
                                 f"({ind.close:.2f} < {ind.sma_regime:.2f})")
    if ind.z <= p.entry_z and ind.rsi < p.entry_rsi:
        tag = " (below regime, half size)" if below_regime else ""
        return Signal.BUY, f"z {ind.z:.2f} <= {p.entry_z}, RSI {ind.rsi:.1f} < {p.entry_rsi:.0f}{tag}"
    return Signal.HOLD, f"no setup (z {ind.z:.2f}, RSI {ind.rsi:.1f})"
