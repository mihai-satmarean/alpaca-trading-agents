"""Live agent cards for the Streamlit cockpit.

Reads logs written by the coordinator. Does not import engines or place orders.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.core.agent_status import read_snapshot, snapshot_path
from src.core.decision_log import recent

AGENTS = (
    ("vampire", "Vampire", "Scalper. Watches mid vs threshold, bleeds both sides."),
    ("vampire_picker", "Vampire picker", "Chooses victims and bleed budgets."),
    ("sixfold_analyst", "SIXFOLD analyst", "Scores the equity universe."),
    ("sixfold", "SIXFOLD executor", "Turns scores into sized orders, gated by council."),
    ("council", "Advisory council", "Four-model vote. Trade proceeds on consensus."),
    ("options", "Options income", "CSP and covered-call cycles."),
    ("risk", "Risk manager", "Breakers, drift, end-of-day flatten."),
    ("coordinator", "Coordinator", "Capital split and cycle timing."),
    ("observer", "Observer", "Dry-run heartbeat snapshot."),
)


def _latest_for(agent: str) -> dict:
    rows = recent(limit=80, agent=agent)
    return rows[-1] if rows else {}


def _fmt_ts(row: dict) -> str:
    ts = str(row.get("ts") or "")
    return ts[11:19] if len(ts) >= 19 else ts or "--"


@st.fragment(run_every=2)
def render_live_agents() -> None:
    snap = read_snapshot()
    path = snapshot_path()
    st.caption(
        f"Cockpit is read-only. Snapshot: `{path}` "
        f"({'present' if snap else 'missing — start dry-run'}). "
        "Live thoughts need the coordinator (`dry-run` or `live`) in another terminal."
    )

    env = snap.get("environment") or "unknown"
    dry = snap.get("dry_run")
    market = snap.get("market_open")
    c1, c2, c3, c4 = st.columns(4)
    book = "STAGING" if str(env) == "staging" else str(env).upper()
    c1.metric("Engine book", book)
    c2.metric("Dry-run", "yes" if dry else "no" if dry is False else "--")
    c3.metric("Market", "open" if market else "closed" if market is False else "--")
    c4.metric("Snapshot age", str(snap.get("ts") or "--")[11:19])

    cols = st.columns(2)
    for i, (key, title, blurb) in enumerate(AGENTS):
        with cols[i % 2]:
            _agent_card(key, title, blurb, snap)


def _agent_card(key: str, title: str, blurb: str, snap: dict) -> None:
    row = _latest_for(key)
    thought = row.get("thought") or "--"
    decision = row.get("decision") or "--"
    symbol = row.get("symbol") or ""
    with st.container(border=True):
        st.markdown(f"**{title}** `{key}`")
        st.caption(blurb)
        st.write(f"{_fmt_ts(row)} {symbol} → **{decision}**")
        st.write(thought)
        _snapshot_extra(key, snap)


def _snapshot_extra(key: str, snap: dict):
    if key == "vampire":
        status = snap.get("vampire") or {}
        if not status:
            return
        rows = []
        for sym, info in status.items():
            last = info.get("last_thought") or {}
            rows.append({
                "Symbol": sym,
                "State": info.get("state"),
                "Pos": info.get("net_position"),
                "P&L": info.get("daily_pnl"),
                "Need": info.get("threshold"),
                "Thought": last.get("decision") or last.get("thought") or "",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        return
    if key == "vampire_picker":
        picker = snap.get("vampire_picker") or {}
        lineup = picker.get("lineup") or []
        st.caption(
            f"picker={'on' if picker.get('enabled') else 'off'} · "
            f"hunt_llm={'on' if picker.get('llm_hunt') else 'off'} · "
            f"victims={', '.join(picker.get('symbols') or []) or '(none)'}"
        )
        if lineup:
            st.dataframe(pd.DataFrame(lineup), use_container_width=True, hide_index=True)
        return
    if key == "sixfold_analyst":
        block = snap.get("sixfold_analyst") or {}
        cands = block.get("buy_candidates") or []
        st.caption(
            f"Last scan {block.get('last_scan') or '--'} · "
            f"buy {', '.join(cands) or '(none)'}"
        )
        return
    if key == "sixfold":
        block = snap.get("sixfold") or snap.get("sixfold_executor") or {}
        rejs = block.get("rejections") or []
        if rejs:
            st.dataframe(pd.DataFrame(rejs[:8]), use_container_width=True, hide_index=True)
        return
    if key == "council":
        council = snap.get("council") or {}
        votes = council.get("votes") or []
        if votes:
            st.dataframe(
                pd.DataFrame(
                    [{"Role": v.get("role"), "Vote": v.get("verdict"),
                      "Why": v.get("reasoning")} for v in votes]
                ),
                use_container_width=True,
                hide_index=True,
            )
        return
    if key == "options":
        opts = snap.get("options") or {}
        st.caption(
            f"status={opts.get('status') or '--'} "
            f"CSP={len(opts.get('csp_trades') or [])} "
            f"CC={len(opts.get('cc_trades') or [])}"
        )
        return
    if key == "risk":
        risk = snap.get("risk") or {}
        alerts = risk.get("alerts") or []
        allowed = risk.get("trading_allowed")
        st.caption(f"trading_allowed={allowed}")
        if alerts:
            st.dataframe(pd.DataFrame(alerts), use_container_width=True, hide_index=True)
        return
    if key == "coordinator":
        block = snap.get("coordinator") or {}
        if block:
            st.caption(
                f"equity=${block.get('equity', 0):,.0f} "
                f"pnl=${block.get('daily_pnl', 0):+,.0f} "
                f"pos={block.get('positions', '--')} "
                f"trades={block.get('trades_today', '--')}"
            )
        return
    if key == "observer":
        thoughts = (snap.get("observer") or {}).get("vampire_thoughts") or {}
        if thoughts:
            rows = [
                {
                    "Symbol": sym,
                    "Decision": (row or {}).get("decision"),
                    "Thought": (row or {}).get("thought"),
                }
                for sym, row in thoughts.items()
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.caption("Observer heartbeats appear here once dry-run is up.")


def render_operator_future() -> None:
    st.subheader("Operator controls")
    st.caption(
        "Future version. Buttons are visible so the layout is honest, "
        "and disabled so this process cannot trade."
    )
    a, b, c, d = st.columns(4)
    a.button("Pause Vampire", disabled=True)
    b.button("Skip SIXFOLD cycle", disabled=True)
    c.button("Skip options cycle", disabled=True)
    d.button("Flatten intraday", disabled=True)


@st.fragment(run_every=2)
def render_decision_feed(limit: int = 25) -> None:
    rows = recent(limit=limit)
    if not rows:
        st.info("No decision journal yet.")
        return
    shown = [
        {
            "Time": _fmt_ts(r),
            "Agent": r.get("agent"),
            "Event": r.get("event"),
            "Symbol": r.get("symbol"),
            "Thought": r.get("thought"),
            "Decision": r.get("decision"),
        }
        for r in reversed(rows)
    ]
    st.dataframe(pd.DataFrame(shown), use_container_width=True, hide_index=True)
