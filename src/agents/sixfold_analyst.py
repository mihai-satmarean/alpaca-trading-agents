"""SIXFOLD Analyst Agent -- equity screening and stock selection.

Runs periodic SIXFOLD analysis on the trading universe and provides
scored recommendations to the coordinator for options underlying selection
and position management.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from src.core.alpaca_client import AlpacaClient
from src.core.financial_data import FinancialDataProvider
from src.strategies.sixfold_engine import SixfoldEngine, SixfoldScore, ConfidenceTier

log = logging.getLogger(__name__)

SCAN_INTERVAL = 3600  # re-scan every hour
ET = ZoneInfo("America/New_York")


@dataclass
class SixfoldRecommendation:
    """Actionable output from the SIXFOLD analyst."""
    symbol: str
    score: float
    confidence: str
    action: str  # "buy_candidate", "hold", "dispose", "avoid", "out_of_scope"
    rationale: str
    lens_summary: dict[str, float] = field(default_factory=dict)


class SixfoldAnalystAgent:
    """Agent that runs SIXFOLD analysis and feeds scored recommendations.

    Responsibilities:
    - Periodically scan the universe through all six SIXFOLD lenses
    - Rank securities by composite score
    - Identify buy candidates (score >= 65) for CSP / covered call underlying
    - Flag disposal candidates (score < 45 or deteriorating)
    - Provide market context for regression calibration
    """

    def __init__(
        self,
        client: AlpacaClient,
        universe: list[str] | None = None,
        buy_threshold: float = 65.0,
        hold_threshold: float = 50.0,
        dispose_threshold: float = 40.0,
    ):
        self._client = client
        self.buy_threshold = float(buy_threshold)
        self.hold_threshold = float(hold_threshold)
        self.dispose_threshold = float(dispose_threshold)
        self._data_provider = FinancialDataProvider()
        self._engine = SixfoldEngine()
        self._universe = universe or [
            "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META",
            "JPM", "V", "JNJ", "UNH", "PG", "HD",
            "COST", "ABBV", "LLY", "MRK", "PEP", "KO",
        ]
        self._latest_scores: dict[str, SixfoldScore] = {}
        self._latest_recommendations: list[SixfoldRecommendation] = []
        self._last_scan: datetime | None = None
        self._running = False

    @property
    def scores(self) -> dict[str, SixfoldScore]:
        return self._latest_scores

    @property
    def recommendations(self) -> list[SixfoldRecommendation]:
        return self._latest_recommendations

    @property
    def last_scan(self) -> datetime | None:
        return self._last_scan

    def scan(self) -> list[SixfoldRecommendation]:
        """Run a full SIXFOLD scan of the universe.

        Returns sorted recommendations (highest score first).
        """
        log.info("SIXFOLD scan starting for %d symbols", len(self._universe))

        fundamentals = []
        for symbol in self._universe:
            try:
                data = self._data_provider.get_fundamentals(symbol)
                fundamentals.append(data)
            except Exception:
                log.warning("Failed to fetch fundamentals for %s, skipping", symbol)

        scores = self._engine.score_universe(fundamentals)

        self._latest_scores = {s.symbol: s for s in scores}
        self._latest_recommendations = [
            self._score_to_recommendation(s) for s in scores
        ]
        self._last_scan = datetime.now(ET)

        buy_candidates = [r for r in self._latest_recommendations if r.action == "buy_candidate"]
        log.info(
            "SIXFOLD scan complete: %d scored, %d buy candidates",
            len(scores),
            len(buy_candidates),
        )

        for s in scores[:5]:
            log.info("  %s: %.1f (%s)", s.symbol, s.composite_score, s.confidence.value)

        return self._latest_recommendations

    def _score_to_recommendation(self, score: SixfoldScore) -> SixfoldRecommendation:
        if not score.in_scope:
            return SixfoldRecommendation(
                symbol=score.symbol,
                score=score.composite_score,
                confidence=score.confidence.value,
                action="out_of_scope",
                rationale=score.out_of_scope_reason,
            )

        lens_summary = {lr.name: lr.score for lr in score.lens_results}

        if score.composite_score >= self.buy_threshold:
            action = "buy_candidate"
            rationale = self._build_buy_rationale(score)
        elif score.composite_score >= self.hold_threshold:
            action = "hold"
            rationale = "Adequate quality but not compelling at current price"
        elif score.composite_score >= self.dispose_threshold:
            action = "dispose"
            rationale = self._build_dispose_rationale(score)
        else:
            action = "avoid"
            rationale = "Below threshold -- capital destruction risk or unmeasurable"

        return SixfoldRecommendation(
            symbol=score.symbol,
            score=score.composite_score,
            confidence=score.confidence.value,
            action=action,
            rationale=rationale,
            lens_summary=lens_summary,
        )

    def _build_buy_rationale(self, score: SixfoldScore) -> str:
        strengths = []
        for lr in score.lens_results:
            if lr.score >= 70:
                strengths.append(lr.name)
        return f"Strong on: {', '.join(strengths)}" if strengths else "Composite score above threshold"

    def _build_dispose_rationale(self, score: SixfoldScore) -> str:
        weaknesses = []
        for lr in score.lens_results:
            if lr.score < 40:
                weaknesses.append(lr.name)
        return f"Weak on: {', '.join(weaknesses)}" if weaknesses else "Below quality threshold"

    def get_buy_candidates(self, min_score: float | None = None) -> list[str]:
        """Return symbols that score above the buy threshold."""
        floor = self.buy_threshold if min_score is None else float(min_score)
        return [
            r.symbol for r in self._latest_recommendations
            if r.action == "buy_candidate" and r.score >= floor
        ]

    def get_disposal_candidates(self) -> list[str]:
        """Return symbols flagged for disposal."""
        return [
            r.symbol for r in self._latest_recommendations
            if r.action in ("dispose", "avoid")
        ]

    def get_score(self, symbol: str) -> SixfoldScore | None:
        return self._latest_scores.get(symbol)

    def get_report(self, symbol: str) -> str | None:
        score = self._latest_scores.get(symbol)
        if not score:
            return None
        return self._engine.format_report(score)

    def run_loop(self):
        """Background loop: scan periodically while market is relevant."""
        self._running = True
        log.info("SIXFOLD Analyst agent starting")

        # Initial scan immediately
        try:
            self.scan()
        except Exception:
            log.exception("Initial SIXFOLD scan failed")

        while self._running:
            try:
                time.sleep(SCAN_INTERVAL)
                if not self._running:
                    break
                log.info("SIXFOLD periodic rescan triggered")
                self.scan()
            except Exception:
                log.exception("SIXFOLD scan cycle error")
                time.sleep(60)

    def stop(self):
        self._running = False
