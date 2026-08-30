from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuotedPut:
    """A quoted put option with all relevant fields for scoring."""
    symbol: str
    strike: float
    days_to_expiry: int
    bid: float
    ask: float
    open_interest: int
    delta: Optional[float] = None


@dataclass(frozen=True)
class ScoringConfig:
    """Configuration for evaluating cash-secured puts."""
    min_premium_pct: float
    min_open_interest: int
    max_delta: float
    min_dte: int
    max_dte: int


@dataclass(frozen=True)
class Evaluation:
    """Result of evaluating a put for trading suitability."""
    put: QuotedPut
    rejected: Optional[str]
    credit: float
    collateral: float
    return_on_capital: float
    annualized: float
    score: float


def evaluate(put: QuotedPut, cfg: ScoringConfig) -> Evaluation:
    """
    Evaluate a quoted put for trading suitability based on configured criteria.

    Returns an Evaluation object with score and rejection status.
    Rejected candidates have score 0.0 and a human-readable reason.
    """
    # Compute basic metrics
    credit = put.bid * 100.0
    collateral = put.strike * 100.0
    return_on_capital = credit / collateral if collateral != 0 else 0.0
    annualized = 0.0
    rejected = None

    # Check rejection conditions
    if put.bid <= 0:
        rejected = "No bid available"
    elif return_on_capital < cfg.min_premium_pct:
        rejected = f"Return on capital ({return_on_capital:.3f}) below minimum ({cfg.min_premium_pct})"
    elif put.open_interest < cfg.min_open_interest:
        rejected = f"Open interest ({put.open_interest}) below minimum ({cfg.min_open_interest})"
    elif put.delta is not None and put.delta < cfg.max_delta:
        rejected = f"Delta ({put.delta}) below maximum allowed ({cfg.max_delta})"
    elif put.days_to_expiry < cfg.min_dte or put.days_to_expiry > cfg.max_dte:
        rejected = f"Days to expiry ({put.days_to_expiry}) outside range [{cfg.min_dte}, {cfg.max_dte}]"

    # Calculate annualized return if valid
    if put.days_to_expiry > 0 and rejected is None:
        annualized = return_on_capital * 365.0 / put.days_to_expiry

    # Compute score
    score = 0.0
    if rejected is None:
        # Base score from annualized return
        score = annualized
        # Add liquidity factor to break ties
        score += put.open_interest * 1e-10

    return Evaluation(
        put=put,
        rejected=rejected,
        credit=credit,
        collateral=collateral,
        return_on_capital=return_on_capital,
        annualized=annualized,
        score=score
    )


def rank(puts: list[QuotedPut], cfg: ScoringConfig) -> list[Evaluation]:
    """
    Rank puts by their evaluation scores, excluding rejected candidates.

    Returns a list of Evaluations sorted by score descending.
    """
    evaluations = [evaluate(p, cfg) for p in puts]
    accepted = [e for e in evaluations if e.rejected is None]
    return sorted(accepted, key=lambda e: e.score, reverse=True)
