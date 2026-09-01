"""Advisory Council: 4-model consensus for trade decisions.

Four LLMs with different training biases evaluate every trade candidate
in parallel. A trade proceeds only when at least 2 of 4 advisors agree.
Each advisor's reasoning is logged for full transparency.

The council is best-effort: if a model is unreachable it counts as an
abstention, not a veto. If fewer than 2 advisors respond, the trade
proceeds on the deterministic signal alone (no AI gate).

Models:
  - dell4-finance   (Fin-R1 7B)     -- financial domain specialist
  - dell4-fino1-14b (Fino1 14B)     -- financial reasoning, FinQA SOTA
  - dell4-chat      (Qwen3.6-35B)   -- general reasoning, broad context
  - dell4-qwen38    (Qwen3.8-27B)   -- strong general, multimodal capable
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
    ("dell4-fino1-14b", "Financial Reasoner"),
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


_APPROVE_NEGATIONS = ("NOT APPROVE", "N'T APPROVE", "CANNOT APPROVE",
                      "NEVER APPROVE", "WOULD NOT APPROVE", "DO NOT APPROVE")


def _parse_verdict(text: str, approve_word: str, reject_word: str) -> str:
    """Strict-polarity parse of an advisor's verdict.

    Two rules, both learned the expensive way today:

    Ambiguity is an ABSTENTION, never an approval. An unparseable answer that
    counts as approve silently neutralises the gate while the log shows a vote
    that was never cast; abstaining is honest and simply reduces quorum, which
    falls back to the deterministic signal.

    In the substring phase REJECT is checked FIRST. "I would NOT APPROVE this"
    contains APPROVE, so approve-first parsing inverts an explicit negative.
    Errors in the reject direction only skip one buy, which is the cheap
    direction for a veto gate to be wrong in.
    """
    upper = text.upper()
    head = upper[:80]
    if upper.startswith(reject_word.upper()):
        return "reject"
    if upper.startswith(approve_word.upper()):
        return "approve"
    if reject_word.upper() in head:
        return "reject"
    if approve_word.upper() in head:
        if any(neg in head for neg in _APPROVE_NEGATIONS):
            return "abstain"
        return "approve"
    return "abstain"


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

    wall_timeout = float(os.environ.get("COUNCIL_TIMEOUT", "25")) + 10

    with ThreadPoolExecutor(max_workers=len(COUNCIL_MODELS)) as pool:
        futures = {}
        for model, role in COUNCIL_MODELS:
            system = system_prompts.get(role, system_prompts.get("default", ""))
            fut = pool.submit(_query_advisor, model, role, system, user_prompt,
                              approve_word, reject_word)
            futures[fut] = role

        done: set = set()
        try:
            for fut in as_completed(futures, timeout=wall_timeout):
                opinions.append(fut.result())
                done.add(fut)
        except TimeoutError:
            # An advisor overrunning the wall must cost an abstention, not the
            # cycle: an uncaught TimeoutError here propagates into run_cycle and
            # kills every remaining candidate, which is precisely the "AI
            # failure blocks trading" this council promises not to cause.
            for fut, role in futures.items():
                if fut not in done:
                    fut.cancel()
                    opinions.append(AdvisorOpinion(
                        model="", role=role, verdict="abstain",
                        reasoning="advisor exceeded the council wall timeout",
                        responded=False,
                    ))

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
    "Financial Reasoner": (
        "You are a financial reasoning expert trained on financial QA datasets. "
        "Evaluate this equity buy signal using quantitative reasoning. "
        "Start your answer with APPROVE or REJECT, then give 2-3 sentences of reasoning. "
        "Focus on: numerical consistency of the score, P/E and growth rate alignment, "
        "and whether the valuation metrics support the buy thesis."
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
    "Financial Reasoner": (
        "You are a financial reasoning expert. Evaluate this cash-secured put "
        "using quantitative analysis. "
        "Start with APPROVE or REJECT, then 2-3 sentences. "
        "Focus on: annualized return vs risk-free rate, implied volatility "
        "rank, and whether the premium compensates for assignment risk."
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


# ---------------------------------------------------------------------------
# Council Metrics Tracker
# ---------------------------------------------------------------------------

class CouncilMetrics:
    """Tracks approval rates, model agreement, and outcome correlation."""

    def __init__(self):
        self._decisions: list[dict] = []

    def record(self, decision: CouncilDecision, outcome_pnl: float | None = None) -> None:
        """Record a council decision and optionally its trading outcome."""
        per_model: dict[str, str] = {}
        for op in decision.opinions:
            per_model[op.model] = op.verdict

        self._decisions.append({
            "symbol": decision.symbol,
            "action": decision.action,
            "approved": decision.approved,
            "votes_for": decision.votes_for,
            "votes_against": decision.votes_against,
            "abstentions": decision.abstentions,
            "per_model": per_model,
            "outcome_pnl": outcome_pnl,
        })

    def update_outcome(self, symbol: str, pnl: float) -> None:
        """Backfill outcome P&L for the most recent decision on a symbol."""
        for d in reversed(self._decisions):
            if d["symbol"] == symbol and d["outcome_pnl"] is None:
                d["outcome_pnl"] = pnl
                break

    @property
    def total_decisions(self) -> int:
        return len(self._decisions)

    @property
    def approval_rate(self) -> float:
        if not self._decisions:
            return 0.0
        return sum(1 for d in self._decisions if d["approved"]) / len(self._decisions)

    def model_agreement_rate(self) -> dict[str, float]:
        """For each model, how often it voted with the majority."""
        if not self._decisions:
            return {}

        model_agree: dict[str, int] = {}
        model_total: dict[str, int] = {}

        for d in self._decisions:
            majority = "approve" if d["approved"] else "reject"
            for model, verdict in d["per_model"].items():
                model_total[model] = model_total.get(model, 0) + 1
                if verdict == majority:
                    model_agree[model] = model_agree.get(model, 0) + 1

        return {
            m: model_agree.get(m, 0) / model_total[m]
            for m in model_total
        }

    def outcome_correlation(self) -> dict[str, dict]:
        """Correlate council decisions with trading outcomes.

        Returns stats for approved-and-profitable, approved-and-lost, etc.
        """
        stats = {
            "approved_profit": 0,
            "approved_loss": 0,
            "rejected_would_profit": 0,
            "rejected_would_loss": 0,
            "no_outcome": 0,
        }
        for d in self._decisions:
            if d["outcome_pnl"] is None:
                stats["no_outcome"] += 1
            elif d["approved"] and d["outcome_pnl"] > 0:
                stats["approved_profit"] += 1
            elif d["approved"] and d["outcome_pnl"] <= 0:
                stats["approved_loss"] += 1
            elif not d["approved"] and d["outcome_pnl"] is not None and d["outcome_pnl"] > 0:
                stats["rejected_would_profit"] += 1
            elif not d["approved"] and d["outcome_pnl"] is not None:
                stats["rejected_would_loss"] += 1
        return stats

    def summary(self) -> str:
        """Human-readable metrics summary."""
        lines = [
            f"Council Metrics ({self.total_decisions} decisions):",
            f"  Approval rate: {self.approval_rate:.1%}",
        ]
        agreement = self.model_agreement_rate()
        if agreement:
            lines.append("  Model agreement with majority:")
            for model, rate in sorted(agreement.items()):
                lines.append(f"    {model}: {rate:.1%}")
        corr = self.outcome_correlation()
        if corr["no_outcome"] < self.total_decisions:
            lines.append("  Outcome correlation:")
            lines.append(f"    Approved & profitable: {corr['approved_profit']}")
            lines.append(f"    Approved & lost: {corr['approved_loss']}")
            lines.append(f"    Rejected (would profit): {corr['rejected_would_profit']}")
            lines.append(f"    Rejected (would lose): {corr['rejected_would_loss']}")
        return "\n".join(lines)


# Global metrics instance
council_metrics = CouncilMetrics()
