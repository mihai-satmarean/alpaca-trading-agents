"""Validate candidate Vampire entry gates against REAL FILLS, not a fill model.

    python scripts/validate_vampire_gates.py --data ./data/vampire --since 2026-09-01


Realized P&L (average-cost, per fill, same arithmetic as the broker-truth report) is
bucketed into the 15-minute windows the advisor would have ruled on. A gate that
switches a window off removes that window's realized P&L. Skill = the windows it
switched off were worse than the ones it kept; a permutation test says whether that
beats shuffling the same on/off labels at random.
"""
import os, sys, json, glob, random, bisect, datetime as dt, collections, statistics as st
import pandas as pd
from dotenv import load_dotenv; load_dotenv(os.path.expanduser("~/Documents/Development/alpaca-trading-agents/.env"))
from alpaca.trading.client import TradingClient
import argparse
ap=argparse.ArgumentParser(); ap.add_argument("--data", required=True); ap.add_argument("--since", default="2026-09-01"); ap.add_argument("--symbols", default="QQQ,TQQQ"); ap.add_argument("--days", default="2026-09-01,2026-09-02")
A=ap.parse_args(); D=A.data; SYMS=A.symbols.split(","); DAYS=A.days.split(","); random.seed(7)
T=TradingClient(os.environ["ALPACA_API_KEY"],os.environ["ALPACA_SECRET_KEY"],paper=True)
fills=[]; page=None
while True:
    p={"after":A.since+"T00:00:00Z","page_size":100,"direction":"asc"}
    if page: p["page_token"]=page
    raw=T.get("/account/activities/FILL", p)
    if not raw: break
    fills+=raw
    if len(raw)<100: break
    page=raw[-1]["id"]
ET=dt.timezone(dt.timedelta(hours=-4))
def window_of(t):  # 15-minute bucket label, matching the advisor's window starts
    return f"{t.hour:02d}:{(t.minute//15)*15:02d}"
pnl=collections.defaultdict(float); nfill=collections.Counter(); pos={}
for f in fills:
    s=f["symbol"]
    if s not in SYMS: continue
    q=float(f["qty"]); px=float(f["price"]); sq=q if f["side"]=="buy" else -q
    t=dt.datetime.fromisoformat(f["transaction_time"].replace("Z","+00:00")).astimezone(ET)
    key=(s,t.date().isoformat(),window_of(t)); nfill[key]+=1
    cq,ca=pos.get(s,(0.0,0.0))
    if cq==0 or (cq>0)==(sq>0):
        nq=cq+sq; pos[s]=(nq,(ca*abs(cq)+px*abs(sq))/abs(nq) if nq else 0.0)
    else:
        closing=min(abs(sq),abs(cq)); pnl[key]+=(px-ca)*closing*(1 if cq>0 else -1)
        rem=abs(cq)-closing; left=abs(sq)-closing
        pos[s]=(sq/abs(sq)*left,px) if left>0 else ((cq/abs(cq)*rem if rem else 0.0), (ca if rem else 0.0))
windows=sorted(set(list(pnl)+list(nfill)))
# --- gates ---
def er_gate(sym, day, lookback=10, max_er=0.45):
    bars=pd.read_csv(f"{D}/{sym}_{day}_1min.csv"); t=pd.to_datetime(bars["ts"],utc=True,format="ISO8601").dt.tz_convert("America/New_York")
    hm=t.dt.strftime("%H:%M").tolist(); c=bars["close"].tolist()
    def g(w):
        i=bisect.bisect_left(hm, w)  # bars strictly before the window start
        if i<lookback+1: return None
        seg=c[i-lookback-1:i]; path=sum(abs(seg[k]-seg[k-1]) for k in range(1,len(seg)))
        return (abs(seg[-1]-seg[0])/path<=max_er) if path>0 else None
    return g
def llm_gate(model, min_conf=None):
    v={}
    for fp in glob.glob(f"{D}/regime_*_{model}.jsonl"):
        for line in open(fp):
            if not line.strip(): continue
            r=json.loads(line)
            if not r.get("parsed"): v[(r["symbol"],r["day"],r["window"])]=None; continue
            ok=(r.get("regime")=="chop")
            if min_conf is not None:
                try: ok=ok and float(r.get("confidence") or 0)>=min_conf
                except ValueError: ok=False
            v[(r["symbol"],r["day"],r["window"])]=ok
    return lambda key: v.get(key, None), len(v)
gates={"none": lambda key: True}
er={(s,d):er_gate(s,d) for s in SYMS for d in DAYS}
gates["er(10,0.45)"]=lambda key: er[(key[0],key[1])](key[2])
for model in ("dell4-finance","dell4-chat"):
    g,n=llm_gate(model)
    if n: gates[f"llm:{model}"]=g
    g2,_=llm_gate(model,0.7)
    if n: gates[f"llm:{model} conf>=0.7"]=g2
print(f"real fills bucketed: {sum(nfill.values())} fills, {len(windows)} windows, realized total {sum(pnl.values()):+.2f}")
print(f"\n{'gate':<28}{'ruled':>6}{'on':>5}{'off':>5}{'P&L kept':>11}{'P&L removed':>13}{'mean on':>9}{'mean off':>10}{'p(perm)':>9}")
for name,g in gates.items():
    on=[];off=[];unruled=[]
    for key in windows:
        r=g(key)
        (unruled if r is None else (on if r else off)).append(pnl[key])
    kept=sum(on); removed=sum(off)
    if on and off:
        obs=st.mean(off)-st.mean(on); allv=on+off; k=len(on); cnt=0; N=5000
        for _ in range(N):
            random.shuffle(allv); d=st.mean(allv[k:])-st.mean(allv[:k])
            if d<=obs: cnt+=1
        p=cnt/N  # one-sided: how often random labelling removes windows at least this bad
    else: p=float("nan")
    print(f"{name:<28}{len(on)+len(off):>6}{len(on):>5}{len(off):>5}{kept:>+11.2f}{removed:>+13.2f}"
          f"{(st.mean(on) if on else 0):>+9.2f}{(st.mean(off) if off else 0):>+10.2f}{p:>9.3f}   unruled windows: {len(unruled)} (P&L {sum(unruled):+.2f})")
print("\nper-window realized P&L (real fills):")
for s in SYMS:
    for d in DAYS:
        row=[(k[2],pnl[k]) for k in windows if k[0]==s and k[1]==d]
        print(f"  {s:<5}{d}: "+" ".join(f"{w[:5]}={v:+.0f}" for w,v in row))
