"""Vampire hunt gate: finance LLMs veto picker candidates, never ticks.

Ticks stay deterministic. Hunt (pre-market pick and mid-session replacement)
asks Fino1-14B and Fin-R1 in parallel. A REJECT from either model drops the
name; abstain or a dead model leaves the quantitative rank in place.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from src.core.finance_advisor import _answer_for_verdict, _parse_verdict, _safe_call

HUNT_MODELS = (
    ("dell4-fino1-14b", "Financial Reasoner"),
    ("dell4-finance", "Finance Specialist"),
)

_HUNT_SYSTEM = (
    "You are selecting symbols for a bi-directional micro-scalper. "
    "The quantitative ranker already scored ATR/spread. "
    "Think if you need to. After any thinking, the visible answer must start "
    "with APPROVE or REJECT, then two sentences. "
    "APPROVE only if the name is fit for tight-spread two-sided noise "
    "(shortable, mean-reverting, not a gap-and-go single-name bet)."
)


@dataclass
class HuntDecision:
    symbol: str
    approved: bool
    summary: str
    votes: list[dict] = field(default_factory=list)


def hunt_llm_enabled() -> bool:
    flag = os.environ.get("VAMPIRE_HUNT_LLM", "1").strip().lower()
    return flag not in {"0", "false", "off", "no"}


def evaluate_hunt(symbol: str, metrics: dict | None = None) -> HuntDecision:
    """Veto gate for one hunt candidate. Never blocks on model failure."""
    if not hunt_llm_enabled():
        return HuntDecision(symbol, True, "LLM hunt disabled", [])

    prompt = f"Symbol: {symbol}\n"
    if metrics:
        for key, value in metrics.items():
            prompt += f"{key}: {value}\n"

    votes: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(HUNT_MODELS)) as pool:
        futs = {
            pool.submit(_safe_call, model, _HUNT_SYSTEM, prompt, f"hunt-{role}"): (model, role)
            for model, role in HUNT_MODELS
        }
        for fut in as_completed(futs):
            model, role = futs[fut]
            text = fut.result()
            if not text:
                votes.append({
                    "model": model, "role": role,
                    "verdict": "abstain", "reasoning": "model unavailable",
                })
                continue
            verdict = _parse_verdict(_answer_for_verdict(text), "APPROVE", "REJECT")
            votes.append({
                "model": model, "role": role,
                "verdict": verdict, "reasoning": text[:800],
            })

    rejects = [v for v in votes if v["verdict"] == "reject"]
    approved = not rejects
    if rejects:
        summary = (
            f"Hunt veto {symbol}: "
            + "; ".join(f"{v['role']} REJECT" for v in rejects)
        )
    else:
        responded = sum(1 for v in votes if v["verdict"] != "abstain")
        summary = (
            f"Hunt keep {symbol} ({responded} finance model(s) responded; "
            "no REJECT)"
        )

    from src.core.decision_log import record
    record(
        "vampire_picker", "hunt",
        symbol=symbol,
        thought=summary,
        decision="approved" if approved else "rejected",
        votes=votes,
    )
    return HuntDecision(symbol, approved, summary, votes)


def filter_hunt_candidates(
    ranked: list,
    *,
    count: int,
    enabled: bool,
    max_probes: int = 12,
    keep_symbols: set[str] | None = None,
) -> list:
    """Walk a scored list, dropping LLM vetoes, until `count` remain.

    Candidates are probed in one parallel wave so a hunt is one model
    round-trip, not N sequential council timeouts.
    """
    if count <= 0:
        return []
    if not enabled or not hunt_llm_enabled():
        return list(ranked[:count])

    wave = list(ranked[:max_probes])
    if not wave:
        return []

    def _metrics(item) -> dict:
        if not hasattr(item, "atr_pct"):
            return {}
        return {
            "price": round(float(item.price), 2),
            "spread_pct": f"{float(item.spread_pct) * 100:.3f}%",
            "atr_pct": f"{float(item.atr_pct) * 100:.2f}%",
            "gap_pct": f"{float(item.gap_pct) * 100:.2f}%",
            "score": round(float(item.score), 3),
        }

    decisions: dict[str, HuntDecision] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(wave))) as pool:
        futs = {
            pool.submit(
                evaluate_hunt,
                str(getattr(item, "symbol", item)),
                _metrics(item),
            ): item
            for item in wave
        }
        for fut in as_completed(futs):
            item = futs[fut]
            symbol = str(getattr(item, "symbol", item))
            try:
                decisions[symbol] = fut.result()
            except Exception:
                decisions[symbol] = HuntDecision(symbol, True, "hunt probe failed", [])

    chosen = []
    keep = {str(s).upper() for s in (keep_symbols or [])}
    for item in wave:
        symbol = str(getattr(item, "symbol", item))
        decision = decisions.get(symbol)
        if symbol.upper() in keep:
            chosen.append(item)
        elif decision is None or decision.approved:
            chosen.append(item)
        if len(chosen) >= count:
            break
    if not chosen and keep:
        # Mass veto of the wave: do not empty a live profitable book.
        chosen = [
            item for item in ranked
            if str(getattr(item, "symbol", item)).upper() in keep
        ][:count]
    return chosen
