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
from src.core.config import load_config
try:
    from theme import (inject_theme, hero_html, sparkline_svg, sleeve_cards_html,
                       positions_table_html)
except ImportError:
    from dashboard.theme import (inject_theme, hero_html, sparkline_svg, sleeve_cards_html,
                                 positions_table_html)
try:
    from council import render_council          # streamlit puts dashboard/ on sys.path
except ImportError:
    from dashboard.council import render_council  # tests import the package

load_dotenv()

st.set_page_config(
    page_title="ProductAdvisors Trading Dashboard",
    page_icon="PA",
    layout="wide",
)

ET = ZoneInfo("America/New_York")


@st.cache_resource
def _token_ok(candidate: str | None, expected: str | None) -> bool:
    """Fail closed. No configured token means nobody gets in, not everybody.

    The dashboard shows live positions and P&L for the whole account. The
    failure mode to design against is the one that cannot be seen: a missing
    DASHBOARD_TOKEN in the environment after a redeploy, which with the
    opposite default would silently publish the book to the internet.
    """
    import hmac
    if not expected or not candidate:
        return False
    return hmac.compare_digest(str(candidate), str(expected))


def require_token() -> None:
    """Gate the page on a shared token, via ?token= or a password box.

    Streamlit has no authentication. This is a shared secret for a paper
    account dashboard whose audience is the team and the judging panel, not a
    login system; it exists so the URL alone is not enough.
    """
    expected = os.environ.get("DASHBOARD_TOKEN")
    if st.session_state.get("_authed") is True:
        return
    supplied = None
    try:
        supplied = st.query_params.get("token")
    except Exception:
        supplied = None
    if _token_ok(supplied, expected):
        st.session_state["_authed"] = True
        return
    st.title("ProductAdvisors trading dashboard")
    if not expected:
        st.error("This dashboard has no access token configured, so it is locked.")
        st.stop()
    typed = st.text_input("Access token", type="password")
    if typed and _token_ok(typed, expected):
        st.session_state["_authed"] = True
        st.rerun()
    if typed:
        st.error("That token is not valid.")
    st.caption("Ask the ProductAdvisors team for the token, or open the link that includes it.")
    st.stop()


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
    return AllocationManager(get_tracker(), AllocationConfig.from_config())


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


# The hackathon fixes the starting balance at $100,000, so "since start" is
# measured against that rather than against whatever equity the process saw
# at boot, which is the mistake the daily figure used to make.
STARTING_EQUITY = 100_000.0


def pnl_figures(equity: float, last_equity: float, start: float = STARTING_EQUITY) -> dict:
    """Today's and cumulative P&L, in dollars and percent. Pure, so it is testable.

    Mihai asked whether the dashboard's gain was daily or total. It was daily
    only, and unlabelled. Both are shown now and each says which it is.
    """
    today = equity - last_equity
    since = equity - start
    return {
        "today": today,
        "today_pct": (today / last_equity * 100.0) if last_equity else 0.0,
        "since_start": since,
        "since_start_pct": (since / start * 100.0) if start else 0.0,
    }


def render_account(client: AlpacaClient, tracker: PositionTracker):
    account = client.get_account()
    equity = float(account.equity)
    f = pnl_figures(equity, float(getattr(account, "last_equity", equity) or equity))

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        st.metric("Equity", f"${equity:,.2f}")
    with c2:
        st.metric("Today", f"${f['today']:+,.2f}", f"{f['today_pct']:+.2f}% vs prior close")
    with c3:
        st.metric("Since start", f"${f['since_start']:+,.2f}",
                  f"{f['since_start_pct']:+.2f}% on ${STARTING_EQUITY:,.0f}")
    with c4:
        st.metric("Cash", f"${float(account.cash):,.2f}")
    with c5:
        st.metric("Positions", len(client.get_positions()))
    with c6:
        st.metric("Trades Today", tracker.trade_count_today)


def _session_series(client):
    """Today's equity at five-minute steps, and the prior close as baseline."""
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        h = client.trading.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1D", timeframe="5Min", extended_hours=False))
        vals = [float(e) for e in (h.equity or []) if e]
        base = float(h.base_value) if getattr(h, "base_value", None) else None
        return vals, base
    except Exception:
        return [], None


def render_hero(client: AlpacaClient, tracker: PositionTracker):
    account = client.get_account()
    equity = float(account.equity)
    last = float(getattr(account, "last_equity", equity) or equity)
    f = pnl_figures(equity, last)
    try:
        clock = client.get_clock()
        is_open = bool(clock.is_open)
        status = "Market open" if is_open else f"Closed · opens {clock.next_open.astimezone(ET):%a %H:%M} ET"
    except Exception:
        is_open, status = False, "Market status unavailable"
    vals, base = _session_series(client)
    st.markdown(hero_html(
        equity=equity, today=f["today"], today_pct=f["today_pct"],
        since=f["since_start"], since_pct=f["since_start_pct"],
        market_open=is_open, status_text=status,
        clock_text=datetime.now(ET).strftime("%H:%M ET"),
        spark_svg=sparkline_svg(vals, baseline=base or last),
        session_low=min(vals) if vals else None, session_high=max(vals) if vals else None,
    ), unsafe_allow_html=True)


def _vampire_paused(cfg) -> bool:
    """The pause is a date the engine compares against today, not a flag: an
    expired paused_until must read as active, not armed."""
    from src.strategies.vampire_engine import VampireConfig, VampireEngine
    probe = VampireEngine.__new__(VampireEngine)
    probe.cfg = VampireConfig(symbol="_", paused_until=cfg.vampire_paused_until)
    return probe._is_paused()


def _sleeve_rows(allocator: AllocationManager) -> list[dict]:
    budget = allocator.get_budget()
    cfg = load_config()
    scal = {x.upper() for x in cfg.vampire_symbols}
    six_used = sum(abs(float(p.get("market_value", 0.0)))
                   for sym, p in get_tracker().get_snapshot().positions.items()
                   if len(sym) <= 6 and sym.upper() not in scal and sym.upper() != cfg.pendulum_symbol)
    return [
        dict(name="SixFold", target_pct=cfg.sixfold_pct, budget=budget.sixfold_budget, used=six_used, status="active"),
        dict(name="CSP", target_pct=cfg.options_pct, budget=budget.options_budget, used=budget.options_used, status="active"),
        dict(name="Pendulum", target_pct=cfg.pendulum_pct, budget=budget.pendulum_budget, used=budget.pendulum_used,
             status="active" if budget.pendulum_used > 0 else "armed"),
        dict(name="Vampire", target_pct=cfg.vampire_pct, budget=budget.vampire_budget, used=budget.vampire_used,
             status="retired" if cfg.vampire_pct == 0 else ("armed" if _vampire_paused(cfg) else "active")),
    ]


def render_sleeves(allocator: AllocationManager):
    st.markdown(sleeve_cards_html(_sleeve_rows(allocator)), unsafe_allow_html=True)


def render_allocation(allocator: AllocationManager):
    """Every sleeve, not three of them.

    This chart showed Options, Vampire and Cash while the account ran five
    sleeves, so SIXFOLD's 45% and Pendulum's 15% were both being drawn as
    "Cash". A capital chart that omits the largest strategy is worse than no
    chart, because it looks authoritative.
    """
    budget = allocator.get_budget()
    cfg = load_config()
    eq = budget.total_equity or 1.0

    sixfold_used = sum(
        abs(float(p.get("market_value", 0.0)))
        for sym, p in get_tracker().get_snapshot().positions.items()
        if len(sym) <= 6
        and sym.upper() not in {x.upper() for x in cfg.vampire_symbols}
        and sym.upper() != cfg.pendulum_symbol
    )
    sleeves = [
        ("SixFold",  cfg.sixfold_pct,  budget.sixfold_budget,  sixfold_used,        "#1d1d1f"),
        ("CSP",      cfg.options_pct,  budget.options_budget,  budget.options_used, "#503AA8"),
        ("Pendulum", cfg.pendulum_pct, budget.pendulum_budget, budget.pendulum_used, "#8a6ff0"),
        ("Vampire",  cfg.vampire_pct,  budget.vampire_budget,  budget.vampire_used, "#c7c7cc"),
    ]
    deployed = sum(x[3] for x in sleeves)
    idle = max(0.0, eq - deployed)

    fig = go.Figure(data=[go.Pie(
        labels=[f"{n} ({u / eq * 100:.0f}%)" for n, _, _, u, _ in sleeves] + [f"Idle ({idle / eq * 100:.0f}%)"],
        values=[max(u, 0.01) for _, _, _, u, _ in sleeves] + [idle],
        hole=0.5,
        marker_colors=[c for *_, c in sleeves] + ["#e5e5ea"],
        textinfo="label",
    )])
    fig.update_layout(height=300, margin=dict(t=10, b=10, l=10, r=10),
                      showlegend=False, font=dict(size=12))
    st.plotly_chart(fig, use_container_width=True)

    rows = []
    for name, pct, bud, used, _ in sleeves:
        over = used - bud
        rows.append({
            "Sleeve": name,
            "Target": f"{pct * 100:.0f}%",
            "Budget": f"${bud:,.0f}",
            "Used": f"${used:,.0f}",
            "Status": f"OVER ${over:,.0f}" if over > 1 else f"${bud - used:,.0f} free",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption(f"Idle capital ${idle:,.0f} ({idle / eq * 100:.1f}%). "
               f"Reserve target ${budget.reserve_target:,.0f}.")


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
    cfg = load_config()
    scal = {x.upper() for x in cfg.vampire_symbols}
    from src.risk.allocation import parse_occ
    rows = []
    for p in positions:
        sym = str(p.symbol).upper()
        sleeve = ("CSP" if parse_occ(sym) else "Pendulum" if sym == cfg.pendulum_symbol
                  else "Vampire" if sym in scal else "SixFold")
        rows.append({"sleeve": sleeve, "symbol": p.symbol, "qty": float(p.qty),
                     "entry": float(p.avg_entry_price), "last": float(p.current_price),
                     "pl": float(p.unrealized_pl), "plpc": float(p.unrealized_plpc) * 100,
                     "mv": float(p.market_value)})
    st.markdown(positions_table_html(rows), unsafe_allow_html=True)


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
    strategies = {"csp": "Cash-Secured Puts", "covered_call": "Covered Calls", "vampire": "Vampire"}
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
    """Read the live config rather than assert a fixed answer.

    This was static markdown claiming SPY and QQQ were "Watching" with zero
    P&L. Both facts had been false for days: the symbols are QQQ and TQQQ, and
    the strategy is halted. A hardcoded status panel does not degrade when the
    system changes, it just becomes wrong while still looking live.
    """
    cfg = load_config()
    from src.strategies.vampire_engine import VampireConfig, VampireEngine

    paused_until = cfg.vampire_paused_until
    probe = VampireEngine.__new__(VampireEngine)
    probe.cfg = VampireConfig(symbol="_", paused_until=paused_until)
    paused = probe._is_paused()

    if paused:
        st.warning(f"HALTED until {paused_until}. The pause lifts itself on that "
                   f"date; no manual step is needed.")
    else:
        st.success("Active" + (f" (pause expired {paused_until})" if paused_until else ""))

    st.dataframe(pd.DataFrame([
        {"Symbol": sym, "Status": "Halted" if paused else "Watching",
         "Threshold": f"${cfg.vampire.get('tick_threshold', 0.02):.2f}",
         "Max position": cfg.vampire.get("max_position", "-")}
        for sym in (cfg.vampire_symbols or ["-"])
    ]), hide_index=True, use_container_width=True)
    _render_regime_verdicts(cfg)


def _render_regime_verdicts(cfg) -> None:
    """The LLM's latest regime call per symbol, read from its journal.

    The advisor lives in the agent process; the journal is the only channel
    that survives the process boundary and it also records every failure, so
    "no verdict" shows as a closed gate rather than as silence.
    """
    from src.strategies.regime_advisor import read_regime_journal
    adv = cfg.vampire_regime_advisor
    if not adv:
        return
    st.caption(f"LLM regime advisor: {adv.get('model')} every "
               f"{adv.get('window_minutes', 15)} min; new lots only in chop, "
               f"exits never gated, no verdict = closed")
    latest: dict[str, dict] = {}
    for rec in read_regime_journal(limit=400):
        sym = rec.get("symbol")
        if sym and sym not in latest:
            latest[sym] = rec
    if not latest:
        st.info("No verdicts journalled yet. The advisor starts with the agent and runs in shadow mode while the sleeve is unfunded.")
        return
    now = time.time()
    rows = []
    for sym, rec in sorted(latest.items()):
        age = now - float(rec.get("at") or now)
        rows.append({
            "Symbol": sym,
            "Regime": rec.get("regime") or "no verdict",
            "Entries": "open" if rec.get("regime") == "chop" and age <= adv.get("ttl_minutes", 20) * 60 else "closed",
            "Confidence": rec.get("confidence"),
            "Age": f"{int(age // 60)}m",
            "Reason": rec.get("reason") or rec.get("error") or "",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    from src.strategies.regime_advisor import JOURNAL_PATH as REGIME_JOURNAL
    _render_journal_integrity(REGIME_JOURNAL, "Regime journal")


def render_recent_notifications(limit: int = 6) -> None:
    """The last few engine notifications, on the overview where they get seen.

    The full feed lives in its own tab, which is easy to miss in a tab strip.
    Read from the journal (every send is recorded with its delivery outcome), so
    a failed alert shows here as failed rather than as silence.
    """
    from src.core.notify import read_journal
    recs = read_journal(limit=limit)
    if not recs:
        st.info("No notifications journalled yet.")
        return
    rows = []
    for r in recs:
        when = r.get("ts") or r.get("at") or r.get("time") or ""
        if isinstance(when, (int, float)):
            when = datetime.fromtimestamp(when).strftime("%m-%d %H:%M")
        else:
            when = str(when)[5:16].replace("T", " ")
        rows.append({
            "When": when,
            "Title": r.get("title") or r.get("subject") or "",
            "Message": str(r.get("message") or r.get("body") or "")[:110],
            "Sent": "yes" if r.get("delivered") else ("no: " + str(r.get("error") or "")[:40] if r.get("error") else "no"),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("Full feed, including what ntfy still holds, in the Notifications tab.")


def _render_journal_integrity(path: str, name: str) -> None:
    """Every entry is SHA-256 linked to the one before it; this checks the
    whole file on every load and says so in one line, or shouts if a link
    is broken. A journal is only an audit trail if someone can verify it."""
    from src.core.journal import describe, verify_chain
    rep = verify_chain(path)
    if rep["intact"] is None:
        return
    text = describe(rep, name)
    if rep["intact"]:
        st.caption("Integrity: " + text)
    else:
        st.error("Integrity: " + text)


def _ntfy_live(topic: str, since: str = "12h",
               limit: int = 40) -> tuple[list[dict], str | None]:
    """What ntfy still holds. Returns (messages, error) and never raises.

    The error is returned rather than swallowed. An earlier version caught
    every exception and returned an empty list, so a TLS failure rendered as
    "0 messages on ntfy" -- identical to a genuinely quiet system, and wrong
    in the direction that hides an outage. A panel that cannot distinguish
    "nothing was sent" from "I could not look" is worse than no panel.

    Uses notify's certifi context because this Python has no system CA bundle
    and the default context fails verification against ntfy.sh.
    """
    import json as _json
    import urllib.request
    from src.core.notify import _SSL_CTX
    try:
        url = f"https://ntfy.sh/{topic}/json?poll=1&since={since}"
        with urllib.request.urlopen(url, timeout=8, context=_SSL_CTX) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as exc:
        return [], f"{type(exc).__name__}: {str(exc)[:120]}"
    out = []
    for line in raw.splitlines():
        try:
            m = _json.loads(line)
        except Exception:
            continue
        if m.get("event") == "message":
            out.append(m)
    return list(reversed(out))[:limit], None


def _unread_notification_count(journal: list[dict], session_state=None) -> int:
    """How many journal entries arrived since this browser session opened.

    Streamlit reruns the whole script on every interaction and renders every
    tab's body regardless of which one is visually selected, so there is no
    signal here for "the user actually looked at this tab" -- only "this
    session has been open since T". The baseline is set once, to the newest
    entry already on file at page load, so a fresh session opens at zero
    rather than counting the entire history as unread. "Mark all as read"
    lets the viewer clear it explicitly rather than guessing when they did.

    session_state defaults to Streamlit's real one; a plain dict works
    identically here (get/set/`in`) and is what the tests pass, since
    st.session_state has no meaning outside a running Streamlit script.
    """
    session_state = st.session_state if session_state is None else session_state
    newest_at_load = journal[0].get("ts", "") if journal else ""
    if "notif_seen_before" not in session_state:
        session_state["notif_seen_before"] = newest_at_load
    baseline = session_state["notif_seen_before"]
    return sum(1 for j in journal if (j.get("ts") or "") > baseline)


def render_notifications():
    """What the engine has been saying, and whether it arrived.

    Two sources on purpose. The journal is written by notify() on every send
    and records failures, which no delivery channel does: a silently failing
    alert path leaves no trace and hides an outage for days. The ntfy poll is
    the independent confirmation that a message actually landed.
    """
    from src.core.notify import DEFAULT_TOPIC, JOURNAL_PATH, read_journal

    cfg_topic = os.environ.get("NTFY_TOPIC", DEFAULT_TOPIC)
    sns_on = bool(os.environ.get("SNS_TOPIC_ARN"))

    st.caption(
        f"Channel: **ntfy** topic `{cfg_topic}`. "
        + ("SNS fan-out enabled." if sns_on else
           "SNS fan-out is off by design: the bridge forwards to the shared live "
           "trading topic, which would mix paper alerts into real ones.")
    )

    journal = read_journal(limit=60)
    live, live_err = _ntfy_live(cfg_topic)

    failed = [j for j in journal if not j.get("delivered")]
    c1, c2, c3 = st.columns(3)
    c1.metric("Sent (journal)", len(journal))
    c2.metric("On ntfy now", "?" if live_err else len(live),
              help="ntfy.sh retains about 12 hours")
    c3.metric("Failed sends", len(failed), delta=None if not failed else "check the log")
    _render_journal_integrity(JOURNAL_PATH, "Send journal")
    if failed:
        st.error(f"{len(failed)} notification(s) failed to deliver. "
                 "An alert that never arrives is indistinguishable from a quiet system.")

    if live_err:
        st.warning(f"Could not reach ntfy to confirm delivery ({live_err}). "
                   "The journal below is unaffected; this only means the "
                   "independent confirmation is unavailable right now.")

    if not journal and not live and not live_err:
        st.info("No notifications yet. The journal starts filling from the next "
                "send; alerts fire on session-clock reports and on sleeve breaches, "
                "not continuously.")
        return

    if journal:
        col_hdr, col_btn = st.columns([4, 1])
        col_hdr.markdown("**Send journal** (durable, includes failures) "
                         "&mdash; click a row for the full message")
        if col_btn.button("Mark all as read", use_container_width=True):
            st.session_state["notif_seen_before"] = journal[0].get("ts", "")
            st.rerun()
        from dashboard.theme import notification_rows_html
        rows = [{
            "when": (j.get("ts") or "")[:19].replace("T", " "),
            "severity": j.get("severity", "default"),
            "title": j.get("title") or "",
            "message": j.get("message") or "",
            "via": j.get("transport") or "-",
            "delivered": j.get("delivered", False),
            "error": j.get("error"),
        } for j in journal]
        st.markdown(notification_rows_html(rows), unsafe_allow_html=True)

    if live:
        st.markdown("**Currently on ntfy** (last 12h, as delivered)")
        for m in live:
            ts = datetime.fromtimestamp(m.get("time", 0), ZoneInfo("America/New_York"))
            with st.expander(f"{ts:%m/%d %H:%M} ET  ·  {m.get('title', '(no title)')[:80]}"):
                st.markdown(m.get("message", ""))


def render_pendulum():
    """Tashi's long-Treasury mean-reversion sleeve.

    Shows the signal the engine will actually act on, computed by the same
    decide() the live agent calls, so this panel cannot drift away from the
    strategy it claims to describe.
    """
    cfg = load_config()
    if cfg.pendulum_pct <= 0:
        st.info("Pendulum is not allocated.")
        return

    from src.strategies.pendulum import (
        PendulumParams, compute_indicators, decide, stop_price,
    )
    p = PendulumParams(
        entry_z=float(cfg.pendulum.get("entry_z", -2.0)),
        entry_rsi=float(cfg.pendulum.get("entry_rsi", 10)),
        add_z=float(cfg.pendulum.get("add_z", -2.75)),
        exit_rsi=float(cfg.pendulum.get("exit_rsi", 70)),
        allow_below_regime=bool(cfg.pendulum.get("allow_below_regime", False)),
        below_regime_size_mult=float(cfg.pendulum.get("below_regime_size_mult", 0.5)),
        below_regime_atr_mult=float(cfg.pendulum.get("below_regime_atr_mult", 1.0)),
    )
    sym = cfg.pendulum_symbol
    mode = "Aggressive" if p.allow_below_regime else "Conservative"

    try:
        import datetime as _dt
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(
            symbol_or_symbols=sym, timeframe=TimeFrame.Day,
            start=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=420),
            adjustment=Adjustment.ALL, feed="sip")
        bars = list(get_data()._data.get_stock_bars(req).data.get(sym, []))
        today = datetime.now(ZoneInfo("America/New_York")).date()
        bars = [b for b in bars if b.timestamp.astimezone(ZoneInfo("America/New_York")).date() < today]
    except Exception as exc:
        st.error(f"Could not load {sym} history: {exc}")
        return

    if len(bars) < 205:
        st.warning(f"Only {len(bars)} bars; needs 205 for the 200-day regime filter.")
        return

    ind = compute_indicators([float(b.high) for b in bars],
                            [float(b.low) for b in bars],
                            [float(b.close) for b in bars], p)
    sig, why = decide(ind, None, p)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"{sym} close", f"${ind.close:,.2f}")
    c2.metric("z-score", f"{ind.z:+.2f}", f"entry <= {p.entry_z}")
    c3.metric("RSI(2)", f"{ind.rsi:.1f}", f"entry < {p.entry_rsi:.0f}")
    c4.metric("Signal", sig.value)

    regime_ok = ind.close >= ind.sma_regime
    st.caption(
        f"{mode} mode. SMA20 ${ind.sma:,.2f} | SMA200 ${ind.sma_regime:,.2f} "
        f"({'above' if regime_ok else 'BELOW'}) | ATR(14) ${ind.atr:,.2f} | "
        f"bar {bars[-1].timestamp.astimezone(ZoneInfo('America/New_York')).date()}"
    )
    st.info(f"**{sig.value}** {why}")

    if not regime_ok and p.allow_below_regime:
        st.caption(f"Below the 200-day: entries run at "
                   f"{p.below_regime_size_mult:.0%} size with a "
                   f"{p.below_regime_atr_mult}x ATR stop instead of {p.atr_mult}x.")
    if ind.std:
        trigger = ind.sma + p.entry_z * ind.std
        st.caption(f"A BUY needs a close at or below ${trigger:,.2f} "
                   f"({(trigger / ind.close - 1) * 100:+.2f}% from here).")


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
    require_token()
    inject_theme()
    try:
        client = get_client()
        data_svc = get_data()
        tracker = get_tracker()
        allocator = get_allocator()
    except Exception as e:
        st.error(f"Failed to connect to Alpaca: {e}")
        st.info("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
        return

    render_hero(client, tracker)
    render_sleeves(allocator)
    st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)

    from src.core.notify import read_journal
    unread = _unread_notification_count(read_journal(limit=200), st.session_state)
    notif_label = f"Notifications \U0001F534 {unread}" if unread else "Notifications"

    (tab_overview, tab_council, tab_sixfold, tab_scanner,
     tab_notifications, tab_history) = st.tabs([
        "Live Overview", "AI Council", "SIXFOLD Analysis", "Options Scanner",
        notif_label, "Trade History"
    ])

    with tab_council:
        render_council(client, allocator, tracker)

    with tab_overview:
        st.subheader("Positions")
        render_positions(client)
        st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)
        col_left, col_right = st.columns([3, 2])

        with col_left:
            st.subheader("Open Orders")
            render_orders(client)

            st.subheader("Vampire")
            st.caption("Bi-directional micro-scalping on liquid ETFs: buys dips, shorts rips. "
                       "Entries pass an LLM regime gate; exits never wait for anyone.")
            render_vampire_status()

            st.subheader("Pendulum")
            render_pendulum()

        with col_right:
            st.subheader("Capital Allocation")
            render_allocation(allocator)

            st.subheader("P&L by Strategy")
            render_strategy_pnl(tracker)

            st.subheader("Latest Notifications")
            render_recent_notifications()

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

    with tab_notifications:
        st.subheader("Engine Notifications")
        render_notifications()

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
