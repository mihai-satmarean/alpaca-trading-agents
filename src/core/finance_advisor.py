"""Advisory Council: 3-model consensus for trade decisions.

Three LLMs with different training biases evaluate every trade candidate
in parallel. A trade proceeds only when at least 2 of 3 advisors agree.
Each advisor's reasoning is logged for full transparency.

The council is best-effort: if a model is unreachable it counts as an
abstention, not a veto. If fewer than 2 advisors respond, the trade
proceeds on the deterministic signal alone (no AI gate).

Models:
  - dell4-finance  (Fin-R1 7B)     -- financial domain specialist
  - dell4-chat     (Qwen3.6-35B)   -- general reasoning, broad context
  - dell4-qwen38   (Qwen3.8-27B)   -- strong general, multimodal capable
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

try:
    import certifi
    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = None

COUNCIL_MODELS = [
    ("dell4-finance", "Finance Specialist"),
    ("dell4-chat", "General Strategist"),
    ("dell4-qwen38", "Risk Analyst"),
]

CONSENSUS_THRESHOLD = 2  # minimum agreeing advisors to pass


@dataclass
class AdvisorOpinion:
    model: str
    role: str
    verdict: str        # "approve" / "reject" / "abstain"
    reasoning: str
    responded: bool


@dataclass
class CouncilDecision:
    action: str                              # "buy" / "sell_put" / etc.
    symbol: str
    approved: bool
    votes_for: int
    votes_against: int
    abstentions: int
    opinions: list[AdvisorOpinion] = field(default_factory=list)
    summary: str = ""

    def log_decision(self) -> None:
        status = "APPROVED" if self.approved else "REJECTED"
        log.info(
            "Council %s %s %s (%d for, %d against, %d abstain)",
            status, self.action, self.symbol,
            self.votes_for, self.votes_against, self.abstentions,
        )
        for op in self.opinions:
            log.info("  [%s] %s: %s", op.role, op.verdict.upper(), op.reasoning[:100])


def _llm_call(model: str, system: str, user: str,
              max_tokens: int = 350, temperature: float = 0.2) -> str:
    base = os.environ.get("OPENAI_BASE_URL", "http://100.69.81.102:4000/v1").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LITELLM_KEY") or ""
    timeout = float(os.environ.get("COUNCIL_TIMEOUT", "25"))

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()

    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        payload = json.load(r)
    return payload["choices"][0]["message"]["content"].strip()


def _parse_verdict(text: str, approve_word: str, reject_word: str) -> str:
    upper = text.upper()
    if upper.startswith(approve_word.upper()):
        return "approve"
    if upper.startswith(reject_word.upper()):
        return "reject"
    if approve_word.upper() in upper[:60]:
        return "approve"
    if reject_word.upper() in upper[:60]:
        return "reject"
    return "approve"  # ambiguous response treated as weak approval


def _query_advisor(model: str, role: str, system: str, prompt: str,
                   approve_word: str, reject_word: str) -> AdvisorOpinion:
    try:
        text = _llm_call(model, system, prompt)
        verdict = _parse_verdict(text, approve_word, reject_word)
        return AdvisorOpinion(
            model=model, role=role, verdict=verdict,
            reasoning=text[:500], responded=True,
        )
    except Exception:
        log.warning("Advisor %s (%s) unavailable", role, model, exc_info=True)
        return AdvisorOpinion(
            model=model, role=role, verdict="abstain",
            reasoning="model unavailable", responded=False,
        )


def _run_council(action: str, symbol: str, system_prompts: dict[str, str],
                 user_prompt: str, approve_word: str = "APPROVE",
                 reject_word: str = "REJECT") -> CouncilDecision:
    """Query all council members in parallel and tally votes."""
    opinions: list[AdvisorOpinion] = []

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        for model, role in COUNCIL_MODELS:
            system = system_prompts.get(role, system_prompts.get("default", ""))
            fut = pool.submit(_query_advisor, model, role, system, user_prompt,
                              approve_word, reject_word)
            futures[fut] = role

        for fut in as_completed(futures, timeout=35):
            opinions.append(fut.result())

    votes_for = sum(1 for o in opinions if o.verdict == "approve")
    votes_against = sum(1 for o in opinions if o.verdict == "reject")
    abstentions = sum(1 for o in opinions if o.verdict == "abstain")

    responded = votes_for + votes_against
    if responded < CONSENSUS_THRESHOLD:
        approved = True
        summary = f"Insufficient quorum ({responded} responded); proceeding on deterministic signal"
    else:
        approved = votes_for >= CONSENSUS_THRESHOLD
        summary = (
            f"Council {'approved' if approved else 'rejected'}: "
            f"{votes_for} for, {votes_against} against, {abstentions} abstain"
        )

    decision = CouncilDecision(
        action=action, symbol=symbol, approved=approved,
        votes_for=votes_for, votes_against=votes_against,
        abstentions=abstentions, opinions=opinions, summary=summary,
    )
    decision.log_decision()
    return decision


# ---------------------------------------------------------------------------
# Council sessions for each trade type
# ---------------------------------------------------------------------------

_EQUITY_BUY_PROMPTS = {
    "Finance Specialist": (
        "You are a financial analyst specializing in equity valuation. "
        "A quantitative scoring system recommends buying this stock. "
        "Assess the recommendation based on the fundamentals provided. "
        "Start your answer with APPROVE or REJECT, then give 2-3 sentences of reasoning. "
        "Focus on: valuation, earnings quality, and balance sheet strength."
    ),
    "General Strategist": (
        "You are a portfolio strategist reviewing an equity buy signal. "
        "Start your answer with APPROVE or REJECT, then give 2-3 sentences of reasoning. "
        "Focus on: market timing, sector momentum, and whether this fits a diversified portfolio."
    ),
    "Risk Analyst": (
        "You are a risk analyst evaluating a proposed equity purchase. "
        "Start your answer with APPROVE or REJECT, then give 2-3 sentences of reasoning. "
        "Focus on: downside risk, volatility, concentration risk, and potential catalysts "
        "that could move the stock against us."
    ),
}


def evaluate_equity_buy(symbol: str, score: float,
                        fundamentals: dict | None = None) -> CouncilDecision:
    """Council evaluates whether to buy an equity position."""
    prompt = f"Symbol: {symbol}\nSIXFOLD composite score: {score:.1f}/100\n"
    if fundamentals:
        for k, v in fundamentals.items():
            prompt += f"{k}: {v}\n"
    return _run_council("buy", symbol, _EQUITY_BUY_PROMPTS, prompt)


_CSP_PROMPTS = {
    "Finance Specialist": (
        "You are a quantitative options analyst. Assess whether selling this "
        "cash-secured put is a sound risk/reward trade. "
        "Start with APPROVE or REJECT, then 2-3 sentences. "
        "Focus on: premium adequacy, whether you'd want to own the stock at "
        "the effective cost basis, and earnings/event risk during the contract."
    ),
    "General Strategist": (
        "You are a portfolio strategist evaluating an options income trade. "
        "Start with APPROVE or REJECT, then 2-3 sentences. "
        "Focus on: whether this trade aligns with income generation goals, "
        "the opportunity cost of the collateral, and market conditions."
    ),
    "Risk Analyst": (
        "You are a risk analyst reviewing a cash-secured put sale. "
        "Start with APPROVE or REJECT, then 2-3 sentences. "
        "Focus on: maximum loss scenario, assignment probability, "
        "and whether the collateral is proportional to the risk taken."
    ),
}


def evaluate_csp(symbol: str, strike: float, dte: int,
                 premium: float, stock_price: float,
                 extra_context: str = "") -> CouncilDecision:
    """Council evaluates whether to sell a cash-secured put."""
    prompt = (
        f"Symbol: {symbol}\n"
        f"Current price: ${stock_price:.2f}\n"
        f"Put strike: ${strike:.2f} ({(strike/stock_price - 1)*100:+.1f}% from spot)\n"
        f"DTE: {dte} days\n"
        f"Premium received: ${premium:.2f} per share\n"
        f"Effective cost basis if assigned: ${strike - premium:.2f}\n"
        f"Annualized yield on collateral: "
        f"{(premium / strike) * (365 / max(dte, 1)) * 100:.1f}%\n"
    )
    if extra_context:
        prompt += f"\nAdditional context: {extra_context}\n"
    return _run_council("sell_put", symbol, _CSP_PROMPTS, prompt)


# ---------------------------------------------------------------------------
# Single-model utilities (non-council, for narration and briefing)
# ---------------------------------------------------------------------------

def _safe_call(model: str, system: str, user: str, label: str, **kw) -> str | None:
    try:
        result = _llm_call(model, system, user, **kw)
        if result:
            log.debug("finance_advisor [%s]: %s", label, result[:120])
        return result
    except Exception:
        log.warning("finance_advisor [%s] unavailable on %s", label, model, exc_info=True)
        return None


_RISK_SYSTEM = (
    "You are a risk management analyst. Explain in 2-3 sentences why the circuit "
    "breaker activated and what the portfolio should do next. Be factual and concise."
)


def explain_risk_event(event_type: str, details: dict) -> str | None:
    """Single-model explanation for risk events (no council needed)."""
    prompt = f"Risk event: {event_type}\n"
    for k, v in details.items():
        prompt += f"{k}: {v}\n"
    text = _safe_call("dell4-finance", _RISK_SYSTEM, prompt, f"risk-{event_type}")
    if not text:
        text = _safe_call("dell4-chat", _RISK_SYSTEM, prompt, f"risk-{event_type}-fallback")
    return text


_MARKET_SYSTEM = (
    "You are a market analyst. Given the current conditions, provide a brief "
    "1-2 sentence market assessment relevant to the trading strategies described. "
    "Focus on: volatility regime, sector rotation, and risk-on/risk-off signals."
)


def market_briefing(equity: float, daily_pnl: float,
                    positions: list[str]) -> str | None:
    """Single-model market briefing for the dashboard."""
    prompt = (
        f"Portfolio equity: ${equity:,.0f}\n"
        f"Daily P&L: ${daily_pnl:+,.0f}\n"
        f"Current positions: {', '.join(positions[:15]) if positions else 'none'}\n"
        f"Strategies active: SIXFOLD equity selection, CSP income, micro-scalping\n"
    )
    return _safe_call("dell4-finance", _MARKET_SYSTEM, prompt, "briefing")
