"""Vampire hunt panel for the Streamlit dashboard.

Read-only: scores victims the same way the picker does, then pulses the
live book against the engine's spread-based trigger. It does not place
orders. Thoughts from a running dry-run appear if DECISION_LOG exists.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from src.core.decision_log import journal_path
from src.strategies.vampire_symbol_picker import PickerConfig, VampireSymbolPicker

SPREAD_MULTIPLE = 2.5
MIN_TICK_THRESHOLD = 0.02
MAX_SPREAD_FRACTION = 0.005


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _decision_log_path() -> Path | None:
    env = journal_path()
    if env:
        return Path(env)
    fallback = _repo_root() / "logs" / "staging-decisions.jsonl"
    return fallback if fallback.exists() else fallback


def load_recent_decisions(limit: int = 40) -> list[dict]:
    path = _decision_log_path()
    if path is None or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for raw in lines[-limit:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return rows


def _run_hunt(client, data_svc, sleeve_budget: float) -> dict:
    picker = VampireSymbolPicker(
        client=client,
        data=data_svc,
        sleeve_budget=sleeve_budget,
        config=PickerConfig(target_count=6),
    )
    result = picker.pick()
    ranked = sorted(result.metrics.values(), key=lambda m: m.score)
    victims = []
    for sym in result.symbols:
        metrics = result.metrics.get(sym)
        budget = result.bleed_budgets.get(sym)
        victims.append({
            "symbol": sym,
            "price": None if metrics is None else metrics.price,
            "spread": None if metrics is None else metrics.spread,
            "spread_pct": None if metrics is None else metrics.spread_pct,
            "atr_pct": None if metrics is None else metrics.atr_pct,
            "score": None if metrics is None else metrics.score,
            "profit_target": None if budget is None else budget.profit_target,
            "loss_limit": None if budget is None else budget.loss_limit,
        })
    ranking = []
    for metrics in ranked:
        ranking.append({
            "symbol": metrics.symbol,
            "price": metrics.price,
            "spread_pct": metrics.spread_pct,
            "atr_pct": metrics.atr_pct,
            "score": metrics.score,
            "picked": metrics.symbol in result.symbols,
        })
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbols": list(result.symbols),
        "victims": victims,
        "ranking": ranking,
        "sleeve": sleeve_budget,
    }


def _pulse(data_svc, symbols: list[str]) -> list[dict]:
    prev = st.session_state.setdefault("vampire_mids", {})
    rows = []
    for sym in symbols:
        try:
            quote = data_svc.get_latest_quote(sym)
        except Exception:
            quote = None
        if quote is None:
            rows.append({
                "Symbol": sym, "Bid": None, "Ask": None, "Mid": None,
                "Spread": None, "Need": None, "Move": None, "Status": "no quote",
            })
            continue
        bid, ask, mid = float(quote.bid), float(quote.ask), float(quote.mid)
        two_sided = bid > 0 and ask > bid
        spread = ask - bid if two_sided else None
        threshold = (
            round(max(spread * SPREAD_MULTIPLE, MIN_TICK_THRESHOLD), 4)
            if spread else None
        )
        last = prev.get(sym)
        move = None if last is None else mid - last
        prev[sym] = mid
        if not two_sided:
            status = "one-sided book — skip"
        elif mid and spread and spread > mid * MAX_SPREAD_FRACTION:
            status = "spread too wide — skip"
        elif threshold is not None and move is not None and abs(move) >= threshold:
            status = "would bleed"
        elif move is None:
            status = "stalking (need another refresh)"
        else:
            status = "stalking"
        rows.append({
            "Symbol": sym,
            "Bid": bid,
            "Ask": ask,
            "Mid": mid,
            "Spread": spread,
            "Need": threshold,
            "Move": move,
            "Status": status,
        })
    return rows


def render_vampire_overview(data_svc) -> None:
    """Compact strip on Live Overview: pulse the last hunt, or the stub symbols."""
    hunt = st.session_state.get("vampire_hunt") or {}
    symbols = hunt.get("symbols") or ["SPY", "QQQ"]
    rows = _pulse(data_svc, symbols)
    if not rows:
        st.info("No vampire tape yet. Open the Vampire Hunt tab and press Hunt for blood.")
        return
    st.dataframe(_format_pulse(rows), use_container_width=True, hide_index=True)
    st.caption(
        "Dashboard watches the book. It does not run the WebSocket engines. "
        "Start `./scripts/run_staging.sh dry-run` in another terminal to let "
        "the scalper actually tick, then refresh this page."
    )


def render_vampire_panel(client, data_svc, allocator) -> None:
    st.subheader("Vampire Hunt")
    st.caption(
        "Streamlit is a live spreadsheet of the account, not the trading "
        "process. This tab runs the same picker the scalper uses, then "
        "watches bid/ask versus the move needed to fire a bleed. No orders."
    )

    sleeve = 10_000.0
    try:
        sleeve = float(allocator.get_budget().vampire_budget or 10_000.0)
    except Exception:
        pass

    col_a, col_b, col_c = st.columns([1, 1, 2])
    with col_a:
        hunt_clicked = st.button("Hunt for blood", type="primary")
    with col_b:
        watch = st.checkbox("Watch the tape (5s)", value=False)
    with col_c:
        st.metric("Vampire sleeve", f"${sleeve:,.0f}")

    if hunt_clicked or "vampire_hunt" not in st.session_state:
        if hunt_clicked or st.session_state.get("vampire_hunt") is None:
            with st.spinner("Vampire is tasting the tape (quotes + ATR on the universe)..."):
                try:
                    st.session_state.vampire_hunt = _run_hunt(client, data_svc, sleeve)
                    st.session_state.vampire_mids = {}
                except Exception as exc:
                    st.error(f"Hunt failed: {exc}")
                    return

    hunt = st.session_state.get("vampire_hunt") or {}
    victims = hunt.get("victims") or []
    if not victims:
        st.warning("Picker returned no victims. Staging IEX books are often one-sided; try again after the open.")
        return

    st.markdown(f"**Lineup** ({hunt.get('at', '')}): `{', '.join(hunt.get('symbols') or [])}`")
    st.caption("Lower score is better: more daily range per unit of spread.")

    victim_rows = []
    for row in victims:
        victim_rows.append({
            "Symbol": row["symbol"],
            "Price": row["price"],
            "Spread": row["spread"],
            "Spread %": None if row["spread_pct"] is None else row["spread_pct"] * 100,
            "ATR %": None if row["atr_pct"] is None else row["atr_pct"] * 100,
            "Score": row["score"],
            "Bleed target": row["profit_target"],
            "Loss limit": row["loss_limit"],
        })
    vf = pd.DataFrame(victim_rows)
    st.dataframe(
        vf.style.format({
            "Price": "${:,.2f}",
            "Spread": "${:.3f}",
            "Spread %": "{:.3f}%",
            "ATR %": "{:.3f}%",
            "Score": "{:.3f}",
            "Bleed target": "${:,.0f}",
            "Loss limit": "${:,.0f}",
        }, na_rep="--"),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Live tape")
    st.caption(
        f"Trigger is {SPREAD_MULTIPLE}x the live spread, floor ${MIN_TICK_THRESHOLD:.2f}. "
        "Move is the change in mid since the last refresh of this page."
    )
    pulse = _pulse(data_svc, hunt.get("symbols") or [])
    st.dataframe(_format_pulse(pulse), use_container_width=True, hide_index=True)

    hungry = [r["Symbol"] for r in pulse if r["Status"] == "would bleed"]
    if hungry:
        st.success(f"Would bleed now: {', '.join(hungry)}")
    else:
        st.info("Stalking — no name has moved a full trigger since the last refresh.")

    ranking = hunt.get("ranking") or []
    if ranking:
        with st.expander("Full ranking (what it considered)"):
            ranked_rows = []
            for i, row in enumerate(ranking[:25], 1):
                ranked_rows.append({
                    "#": i,
                    "Symbol": row["symbol"],
                    "Price": row["price"],
                    "Spread %": row["spread_pct"] * 100,
                    "ATR %": row["atr_pct"] * 100,
                    "Score": row["score"],
                    "Pick": "yes" if row["picked"] else "",
                })
            rf = pd.DataFrame(ranked_rows)
            st.dataframe(
                rf.style.format({
                    "Price": "${:,.2f}",
                    "Spread %": "{:.3f}%",
                    "ATR %": "{:.3f}%",
                    "Score": "{:.3f}",
                }),
                use_container_width=True,
                hide_index=True,
            )

    st.subheader("Decision journal")
    decisions = load_recent_decisions()
    vampire_rows = [
        d for d in decisions
        if str(d.get("agent", "")).startswith("vampire") or d.get("agent") == "observer"
    ]
    if not vampire_rows:
        st.info(
            "No journal yet. Run `./scripts/run_staging.sh dry-run` in another "
            "terminal; thoughts land in logs/staging-decisions.jsonl and show up here."
        )
    else:
        shown = []
        for d in vampire_rows[-25:]:
            shown.append({
                "Time": str(d.get("ts", ""))[11:19],
                "Agent": d.get("agent"),
                "Event": d.get("event"),
                "Symbol": d.get("symbol"),
                "Thought": d.get("thought"),
                "Decision": d.get("decision"),
            })
        st.dataframe(pd.DataFrame(shown), use_container_width=True, hide_index=True)

    if watch:
        import time
        time.sleep(5)
        st.rerun()


def _format_pulse(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)

    def money(val, digits=2):
        if val is None:
            return "--"
        return f"${val:,.{digits}f}"

    out = pd.DataFrame({
        "Symbol": df["Symbol"],
        "Bid": [money(v) for v in df["Bid"]],
        "Ask": [money(v) for v in df["Ask"]],
        "Mid": [money(v) for v in df["Mid"]],
        "Spread": [money(v, 3) for v in df["Spread"]],
        "Need": [money(v, 4) if v is not None else "--" for v in df["Need"]],
        "Move": [
            "--" if v is None else f"{v:+.4f}"
            for v in df["Move"]
        ],
        "Status": df["Status"],
    })
    return out
