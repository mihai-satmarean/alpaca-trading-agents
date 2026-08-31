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

    @classmethod
    def from_config(cls) -> AllocationConfig:
        cfg = get_config()
        return cls(
            options_pct=cfg.options_pct,
            vampire_pct=cfg.vampire_pct,
            reserve_pct=cfg.reserve_pct,
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
        return "unattributed"

    def get_budget(self) -> AllocationBudget:
        snapshot = self._tracker.get_snapshot()
        equity = snapshot.equity

        options_budget = equity * self.config.options_pct
        vampire_budget = equity * self.config.vampire_pct
        reserve_target = equity * self.config.reserve_pct

        used = {"options": 0.0, "vampire": 0.0, "unattributed": 0.0}
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
