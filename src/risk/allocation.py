"""Capital allocation: enforces the configured 80/15/5 split across strategies.

The split itself is Mihai's design and is unchanged. What changed is that the
numbers are now (a) read from config/strategies.yml rather than hardcoded, and
(b) measured against capital actually committed rather than position market
value.

Why (b) matters. A cash-secured put on SPY at a $450 strike ties up $45,000 of
collateral, but the position's market value is only the option's mark, maybe
-$300. Summing market value therefore understated the options sleeve by roughly
two orders of magnitude, so an $80,000 budget was never reached and the cap
never bound. The agent would keep selling puts every scan cycle until the broker
refused for want of buying power.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from src.core.config import get_config
from src.core.position_tracker import PositionTracker

log = logging.getLogger(__name__)

# OCC option symbol: root, YYMMDD, C/P, strike in thousandths (8 digits).
# e.g. SPY241220P00450000 -> SPY, 2024-12-20, put, strike 450.0
OCC_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


@dataclass(frozen=True)
class ParsedOption:
    root: str
    contract_type: str  # "call" | "put"
    strike: float


def parse_occ(symbol: str) -> ParsedOption | None:
    """Parse an OCC option symbol. Returns None for equity symbols."""
    m = OCC_RE.match(symbol.strip().upper())
    if not m:
        return None
    return ParsedOption(
        root=m.group("root"),
        contract_type="call" if m.group("cp") == "C" else "put",
        strike=int(m.group("strike")) / 1000.0,
    )


def capital_committed(symbol: str, position: dict) -> float:
    """Capital a position actually ties up.

    Short put  -> strike x 100 x |qty|, the cash-secured collateral.
    Short call -> 0; the covering shares are counted as their own equity
                  position, and charging both would double-count the sleeve.
    Long option-> the debit paid (market value).
    Equity     -> market value.
    """
    market_value = abs(float(position.get("market_value", 0.0)))
    qty = float(position.get("qty", 0.0))

    parsed = parse_occ(symbol)
    if parsed is None:
        return market_value

    if qty < 0:  # short
        if parsed.contract_type == "put":
            return parsed.strike * 100.0 * abs(qty)
        return 0.0

    return market_value


@dataclass
class AllocationConfig:
    options_pct: float = 0.80
    vampire_pct: float = 0.15
    reserve_pct: float = 0.05
    sixfold_pct: float = 0.0
    pendulum_pct: float = 0.0

    @classmethod
    def from_config(cls) -> AllocationConfig:
        cfg = get_config()
        return cls(
            options_pct=cfg.options_pct,
            vampire_pct=cfg.vampire_pct,
            reserve_pct=cfg.reserve_pct,
            sixfold_pct=cfg.sixfold_pct,
            pendulum_pct=cfg.pendulum_pct,
        )


@dataclass
class AllocationBudget:
    total_equity: float
    options_budget: float
    vampire_budget: float
    reserve_target: float
    options_used: float
    vampire_used: float
    options_available: float
    vampire_available: float
    unattributed_used: float = 0.0
    sixfold_budget: float = 0.0
    pendulum_budget: float = 0.0
    pendulum_used: float = 0.0


class AllocationManager:
    """Enforces the configured capital split between strategies."""

    def __init__(
        self,
        tracker: PositionTracker,
        config: AllocationConfig | None = None,
        vampire_symbols: list[str] | None = None,
    ):
        self._tracker = tracker
        self.config = config or AllocationConfig()
        cfg = get_config()
        self._vampire_symbols = {
            s.upper() for s in (vampire_symbols or cfg.vampire_symbols)
        }
        # Pendulum holds one ticker. Without its own bucket that holding falls
        # to `unattributed`, which is where SIXFOLD's equity also lands, and
        # the two sleeves would then be charged for each other's positions.
        self._pendulum_symbol = cfg.pendulum_symbol

    def _classify(self, symbol: str, position: dict) -> str:
        """Which sleeve a position belongs to: options | vampire | unattributed.

        Equity in a vampire symbol is ambiguous by construction, because the
        vampire and options universes overlap (both trade SPY/QQQ/AAPL). Shares
        assigned from a put, or held to write calls against, look identical to
        scalper inventory from the broker's side. We charge overlapping equity
        to the vampire sleeve, which is the smaller budget and therefore the
        conservative choice, and surface anything we cannot place at all as
        `unattributed` rather than silently folding it into a sleeve.
        """
        if parse_occ(symbol) is not None:
            return "options"
        if symbol.upper() in self._vampire_symbols:
            return "vampire"
        if symbol.upper() == self._pendulum_symbol:
            return "pendulum"
        return "unattributed"

    def get_budget(self) -> AllocationBudget:
        snapshot = self._tracker.get_snapshot()
        equity = snapshot.equity

        options_budget = equity * self.config.options_pct
        vampire_budget = equity * self.config.vampire_pct
        reserve_target = equity * self.config.reserve_pct

        used = {"options": 0.0, "vampire": 0.0, "pendulum": 0.0, "unattributed": 0.0}
        for sym, pos in snapshot.positions.items():
            used[self._classify(sym, pos)] += capital_committed(sym, pos)

        return AllocationBudget(
            total_equity=equity,
            options_budget=options_budget,
            vampire_budget=vampire_budget,
            reserve_target=reserve_target,
            options_used=used["options"],
            vampire_used=used["vampire"],
            options_available=max(0.0, options_budget - used["options"]),
            vampire_available=max(0.0, vampire_budget - used["vampire"]),
            unattributed_used=used["unattributed"],
            sixfold_budget=equity * self.config.sixfold_pct,
            pendulum_budget=equity * self.config.pendulum_pct,
            pendulum_used=used["pendulum"],
        )

    def can_allocate_options(self, amount: float) -> bool:
        budget = self.get_budget()
        if amount > budget.options_available:
            log.info(
                "Options allocation denied: $%.0f requested, $%.0f available "
                "($%.0f of $%.0f budget in use)",
                amount,
                budget.options_available,
                budget.options_used,
                budget.options_budget,
            )
            return False
        return True

    def can_allocate_vampire(self, amount: float) -> bool:
        budget = self.get_budget()
        if amount > budget.vampire_available:
            log.info(
                "Vampire allocation denied: $%.0f requested, $%.0f available",
                amount,
                budget.vampire_available,
            )
            return False
        return True

    def needs_rebalance(self, tolerance_pct: float = 0.05) -> bool:
        """True when a sleeve has drifted beyond tolerance of its target."""
        budget = self.get_budget()
        if budget.total_equity == 0:
            return False

        options_drift = abs(budget.options_used / budget.total_equity - self.config.options_pct)
        vampire_drift = abs(budget.vampire_used / budget.total_equity - self.config.vampire_pct)

        return options_drift > tolerance_pct or vampire_drift > tolerance_pct


# ---------------------------------------------------------------------------
# 4-Tier Profit Cascade
# ---------------------------------------------------------------------------

@dataclass
class CascadeTier:
    """One tier in the profit cascade."""
    name: str
    risk_level: int            # 1 (highest risk) to 4 (lowest risk)
    promotion_threshold: float # profit above which surplus cascades down
    allocation_pct: float      # current sleeve percentage of total equity


@dataclass
class CascadeConfig:
    """Thresholds for the 4-tier profit waterfall.

    Tier 1 (Vampire Scalper): highest risk, generates initial profits.
    Tier 2 (Bull Call Spreads): moderate risk, defined-risk options plays.
    Tier 3 (SIXFOLD + CSP): lower risk, equity positions and premium selling.
    Tier 4 (Cash Reserve): zero risk, safety buffer.

    When a tier's realized P&L exceeds its promotion_threshold, the surplus
    moves to the next tier's budget. This creates a natural flow from high-risk
    to low-risk as the account grows.
    """
    tier1_promotion: float = 100.0   # vampire profits above $100 cascade
    tier2_promotion: float = 200.0   # bull spread profits above $200 cascade
    tier3_promotion: float = 500.0   # SIXFOLD+CSP profits above $500 cascade
    cascade_pct: float = 0.50        # fraction of surplus that cascades (keep some for growth)


class ProfitCascade:
    """Manages the 4-tier profit waterfall from high-risk to low-risk."""

    def __init__(
        self,
        allocator: AllocationManager,
        config: CascadeConfig | None = None,
    ):
        self._allocator = allocator
        self.cfg = config or CascadeConfig()
        self._tier_pnl: dict[str, float] = {
            "vampire": 0.0,
            "bull_spread": 0.0,
            "sixfold_csp": 0.0,
            "reserve": 0.0,
        }
        self._cascade_log: list[dict] = []

    @property
    def tier_pnl(self) -> dict[str, float]:
        return dict(self._tier_pnl)

    @property
    def cascade_history(self) -> list[dict]:
        return list(self._cascade_log)

    def record_pnl(self, tier: str, amount: float) -> None:
        """Record realized P&L for a tier."""
        if tier not in self._tier_pnl:
            log.warning("Unknown cascade tier: %s", tier)
            return
        self._tier_pnl[tier] += amount

    def cascade_profits(self) -> list[dict]:
        """Run the cascade: move surplus profits from higher-risk to lower-risk tiers.

        Returns a list of cascade actions taken.
        """
        actions: list[dict] = []

        thresholds = [
            ("vampire", "bull_spread", self.cfg.tier1_promotion),
            ("bull_spread", "sixfold_csp", self.cfg.tier2_promotion),
            ("sixfold_csp", "reserve", self.cfg.tier3_promotion),
        ]

        for source, target, threshold in thresholds:
            pnl = self._tier_pnl[source]
            if pnl <= threshold:
                continue

            surplus = pnl - threshold
            cascade_amount = surplus * self.cfg.cascade_pct

            self._tier_pnl[source] -= cascade_amount
            self._tier_pnl[target] += cascade_amount

            action = {
                "from": source,
                "to": target,
                "amount": round(cascade_amount, 2),
                "source_remaining": round(self._tier_pnl[source], 2),
                "target_new_total": round(self._tier_pnl[target], 2),
            }
            actions.append(action)
            self._cascade_log.append(action)

            log.info(
                "Cascade: $%.2f from %s -> %s (surplus $%.2f above $%.0f threshold)",
                cascade_amount, source, target, surplus, threshold,
            )

        return actions

    def get_adjusted_budgets(self) -> dict[str, float]:
        """Return budget adjustments from cascaded profits.

        These are additive amounts on top of the base allocation percentages.
        """
        return {
            "vampire_extra": self._tier_pnl["vampire"],
            "bull_spread_extra": self._tier_pnl["bull_spread"],
            "sixfold_csp_extra": self._tier_pnl["sixfold_csp"],
            "reserve_extra": self._tier_pnl["reserve"],
        }

    def summary(self) -> str:
        """Human-readable cascade status."""
        lines = ["Profit Cascade Status:"]
        tier_names = {
            "vampire": "Tier 1 (Vampire Scalper)",
            "bull_spread": "Tier 2 (Bull Call Spreads)",
            "sixfold_csp": "Tier 3 (SIXFOLD + CSP)",
            "reserve": "Tier 4 (Cash Reserve)",
        }
        for key, name in tier_names.items():
            lines.append(f"  {name}: ${self._tier_pnl[key]:+.2f}")
        lines.append(f"  Total cascaded: {len(self._cascade_log)} transfers")
        return "\n".join(lines)
