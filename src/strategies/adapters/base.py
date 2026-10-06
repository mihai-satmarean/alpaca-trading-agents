"""Base class and types for strategy adapters.

Every external strategy (competitor repos, new ideas, experimental algorithms)
is wrapped in an adapter that implements this ABC.  The coordinator discovers
enabled adapters via the registry and treats each one as a sleeve with its own
capital budget, dry-run flag, and journal entries.

The adapter never touches the broker directly.  It receives shared services
(client, tracker, breaker, allocator) and returns typed results.  This keeps
risk logic centralised and outside the adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from src.core.alpaca_client import AlpacaClient
from src.core.market_data import MarketDataService
from src.core.position_tracker import PositionTracker
from src.risk.allocation import AllocationManager
from src.risk.circuit_breakers import CircuitBreaker


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    CLOSE = "close"
    FLAT = "flat"


class SignalAssetType(str, Enum):
    EQUITY = "equity"
    OPTION = "option"


@dataclass(frozen=True)
class Signal:
    """A trade proposal from an adapter, before risk gating."""

    symbol: str
    direction: SignalDirection
    asset_type: SignalAssetType = SignalAssetType.EQUITY
    qty: float = 0.0
    limit_price: float | None = None
    confidence: float = 0.0
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class OrderOutcome(str, Enum):
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    GATED = "gated"
    DRY_RUN = "dry_run"
    ERROR = "error"


@dataclass
class OrderResult:
    """What happened when a signal was submitted (or would have been)."""

    signal: Signal
    outcome: OrderOutcome
    filled_qty: float = 0.0
    filled_price: float = 0.0
    error: str = ""
    timestamp: datetime = field(default_factory=datetime.now)


class AdapterStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AdapterHealth:
    status: AdapterStatus
    message: str = ""
    last_signal_time: datetime | None = None
    signals_today: int = 0
    errors_today: int = 0


@dataclass(frozen=True)
class AdapterConfig:
    """Per-adapter configuration read from config/strategies.yml."""

    name: str
    enabled: bool = False
    capital_pct: float = 0.0
    dry_run: bool = True
    config_variant: str = "default"
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class StrategyAdapter(ABC):
    """Contract that every strategy adapter must implement.

    Lifecycle (called by the coordinator each cycle):
      1. health_check()  -- skip this adapter if unavailable
      2. evaluate()      -- produce signals from market data
      3. execute()       -- submit signals through the shared client
      4. flatten()       -- called at EOD or on circuit breaker trip

    The adapter stores no positions itself; it reads them from the tracker.
    """

    def __init__(
        self,
        config: AdapterConfig,
        client: AlpacaClient,
        data: MarketDataService,
        tracker: PositionTracker,
        breaker: CircuitBreaker,
        allocator: AllocationManager,
    ):
        self.config = config
        self._client = client
        self._data = data
        self._tracker = tracker
        self._breaker = breaker
        self._allocator = allocator

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_dry_run(self) -> bool:
        return self.config.dry_run

    @abstractmethod
    def evaluate(self) -> list[Signal]:
        """Analyse current market state and return trade signals.

        Must be pure of side effects.  No orders, no state mutation.
        """

    @abstractmethod
    def execute(self, signals: list[Signal]) -> list[OrderResult]:
        """Submit signals to the broker (or simulate if dry_run).

        Risk checks (circuit breaker, allocation) are the adapter's
        responsibility to call before placing orders.  The base class
        provides ``_check_risk()`` as a convenience.
        """

    @abstractmethod
    def flatten(self) -> None:
        """Close all positions owned by this adapter.

        Called at EOD and on circuit breaker trips.  Must be unconditional:
        no LLM, no confidence check, no delay.
        """

    def health_check(self) -> AdapterHealth:
        """Override to report adapter-specific health.

        Default returns HEALTHY.  Adapters that depend on external services
        (subprocess, API, model) should check reachability here.
        """
        return AdapterHealth(status=AdapterStatus.HEALTHY)

    def _check_risk(self, signal: Signal) -> str | None:
        """Shared pre-trade risk gate.  Returns rejection reason or None."""
        if self._breaker.is_tripped:
            return f"circuit breaker tripped: {self._breaker.trip_reason}"
        return None
