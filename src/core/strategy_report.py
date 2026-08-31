"""Per-sleeve performance attribution.

The account is shared by three strategies, so a single account P&L number
cannot tell anyone which one is working. Positions are attributed the same way
the allocator does it: OCC symbols are the options sleeve, equity in the
configured scalper universe is the scalper, and anything else is surfaced as
unattributed rather than quietly folded into a sleeve.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.core.config import get_config
from src.core.notify import fmt_money, notify
from src.risk.allocation import capital_committed, parse_occ

log = logging.getLogger(__name__)


@dataclass
class SleeveReport:
    name: str
    budget: float
    committed: float = 0.0
    unrealized: float = 0.0
    positions: list[str] = field(default_factory=list)

    @property
    def utilisation(self) -> float:
        return self.committed / self.budget if self.budget else 0.0


def build_report(snapshot, *, nav_base: float | None = None) -> dict[str, SleeveReport]:
    cfg = get_config()
    equity = snapshot.equity
    scalper_universe = {s.upper() for s in cfg.vampire_symbols}
    # Overlap goes to the scalper, and the executor refuses to buy those names,
    # so a position can always be attributed to exactly one sleeve.
    sixfold_universe = {s.upper() for s in (cfg.sixfold.get("universe") or [])} - scalper_universe

    sleeves = {
        "CSP (options)": SleeveReport("CSP (options)", equity * cfg.options_pct),
        "Vampire (scalper)": SleeveReport("Vampire (scalper)", equity * cfg.vampire_pct),
        "SixFold (Tashi)": SleeveReport("SixFold (Tashi)", equity * cfg.sixfold_pct),
        "Unattributed": SleeveReport("Unattributed", 0.0),
    }

    for sym, pos in snapshot.positions.items():
        if parse_occ(sym) is not None:
            key = "CSP (options)"
        elif sym.upper() in scalper_universe:
            key = "Vampire (scalper)"
        elif sym.upper() in sixfold_universe:
            key = "SixFold (Tashi)"
        else:
            key = "Unattributed"
        s = sleeves[key]
        s.committed += capital_committed(sym, pos)
        s.unrealized += float(pos.get("unrealized_pl", 0.0) or 0.0)
        s.positions.append(sym)

    return sleeves


def _plain(value) -> str:
    """alpaca-py returns enums whose str() is 'OrderSide.SELL'. Reports are read
    on a phone; show the value, not the type."""
    return str(getattr(value, "value", value)).lower()


def describe_orders(orders) -> list[str]:
    """Working orders, which are invisible in a positions-only view.

    An order that is accepted but unfilled is exactly the state worth seeing
    before the open, and it is not a position yet, so nothing in the sleeve
    figures reflects it.
    """
    lines: list[str] = []
    for o in orders or []:
        try:
            sym = getattr(o, "symbol", None) or o.get("symbol")
            side = getattr(o, "side", None) or o.get("side")
            qty = getattr(o, "qty", None) or o.get("qty")
            limit = getattr(o, "limit_price", None) or o.get("limit_price")
            status = getattr(o, "status", None) or o.get("status")
            price = f" @ {float(limit):.2f}" if limit else ""
            lines.append(f"  {sym} {_plain(side)} {qty}{price} · {_plain(status)}")
        except Exception:
            log.warning("could not render an order", exc_info=True)
    return lines


def render(snapshot, sleeves: dict[str, SleeveReport], orders=None) -> str:
    lines = [
        f"**Equity** ${snapshot.equity:,.2f}  |  **Cash** ${snapshot.cash:,.2f}",
        f"**Day P&L** {fmt_money(snapshot.daily_pnl)}",
        "",
    ]
    for s in sleeves.values():
        if s.name == "Unattributed" and not s.positions:
            continue
        if s.name == "SixFold (Tashi)" and not s.positions:
            # Named rather than hidden: the analyst produces recommendations and
            # places no orders, so this sleeve stays at zero until something acts
            # on them. Showing it is how that gap stays visible.
            lines.append(f"**{s.name}** $0 / ${s.budget:,.0f} (0%)")
            lines.append("  analyst only, no order path yet")
            continue
        used = f"${s.committed:,.0f}"
        cap = f" / ${s.budget:,.0f} ({s.utilisation:.0%})" if s.budget else ""
        lines.append(f"**{s.name}** {used}{cap}")
        lines.append(f"  unrealized {fmt_money(s.unrealized)} · {len(s.positions)} position(s)")
        for sym in s.positions[:6]:
            lines.append(f"    {sym}")

    order_lines = describe_orders(orders)
    if order_lines:
        lines += ["", f"**Working orders** ({len(order_lines)})", *order_lines]
    elif not any(s.positions for s in sleeves.values()):
        lines += ["", "_No positions and no working orders._"]
    return "\n".join(lines)


def send_report(snapshot, *, severity: str = "default", orders=None) -> bool:
    sleeves = build_report(snapshot)
    return notify(
        f"Alpaca agent · {fmt_money(snapshot.daily_pnl)} today",
        render(snapshot, sleeves, orders),
        severity=severity,
        tags=["chart_with_upwards_trend"],
    )
