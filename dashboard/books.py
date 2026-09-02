"""Per-agent capital and P&L tables for the Streamlit cockpit."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.core.agent_book import build_books, position_as_dict
from src.core.decision_log import recent


@st.fragment(run_every=5)
def render_sleeve_books(client) -> None:
    st.subheader("Capital by agent")
    st.caption(
        "Budget is the configured sleeve. Invested is capital actually tied up "
        "(CSP collateral = strike × 100 × contracts, not the option mark). "
        "Turnover is dollars the agent put through the market today. "
        "P&L is broker unrealized. Vampire realized comes from the engine snapshot."
    )
    account = client.get_account()
    equity = float(account.equity)
    books = build_books(
        [position_as_dict(p) for p in client.get_positions()],
        equity=equity,
    )
    rows = []
    for b in books:
        rows.append({
            "Agent": b.label,
            "Budget": b.budget,
            "Invested": b.invested,
            "Free": max(0.0, b.budget - b.invested) if b.budget else 0.0,
            "In play %": (b.invested / b.budget * 100) if b.budget else 0.0,
            "Turnover": b.notional_today,
            "Unrealized P&L": b.unrealized_pnl,
            "Realized P&L": float(b.realized_pnl) if b.realized_pnl is not None else 0.0,
            "Positions": b.positions,
            "Fills today": b.fills_today,
        })
    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Budget": st.column_config.NumberColumn(format="$%.0f"),
            "Invested": st.column_config.NumberColumn(format="$%.0f"),
            "Free": st.column_config.NumberColumn(format="$%.0f"),
            "In play %": st.column_config.NumberColumn(format="%.0f%%"),
            "Turnover": st.column_config.NumberColumn(format="$%.0f"),
            "Unrealized P&L": st.column_config.NumberColumn(format="$%.2f"),
            "Realized P&L": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
    holdings = [h for b in books for h in b.holdings]
    if holdings:
        hdf = pd.DataFrame(holdings)
        st.caption("Holdings attributed to each agent")
        st.dataframe(
            hdf,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Invested": st.column_config.NumberColumn(format="$%.0f"),
                "P&L": st.column_config.NumberColumn(format="$%.2f"),
            },
        )


@st.fragment(run_every=5)
def render_journal_trades(limit: int = 80) -> None:
    trade_events = {
        "long_entry", "short_entry", "long_exit", "short_exit", "order",
    }
    rows = [
        {
            "Time": str(r.get("ts") or "")[11:19],
            "Agent": r.get("agent"),
            "Event": r.get("event"),
            "Symbol": r.get("symbol"),
            "Thought": r.get("thought"),
            "Decision": r.get("decision"),
        }
        for r in reversed(recent(limit=400))
        if r.get("event") in trade_events
    ][:limit]
    if not rows:
        st.info("No fills in the decision journal yet.")
        return
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption("Journal fills from the coordinator process, not this dashboard.")
