"""Loads config/strategies.yml so the configured values actually drive the code.

Before this module the YAML file was inert: every threshold Mihai wrote in
config/strategies.yml was ignored in favour of Python defaults scattered across
the strategy classes. The defaults happened to agree on most values, which is
exactly why the divergence was invisible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "strategies.yml"


@dataclass(frozen=True)
class StrategyConfig:
    """Typed view over config/strategies.yml."""

    allocation: dict[str, float] = field(default_factory=dict)
    vampire: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    sixfold: dict[str, Any] = field(default_factory=dict)
    pendulum: dict[str, Any] = field(default_factory=dict)

    @property
    def sixfold_pct(self) -> float:
        return float(self.allocation.get("sixfold_pct", 0.0))

    @property
    def options_pct(self) -> float:
        return float(self.allocation.get("options_pct", 0.80))

    @property
    def vampire_pct(self) -> float:
        return float(self.allocation.get("vampire_pct", 0.15))

    @property
    def pendulum_pct(self) -> float:
        return float(self.allocation.get("pendulum_pct", 0.0))

    @property
    def pendulum_symbol(self) -> str:
        return str(self.pendulum.get("symbol", "TLT")).upper()

    @property
    def reserve_pct(self) -> float:
        return float(self.allocation.get("reserve_pct", 0.05))

    @property
    def options_symbols(self) -> list[str]:
        return list(self.options.get("symbols", []))

    @property
    def vampire_symbols(self) -> list[str]:
        return list(self.vampire.get("symbols", []))

    @property
    def vampire_paused_until(self) -> str | None:
        """ISO date the scalper resumes, or None when it is running.

        Read here rather than defaulted in the engine so the halt is visible
        in the same file that documents why it happened.
        """
        v = self.vampire.get("paused_until")
        return str(v) if v else None

    @property
    def sixfold_universe(self) -> list[str]:
        """The inline list plus, when configured, a one-ticker-per-line file.

        The S&P 400 is 400 names; a yml list that long is unreadable and gets
        edited badly. `universe_file` is relative to the config directory.
        Order is preserved and duplicates dropped, inline names first.
        """
        names = [str(x).upper() for x in (self.sixfold.get("universe") or [])]
        uf = self.sixfold.get("universe_file")
        if uf:
            import os
            path = uf if os.path.isabs(uf) else os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", uf)
            try:
                with open(path, encoding="utf-8") as fh:
                    names += [ln.strip().upper() for ln in fh if ln.strip() and not ln.startswith("#")]
            except OSError as exc:
                raise RuntimeError(f"sixfold.universe_file {uf!r} unreadable: {exc}") from exc
        return list(dict.fromkeys(names))

    @property
    def sixfold_buy_threshold(self) -> float:
        return float(self.sixfold.get("buy_threshold", 65.0))

    @property
    def sixfold_hold_threshold(self) -> float:
        return float(self.sixfold.get("hold_threshold", 50.0))

    @property
    def sixfold_dispose_threshold(self) -> float:
        return float(self.sixfold.get("dispose_threshold", 40.0))

    @property
    def sixfold_max_concurrent(self) -> int:
        return int(self.sixfold.get("max_concurrent", 10))

    @property
    def vampire_engine_overrides(self) -> dict[str, Any]:
        """The yml keys VampireConfig accepts, so the file is the truth.

        The coordinator passed only paused_until through, and every other key
        in the vampire block was silently ignored in favour of dataclass
        defaults that happened to match. Same class as the Pendulum loader bug.
        """
        keys = ("tick_threshold", "position_size", "max_position",
                "bleed_window_seconds", "max_daily_loss", "max_trades_per_min")
        out = {k: self.vampire[k] for k in keys if k in self.vampire}
        if self.vampire_paused_until:
            out["paused_until"] = self.vampire_paused_until
        return out

    @property
    def vampire_regime_advisor(self) -> dict[str, Any]:
        """The regime_advisor block under vampire; empty when the gate is off."""
        block = self.vampire.get("regime_advisor") or {}
        return dict(block) if block.get("enabled", False) else {}

    @property
    def csp(self) -> dict[str, Any]:
        return dict(self.options.get("csp", {}))

    @property
    def covered_call(self) -> dict[str, Any]:
        return dict(self.options.get("covered_call", {}))

    def validate(self) -> list[str]:
        """Return a list of problems. Empty list means the config is coherent."""
        problems: list[str] = []

        total = (self.sixfold_pct + self.options_pct + self.vampire_pct
                 + self.pendulum_pct + self.reserve_pct)
        if abs(total - 1.0) > 1e-6:
            problems.append(
                f"allocation percentages sum to {total:.4f}, expected 1.0 "
                f"(sixfold={self.sixfold_pct}, options={self.options_pct}, "
                f"vampire={self.vampire_pct}, pendulum={self.pendulum_pct}, "
                f"reserve={self.reserve_pct})"
            )

        for name, pct in (
            ("sixfold_pct", self.sixfold_pct),
            ("options_pct", self.options_pct),
            ("vampire_pct", self.vampire_pct),
            ("pendulum_pct", self.pendulum_pct),
            ("reserve_pct", self.reserve_pct),
        ):
            if not 0.0 <= pct <= 1.0:
                problems.append(f"{name}={pct} is outside [0, 1]")

        scalper = {x.upper() for x in self.vampire_symbols}
        sixfold_universe = {x.upper() for x in (self.sixfold.get("universe") or [])}
        shared = scalper & sixfold_universe
        if shared:
            problems.append(
                f"Vampire and SIXFOLD share {sorted(shared)}: the Vampire adopts "
                f"whatever the broker holds in its symbols at startup and flattens "
                f"them at end of day, so a shared symbol hands one sleeve's "
                f"positions to the other"
            )
        shared_csp = scalper & {x.upper() for x in self.options_symbols}
        if shared_csp:
            problems.append(
                f"Vampire and CSP share {sorted(shared_csp)}: assigned shares "
                f"would be adopted and flattened by the Vampire"
            )

        max_delta = self.csp.get("max_delta")
        if max_delta is not None and max_delta > 0:
            problems.append(
                f"csp.max_delta={max_delta} should be negative (puts have negative delta)"
            )

        return problems


def load_config(path: Path | None = None) -> StrategyConfig:
    """Read the YAML config. Missing file yields defaults rather than crashing."""
    target = path or CONFIG_PATH
    if not target.exists():
        log.warning("Config %s not found, falling back to defaults", target)
        return StrategyConfig()

    with target.open() as fh:
        raw = yaml.safe_load(fh) or {}

    cfg = StrategyConfig(
        allocation=raw.get("allocation", {}) or {},
        vampire=raw.get("vampire", {}) or {},
        options=raw.get("options", {}) or {},
        risk=raw.get("risk", {}) or {},
        sixfold=raw.get("sixfold", {}) or {},
        pendulum=raw.get("pendulum", {}) or {},
    )

    for problem in cfg.validate():
        log.warning("Config problem: %s", problem)

    return cfg


@lru_cache(maxsize=1)
def get_config() -> StrategyConfig:
    return load_config()
