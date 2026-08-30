"""Streamlit dashboard for live monitoring of the trading system."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.alpaca_client import AlpacaClient
from src.core.market_data import MarketDataService
from src.core.position_tracker import PositionTracker
from src.core.options_chain import OptionsChain
from src.core.financial_data import FinancialDataProvider
from src.strategies.csp import CashSecuredPutStrategy
from src.strategies.sixfold_engine import SixfoldEngine
from src.risk.allocation import AllocationManager, AllocationConfig

load_dotenv()

st.set_page_config(
    page_title="ProductAdvisors Trading Dashboard",
    page_icon="PA",
    layout="wide",
)

ET = ZoneInfo("America/New_York")


@st.cache_resource
def get_client():
    return AlpacaClient()


@st.cache_resource
def get_data():
    return MarketDataService(get_client())


@st.cache_resource
def get_tracker():
    return PositionTracker(get_client())


@st.cache_resource
def get_allocator():
    return AllocationManager(get_tracker(), AllocationConfig())


@st.cache_resource
def get_chain():
    return OptionsChain(get_client())


@st.cache_resource
def get_sixfold():
    return SixfoldEngine(), FinancialDataProvider()


def render_header(client: AlpacaClient):
    st.title("ProductAdvisors -- AI Trading Agents")

    clock = client.get_clock()
    et_now = datetime.now(ET).strftime("%H:%M:%S ET")

    if clock.is_open:
        status = "MARKET OPEN"
        color = "green"
    else:
        next_open = clock.next_open.strftime("%a %b %d, %H:%M ET")
        status = f"MARKET CLOSED -- opens {next_open}"
        color = "red"

    st.markdown(
        f"<span style='color:{color}; font-weight:bold'>{status}</span>"
        f"&nbsp;&nbsp;|&nbsp;&nbsp;{et_now}"
        f"&nbsp;&nbsp;|&nbsp;&nbsp;Alpaca Paper Account",
        unsafe_allow_html=True,
    )


def render_account(client: AlpacaClient, tracker: PositionTracker):
    account = client.get_account()
    snapshot = tracker.get_snapshot()

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Equity", f"${float(account.equity):,.2f}", f"${snapshot.daily_pnl:+,.2f}")
    with col2:
        st.metric("Cash", f"${float(account.cash):,.2f}")
    with col3:
        st.metric("Buying Power", f"${float(account.buying_power):,.2f}")
    with col4:
        st.metric("Positions", len(client.get_positions()))
    with col5:
        st.metric("Trades Today", tracker.trade_count_today)


def render_allocation(allocator: AllocationManager):
    budget = allocator.get_budget()

    options_pct = budget.options_used / budget.total_equity * 100 if budget.total_equity else 0
    vampire_pct = budget.vampire_used / budget.total_equity * 100 if budget.total_equity else 0
    cash_val = budget.total_equity - budget.options_used - budget.vampire_used
    cash_pct = cash_val / budget.total_equity * 100 if budget.total_equity else 0

    fig = go.Figure(data=[go.Pie(
        labels=[
            f"Options ({options_pct:.0f}%)",
            f"Vampire ({vampire_pct:.0f}%)",
            f"Cash ({cash_pct:.0f}%)",
        ],
        values=[budget.options_used or 1, budget.vampire_used or 1, cash_val],
        hole=0.5,
        marker_colors=["#3b82f6", "#f97316", "#22c55e"],
        textinfo="label",
    )])
    fig.update_layout(
        height=280,
        margin=dict(t=10, b=10, l=10, r=10),
        showlegend=False,
        font=dict(size=12),
    )
    st.plotly_chart(fig, use_container_width=True)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Options", f"${budget.options_budget:,.0f}", f"${budget.options_available:,.0f} free")
    with c2:
        st.metric("Vampire", f"${budget.vampire_budget:,.0f}", f"${budget.vampire_available:,.0f} free")
    with c3:
        st.metric("Reserve", f"${budget.reserve_target:,.0f}")


def render_market_snapshot(data_svc: MarketDataService):
    symbols = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "META"]
    rows = []
    for sym in symbols:
        try:
            q = data_svc.get_latest_quote(sym)
            if q:
                spread = q.ask - q.bid
                rows.append({
                    "Symbol": sym,
                    "Bid": q.bid,
                    "Ask": q.ask,
                    "Mid": q.mid,
                    "Spread": spread,
                    "Spread %": f"{spread / q.mid * 100:.3f}%" if q.mid else "--",
                })
        except Exception:
            pass

    if rows:
        df = pd.DataFrame(rows)
        df["Bid"] = df["Bid"].map("${:,.2f}".format)
        df["Ask"] = df["Ask"].map("${:,.2f}".format)
        df["Mid"] = df["Mid"].map("${:,.2f}".format)
        df["Spread"] = df["Spread"].map("${:.3f}".format)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.warning("No quotes available")


def render_csp_scanner(client: AlpacaClient, data_svc: MarketDataService):
    chain = get_chain()
    csp = CashSecuredPutStrategy(client, chain, data_svc, get_tracker())

    with st.spinner("Scanning options chains..."):
        opps = csp.scan()

    if not opps:
        st.info("No CSP opportunities found")
        return

    rows = []
    for o in opps[:20]:
        c = o.candidate
        rows.append({
            "Contract": c.symbol,
            "Strike": c.strike_price,
            "Expiry": str(c.expiration),
            "DTE": c.days_to_expiry,
            "Open Int.": c.open_interest or 0,
            "Cash Req.": o.cash_required,
            "Ann. Return": f"{o.annualized_return * 100:.1f}%",
            "Score": round(o.score, 2),
        })

    df = pd.DataFrame(rows)
    df["Strike"] = df["Strike"].map("${:,.2f}".format)
    df["Cash Req."] = df["Cash Req."].map("${:,.0f}".format)

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Score": st.column_config.ProgressColumn(
                min_value=0, max_value=8, format="%.1f",
            ),
        },
    )

    affordable = [o for o in opps if o.cash_required <= 10_000]
    st.caption(f"{len(opps)} total opportunities | {len(affordable)} affordable (< $10K cash)")


def render_positions(client: AlpacaClient):
    positions = client.get_positions()
    if not positions:
        st.info("No open positions")
        return

    rows = []
    total_pnl = 0.0
    for p in positions:
        pnl = float(p.unrealized_pl)
        total_pnl += pnl
        rows.append({
            "Symbol": p.symbol,
            "Qty": float(p.qty),
            "Side": p.side.value if hasattr(p.side, "value") else str(p.side),
            "Avg Entry": float(p.avg_entry_price),
            "Current": float(p.current_price),
            "P&L": pnl,
            "P&L %": float(p.unrealized_plpc) * 100,
            "Mkt Value": float(p.market_value),
        })

    df = pd.DataFrame(rows)
    df["Avg Entry"] = df["Avg Entry"].map("${:,.2f}".format)
    df["Current"] = df["Current"].map("${:,.2f}".format)
    df["P&L"] = df["P&L"].map("${:+,.2f}".format)
    df["P&L %"] = df["P&L %"].map("{:+.2f}%".format)
    df["Mkt Value"] = df["Mkt Value"].map("${:,.2f}".format)
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(f"Total unrealized P&L: ${total_pnl:+,.2f}")


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
            "Limit": f"${float(o.limit_price):.2f}" if o.limit_price else "--",
            "Status": o.status.value,
            "Created": str(o.created_at)[:19],
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def render_strategy_pnl(tracker: PositionTracker):
    strategies = {"csp": "Cash-Secured Puts", "covered_call": "Covered Calls", "vampire": "Vampire Scalper"}
    data = []
    for key, label in strategies.items():
        data.append({
            "Strategy": label,
            "P&L": tracker.strategy_pnl(key),
            "Trades": tracker.strategy_trade_count(key),
        })

    df = pd.DataFrame(data)
    colors = ["#3b82f6" if v >= 0 else "#ef4444" for v in df["P&L"]]

    fig = go.Figure(data=[go.Bar(
        x=df["Strategy"], y=df["P&L"],
        marker_color=colors,
        text=df["P&L"].map("${:+,.2f}".format),
        textposition="outside",
    )])
    fig.update_layout(
        yaxis_title="P&L ($)",
        height=280,
        margin=dict(t=10, b=20, l=20, r=20),
    )
    st.plotly_chart(fig, use_container_width=True)


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
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def render_vampire_status():
    st.markdown("""
    | Symbol | Mode | Net Pos | Daily P&L | Bleeds |
    |--------|------|---------|-----------|--------|
    | SPY | Watching (live stream) | 0 | $0.00 | 0 |
    | QQQ | Watching (live stream) | 0 | $0.00 | 0 |

    *Vampire engines are subscribed to real-time WebSocket quotes.
    Trading begins when market opens and price oscillations exceed the $0.02 threshold.*
    """)


def render_sixfold_scanner():
    """Run SIXFOLD analysis on watchlist and show scores."""
    engine, data_provider = get_sixfold()

    symbols = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META",
               "JPM", "V", "JNJ", "UNH", "PG", "HD",
               "COST", "ABBV", "LLY", "MRK"]

    with st.spinner("Running SIXFOLD analysis (fetching fundamentals)..."):
        fundamentals = []
        for sym in symbols:
            try:
                data = data_provider.get_fundamentals(sym)
                fundamentals.append(data)
            except Exception:
                pass

        scores = engine.score_universe(fundamentals)

    if not scores:
        st.warning("No SIXFOLD scores available")
        return

    rows = []
    for s in scores:
        if not s.in_scope:
            continue
        lens_dict = {lr.name: lr.score for lr in s.lens_results}
        rows.append({
            "Symbol": s.symbol,
            "Score": s.composite_score,
            "Tier": s.confidence.value,
            "Sector": s.sector,
            "Buffett": round(lens_dict.get("Buffett Durable Advantage", 0), 0),
            "ROIC": round(lens_dict.get("Return on Invested Capital", 0), 0),
            "Mismatch": round(lens_dict.get("Valuation Mismatch", 0), 0),
            "Regression": round(lens_dict.get("Damodaran Regression", 0), 0),
            "Insiders": round(lens_dict.get("Capital Signals", 0), 0),
            "History": round(lens_dict.get("Historical Returns", 0), 0),
            "Action": "BUY" if s.composite_score >= 65 else "HOLD" if s.composite_score >= 50 else "AVOID",
        })

    df = pd.DataFrame(rows)

    buy_count = len([r for r in rows if r["Action"] == "BUY"])
    st.caption(f"{len(rows)} securities scored | {buy_count} buy candidates (score >= 65)")

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Score": st.column_config.ProgressColumn(
                min_value=0, max_value=100, format="%.0f",
            ),
            "Buffett": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
            "ROIC": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
            "Mismatch": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
            "Regression": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
            "Insiders": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
            "History": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
        },
    )

    # Detail view for selected symbol
    selected = st.selectbox("View detailed report", ["--"] + [r["Symbol"] for r in rows])
    if selected != "--":
        score_obj = next((s for s in scores if s.symbol == selected), None)
        if score_obj:
            report = engine.format_report(score_obj)
            st.code(report, language="text")


def main():
    try:
        client = get_client()
        data_svc = get_data()
        tracker = get_tracker()
        allocator = get_allocator()
    except Exception as e:
        st.error(f"Failed to connect to Alpaca: {e}")
        st.info("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
        return

    render_header(client)
    st.divider()
    render_account(client, tracker)
    st.divider()

    tab_overview, tab_sixfold, tab_scanner, tab_history = st.tabs([
        "Live Overview", "SIXFOLD Analysis", "Options Scanner", "Trade History"
    ])

    with tab_overview:
        col_left, col_right = st.columns([3, 2])

        with col_left:
            st.subheader("Positions")
            render_positions(client)

            st.subheader("Open Orders")
            render_orders(client)

            st.subheader("Vampire Engines")
            render_vampire_status()

        with col_right:
            st.subheader("Capital Allocation")
            render_allocation(allocator)

            st.subheader("P&L by Strategy")
            render_strategy_pnl(tracker)

    with tab_sixfold:
        st.subheader("SIXFOLD Equity Analysis")
        st.caption(
            "Six independent lenses scoring securities on competitive advantage, "
            "returns on capital, valuation, and insider signals. "
            "Methodology by Tashi (ProductAdvisors)."
        )
        render_sixfold_scanner()

    with tab_scanner:
        col_a, col_b = st.columns([3, 2])

        with col_a:
            st.subheader("CSP Opportunities")
            st.caption("Puts the agent would sell when market opens. Ranked by composite score.")
            render_csp_scanner(client, data_svc)

        with col_b:
            st.subheader("Market Snapshot")
            st.caption("Latest quotes for watchlist symbols")
            render_market_snapshot(data_svc)

    with tab_history:
        st.subheader("Trade Log")
        render_trade_history(tracker)

    st.divider()
    col_r1, col_r2 = st.columns(2)
    with col_r1:
        if st.button("Refresh Now"):
            st.rerun()
    with col_r2:
        auto = st.checkbox("Auto-refresh (15s)", value=False)
        if auto:
            time.sleep(15)
            st.rerun()


if __name__ == "__main__":
    main()
