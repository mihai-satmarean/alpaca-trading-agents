"""Adapter registry: discovers and instantiates enabled strategy adapters.

The coordinator calls ``load_adapters()`` once at startup.  Each enabled
adapter in ``config/strategies.yml`` under the ``adapters:`` key is looked
up by name, instantiated with the shared services, and returned in a list
ordered by config position.

Adding a new adapter:
  1. Write ``src/strategies/adapters/<name>.py`` with a class that extends
     ``StrategyAdapter``.
  2. Register it in ``ADAPTER_CLASSES`` below.
  3. Add the YAML block under ``adapters:`` in ``config/strategies.yml``.
  4. Set ``enabled: true`` and ``dry_run: true`` to test without trading.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.core.config import get_config
from src.strategies.adapters.base import AdapterConfig, StrategyAdapter

if TYPE_CHECKING:
    from src.core.alpaca_client import AlpacaClient
    from src.core.market_data import MarketDataService
    from src.core.position_tracker import PositionTracker
    from src.risk.allocation import AllocationManager
    from src.risk.circuit_breakers import CircuitBreaker

log = logging.getLogger(__name__)

# Map adapter name -> class.  Import lazily to avoid circular deps and to
# keep adapters that depend on competitor code from breaking the import of
# adapters that do not.
ADAPTER_CLASSES: dict[str, str] = {
    "autobelay": "src.strategies.adapters.autobelay.AutobelayAdapter",
    "killswitch": "src.strategies.adapters.killswitch.KillswitchAdapter",
    "shouldai": "src.strategies.adapters.shouldai.ShouldAIAdapter",
    "neural_trader": "src.strategies.adapters.neural_trader.NeuralTraderAdapter",
    "bull_spread": "src.strategies.adapters.bull_spread.BullSpreadAdapter",
}


def _import_class(dotted: str) -> type[StrategyAdapter]:
    """Import a class from a dotted path like 'pkg.mod.ClassName'."""
    module_path, class_name = dotted.rsplit(".", 1)
    import importlib

    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    if not (isinstance(cls, type) and issubclass(cls, StrategyAdapter)):
        raise TypeError(f"{dotted} is not a StrategyAdapter subclass")
    return cls


def _parse_adapter_config(name: str, raw: dict) -> AdapterConfig:
    return AdapterConfig(
        name=name,
        enabled=bool(raw.get("enabled", False)),
        capital_pct=float(raw.get("capital_pct", 0.0)),
        dry_run=bool(raw.get("dry_run", True)),
        config_variant=str(raw.get("config_variant", "default")),
        source=str(raw.get("source", "")),
        extra={k: v for k, v in raw.items()
               if k not in ("enabled", "capital_pct", "dry_run",
                            "config_variant", "source")},
    )


def load_adapters(
    client: "AlpacaClient",
    data: "MarketDataService",
    tracker: "PositionTracker",
    breaker: "CircuitBreaker",
    allocator: "AllocationManager",
) -> list[StrategyAdapter]:
    """Instantiate all enabled adapters from config."""
    cfg = get_config()
    raw_adapters: dict = cfg.adapters if hasattr(cfg, "adapters") else {}

    adapters: list[StrategyAdapter] = []
    for name, raw in raw_adapters.items():
        acfg = _parse_adapter_config(name, raw)
        if not acfg.enabled:
            log.debug("Adapter %s: disabled, skipping", name)
            continue

        dotted = ADAPTER_CLASSES.get(name)
        if dotted is None:
            log.warning("Adapter %s: no class registered in ADAPTER_CLASSES", name)
            continue

        try:
            cls = _import_class(dotted)
            adapter = cls(
                config=acfg,
                client=client,
                data=data,
                tracker=tracker,
                breaker=breaker,
                allocator=allocator,
            )
            adapters.append(adapter)
            log.info("Adapter %s: loaded (dry_run=%s, capital=%.1f%%)",
                     name, acfg.dry_run, acfg.capital_pct * 100)
        except Exception:
            log.exception("Adapter %s: failed to load", name)

    return adapters
