"""Per-agent capital and P&L from broker positions.

The dashboard process does not share PositionTracker with the coordinator.
This module classifies live Alpaca positions into sleeves so each agent card
can show budget, capital tied up, and unrealized P&L.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.core.agent_status import read_snapshot
from src.core.config import get_config
from src.core.decision_log import recent
from src.risk.allocation import capital_committed, parse_occ

_FILL_EVENTS = {
    "vampire": {"long_entry", "short_entry", "long_exit", "short_exit"},
    "sixfold": {"order"},
    "options": {"cycle"},
    "csp": {"cycle"},
}


@dataclass
class SleeveBook:
    agent: str
    label: str
    budget: float
    invested: float
    unrealized_pnl: float
    realized_pnl: float | None
    positions: int
    fills_today: int
    holdings: list[dict] = field(default_factory=list)


def vampire_universe(snap: dict | None = None) -> set[str]:
    cfg = get_config()
    symbols = {s.upper() for s in cfg.vampire_symbols}
    data = snap if snap is not None else read_snapshot()
    symbols.update(str(k).upper() for k in (data.get("vampire") or {}))
    picker = data.get("vampire_picker") or {}
    symbols.update(str(s).upper() for s in (picker.get("symbols") or []))
    for row in picker.get("lineup") or []:
        if isinstance(row, dict) and row.get("symbol"):
            symbols.add(str(row["symbol"]).upper())
    return symbols


def classify_symbol(symbol: str, vampire_symbols: set[str]) -> str:
    parsed = parse_occ(symbol)
    if parsed is not None:
        return "csp" if parsed.contract_type == "put" else "covered_call"
    if symbol.upper() in vampire_symbols:
        return "vampire"
    pendulum = str(getattr(get_config(), "pendulum_symbol", "") or "").upper()
    if pendulum and symbol.upper() == pendulum:
        return "pendulum"
    return "sixfold"


def position_as_dict(pos) -> dict:
    return {
        "symbol": pos.symbol,
        "qty": float(pos.qty),
        "market_value": float(pos.market_value),
        "unrealized_pl": float(pos.unrealized_pl),
        "avg_entry": float(pos.avg_entry_price),
        "current": float(getattr(pos, "current_price", 0) or 0),
    }


def fills_today_by_agent() -> dict[str, int]:
    counts = {"vampire": 0, "sixfold": 0, "options": 0, "csp": 0, "council": 0}
    for row in recent(limit=5000):
        agent = str(row.get("agent") or "")
        event = str(row.get("event") or "")
        if agent == "vampire" and event in _FILL_EVENTS["vampire"]:
            counts["vampire"] += 1
        elif agent == "sixfold" and event == "order":
            counts["sixfold"] += 1
        elif agent == "options" and event == "cycle":
            counts["options"] += 1
            counts["csp"] += 1
        elif agent == "council" and event == "verdict":
            counts["council"] += 1
    return counts


def _empty(agent: str, label: str, budget: float, fills: int) -> SleeveBook:
    return SleeveBook(
        agent=agent,
        label=label,
        budget=budget,
        invested=0.0,
        unrealized_pnl=0.0,
        realized_pnl=None,
        positions=0,
        fills_today=fills,
    )


def build_books(
    positions: list[dict],
    *,
    equity: float,
    vampire_symbols: set[str] | None = None,
    snap: dict | None = None,
    fills: dict[str, int] | None = None,
) -> list[SleeveBook]:
    cfg = get_config()
    snap = snap if snap is not None else read_snapshot()
    vset = vampire_symbols if vampire_symbols is not None else vampire_universe(snap)
    fills = fills if fills is not None else fills_today_by_agent()

    vampires = _empty("vampire", "Vampire scalper", equity * cfg.vampire_pct, fills.get("vampire", 0))
    csp = _empty("csp", "Cash-secured puts", equity * cfg.options_pct, fills.get("csp", 0))
    cc = _empty("covered_call", "Covered calls", 0.0, fills.get("covered_call", 0))
    six = _empty("sixfold", "SIXFOLD executor", equity * cfg.sixfold_pct, fills.get("sixfold", 0))
    pendulum = _empty(
        "pendulum", "Pendulum (TLT)",
        equity * getattr(cfg, "pendulum_pct", 0.0), fills.get("pendulum", 0),
    )
    sleeves = {
        "vampire": vampires, "csp": csp, "covered_call": cc,
        "sixfold": six, "pendulum": pendulum,
    }

    for pos in positions:
        symbol = str(pos["symbol"])
        sleeve_key = classify_symbol(symbol, vset)
        book = sleeves[sleeve_key]
        committed = capital_committed(symbol, pos)
        pnl = float(pos.get("unrealized_pl") or 0.0)
        book.invested += committed
        book.unrealized_pnl += pnl
        book.positions += 1
        book.holdings.append({
            "Symbol": symbol,
            "Agent": book.label,
            "Qty": pos.get("qty"),
            "Invested": committed,
            "P&L": pnl,
        })

    realized = 0.0
    for info in (snap.get("vampire") or {}).values():
        if isinstance(info, dict):
            realized += float(info.get("daily_pnl") or 0.0)
    vampires.realized_pnl = realized

    reserve = SleeveBook(
        agent="reserve",
        label="Cash reserve (target)",
        budget=equity * cfg.reserve_pct,
        invested=0.0,
        unrealized_pnl=0.0,
        realized_pnl=None,
        positions=0,
        fills_today=0,
    )
    return [vampires, csp, cc, six, pendulum, reserve]
