"""Streamlit dashboard for live monitoring of the trading system."""

from __future__ import annotations

import os
import sys
import time

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.alpaca_client import AlpacaClient
from src.core.position_tracker import PositionTracker
from src.risk.allocation import AllocationManager, AllocationConfig

load_dotenv()

st.set_page_config(
    page_title="ProductAdvisors Trading Dashboard",
    page_icon="PA",
    layout="wide",
)


@st.cache_resource
def get_client():
    return AlpacaClient()


@st.cache_resource
def get_tracker():
    return PositionTracker(get_client())


@st.cache_resource
def get_allocator():
    return AllocationManager(get_tracker(), AllocationConfig())


def render_header():
    st.title("ProductAdvisors -- AI Trading Agents")
    st.caption("Alpaca AI Trading Agents Hackathon 2026 | Paper Account")


def render_account_summary(client: AlpacaClient, tracker: PositionTracker):
    account = client.get_account()
    snapshot = tracker.get_snapshot()

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Equity", f"${float(account.equity):,.2f}", f"${snapshot.daily_pnl:+,.2f}")
    with col2:
        st.metric("Cash", f"${float(account.cash):,.2f}")
    with col3:
        st.metric("Buying Power", f"${float(account.buying_power):,.2f}")
    with col4:
        st.metric("Trades Today", tracker.trade_count_today)


def render_positions(client: AlpacaClient):
    positions = client.get_positions()
    if not positions:
        st.info("No open positions")
        return

    rows = []
    for p in positions:
        rows.append({
            "Symbol": p.symbol,
            "Qty": float(p.qty),
            "Side": p.side.value if hasattr(p.side, "value") else str(p.side),
            "Avg Entry": f"${float(p.avg_entry_price):.2f}",
            "Current": f"${float(p.current_price):.2f}",
            "P&L": f"${float(p.unrealized_pl):+,.2f}",
            "P&L %": f"{float(p.unrealized_plpc) * 100:+.2f}%",
            "Mkt Value": f"${float(p.market_value):,.2f}",
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_allocation(allocator: AllocationManager):
    budget = allocator.get_budget()

    fig = go.Figure(data=[go.Pie(
        labels=["Options", "Vampire", "Cash Reserve"],
        values=[budget.options_used, budget.vampire_used, budget.total_equity - budget.options_used - budget.vampire_used],
        hole=0.4,
        marker_colors=["#1f77b4", "#ff7f0e", "#2ca02c"],
    )])
    fig.update_layout(
        title="Capital Allocation",
        height=350,
        margin=dict(t=50, b=20, l=20, r=20),
    )
    st.plotly_chart(fig, use_container_width=True)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Options Budget", f"${budget.options_budget:,.0f}", f"${budget.options_available:,.0f} available")
    with col2:
        st.metric("Vampire Budget", f"${budget.vampire_budget:,.0f}", f"${budget.vampire_available:,.0f} available")
    with col3:
        st.metric("Reserve Target", f"${budget.reserve_target:,.0f}")


def render_orders(client: AlpacaClient):
    orders = client.get_orders("open")
    if not orders:
        st.info("No open orders")
        return

    rows = []
    for o in orders:
        rows.append({
            "Symbol": o.symbol,
            "Side": o.side.value,
            "Qty": o.qty,
            "Type": o.type.value,
            "Status": o.status.value,
            "Created": str(o.created_at)[:19],
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_trade_history(tracker: PositionTracker):
    trades = tracker.trades
    if not trades:
        st.info("No trades recorded this session")
        return

    rows = []
    for t in trades[-50:]:
        rows.append({
            "Time": t.timestamp.strftime("%H:%M:%S"),
            "Symbol": t.symbol,
            "Side": t.side,
            "Qty": t.qty,
            "Price": f"${t.price:.2f}",
            "Strategy": t.strategy,
            "P&L": f"${t.pnl:+.2f}" if t.pnl else "--",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_strategy_pnl(tracker: PositionTracker):
    strategies = ["csp", "covered_call", "vampire"]
    data = []
    for s in strategies:
        data.append({
            "Strategy": s.replace("_", " ").title(),
            "P&L": tracker.strategy_pnl(s),
            "Trades": tracker.strategy_trade_count(s),
        })
    df = pd.DataFrame(data)

    fig = go.Figure(data=[go.Bar(
        x=df["Strategy"],
        y=df["P&L"],
        marker_color=["#1f77b4", "#ff7f0e", "#d62728"],
    )])
    fig.update_layout(
        title="P&L by Strategy",
        yaxis_title="P&L ($)",
        height=300,
        margin=dict(t=50, b=20, l=20, r=20),
    )
    st.plotly_chart(fig, use_container_width=True)


def main():
    render_header()

    try:
        client = get_client()
        tracker = get_tracker()
        allocator = get_allocator()
    except Exception as e:
        st.error(f"Failed to connect to Alpaca: {e}")
        st.info("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
        return

    render_account_summary(client, tracker)
    st.divider()

    col_left, col_right = st.columns([3, 2])

    with col_left:
        st.subheader("Positions")
        render_positions(client)

        st.subheader("Open Orders")
        render_orders(client)

    with col_right:
        render_allocation(allocator)
        render_strategy_pnl(tracker)

    st.divider()
    st.subheader("Trade History")
    render_trade_history(tracker)

    st.divider()
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("Refresh Data"):
            st.rerun()
    with col_b:
        auto_refresh = st.checkbox("Auto-refresh (30s)", value=False)
        if auto_refresh:
            time.sleep(30)
            st.rerun()


if __name__ == "__main__":
    main()
