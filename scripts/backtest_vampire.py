"""Replay the scalper over the IEX quote stream it actually trades on.

Runs the real VampireEngine.tick() per quote, with a broker stub that fills
IOC market orders at the touch (buy at the ask, sell at the bid), which is
what the live engine pays. The engine's own counters are ignored for
scoring; a separate average-cost ledger is the scoreboard, the same way the
broker's fill ledger was used to retire the sleeve.

Variants are gates: a callable of the quote timestamp that says whether the
engine may open new positions right now. Exits are never gated.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import statistics
import sys
from collections import deque
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.market_data import VWAPTracker  # noqa: E402
from src.strategies import vampire_engine as VE  # noqa: E402
from src.strategies.vampire_engine import VampireConfig, VampireEngine  # noqa: E402

ET = ZoneInfo("America/New_York")


class Clock:
    """The replay's time source. The engine reads time.time() for its rate
    limiter and datetime.now(ET) for session gates; both are redirected here."""
    def __init__(self):
        self.t = 0.0
    def time(self):
        return self.t
    def now(self, tz=None):
        d = dt.datetime.fromtimestamp(self.t, tz=dt.timezone.utc)
        return d.astimezone(tz) if tz else d.replace(tzinfo=None)


class TouchFillBroker:
    """IOC market orders fill in full at the touch. Tracks a broker-side
    average-cost ledger so the score does not depend on the engine's counters."""
    def __init__(self):
        self.bid = self.ask = 0.0
        self.pos = 0
        self.avg = 0.0
        self.realized = 0.0
        self.fills = 0
        self.round_trips: list[float] = []
        self._orders = {}
        self._n = 0

    def _apply(self, qty: int, px: float, buy: bool):
        signed = qty if buy else -qty
        if self.pos == 0 or (self.pos > 0) == (signed > 0):
            new = self.pos + signed
            self.avg = (self.avg * abs(self.pos) + px * qty) / abs(new) if new else 0.0
            self.pos = new
        else:
            closing = min(qty, abs(self.pos))
            pnl = (px - self.avg) * closing * (1 if self.pos > 0 else -1)
            self.realized += pnl; self.round_trips.append(pnl)
            left = qty - closing
            self.pos += signed if left == 0 else (closing if signed > 0 else -closing)
            if left:
                self.pos = left if signed > 0 else -left; self.avg = px
            elif self.pos == 0:
                self.avg = 0.0
        self.fills += 1

    def market_order(self, symbol, qty, side, tif=None):
        buy = str(side).lower().endswith("buy")
        px = self.ask if buy else self.bid
        self._apply(int(qty), px, buy)
        self._n += 1
        o = type("O", (), {})(); o.id = f"o{self._n}"; o.status = "filled"; o.filled_qty = str(qty)
        self._orders[o.id] = o
        return o

    def get_order(self, oid): return self._orders[oid]
    def get_orders(self, status=None): return []
    def cancel_order(self, oid): pass
    def close_position(self, symbol):
        if self.pos > 0: self._apply(self.pos, self.bid, buy=False)
        elif self.pos < 0: self._apply(-self.pos, self.ask, buy=True)


def load_quotes(path):
    with open(path) as fh:
        r = csv.DictReader(fh)
        for row in r:
            ts = dt.datetime.fromisoformat(row["ts"])
            b, a = float(row["bid"]), float(row["ask"])
            if b <= 0 or a <= 0 or a < b: continue
            yield ts, b, a, float(row["bid_size"] or 1)


def load_bars(path):
    with open(path) as fh:
        return [dict(ts=dt.datetime.fromisoformat(r["ts"]), o=float(r["open"]), h=float(r["high"]),
                     l=float(r["low"]), c=float(r["close"]), v=float(r["volume"])) for r in csv.DictReader(fh)]


def efficiency_ratio_gate(bars, window=10, max_er=0.45):
    """Deterministic regime gate. Over the trailing `window` minutes, net move
    divided by the sum of absolute one-minute moves. Near 1 = a straight line
    (trending; stand aside). Near 0 = oscillating around a level (scalp)."""
    import bisect
    closes = [b["c"] for b in bars]; times = [b["ts"] for b in bars]
    cache: dict[int, bool] = {}
    def gate(ts):
        i = bisect.bisect_right(times, ts) - 1
        if i < window: return True
        if i in cache: return cache[i]
        seg = closes[i - window:i + 1]
        path = sum(abs(seg[k] - seg[k - 1]) for k in range(1, len(seg))) or 1e-9
        er = abs(seg[-1] - seg[0]) / path
        cache[i] = er <= max_er
        return cache[i]
    return gate


def llm_gate(verdicts_path, day):
    """Precomputed advisor verdicts (scripts/precompute_regime_verdicts.py): one JSON
    line per 15-minute window, labelled by the ET time the window STARTS at. Trade iff
    regime == "chop", the same rule as RegimeAdvisor.entry_allowed; fail closed outside
    any ruled window, which is what the live gate does with no verdict."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    y, m, d = map(int, day.split("-"))
    wins = []
    for line in open(verdicts_path):
        if not line.strip():
            continue
        r = json.loads(line)
        hh, mm = map(int, r["window"].split(":"))
        start = dt.datetime(y, m, d, hh, mm, tzinfo=et)
        wins.append((start, start + dt.timedelta(minutes=15), r.get("regime") == "chop"))
    def gate(ts):
        for s, e, ok in wins:
            if s <= ts < e:
                return ok
        return False
    return gate


def run(sym, day, quotes_path, gate=None, threshold=None, position_size=10, max_position=100,
        max_notional=7_464.0, max_daily_loss=50.0, bracket=None):
    clock = Clock()
    VE.time.time = clock.time
    VE.datetime = type("DT", (), {"now": staticmethod(clock.now)})
    broker = TouchFillBroker()
    tracker = type("T", (), {"record_trade": lambda *a, **k: None})()
    cfg = VampireConfig(symbol=sym, tick_threshold=threshold or 0.02, position_size=position_size,
                        max_position=max_position, max_daily_loss=max_daily_loss, max_notional=max_notional)
    eng = VampireEngine(broker, None, tracker, cfg)
    eng._is_market_hours = lambda: True
    vwap = VWAPTracker(window_seconds=5)
    spreads = deque(maxlen=200); calibrated = threshold is not None
    n = 0; gated_off_ticks = 0; t_open = None
    for ts, b, a, bsz in load_quotes(quotes_path):
        if t_open is None: t_open = ts
        clock.t = ts.timestamp(); broker.bid, broker.ask = b, a
        mid = (a + b) / 2
        vwap.update(mid, bsz or 1.0, ts)
        if not calibrated:
            # Live calibration (VampireAgent._apply_spread_thresholds): median of
            # spread reads no wider than 0.5% of price, x2.5, floored at $0.02.
            # The first minute of IEX quotes is skipped: at 09:30:00 the IEX
            # book is wide and stale and a median taken there was $1.18 on a
            # $570 stock, which the live filter would have thrown out.
            if (ts - t_open).total_seconds() < 60:
                continue
            if (a - b) <= mid * 0.005:
                spreads.append(a - b)
            if len(spreads) >= 100:
                med = statistics.median(spreads)
                eng.cfg.tick_threshold = round(max(med * 2.5, 0.02), 4); calibrated = True
            continue
        allowed = gate(ts) if gate else True
        if not allowed and eng._net_position == 0:
            gated_off_ticks += 1; continue           # no new entries while the gate is closed
        if not allowed and eng._net_position != 0:
            # gate closed while holding: only exits may run; emulate by forbidding adds
            eng.cfg.max_position = abs(eng._net_position)
        elif allowed:
            eng.cfg.max_position = max_position
        if bracket and eng._net_position != 0 and eng._avg_entry:
            tp, sl = bracket
            move = (mid - eng._avg_entry) * (1 if eng._net_position > 0 else -1)
            if move >= tp * eng.cfg.tick_threshold or move <= -sl * eng.cfg.tick_threshold:
                eng._flatten_all("bracket"); continue
        eng.tick(mid, vwap.value)
        n += 1
    broker.close_position(sym)   # end-of-day flatten, as live
    rt = broker.round_trips
    wins = [x for x in rt if x > 0]
    return dict(symbol=sym, day=day, threshold=eng.cfg.tick_threshold, ticks=n, fills=broker.fills,
                realized=round(broker.realized, 2), round_trips=len(rt),
                win_rate=round(len(wins) / len(rt) * 100, 1) if rt else 0.0,
                avg_win=round(statistics.mean(wins), 3) if wins else 0.0,
                avg_loss=round(statistics.mean([x for x in rt if x <= 0]), 3) if len(rt) > len(wins) else 0.0,
                gated_off_pct=round(gated_off_ticks / max(1, gated_off_ticks + n) * 100, 1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True); ap.add_argument("--symbols", default="QQQ,TQQQ")
    ap.add_argument("--days", default="2026-09-01,2026-09-02"); ap.add_argument("--variants", default="none,er")
    ap.add_argument("--llm-dir", default=None)
    ap.add_argument("--thresholds", default="", help="e.g. QQQ=0.10,TQQQ=0.025 (live values); empty = calibrate")
    a = ap.parse_args()
    thr = {k: float(v) for k, v in (x.split("=") for x in a.thresholds.split(",") if x)}
    # The live sleeve caps on 2026-09-02: QQQ 10 shares (one lot), TQQQ 100.
    caps = {"QQQ": 10, "TQQQ": 100}
    rows = []
    for sym in a.symbols.split(","):
        for day in a.days.split(","):
            q = f"{a.data}/{sym}_{day}_quotes.csv"; bars = load_bars(f"{a.data}/{sym}_{day}_1min.csv")
            for v in a.variants.split(","):
                gate = None; bracket = None
                if v == "er": gate = efficiency_ratio_gate(bars)
                elif v.startswith("llm:"): gate = llm_gate(f"{a.llm_dir or a.data}/regime_{sym}_{day}_{v[4:]}.jsonl", day)
                elif v == "bracket": bracket = (1.0, 2.0)
                elif v == "er+bracket": gate = efficiency_ratio_gate(bars); bracket = (1.0, 2.0)
                r = run(sym, day, q, gate=gate, bracket=bracket, threshold=thr.get(sym),
                        max_position=caps.get(sym, 100)); r["variant"] = v; rows.append(r)
                print(f"  {sym:<5} {day} {v:<12} thr {r['threshold']:.3f}  fills {r['fills']:>5}  rt {r['round_trips']:>4}  "
                      f"win {r['win_rate']:>5.1f}%  realized {r['realized']:>+9.2f}  gated-off {r['gated_off_pct']:>5.1f}%", flush=True)
                json.dump(rows, open(f"{a.data}/results_{a.symbols}_{a.days}.json", "w"), indent=1)
    json.dump(rows, open(f"{a.data}/results_{a.symbols}_{a.days}.json", "w"), indent=1)
