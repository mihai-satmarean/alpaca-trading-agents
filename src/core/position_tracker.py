"""Portfolio state and P&L tracking."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.core.alpaca_client import AlpacaClient

log = logging.getLogger(__name__)

TRADE_JOURNAL_PATH = Path(
    os.environ.get("TRADE_JOURNAL_PATH", "data/trade_journal.jsonl")
)


@dataclass
class TradeRecord:
    symbol: str
    side: str
    qty: float
    price: float
    timestamp: datetime
    strategy: str
    pnl: float = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "timestamp": self.timestamp.isoformat(),
            "strategy": self.strategy,
            "pnl": self.pnl,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TradeRecord:
        return cls(
            symbol=d["symbol"],
            side=d["side"],
            qty=float(d["qty"]),
            price=float(d["price"]),
            timestamp=datetime.fromisoformat(d["timestamp"]),
            strategy=d.get("strategy", "unknown"),
            pnl=float(d.get("pnl", 0.0)),
        )


@dataclass
class PortfolioSnapshot:
    equity: float
    cash: float
    buying_power: float
    positions: dict[str, dict]
    daily_pnl: float
    total_pnl: float
    timestamp: datetime


class PositionTracker:
    """Tracks positions, P&L, and trade history from Alpaca account state."""

    def __init__(self, client: AlpacaClient):
        self._client = client
        self._trades: list[TradeRecord] = []
        self._initial_equity: float | None = None
        self._daily_start_equity: float | None = None
        self._daily_start_date = None
        self._load_journal_from_disk()

    def _load_journal_from_disk(self):
        """Load persisted trade journal on startup."""
        if not TRADE_JOURNAL_PATH.exists():
            return
        try:
            with open(TRADE_JOURNAL_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._trades.append(TradeRecord.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, KeyError):
                        continue
            log.info("Loaded %d trades from journal %s", len(self._trades), TRADE_JOURNAL_PATH)
        except OSError as e:
            log.warning("Could not load trade journal: %s", e)

    def _persist_trade(self, record: TradeRecord):
        """Append a single trade to the JSONL journal on disk."""
        try:
            TRADE_JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(TRADE_JOURNAL_PATH, "a") as f:
                f.write(json.dumps(record.to_dict()) + "\n")
        except OSError as e:
            log.warning("Could not persist trade: %s", e)

    def record_trade(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        strategy: str,
        pnl: float = 0.0,
    ):
        record = TradeRecord(
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            timestamp=datetime.now(),
            strategy=strategy,
            pnl=pnl,
        )
        self._trades.append(record)
        self._persist_trade(record)

    def get_snapshot(self) -> PortfolioSnapshot:
        account = self._client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        buying_power = float(account.buying_power)

        if self._initial_equity is None:
            self._initial_equity = equity
        # The day's baseline is the broker's previous-close equity, not the
        # equity at process boot. Boot-time baselining made daily_pnl read
        # about zero after every restart (live: tracker -$0.03 while the broker
        # said +$502), so the 2% daily-loss breaker measured loss since the
        # last restart rather than since the open, and a restart mid-drawdown
        # silently re-armed the account for another 2%. Re-baselined when the
        # session date rolls, for a process that runs across midnight.
        today = datetime.now(ZoneInfo("America/New_York")).date()
        if self._daily_start_equity is None or self._daily_start_date != today:
            self._daily_start_equity = float(
                getattr(account, "last_equity", None) or equity
            )
            self._daily_start_date = today

        positions = {}
        for pos in self._client.get_positions():
            positions[pos.symbol] = {
                "qty": float(pos.qty),
                "side": pos.side.value if hasattr(pos.side, "value") else str(pos.side),
                "avg_entry": float(pos.avg_entry_price),
                "current_price": float(pos.current_price),
                "market_value": float(pos.market_value),
                "unrealized_pl": float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc),
            }

        return PortfolioSnapshot(
            equity=equity,
            cash=cash,
            buying_power=buying_power,
            positions=positions,
            daily_pnl=equity - self._daily_start_equity,
            total_pnl=equity - self._initial_equity,
            timestamp=datetime.now(),
        )

    def reset_daily(self):
        account = self._client.get_account()
        self._daily_start_equity = float(
            getattr(account, "last_equity", None) or account.equity
        )
        self._daily_start_date = datetime.now(ZoneInfo("America/New_York")).date()

    @property
    def trades(self) -> list[TradeRecord]:
        return list(self._trades)

    @property
    def trade_count_today(self) -> int:
        today = datetime.now().date()
        return sum(1 for t in self._trades if t.timestamp.date() == today)

    def strategy_pnl(self, strategy: str) -> float:
        return sum(t.pnl for t in self._trades if t.strategy == strategy)

    def strategy_trade_count(self, strategy: str) -> int:
        return sum(1 for t in self._trades if t.strategy == strategy)
