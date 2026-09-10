"""SIXFOLD exit rules, ported from the live Tradier deployment.

Source: Options-Trader/sixfold_trader/src/exits.mjs, live on Lambda
sixfold-trader since 2026-09-01. Four rules, in this precedence:

  1. Time stop (default 183 days). Checked first, independent of price, so
     it still fires when a quote is unavailable -- an exit rule that stops
     working during a data outage is not an exit rule. Scoped to symbols
     this process's own registry recorded as opened by SIXFOLD (see
     sixfold_registry.py); a symbol with no recorded age never triggers
     this rule, which is the safe direction.
  2. Take-profit (default +35% unrealized). Reads Alpaca's own
     unrealized_plpc directly rather than recomputing it from cost basis.
     Scoped to registry-tracked symbols for the same reason as the time
     stop.
  3. NO stop-loss. Deliberate, matching the source's own SPEC 3.7. A test
     sweeps unrealized_plpc from -10% through -99% and asserts none of it
     alone triggers an exit.
  4. Score-dispose. This is the PRE-EXISTING rule, already live in
     sixfold_executor.py and driven by sixfold_analyst.py's own action
     band (get_disposal_candidates()). It is NOT re-derived here: the
     original code deliberately decoupled "is this symbol in the analyst's
     disposal set" from "can we read a fresh score number for it right
     now" -- "the sale is driven by the analyst's action band, not by this
     number, so an unreadable score must not block the exit." Recomputing
     a `score < threshold` check inside this module broke exactly that
     invariant on first attempt (a symbol whose score object was
     momentarily unreadable stopped being disposed even though the
     analyst had already flagged it), so this module takes the analyst's
     decision as a plain bool rather than re-deriving it from a number
     that can legitimately be missing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExitCfg:
    take_profit_pct: float = 35.0
    time_stop_days: float = 183.0


DEFAULT_CFG = ExitCfg()


@dataclass(frozen=True)
class ExitDecision:
    exit: bool
    reason: str | None = None


def exit_decision(unrealized_plpc: float | None, score_flagged: bool,
                  age_days: float | None, registry_tracked: bool,
                  cfg: ExitCfg = DEFAULT_CFG) -> ExitDecision:
    """Decide whether one held position should be exited.

    unrealized_plpc: Alpaca's own field, a fraction (0.35 == +35%), or None
        if unreadable this cycle.
    score_flagged: whether the analyst's own action band already flagged
        this symbol for disposal (get_disposal_candidates()) -- an
        already-decided bool, not re-derived here.
    age_days: from the registry, or None if unknown.
    registry_tracked: whether this symbol has a recorded SIXFOLD open. Gates
        BOTH new rules, at this boundary rather than trusting every caller
        to remember it: a symbol this process never registered as its own
        buy (a CSP assignment, a pre-existing position, a name whose
        open-order never got confirmed) must not be sold on price or age,
        only ever on the pre-existing score rule.
    """
    if registry_tracked and age_days is not None and age_days >= cfg.time_stop_days:
        return ExitDecision(True, f"time stop ({age_days:.0f}d >= {cfg.time_stop_days:.0f}d)")

    if registry_tracked and unrealized_plpc is not None:
        pct = unrealized_plpc * 100.0
        if pct >= cfg.take_profit_pct:
            return ExitDecision(True, f"profit target +{pct:.1f}%")
        # No stop-loss branch here, deliberately (SPEC 3.7).

    if score_flagged:
        return ExitDecision(True, "score below the hold band")

    return ExitDecision(False)
