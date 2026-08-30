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

    @property
    def options_pct(self) -> float:
        return float(self.allocation.get("options_pct", 0.80))

    @property
    def vampire_pct(self) -> float:
        return float(self.allocation.get("vampire_pct", 0.15))

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
    def csp(self) -> dict[str, Any]:
        return dict(self.options.get("csp", {}))

    @property
    def covered_call(self) -> dict[str, Any]:
        return dict(self.options.get("covered_call", {}))

    def validate(self) -> list[str]:
        """Return a list of problems. Empty list means the config is coherent."""
        problems: list[str] = []

        total = self.options_pct + self.vampire_pct + self.reserve_pct
        if abs(total - 1.0) > 1e-6:
            problems.append(
                f"allocation percentages sum to {total:.4f}, expected 1.0 "
                f"(options={self.options_pct}, vampire={self.vampire_pct}, "
                f"reserve={self.reserve_pct})"
            )

        for name, pct in (
            ("options_pct", self.options_pct),
            ("vampire_pct", self.vampire_pct),
            ("reserve_pct", self.reserve_pct),
        ):
            if not 0.0 <= pct <= 1.0:
                problems.append(f"{name}={pct} is outside [0, 1]")

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
    )

    for problem in cfg.validate():
        log.warning("Config problem: %s", problem)

    return cfg


@lru_cache(maxsize=1)
def get_config() -> StrategyConfig:
    return load_config()
