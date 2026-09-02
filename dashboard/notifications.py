"""ntfy notification feed for the Streamlit dashboard.

Polls the same ntfy topic the engine publishes to (via NTFY_TOPIC env var)
and renders a reverse-chronological feed. Read-only: never publishes.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
from datetime import datetime, timezone
from typing import Any

import streamlit as st

try:
    import certifi
    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = None

log = logging.getLogger(__name__)

PRIORITY_LABEL = {1: "min", 2: "low", 3: "default", 4: "high", 5: "urgent"}
PRIORITY_COLOR = {
    1: "#94a3b8",  # slate
    2: "#60a5fa",  # blue
    3: "#a3a3a3",  # neutral
    4: "#fb923c",  # orange
    5: "#ef4444",  # red
}

DEFAULT_TOPIC = "frank-trading-a0372d5e65"
NTFY_BASE = "https://ntfy.sh"


def _resolve_topic() -> str:
    return os.environ.get("NTFY_TOPIC", DEFAULT_TOPIC)


def _poll_messages(topic: str, since: str = "2h", limit: int = 100) -> list[dict]:
    """Poll ntfy.sh for cached messages. Returns list of message dicts."""
    import urllib.request

    url = f"{NTFY_BASE}/{topic}/json?poll=1&since={since}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception:
        log.warning("ntfy poll failed for topic %s", topic, exc_info=True)
        return []

    messages = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("event") != "message":
            continue
        messages.append(obj)

    messages.sort(key=lambda m: m.get("time", 0), reverse=True)
    return messages[:limit]


def _format_time(unix_ts: int) -> str:
    try:
        dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
        return dt.strftime("%H:%M:%S UTC")
    except Exception:
        return str(unix_ts)


def _render_message_card(msg: dict) -> None:
    """Render a single notification as a styled card."""
    priority = msg.get("priority", 3)
    color = PRIORITY_COLOR.get(priority, "#a3a3a3")
    label = PRIORITY_LABEL.get(priority, "default")
    title = msg.get("title") or "(no title)"
    body = msg.get("message") or ""
    tags = msg.get("tags") or []
    ts = _format_time(msg.get("time", 0))

    tag_str = " ".join(f"`{t}`" for t in tags) if tags else ""

    st.markdown(
        f"<div style='"
        f"border-left: 4px solid {color}; "
        f"padding: 8px 12px; "
        f"margin-bottom: 6px; "
        f"background: rgba(0,0,0,0.02); "
        f"border-radius: 0 6px 6px 0;"
        f"'>"
        f"<div style='display:flex; justify-content:space-between; align-items:baseline;'>"
        f"<span style='font-weight:600; font-size:0.95rem;'>{title}</span>"
        f"<span style='font-size:0.78rem; color:#888;'>{ts} &middot; {label}</span>"
        f"</div>"
        f"<div style='font-size:0.88rem; margin-top:2px; white-space:pre-wrap;'>{body}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if tag_str:
        st.caption(tag_str)


@st.fragment(run_every=15)
def _auto_poll_fragment(since: str = "2h") -> None:
    """Fragment that auto-refreshes. Stores results in session_state."""
    topic = _resolve_topic()
    messages = _poll_messages(topic, since=since)
    st.session_state["_ntfy_messages"] = messages
    st.session_state["_ntfy_last_poll"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    st.session_state["_ntfy_topic"] = topic


def render_notifications(since: str = "2h") -> None:
    """Notification feed with auto-refresh and manual refresh button."""
    # The fragment runs every 15s and updates session_state
    _auto_poll_fragment(since=since)

    topic = st.session_state.get("_ntfy_topic", _resolve_topic())
    last_poll = st.session_state.get("_ntfy_last_poll", "never")
    messages = st.session_state.get("_ntfy_messages", [])

    col_title, col_poll, col_btn, col_link = st.columns([2, 1, 1, 1])
    with col_title:
        st.caption(f"Topic: `{topic}` -- last {since}")
    with col_poll:
        st.caption(f"Polled: {last_poll}")
    with col_btn:
        if st.button("Refresh now", key="ntfy_refresh", use_container_width=True):
            fresh = _poll_messages(topic, since=since)
            st.session_state["_ntfy_messages"] = fresh
            st.session_state["_ntfy_last_poll"] = datetime.now(
                timezone.utc
            ).strftime("%H:%M:%S UTC")
            messages = fresh
    with col_link:
        st.markdown(
            f"<a href='{NTFY_BASE}/{topic}' target='_blank' "
            f"style='font-size:0.8rem;'>Open in ntfy</a>",
            unsafe_allow_html=True,
        )

    if not messages:
        st.info(
            "No notifications in the last 2 hours. "
            "Notifications appear here when the engine publishes to ntfy. "
            "The fragment auto-polls every 15 seconds when this tab is visible."
        )
        return

    urgent = [m for m in messages if m.get("priority", 3) >= 4]
    if urgent:
        st.warning(f"{len(urgent)} high-priority notification(s) in the last {since}")

    for msg in messages:
        _render_message_card(msg)

    st.caption(f"{len(messages)} notification(s) shown")
