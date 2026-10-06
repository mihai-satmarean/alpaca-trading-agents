"""Performance analytics panels for the trading dashboard.

Extension module -- imported by app.py with one line to minimize merge
conflicts with Frank's dashboard work. All analytics panels live here.

Data sources:
  - Alpaca portfolio history API (equity curve, daily P&L)
  - PositionTracker trade journal (strategy metrics, hourly heatmap)
  - Alpaca positions (realized vs unrealized)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.core.position_tracker import PositionTracker

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
STARTING_EQUITY = 100_000.0


def _fetch_portfolio_history(client, period: str = "all", timeframe: str = "1D"):
    """Fetch equity history from Alpaca. Returns (timestamps, equity, pnl) or empties."""
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        h = client.trading.get_portfolio_history(
            GetPortfolioHistoryRequest(
                period=period,
                timeframe=timeframe,
                pnl_reset="per_day" if timeframe != "1D" else "no_reset",
            )
        )
        timestamps = [
            datetime.fromtimestamp(ts, tz=ET)
            for ts in (h.timestamp or [])
        ]
        equity = [float(e) for e in (h.equity or [])]
        pnl = [float(p) for p in (h.profit_loss or [])]
        base = float(h.base_value) if getattr(h, "base_value", None) else STARTING_EQUITY
        return timestamps, equity, pnl, base
    except Exception as exc:
        log.warning("Portfolio history unavailable: %s", exc)
        return [], [], [], STARTING_EQUITY


@st.cache_data(ttl=120)
def _cached_history(_client_id: int, period: str, timeframe: str):
    """Streamlit-friendly wrapper keyed on client identity."""
    client = st.session_state.get("_perf_client")
    if client is None:
        return [], [], [], STARTING_EQUITY
    return _fetch_portfolio_history(client, period, timeframe)


def render_equity_curve(client):
    """Cumulative equity curve since account start."""
    st.session_state["_perf_client"] = client
    timestamps, equity, pnl, base = _fetch_portfolio_history(client)
    if len(equity) < 2:
        st.info("Not enough history for equity curve yet.")
        return

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=timestamps, y=equity,
        mode="lines",
        name="Portfolio equity",
        line=dict(color="#3b82f6", width=2),
        fill="tozeroy",
        fillcolor="rgba(59, 130, 246, 0.08)",
    ))
    fig.add_hline(
        y=STARTING_EQUITY, line_dash="dash",
        line_color="#888", annotation_text=f"Start ${STARTING_EQUITY:,.0f}",
    )

    peak = max(equity)
    trough = min(equity)
    current = equity[-1]
    fig.update_layout(
        title="Equity Curve (since account start)",
        yaxis_title="Account Value ($)",
        xaxis_title="Date",
        height=350,
        margin=dict(t=40, b=30, l=60, r=20),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Current", f"${current:,.0f}")
    c2.metric("Peak", f"${peak:,.0f}")
    c3.metric("Trough", f"${trough:,.0f}")
    c4.metric("Return", f"{(current / STARTING_EQUITY - 1) * 100:+.2f}%")


def render_daily_pnl_bars(client):
    """Green/red bars for daily P&L."""
    timestamps, equity, pnl, base = _fetch_portfolio_history(client)
    if len(equity) < 2:
        st.info("Not enough history for daily P&L chart.")
        return

    daily_pnl = []
    dates = []
    for i in range(1, len(equity)):
        diff = equity[i] - equity[i - 1]
        daily_pnl.append(diff)
        dates.append(timestamps[i])

    colors = ["#22c55e" if v >= 0 else "#ef4444" for v in daily_pnl]

    fig = go.Figure(data=[go.Bar(
        x=dates, y=daily_pnl,
        marker_color=colors,
        text=[f"${v:+,.0f}" for v in daily_pnl],
        textposition="outside",
        textfont=dict(size=9),
    )])
    fig.update_layout(
        title="Daily P&L",
        yaxis_title="P&L ($)",
        xaxis_title="Date",
        height=300,
        margin=dict(t=40, b=30, l=60, r=20),
    )
    st.plotly_chart(fig, use_container_width=True)

    wins = sum(1 for v in daily_pnl if v > 0)
    losses = sum(1 for v in daily_pnl if v < 0)
    best = max(daily_pnl) if daily_pnl else 0
    worst = min(daily_pnl) if daily_pnl else 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Winning days", f"{wins}")
    c2.metric("Losing days", f"{losses}")
    c3.metric("Best day", f"${best:+,.0f}")
    c4.metric("Worst day", f"${worst:+,.0f}")


def render_drawdown_chart(client):
    """Drawdown as percentage from peak equity."""
    timestamps, equity, pnl, base = _fetch_portfolio_history(client)
    if len(equity) < 2:
        st.info("Not enough history for drawdown chart.")
        return

    running_max = []
    peak = 0.0
    for e in equity:
        peak = max(peak, e)
        running_max.append(peak)

    drawdown_pct = [
        ((e - rm) / rm * 100) if rm > 0 else 0.0
        for e, rm in zip(equity, running_max)
    ]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=timestamps, y=drawdown_pct,
        mode="lines",
        name="Drawdown %",
        line=dict(color="#ef4444", width=1.5),
        fill="tozeroy",
        fillcolor="rgba(239, 68, 68, 0.15)",
    ))
    max_dd = min(drawdown_pct)
    fig.update_layout(
        title=f"Drawdown from Peak (max: {max_dd:.2f}%)",
        yaxis_title="Drawdown (%)",
        xaxis_title="Date",
        height=250,
        margin=dict(t=40, b=30, l=60, r=20),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_strategy_metrics(tracker: PositionTracker):
    """Win rate, profit factor, avg win/loss per strategy."""
    strategies = defaultdict(lambda: {"wins": 0, "losses": 0, "win_pnl": 0.0,
                                       "loss_pnl": 0.0, "total_pnl": 0.0, "trades": 0})
    for t in tracker.trades:
        s = strategies[t.strategy]
        s["trades"] += 1
        s["total_pnl"] += t.pnl
        if t.pnl > 0:
            s["wins"] += 1
            s["win_pnl"] += t.pnl
        elif t.pnl < 0:
            s["losses"] += 1
            s["loss_pnl"] += abs(t.pnl)

    if not strategies:
        st.info("No trades recorded yet -- metrics will appear after the first trading session.")
        return

    rows = []
    for name, m in sorted(strategies.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
        total = m["wins"] + m["losses"]
        win_rate = (m["wins"] / total * 100) if total > 0 else 0
        avg_win = (m["win_pnl"] / m["wins"]) if m["wins"] > 0 else 0
        avg_loss = (m["loss_pnl"] / m["losses"]) if m["losses"] > 0 else 0
        profit_factor = (m["win_pnl"] / m["loss_pnl"]) if m["loss_pnl"] > 0 else float("inf") if m["win_pnl"] > 0 else 0

        rows.append({
            "Strategy": name.replace("_", " ").title(),
            "Trades": m["trades"],
            "Wins": m["wins"],
            "Losses": m["losses"],
            "Win Rate": f"{win_rate:.0f}%",
            "Total P&L": m["total_pnl"],
            "Avg Win": f"${avg_win:,.2f}",
            "Avg Loss": f"-${avg_loss:,.2f}",
            "Profit Factor": f"{profit_factor:.2f}" if profit_factor != float("inf") else "inf",
        })

    df = pd.DataFrame(rows)

    total_pnl_values = [r["Total P&L"] for r in rows]
    colors = ["#22c55e" if v >= 0 else "#ef4444" for v in total_pnl_values]
    fig = go.Figure(data=[go.Bar(
        x=[r["Strategy"] for r in rows],
        y=total_pnl_values,
        marker_color=colors,
        text=[f"${v:+,.0f}" for v in total_pnl_values],
        textposition="outside",
    )])
    fig.update_layout(
        title="Total P&L by Strategy",
        yaxis_title="P&L ($)",
        height=280,
        margin=dict(t=40, b=30, l=60, r=20),
    )
    st.plotly_chart(fig, use_container_width=True)

    df["Total P&L"] = df["Total P&L"].map("${:+,.2f}".format)
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_realized_vs_unrealized(client, tracker: PositionTracker):
    """Realized (from closed trades) vs unrealized (open positions) P&L."""
    realized = sum(t.pnl for t in tracker.trades)

    positions = client.get_positions()
    unrealized = sum(float(p.unrealized_pl) for p in positions) if positions else 0.0

    total = realized + unrealized

    fig = go.Figure(data=[go.Bar(
        x=["Realized (closed)", "Unrealized (open)", "Total"],
        y=[realized, unrealized, total],
        marker_color=[
            "#22c55e" if realized >= 0 else "#ef4444",
            "#3b82f6" if unrealized >= 0 else "#f97316",
            "#22c55e" if total >= 0 else "#ef4444",
        ],
        text=[f"${realized:+,.2f}", f"${unrealized:+,.2f}", f"${total:+,.2f}"],
        textposition="outside",
    )])
    fig.update_layout(
        title="Realized vs Unrealized P&L",
        yaxis_title="P&L ($)",
        height=280,
        margin=dict(t=40, b=30, l=60, r=20),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_strategy_curves(tracker: PositionTracker):
    """Cumulative P&L curve per strategy over time."""
    if not tracker.trades:
        st.info("No trade data for strategy curves yet.")
        return

    strat_cum: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    strat_running: dict[str, float] = defaultdict(float)

    sorted_trades = sorted(tracker.trades, key=lambda t: t.timestamp)
    for t in sorted_trades:
        strat_running[t.strategy] += t.pnl
        strat_cum[t.strategy].append((t.timestamp, strat_running[t.strategy]))

    if not strat_cum:
        return

    palette = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#8b5cf6",
               "#06b6d4", "#ec4899", "#14b8a6", "#f97316"]
    fig = go.Figure()
    for i, (strat, points) in enumerate(sorted(strat_cum.items())):
        times = [p[0] for p in points]
        values = [p[1] for p in points]
        fig.add_trace(go.Scatter(
            x=times, y=values,
            mode="lines+markers",
            name=strat.replace("_", " ").title(),
            line=dict(color=palette[i % len(palette)], width=2),
            marker=dict(size=4),
        ))

    fig.update_layout(
        title="Cumulative P&L by Strategy",
        yaxis_title="Cumulative P&L ($)",
        xaxis_title="Time",
        height=350,
        margin=dict(t=40, b=30, l=60, r=20),
        hovermode="x unified",
        legend=dict(orientation="h", y=-0.15),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_hourly_heatmap(tracker: PositionTracker):
    """P&L heatmap by hour of day -- when do we make/lose money?"""
    if not tracker.trades:
        st.info("No trade data for hourly heatmap yet.")
        return

    hourly: dict[int, float] = defaultdict(float)
    hourly_count: dict[int, int] = defaultdict(int)
    for t in tracker.trades:
        hour = t.timestamp.astimezone(ET).hour if t.timestamp.tzinfo else t.timestamp.hour
        hourly[hour] += t.pnl
        hourly_count[hour] += 1

    if not hourly:
        return

    hours = list(range(4, 21))
    pnl_vals = [hourly.get(h, 0.0) for h in hours]
    counts = [hourly_count.get(h, 0) for h in hours]
    labels = [f"{h}:00" for h in hours]

    colors = ["#22c55e" if v >= 0 else "#ef4444" for v in pnl_vals]
    fig = go.Figure(data=[go.Bar(
        x=labels, y=pnl_vals,
        marker_color=colors,
        text=[f"${v:+,.0f} ({c})" for v, c in zip(pnl_vals, counts)],
        textposition="outside",
        textfont=dict(size=9),
    )])
    fig.update_layout(
        title="P&L by Hour of Day (ET)",
        yaxis_title="P&L ($)",
        xaxis_title="Hour (ET)",
        height=280,
        margin=dict(t=40, b=30, l=60, r=20),
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Number of trades shown in parentheses. Hours without trades are omitted.")


def render_performance_tab(client, tracker: PositionTracker):
    """Main entry point -- renders the full Performance Analytics tab."""
    st.subheader("Equity Curve")
    render_equity_curve(client)

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Daily P&L")
        render_daily_pnl_bars(client)

    with col_right:
        st.subheader("Drawdown")
        render_drawdown_chart(client)

    st.markdown("---")

    st.subheader("Strategy Performance")
    render_strategy_metrics(tracker)

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Realized vs Unrealized")
        render_realized_vs_unrealized(client, tracker)

    with col_b:
        st.subheader("P&L by Hour")
        render_hourly_heatmap(tracker)

    st.subheader("Strategy P&L Curves")
    render_strategy_curves(tracker)
