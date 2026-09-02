"""Ask the regime advisor about every 15-minute window of a session, offline.

    python scripts/precompute_regime_verdicts.py --data ./data/vampire --model dell4-chat \
        --symdays QQQ:2026-09-01,TQQQ:2026-09-01

One verdict per window from the 30 one-minute bars that END at the window start,
through the same prompt and parser the live agent uses (src/strategies/regime_advisor),
so the replay and the validation judge exactly what production would have asked.
Writes JSON lines as it goes; a partial run is usable and re-running resumes.
"""
from __future__ import annotations

import argparse
import json
import os
import time

import pandas as pd
from dotenv import load_dotenv

from src.core.finance_advisor import _llm_call
from src.strategies.regime_advisor import SYSTEM_PROMPT, build_user_prompt, format_bars, parse_verdict

WINDOWS = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in (0, 15, 30, 45)]
WINDOWS = [w for w in WINDOWS if "09:45" <= w <= "15:45"]


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--model", default="dell4-chat")
    ap.add_argument("--symdays", required=True, help="SYM:YYYY-MM-DD,...")
    a = ap.parse_args()
    for symday in a.symdays.split(","):
        sym, day = symday.split(":")
        bars = pd.read_csv(f"{a.data}/{sym}_{day}_1min.csv")
        bars["t"] = pd.to_datetime(bars["ts"], utc=True, format="ISO8601")
        bars["hm"] = bars["t"].dt.tz_convert("America/New_York").dt.strftime("%H:%M")
        out = f"{a.data}/regime_{sym}_{day}_{a.model}.jsonl"
        done = set()
        if os.path.exists(out):
            done = {json.loads(l)["window"] for l in open(out) if l.strip()}
        with open(out, "a") as fh:
            for w in WINDOWS:
                if w in done:
                    continue
                hist = bars[bars["hm"] < w].tail(30)
                rows = [dict(timestamp=r.t.to_pydatetime(), open=r.open, high=r.high, low=r.low,
                             close=r.close, volume=r.volume) for r in hist.itertuples()]
                lines = format_bars(rows)
                if len(lines) < 10:
                    continue
                t0 = time.time()
                rec = {"window": w, "symbol": sym, "day": day, "model": a.model}
                try:
                    text = _llm_call(a.model, SYSTEM_PROMPT, build_user_prompt(sym, lines),
                                     max_tokens=4000, temperature=0.0)
                    v = parse_verdict(text, sym, a.model, time.time() - t0)
                    rec.update(latency=round(time.time() - t0, 1), parsed=v is not None,
                               regime=v.regime if v else None, confidence=v.confidence if v else None,
                               reason=v.reason if v else None, raw=None if v else text[:200])
                except Exception as exc:
                    rec.update(parsed=False, regime=None, error=f"{type(exc).__name__}: {str(exc)[:120]}")
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                print(sym, day, w, rec.get("regime"), rec.get("latency"), rec.get("error", ""), flush=True)


if __name__ == "__main__":
    main()
