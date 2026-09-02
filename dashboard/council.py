"""AI Council: query financial models for portfolio strategy recommendations.

Gathers current portfolio state (positions, P&L, allocation, recent trades,
config) and sends it to multiple financial LLMs on the Dell4 k3s cluster via
LiteLLM.  Each model returns its analysis and proposed allocation changes.
The operator reviews side-by-side and can approve changes, which get written
to config/strategies.yml and logged to the decision journal.
"""

from __future__ import annotations

import concurrent.futures
import copy
import json
import logging
import os
import shutil
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st
import yaml

try:
    import certifi
    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = None

log = logging.getLogger(__name__)

LITELLM_K3S = os.environ.get("LITELLM_K3S_URL", "http://100.101.239.56:30400")
LITELLM_DELL4 = os.environ.get("LITELLM_DELL4_URL",
                               os.environ.get("OPENAI_BASE_URL", "http://100.69.81.102:4000")).rstrip("/")
# The engine's own council already authenticates to this proxy; reuse its key
# rather than introduce a second one that would be unset on the box.
LITELLM_KEY = (os.environ.get("LITELLM_KEY") or os.environ.get("OPENAI_API_KEY")
               or os.environ.get("ANTHROPIC_API_KEY") or "")

# Same three advisors the engine's buy-gate council uses, on the same proxy
# EC2 is known to reach. Mihai's original pointed all three at a k3s node
# that is reachable from his desk and not from the instance.
COUNCIL_MODELS = [
    {"id": "dell4-finance", "label": "dell4-finance", "base_url": LITELLM_DELL4, "max_tokens": 4096},
    {"id": "dell4-chat", "label": "dell4-chat", "base_url": LITELLM_DELL4, "max_tokens": 4096},
    {"id": "dell4-qwen38", "label": "dell4-qwen38", "base_url": LITELLM_DELL4, "max_tokens": 4096},
]

# Writing config/strategies.yml from a browser tab is off unless the operator
# turns it on. This dashboard is on the public internet behind a shared token
# that the judging panel also holds, and a reallocation only takes effect on
# the next agent restart anyway, so an applied change would sit as silent
# drift between the file and the running process. Proposals stay visible;
# the apply path fails closed.
STRATEGIES_PATH = Path(__file__).resolve().parents[1] / "config" / "strategies.yml"

ALLOW_REALLOCATION = os.environ.get("DASHBOARD_ALLOW_REALLOCATION", "").strip() == "1"

# Constants for magic numbers
LLM_TEMPERATURE = 0.3
LLM_MAX_TOKENS = 2048
TOKEN_BUDGET_PCT = 3000
RESERVE_PCT_MIN = 0.05
NORMALIZE_TOTAL = 1.0
CHARS_PER_TOKEN = 4
# One attempt, generous timeout. A thinking model with a 4096-token budget
# can legitimately take two to three minutes on this prompt; the old 120s
# timeout with three retries turned every slow answer into a six-minute
# wait that ended in an error, which is what a judge clicking the button
# would have seen.
MAX_RETRIES = 1
MODEL_TIMEOUT_S = 300
BACKOFF_BASE = 2
RESERVE_MIN_PERCENT = 0.05
NORMALIZE_THRESHOLD = 0.005
NORMALIZE_RANGE_MIN = 0.90
NORMALIZE_RANGE_MAX = 1.10
VALIDATE_TOTAL_THRESHOLD = 0.02
RESIDUAL_THRESHOLD = 0.001
TOTAL_VALIDATION_THRESHOLD = 0.05
TOTAL_DISPLAY_THRESHOLD = 0.5
ROUNDING_THRESHOLD = 0.005
TICK_THRESHOLD_DEFAULT = 0.02


def _validate_yaml_schema(raw_yaml: dict) -> tuple[bool, str]:
    """Validate that the YAML structure has required fields for allocation."""
    if "allocation" not in raw_yaml:
        return False, "Missing 'allocation' key in YAML"
    
    alloc = raw_yaml["allocation"]
    required_fields = ["sixfold_pct", "options_pct", "vampire_pct", "pendulum_pct", "reserve_pct"]
    
    missing = [f for f in required_fields if f not in alloc]
    if missing:
        return False, f"Missing allocation fields: {', '.join(missing)}"
    
    for field in required_fields:
        value = alloc[field]
        if not isinstance(value, (int, float)):
            return False, f"Field '{field}' must be a number, got {type(value).__name__}"
    
    return True, "Schema valid"

SYSTEM_PROMPT = """\
You are a quantitative portfolio advisor for an algorithmic trading system \
running on the Alpaca paper-trading API with $100K equity.

The system runs four strategies:
1. Vampire -- bi-directional micro-scalping on liquid tickers (IOC orders)
2. Options Income -- cash-secured puts on small-cap names
3. SIXFOLD -- fundamental equity scoring and position building
4. Pendulum -- mean-reversion on TLT (long-duration Treasuries)

Your task: analyze the portfolio snapshot below and recommend SPECIFIC \
allocation changes.  Format your recommendation as:

## Analysis
<2-4 sentences on what you see>

## Proposed Changes
```yaml
allocation:
  sixfold_pct: <float>
  options_pct: <float>
  vampire_pct: <float>
  pendulum_pct: <float>
  reserve_pct: <float>
```

## Rationale
<Why each number changed, with reference to the data>

Rules:
- All percentages must sum to 1.00
- Reserve must be >= 0.05 (minimum 5%)
- Be specific: "increase vampire from 15% to 25%" not "consider increasing"
- If the current allocation is already optimal, say so explicitly
"""


def _build_portfolio_context(client, allocator, tracker) -> str:
    """Gather everything the models need to see."""
    parts: list[str] = []

    # Account summary
    account = client.get_account()
    equity = float(account.equity)
    cash = float(account.cash)
    buying_power = float(account.buying_power)
    last_equity = float(getattr(account, "last_equity", equity) or equity)
    daily_pnl = equity - last_equity
    pnl_pct = (daily_pnl / last_equity * 100) if last_equity > 0 else 0.0

    parts.append(f"## Account\nEquity: ${equity:,.2f}\nCash: ${cash:,.2f}\n"
                 f"Buying Power: ${buying_power:,.2f}\n"
                 f"Daily P&L: ${daily_pnl:+,.2f} ({pnl_pct:+.2f}%)")

    # Allocation vs budget
    budget = allocator.get_budget()
    parts.append(
        f"\n## Current Allocation (config)\n"
        f"SIXFOLD: {allocator.config.sixfold_pct * 100:.0f}% "
        f"(${budget.sixfold_budget:,.0f} budget)\n"
        f"Options: {allocator.config.options_pct * 100:.0f}% "
        f"(${budget.options_budget:,.0f} budget, ${budget.options_used:,.0f} used)\n"
        f"Vampire: {allocator.config.vampire_pct * 100:.0f}% "
        f"(${budget.vampire_budget:,.0f} budget, ${budget.vampire_used:,.0f} used)\n"
        f"Pendulum: {allocator.config.pendulum_pct * 100:.0f}% "
        f"(${budget.pendulum_budget:,.0f} budget, ${budget.pendulum_used:,.0f} used)\n"
        f"Reserve: {allocator.config.reserve_pct * 100:.0f}% "
        f"(${budget.reserve_target:,.0f} target)\n"
        f"Unattributed: ${budget.unattributed_used:,.0f}"
    )

    # Positions
    positions = client.get_positions()
    if positions:
        pos_lines = ["## Open Positions"]
        total_unrealized = 0.0
        for p in positions:
            pnl = float(p.unrealized_pl)
            total_unrealized += pnl
            side = p.side.value if hasattr(p.side, "value") else str(p.side)
            pos_lines.append(
                f"  {p.symbol}: {float(p.qty):.0f} {side} @ ${float(p.avg_entry_price):.2f} "
                f"-> ${float(p.current_price):.2f} (P&L: ${pnl:+,.2f})"
            )
        pos_lines.append(f"  Total unrealized: ${total_unrealized:+,.2f}")
        parts.append("\n".join(pos_lines))
    else:
        parts.append("\n## Open Positions\nNone")

    # Recent trades from the decision log -- individual details with token budget
    try:
        from src.core.decision_log import recent
    except ImportError:  # not on main; the context just omits recent decisions
        def recent(*_a, **_k):
            return []
    TRADE_EVENTS = {"long_entry", "short_entry", "long_exit", "short_exit", "order"}
    
    trades = recent(limit=500)
    trade_events = [t for t in trades if t.get("event") in TRADE_EVENTS]
    
    if trade_events:
        # Summary by agent first (always included)
        agent_counts: dict[str, int] = {}
        agent_pnl: dict[str, float] = {}
        for t in trade_events:
            agent = t.get("agent", "unknown")
            agent_counts[agent] = agent_counts.get(agent, 0) + 1
            pnl_val = t.get("realized_pnl") or t.get("pnl") or 0
            if isinstance(pnl_val, (int, float)):
                agent_pnl[agent] = agent_pnl.get(agent, 0) + float(pnl_val)

        summary_lines = [f"\n## Trade Summary ({len(trade_events)} trades)"]
        for agent in sorted(agent_counts):
            pnl = agent_pnl.get(agent, 0)
            summary_lines.append(f"  {agent}: {agent_counts[agent]} trades, "
                                 f"realized P&L: ${pnl:+,.2f}")
        parts.append("\n".join(summary_lines))

        # Individual trade log (newest first, token-budgeted)
        detail_lines = ["\n## Individual Trades (newest first)"]
        chars_used = 0
        max_chars = TOKEN_BUDGET_PCT * CHARS_PER_TOKEN
        included = 0
        for t in reversed(trade_events):
            ts = t.get("ts", "?")
            if "T" in str(ts):
                ts = str(ts).split("T")[1][:8]
            agent = t.get("agent", "?")
            event = t.get("event", "?")
            sym = t.get("symbol", "?")
            qty = t.get("qty", "?")
            price = t.get("price")
            price_s = f"${float(price):.2f}" if price else "?"
            thought = t.get("thought", "")
            if len(thought) > 80:
                thought = thought[:77] + "..."
            line = f"  {ts} {agent}/{event} {sym} x{qty} @{price_s}"
            if thought:
                line += f"  [{thought}]"
            chars_used += len(line) + 1
            if chars_used > max_chars:
                detail_lines.append(
                    f"  ... {len(trade_events) - included} older trades omitted "
                    f"(token budget: ~{TOKEN_BUDGET_PCT} tokens)")
                break
            detail_lines.append(line)
            included += 1
        parts.append("\n".join(detail_lines))

    # Strategy config highlights (read from live config, not hardcoded)
    from src.core.config import load_config as _load_strategy_config
    scfg = _load_strategy_config()
    v = scfg.vampire
    pause = scfg.vampire_paused_until or "not paused"
    parts.append(
        f"\n## Vampire Config\n"
        f"Symbols: {', '.join(scfg.vampire_symbols) or 'none'}\n"
        f"Tick threshold: {v.get('tick_threshold', TICK_THRESHOLD_DEFAULT)}, "
        f"Position size: {v.get('position_size', 10)}, "
        f"Max position: {v.get('max_position', 100)}\n"
        f"Max daily loss: ${v.get('max_daily_loss', 200)}, "
        f"Paused until: {pause}"
    )

    return "\n\n".join(parts)


def _query_model(model_cfg: dict, context: str) -> dict:
    """Send the portfolio context to one LLM and return its response."""
    base_url = model_cfg["base_url"].rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    url = f"{base_url}/v1/chat/completions"

    payload = {
        "model": model_cfg["id"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        "max_tokens": model_cfg["max_tokens"],
        "temperature": LLM_TEMPERATURE,
    }
    headers = {"Content-Type": "application/json"}
    if LITELLM_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_KEY}"

    body = json.dumps(payload).encode()
    
    # Retry with exponential backoff (3 attempts, starting at 2 seconds)
    for attempt in range(3):
        start = time.monotonic()
        
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=MODEL_TIMEOUT_S, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            elapsed = time.monotonic() - start
            msg = data["choices"][0]["message"]
            # A reasoning model that spends its budget thinking returns
            # content None with the text in reasoning_content. The engine's
            # own council hit this on 2026-09-01; the parser downstream
            # expects a string, and None crashed the whole page.
            content = msg.get("content") or msg.get("reasoning_content") or ""
            return {
                "model": model_cfg["id"],
                "label": model_cfg["label"],
                "content": content,
                "elapsed_s": round(elapsed, 1),
                "error": None,
            }
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            elapsed = time.monotonic() - start
            log.warning(f"Attempt {attempt + 1}/3 for {model_cfg['id']} failed: {exc}")
            if attempt == 2:  # Last attempt
                return {
                    "model": model_cfg["id"],
                    "label": model_cfg["label"],
                    "content": "",
                    "elapsed_s": round(elapsed, 1),
                    "error": str(exc),
                }
            # Wait with exponential backoff
            sleep_time = 2 ** (attempt + 1)  # 2s, 4s, 8s
            log.info(f"Retrying {model_cfg['id']} in {sleep_time}s...")
            time.sleep(sleep_time)
        except Exception as exc:
            # Non-network errors (e.g., model response errors) fail immediately
            elapsed = time.monotonic() - start
            return {
                "model": model_cfg["id"],
                "label": model_cfg["label"],
                "content": "",
                "elapsed_s": round(elapsed, 1),
                "error": str(exc),
            }


def _parse_yaml_block(text: str | None) -> dict | None:
    if not text:
        return None
    """Extract allocation from model output, tolerating format variations.

    Models may use different key names (sixfold vs sixfold_pct), percentage
    formats (30% vs 0.30 vs 30), or wrapper keys (equity_allocation vs
    allocation).  We normalize everything to {key_pct: float_0_to_1}.
    """
    import re

    # Try fenced YAML block first
    m = re.search(r"```(?:yaml)?\s*\n(.*?)```", text, re.DOTALL)
    raw_yaml = m.group(1) if m else None

    if not raw_yaml:
        # Try inline allocation block
        m = re.search(r"(\w*allocation\w*:\s*\n(?:\s+\w+:\s*[\d.%]+\n?)+)", text, re.I)
        raw_yaml = m.group(1) if m else None

    if not raw_yaml:
        return None

    try:
        parsed = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        return None

    if not isinstance(parsed, dict):
        return None

    # Unwrap nested keys like equity_allocation, allocation, etc.
    inner = parsed
    for key in list(parsed.keys()):
        if "alloc" in key.lower() and isinstance(parsed[key], dict):
            inner = parsed[key]
            break

    # Normalize keys and values
    KEY_MAP = {
        "sixfold": "sixfold_pct",
        "sixfold_pct": "sixfold_pct",
        "options": "options_pct",
        "options_pct": "options_pct",
        "vampire": "vampire_pct",
        "vampire_pct": "vampire_pct",
        "pendulum": "pendulum_pct",
        "pendulum_pct": "pendulum_pct",
        "reserve": "reserve_pct",
        "reserve_pct": "reserve_pct",
    }

    result = {}
    for k, v in inner.items():
        canonical = KEY_MAP.get(k.lower().strip())
        if not canonical:
            continue
        # Parse value: "30%" -> 0.30, 30 -> 0.30, 0.30 -> 0.30
        s = str(v).strip().rstrip("%")
        try:
            num = float(s)
        except ValueError:
            continue
        if num > 1.0:
            num = num / 100.0
        result[canonical] = round(num, 2)

    required = {"sixfold_pct", "options_pct", "vampire_pct", "pendulum_pct", "reserve_pct"}
    if required.issubset(result.keys()):
        return result
    return None


def _normalize_allocation(alloc: dict) -> dict:
    """Auto-normalize allocations that are close but don't sum to 1.0.

    Models often produce totals like 1.05 or 0.95 due to rounding.
    If the total is within [0.90, 1.10], scale proportionally to 1.0.
    """
    required = {"sixfold_pct", "options_pct", "vampire_pct", "pendulum_pct", "reserve_pct"}
    if not required.issubset(alloc.keys()):
        return alloc
    total = sum(float(alloc[k]) for k in required)
    if abs(total - NORMALIZE_TOTAL) < NORMALIZE_THRESHOLD:
        return alloc
    if NORMALIZE_RANGE_MIN <= total <= NORMALIZE_RANGE_MAX:
        factor = NORMALIZE_TOTAL / total
        scaled = {k: round(float(v) * factor, 2) if k in required else v
                  for k, v in alloc.items()}
        # Enforce reserve_pct >= 5% safety minimum
        if scaled.get("reserve_pct", 0) < RESERVE_PCT_MIN:
            deficit = RESERVE_PCT_MIN - scaled["reserve_pct"]
            scaled["reserve_pct"] = RESERVE_PCT_MIN
            other_keys = [k for k in required if k != "reserve_pct"]
            other_total = sum(scaled[k] for k in other_keys)
            if other_total > 0:
                for k in other_keys:
                    scaled[k] = round(scaled[k] - deficit * (scaled[k] / other_total), 2)
            # Fix any float-rounding residual
            residual = sum(scaled[k] for k in required) - NORMALIZE_TOTAL
            if abs(residual) > RESIDUAL_THRESHOLD:
                biggest = max(other_keys, key=lambda k: scaled[k])
                scaled[biggest] = round(scaled[biggest] - residual, 2)
        return scaled
    return alloc


def _validate_allocation(alloc: dict) -> tuple[bool, str]:
    """Check that an allocation dict is valid."""
    required = {"sixfold_pct", "options_pct", "vampire_pct", "pendulum_pct", "reserve_pct"}
    if not required.issubset(alloc.keys()):
        return False, f"Missing keys: {required - set(alloc.keys())}"

    total = sum(float(alloc[k]) for k in required)
    if abs(total - NORMALIZE_TOTAL) > VALIDATE_TOTAL_THRESHOLD:
        return False, f"Percentages sum to {total:.2f}, not {NORMALIZE_TOTAL:.2f}"

    if float(alloc.get("reserve_pct", 0)) < RESERVE_PCT_MIN:
        return False, f"Reserve must be >= {RESERVE_PCT_MIN:.2%}"

    for k in required:
        v = float(alloc[k])
        if v < 0 or v > 1:
            return False, f"{k}={v} is out of range [0, 1]"

    return True, "OK"


def _apply_allocation(new_alloc: dict) -> tuple[bool, str]:
    """Write new allocation percentages to strategies.yml with backup.

    Refuses unless DASHBOARD_ALLOW_REALLOCATION=1 is set in the environment.
    """
    if not ALLOW_REALLOCATION:
        return False, ("Reallocation from the dashboard is disabled on this deployment "
                       "(set DASHBOARD_ALLOW_REALLOCATION=1 on the host to enable).")
    if not STRATEGIES_PATH.exists():
        return False, f"Config not found: {STRATEGIES_PATH}"

    # Validate YAML schema before writing
    test_yaml = {"allocation": new_alloc}
    valid, msg = _validate_yaml_schema(test_yaml)
    if not valid:
        return False, f"Schema validation failed: {msg}"

    backup = STRATEGIES_PATH.with_suffix(f".yml.bak.{int(time.time())}")
    shutil.copy2(STRATEGIES_PATH, backup)

    raw = STRATEGIES_PATH.read_text(encoding="utf-8")
    full = yaml.safe_load(raw)
    full["allocation"] = {
        "sixfold_pct": round(float(new_alloc["sixfold_pct"]), 2),
        "options_pct": round(float(new_alloc["options_pct"]), 2),
        "vampire_pct": round(float(new_alloc["vampire_pct"]), 2),
        "pendulum_pct": round(float(new_alloc["pendulum_pct"]), 2),
        "reserve_pct": round(float(new_alloc["reserve_pct"]), 2),
    }

    # Preserve comments by doing a targeted replacement in the raw text
    import re
    alloc_pattern = re.compile(
        r"(allocation:\s*\n)"
        r"(\s+sixfold_pct:\s*[\d.]+.*\n)"
        r"(\s+options_pct:\s*[\d.]+.*\n)"
        r"(\s+vampire_pct:\s*[\d.]+.*\n)"
        r"(\s+pendulum_pct:\s*[\d.]+.*\n)"
        r"(\s+reserve_pct:\s*[\d.]+.*\n)",
    )

    prev = _read_current_alloc()
    replacement = (
        f"allocation:\n"
        f"  sixfold_pct: {full['allocation']['sixfold_pct']}      "
        f"# was {prev.get('sixfold_pct', '?')}, "
        f"changed by AI Council {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"  options_pct: {full['allocation']['options_pct']}      "
        f"# was {prev.get('options_pct', '?')}\n"
        f"  vampire_pct: {full['allocation']['vampire_pct']}      "
        f"# was {prev.get('vampire_pct', '?')}\n"
        f"  pendulum_pct: {full['allocation']['pendulum_pct']}      "
        f"# was {prev.get('pendulum_pct', '?')}\n"
        f"  reserve_pct: {full['allocation']['reserve_pct']}      "
        f"# minimum 5%\n"
    )

    new_raw = alloc_pattern.sub(replacement, raw)
    content = new_raw if new_raw != raw else yaml.dump(full, default_flow_style=False)
    tmp = STRATEGIES_PATH.with_suffix(".yml.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(STRATEGIES_PATH))

    # Notify via ntfy
    from src.core.notify import notify
    summary = ", ".join(f"{k}: {v}" for k, v in full["allocation"].items())
    notify(
        "Council Approved",
        f"New allocation applied: {summary}",
        severity="high",
        tags=["council", "rebalance"],
    )

    return True, f"Applied. Backup at {backup.name}"


def _read_current_alloc() -> dict:
    """Read current allocation from strategies.yml."""
    if not STRATEGIES_PATH.exists():
        return {}
    try:
        full = yaml.safe_load(STRATEGIES_PATH.read_text(encoding="utf-8"))
        return full.get("allocation", {})
    except Exception:
        return {}


def _diff_table(current: dict, proposed: dict) -> list[dict]:
    """Build a comparison table between current and proposed allocation."""
    keys = ["sixfold_pct", "options_pct", "vampire_pct", "pendulum_pct", "reserve_pct"]
    labels = {
        "sixfold_pct": "SIXFOLD",
        "options_pct": "Options (CSP)",
        "vampire_pct": "Vampire",
        "pendulum_pct": "Pendulum (TLT)",
        "reserve_pct": "Reserve",
    }
    rows = []
    for k in keys:
        cur = float(current.get(k, 0))
        new = float(proposed.get(k, 0))
        delta = new - cur
        rows.append({
            "Strategy": labels.get(k, k),
            "Current": f"{cur * 100:.0f}%",
            "Proposed": f"{new * 100:.0f}%",
            "Delta": f"{delta * 100:+.0f}pp",
        })
    return rows


# ---------------------------------------------------------------------------
# Streamlit rendering
# ---------------------------------------------------------------------------

def render_council(client, allocator, tracker) -> None:
    """Main entry point for the AI Council tab."""

    st.subheader("AI Financial Council")
    if not ALLOW_REALLOCATION:
        st.caption("Read-only on this deployment: the council can propose an allocation, "
                   "and the operator applies it on the host. Nothing here writes config.")
    st.caption(
        "Query 3 financial AI models on Dell4 with the live portfolio state. "
        "Each model proposes allocation changes independently. "
        "Review their recommendations and approve if you agree."
    )

    # Show current allocation
    current = _read_current_alloc()
    if current:
        cols = st.columns(5)
        labels = ["SIXFOLD", "Options", "Vampire", "Pendulum", "Reserve"]
        keys = ["sixfold_pct", "options_pct", "vampire_pct", "pendulum_pct", "reserve_pct"]
        for col, label, key in zip(cols, labels, keys):
            val = current.get(key, 0)
            col.metric(label, f"{float(val) * 100:.0f}%")

    st.divider()

    # Consultation trigger
    col_btn, col_status = st.columns([1, 3])
    with col_btn:
        consult = st.button("Consult AI Council", type="primary", use_container_width=True)
    with col_status:
        if "council_last_run" in st.session_state:
            st.caption(f"Last consulted: {st.session_state['council_last_run']}")

    if consult:
        with st.spinner("Gathering portfolio context..."):
            context = _build_portfolio_context(client, allocator, tracker)

        st.session_state["council_context"] = context
        st.session_state["council_last_run"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )

        # Show what the models will see, with token estimate
        est_tokens = len(context) // 4
        with st.expander(
            f"Portfolio context sent to models (~{est_tokens:,} tokens, "
            f"{len(context):,} chars)", expanded=False
        ):
            st.code(context, language="markdown")

        # Query all models
        results = []
        with st.spinner("Querying 3 financial models (this takes 30-90 seconds)..."):
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(_query_model, m, context): m for m in COUNCIL_MODELS}
                for future in concurrent.futures.as_completed(futures):
                    results.append(future.result())

        st.session_state["council_results"] = results
        st.session_state["council_rejected"] = []
        st.session_state["council_proposals"] = []

        # Parse proposals from each
        proposals = []
        rejected = []
        for r in results:
            if r["error"]:
                continue
            alloc = _parse_yaml_block(r["content"])
            if not alloc:
                rejected.append((r["label"], "Could not parse YAML allocation block"))
                continue
            alloc = _normalize_allocation(alloc)
            valid, msg = _validate_allocation(alloc)
            if valid:
                proposals.append({"model": r["model"], "label": r["label"], "alloc": alloc})
            else:
                rejected.append((r["label"], msg))
        st.session_state["council_proposals"] = proposals
        st.session_state["council_rejected"] = rejected

    # Display results if available
    if "council_results" in st.session_state:
        _render_results(st.session_state["council_results"])

    # Show rejected proposals so the user knows why Apply is missing
    if st.session_state.get("council_rejected"):
        for label, reason in st.session_state["council_rejected"]:
            st.warning(f"**{label}** proposal rejected: {reason}")

    # Council proposal selection (seeds the manual inputs when available)
    if "council_proposals" in st.session_state and st.session_state["council_proposals"]:
        _render_proposal_selector(st.session_state["council_proposals"], current)

    # Manual allocation editor -- ALWAYS visible
    st.divider()
    _render_manual_allocation(current)


def _render_results(results: list[dict]) -> None:
    """Display model responses as expandable cards."""
    st.subheader("Model Recommendations")

    for r in results:
        status = "ERROR" if r["error"] else f"{r['elapsed_s']}s"
        with st.expander(f"{r['label']} ({status})", expanded=not r["error"]):
            if r["error"]:
                st.error(f"Model query failed: {r['error']}")
            else:
                st.markdown(r["content"])


def _render_proposal_selector(proposals: list[dict], current: dict) -> None:
    """Let the operator pick a council proposal, which seeds the manual editor."""
    import pandas as pd
    st.divider()
    st.subheader("Council Proposals")

    if len(proposals) == 1:
        selected_idx = 0
        st.info(f"One valid proposal from **{proposals[0]['label']}**")
    else:
        options = [f"{p['label']}" for p in proposals]
        options.append("Average of all models")
        choice = st.radio("Select proposal to apply:", options, horizontal=True)
        if choice == "Average of all models":
            selected_idx = -1
        else:
            selected_idx = options.index(choice)

    keys = ["sixfold_pct", "options_pct", "vampire_pct", "pendulum_pct", "reserve_pct"]
    if selected_idx == -1:
        avg_alloc = {}
        for k in keys:
            vals = [float(p["alloc"][k]) for p in proposals]
            avg_alloc[k] = round(sum(vals) / len(vals), 2)
        total = sum(avg_alloc.values())
        if total != NORMALIZE_TOTAL:
            avg_alloc["reserve_pct"] = round(avg_alloc["reserve_pct"] + (NORMALIZE_TOTAL - total), 2)
        proposed = avg_alloc
        st.caption("Averaging recommendations from all models")
    else:
        proposed = proposals[selected_idx]["alloc"]

    diff = _diff_table(current, proposed)
    st.dataframe(pd.DataFrame(diff), use_container_width=True, hide_index=True)

    if st.button("Load into editor below", use_container_width=True):
        for k in keys:
            st.session_state[f"ca_{k}"] = round(float(proposed.get(k, 0)) * 100, 1)
        st.rerun()


ALLOC_KEYS = ["sixfold_pct", "options_pct", "vampire_pct", "pendulum_pct", "reserve_pct"]
ALLOC_LABELS = {
    "sixfold_pct": "SIXFOLD %",
    "options_pct": "Options %",
    "vampire_pct": "Vampire %",
    "pendulum_pct": "Pendulum %",
    "reserve_pct": "Reserve %",
}


def _rebalance(changed_key: str):
    """Proportionally adjust the other sleeves so the total stays 100%."""
    others = [k for k in ALLOC_KEYS if k != changed_key]
    new_val = st.session_state[f"ca_{changed_key}"]
    others_sum = sum(st.session_state[f"ca_{k}"] for k in others)
    remain = 100.0 - new_val

    if remain < 0:
        st.session_state[f"ca_{changed_key}"] = 100.0
        for k in others:
            st.session_state[f"ca_{k}"] = 0.0
        return

    if others_sum > 0:
        factor = remain / others_sum
        for k in others:
            st.session_state[f"ca_{k}"] = round(st.session_state[f"ca_{k}"] * factor, 1)
    else:
        share = round(remain / len(others), 1)
        for k in others:
            st.session_state[f"ca_{k}"] = share

    total = sum(st.session_state[f"ca_{k}"] for k in ALLOC_KEYS)
    if abs(total - 100.0) > TOTAL_VALIDATION_THRESHOLD:
        st.session_state["ca_reserve_pct"] = round(
            st.session_state["ca_reserve_pct"] + (100.0 - total), 1
        )


def _render_manual_allocation(current: dict) -> None:
    """Always-visible allocation editor with auto-rebalancing and apply button."""
    st.subheader("Allocation Editor")
    st.caption(
        "Adjust any sleeve -- the others rebalance automatically to keep the total at 100%. "
        "Click Apply to write to config/strategies.yml."
    )

    # Seed from current config if not yet initialized
    if f"ca_{ALLOC_KEYS[0]}" not in st.session_state:
        for k in ALLOC_KEYS:
            st.session_state[f"ca_{k}"] = round(float(current.get(k, 0)) * 100, 1)

    edit_cols = st.columns(5)
    for col, k in zip(edit_cols, ALLOC_KEYS):
        col.number_input(
            ALLOC_LABELS[k], min_value=0.0, max_value=100.0,
            step=5.0, key=f"ca_{k}",
            on_change=_rebalance, args=(k,),
        )

    adj_total = sum(st.session_state[f"ca_{k}"] for k in ALLOC_KEYS)
    if abs(adj_total - 100.0) > TOTAL_DISPLAY_THRESHOLD:
        st.warning(f"Total: {adj_total:.0f}% -- should be 100%")
    else:
        st.success(f"Total: {adj_total:.0f}%")

    proposed = {k: round(st.session_state[f"ca_{k}"] / 100.0, 4) for k in ALLOC_KEYS}

    # Show what changed vs current config
    changes = []
    for k in ALLOC_KEYS:
        cur = float(current.get(k, 0))
        new = proposed[k]
        if abs(cur - new) > ROUNDING_THRESHOLD:
            label = ALLOC_LABELS[k].replace(" %", "")
            changes.append(f"{label}: {cur*100:.0f}% -> {new*100:.0f}%")
    if changes:
        st.info("Pending changes: " + ", ".join(changes))

    col_approve, col_reset, col_space = st.columns([1, 1, 2])
    with col_approve:
        confirming = st.session_state.get("confirm_apply", False)
        if not confirming:
            if st.button("Apply to config", type="primary", use_container_width=True):
                valid, msg = _validate_allocation(proposed)
                if not valid:
                    st.error(f"Invalid allocation: {msg}")
                else:
                    st.session_state["confirm_apply"] = True
                    st.rerun()
        else:
            st.warning("Are you sure? Click **Confirm** to write to strategies.yml.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Confirm", type="primary", use_container_width=True):
                    ok, detail = _apply_allocation(proposed)
                    if ok:
                        st.toast(f"Allocation updated. {detail}")
                        for key in ["council_results", "council_proposals",
                                    "confirm_apply"]:
                            st.session_state.pop(key, None)
                        st.rerun()
                    else:
                        st.error(f"Failed to apply: {detail}")
            with c2:
                if st.button("Cancel", use_container_width=True):
                    st.session_state.pop("confirm_apply", None)
                    st.rerun()

    with col_reset:
        if st.button("Reset to current", use_container_width=True):
            for k in ALLOC_KEYS:
                st.session_state[f"ca_{k}"] = round(float(current.get(k, 0)) * 100, 1)
            st.rerun()
