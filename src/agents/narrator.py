"""Explains what the deterministic agents did, for notifications and reports.

This is the project's AI surface, and it is deliberately the only one. The rule
it obeys is the project's own: the model is used where language is the useful
output, and never where a number decides something. It runs after the fact on a
plain snapshot, holds no handle on anything that can transact, and a failed
narration costs a paragraph rather than a session.

Kept free of any trading interface on purpose: a test greps this module's own
source to keep it that way.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

MAX_CHARS = 2000

try:
    import certifi

    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    _SSL_CTX = None

_RULES = (
    "You are describing trading decisions that have ALREADY been made by a "
    "deterministic system. Report what happened and why, in plain English, in "
    "at most four short sentences. Do not recommend any trade, do not give "
    "advice, and do not decide anything: the numbers below are the outcome, "
    "not a question. Never invent a figure that is not given to you."
)


@dataclass(frozen=True)
class NarrationRequest:
    equity: float
    cash: float
    daily_pnl: float
    sleeves: dict[str, dict] = field(default_factory=dict)
    actions: list[dict] = field(default_factory=list)
    rejections: list[dict] = field(default_factory=list)


def _money(x: Any) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x)


def _render_facts(req: NarrationRequest) -> list[str]:
    out = [
        f"Account equity: {_money(req.equity)}",
        f"Cash: {_money(req.cash)}",
        f"P&L today: {_money(req.daily_pnl)}",
        "",
        "Sleeves:",
    ]
    for name, s in (req.sleeves or {}).items():
        out.append(
            f"  {name}: committed {_money(s.get('committed', 0))} of "
            f"{_money(s.get('budget', 0))}, unrealized {_money(s.get('unrealized', 0))}, "
            f"{len(s.get('positions') or [])} position(s)"
        )

    out.append("")
    if req.actions:
        out.append("Trades placed this cycle:")
        for a in req.actions:
            bits = [f"  {a.get('symbol')}", f"{a.get('side', '')}".strip()]
            if a.get("credit") is not None:
                bits.append(f"credit {_money(a['credit'])}")
            if a.get("collateral") is not None:
                bits.append(f"collateral {_money(a['collateral'])}")
            if a.get("reason"):
                bits.append(f"({a['reason']})")
            out.append(" ".join(b for b in bits if b))
    else:
        out.append("Trades placed this cycle: none.")

    out.append("")
    if req.rejections:
        # The refusals are the interesting half: they are the risk gates working.
        out.append("Candidates refused, and why:")
        for r in req.rejections:
            out.append(f"  {r.get('symbol')}: {r.get('reason')}")
    else:
        out.append("Candidates refused: none.")
    return out


def build_prompt(req: NarrationRequest) -> str:
    return "\n".join([_RULES, "", *_render_facts(req)])


def _session_prompt(req: NarrationRequest) -> str:
    return "\n".join([
        _RULES,
        "",
        "Summarise the whole session rather than one cycle. If nothing traded, "
        "say so plainly and give the reason from the refusals below.",
        "",
        *_render_facts(req),
    ])


def _chat(system: str, user: str) -> str:
    base = os.environ.get("OPENAI_BASE_URL", "http://100.69.81.102:4000/v1").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LITELLM_KEY") or ""
    timeout = float(os.environ.get("NARRATOR_TIMEOUT", "45"))

    # dell4-chat is a reasoning model: it spends tokens on a hidden
    # reasoning_content field before answering, and 700 was consistently
    # exhausted by that alone, returning content: null with finish_reason
    # "length" - not an error, so nothing here ever raised. Confirmed live
    # against the real endpoint with the production prompt: at 700 tokens
    # every reasoning-capable model tested returned null; at 4000 all of
    # them (dell4-chat 12.5s, dell4-finance 2.0s, dell4-qwen38 34.5s)
    # returned real content with finish_reason "stop". This cluster is
    # self-hosted with no per-token cost, so the budget was the wrong
    # thing to economize - NARRATOR_MAX_TOKENS is generous by default and
    # left tunable rather than hardcoded again.
    max_tokens = int(os.environ.get("NARRATOR_MAX_TOKENS", "4000"))

    body = json.dumps({
        "model": os.environ.get("NARRATOR_MODEL", "dell4-chat"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()

    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        payload = json.load(r)
    return payload["choices"][0]["message"]["content"]


def _run(system: str, user: str, what: str) -> str | None:
    """Narration is best-effort by design: the trading session must not care."""
    try:
        text = _chat(system, user)
    except Exception:
        log.warning("%s unavailable", what, exc_info=True)
        return None
    if not text or not text.strip():
        return None
    return text.strip()[:MAX_CHARS]


def narrate(req: NarrationRequest) -> str | None:
    return _run(_RULES, build_prompt(req), "narration")


def summarise_session(req: NarrationRequest) -> str | None:
    return _run(_RULES, _session_prompt(req), "session summary")
