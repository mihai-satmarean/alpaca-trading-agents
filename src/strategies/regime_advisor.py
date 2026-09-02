"""Regime advisor: the LLM consulted by the Vampire, on the Vampire's terms.

The Vampire fades oscillation. It earns when price chops around a level and
loses when price trends: the afternoon slide of 2026-09-01 cost it more than
the rest of the week combined. Whether the next quarter hour is chop or trend
is a judgement with genuine ambiguity, which is the one place this project
lets a model in.

The contract was fixed by measuring the Dell4 models live on 2026-09-02:

* The model labels the regime. It does not decide whether to trade.
  dell4-finance answered ``"regime": "chop", "trade": false`` in one object;
  a 7B model's boolean is not a decision. The decision is derived here from
  the label, and the Vampire opens lots only in ``chop``.
* Content only. dell4-chat is a reasoning model whose thinking stream holds
  half-written JSON; parsing it would read a draft as a verdict. At an
  800-token budget it never reached an answer at all (finish_reason
  ``length``); 4000 tokens, the council's budget, returns one in 17 to 19 s.
* One verdict per 15-minute window from the 30 one-minute bars that END at
  the window start, so the backtest and the live agent see identical inputs.
* Fail closed. No verdict, a stale verdict, an unparseable answer or an
  unreachable model all mean "do not open a new position". Exits never
  consult the advisor: an outage can stop the Vampire entering, never leaving.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

REGIMES = ("chop", "trend_up", "trend_down", "news")
TRADEABLE_REGIMES = frozenset({"chop"})
MIN_BARS = 10
_ET = ZoneInfo("America/New_York")

SYSTEM_PROMPT = (
    "You are a market-microstructure regime classifier advising a 5-second "
    "bid/ask mean-reversion scalper. The scalper earns a few cents per share "
    "when price oscillates around a level and loses when price trends, gaps, "
    "or reacts to news. Given the last 30 one-minute bars, classify the regime "
    "of the NEXT 15 minutes. Respond with exactly ONE minified JSON object on "
    "one line and nothing else, no prose, no markdown: "
    '{"regime":"chop|trend_up|trend_down|news","confidence":0.0,"reason":"<=15 words"}'
)

JOURNAL_PATH = os.environ.get(
    "REGIME_JOURNAL", os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "logs", "regime.jsonl"))
JOURNAL_MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class RegimeVerdict:
    symbol: str
    regime: str
    confidence: float
    reason: str
    model: str
    at: float          # epoch seconds when the verdict was produced
    latency: float

    @property
    def tradeable(self) -> bool:
        return self.regime in TRADEABLE_REGIMES


# --------------------------------------------------------------------------
# Prompt: one function for the backtest and the live agent
# --------------------------------------------------------------------------

def _field(bar: Any, *names: str):
    for name in names:
        if isinstance(bar, dict) and name in bar:
            return bar[name]
        if hasattr(bar, name):
            return getattr(bar, name)
    raise KeyError(names[0])


def _to_et(ts: Any) -> datetime:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(_ET)


def format_bars(bars: Iterable[Any], limit: int = 30) -> list[str]:
    """Compact ``HH:MM o= h= l= c= v=`` lines, oldest first, last ``limit`` bars.

    Accepts alpaca ``Bar`` objects or dicts with the same field names.
    """
    rows = list(bars)[-limit:]
    lines = []
    for b in rows:
        t = _to_et(_field(b, "timestamp", "ts", "t"))
        lines.append(
            f"{t.strftime('%H:%M')} o={float(_field(b, 'open', 'o')):.2f} "
            f"h={float(_field(b, 'high', 'h')):.2f} l={float(_field(b, 'low', 'l')):.2f} "
            f"c={float(_field(b, 'close', 'c')):.2f} v={int(float(_field(b, 'volume', 'v')))}"
        )
    return lines


def build_user_prompt(symbol: str, lines: list[str]) -> str:
    return f"Symbol {symbol}. Last 30 one-minute bars (ET), oldest first:\n" + "\n".join(lines)


# --------------------------------------------------------------------------
# Parsing: strict, content-only, first balanced object
# --------------------------------------------------------------------------

def extract_json_object(text: str) -> str | None:
    """The first balanced ``{...}`` in ``text``, skipping braces inside strings."""
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start:i + 1]
    return None


def parse_verdict(text: str | None, symbol: str, model: str = "",
                  latency: float = 0.0, now: float | None = None) -> RegimeVerdict | None:
    """A verdict, or None when the answer is not one.

    None is the honest result for empty content, prose, a regime outside the
    contract, or malformed JSON. It is never coerced into "chop": an
    unreadable answer that counted as permission would silently neutralise
    the gate while the log showed a verdict that was never given.
    """
    if not text or not text.strip():
        return None
    raw = extract_json_object(text.replace("```json", " ").replace("```", " "))
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    regime = str(obj.get("regime", "")).strip().lower()
    if regime not in REGIMES:
        return None
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    return RegimeVerdict(
        symbol=symbol, regime=regime, confidence=confidence,
        reason=str(obj.get("reason", ""))[:200], model=model,
        at=time.time() if now is None else now, latency=latency,
    )


# --------------------------------------------------------------------------
# Journal: every verdict and every failure, so the gate is auditable
# --------------------------------------------------------------------------

def _journal(entry: dict) -> None:
    """Append one record. Never raises: an audit trail must not stop trading."""
    try:
        os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
        with open(JOURNAL_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        if os.path.getsize(JOURNAL_PATH) > JOURNAL_MAX_BYTES:
            with open(JOURNAL_PATH, "r", encoding="utf-8") as fh:
                keep = fh.readlines()[-2000:]
            with open(JOURNAL_PATH, "w", encoding="utf-8") as fh:
                fh.writelines(keep)
    except Exception:
        log.debug("regime journal write failed", exc_info=True)


def read_regime_journal(limit: int = 50) -> list[dict]:
    """Most recent records, newest first. Never raises."""
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
    except Exception:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------
# The advisor
# --------------------------------------------------------------------------

def _default_llm_call(model: str, system: str, user: str) -> str:
    from src.core.finance_advisor import _llm_call
    return _llm_call(model, system, user, max_tokens=4000, temperature=0.0)


class RegimeAdvisor:
    """Holds one verdict per symbol and answers "may a new lot be opened?".

    ``refresh`` is called from a timer thread; ``entry_allowed`` from the
    quote thread on every entry signal. The lock keeps a half-written verdict
    from being read.
    """

    def __init__(
        self,
        model: str = "dell4-chat",
        *,
        llm_call: Callable[[str, str, str], str] | None = None,
        window_seconds: int = 15 * 60,
        bars_needed: int = 30,
        ttl_seconds: int = 20 * 60,
        min_confidence: float = 0.0,
        journal: bool = True,
        clock: Callable[[], float] = time.time,
    ):
        self.model = model
        self.window_seconds = float(window_seconds)
        self.bars_needed = int(bars_needed)
        self.ttl_seconds = int(ttl_seconds)
        self.min_confidence = float(min_confidence)
        self._llm = llm_call or _default_llm_call
        self._journal_on = journal
        self._clock = clock
        self._lock = threading.Lock()
        self._verdicts: dict[str, RegimeVerdict | None] = {}
        self._refreshed_at: dict[str, float] = {}
        self._last_error: dict[str, str] = {}

    def classify(self, symbol: str, bars: Iterable[Any]) -> RegimeVerdict | None:
        """One model call. None on any failure; the failure is journalled."""
        try:
            lines = format_bars(bars, self.bars_needed)
        except Exception as exc:
            return self._fail(symbol, f"unreadable bars: {type(exc).__name__}")
        if len(lines) < MIN_BARS:
            return self._fail(symbol, f"only {len(lines)} bars (need {MIN_BARS})")
        t0 = self._clock()
        try:
            text = self._llm(self.model, SYSTEM_PROMPT, build_user_prompt(symbol, lines))
        except Exception as exc:
            return self._fail(symbol, f"model unavailable: {type(exc).__name__}: {str(exc)[:120]}")
        latency = self._clock() - t0
        verdict = parse_verdict(text, symbol, self.model, latency, now=self._clock())
        if verdict is None:
            return self._fail(symbol, f"unparseable answer: {str(text)[:120]!r}")
        self._last_error.pop(symbol, None)
        self._record({**asdict(verdict), "window": lines[-1][:5]})
        return verdict

    def refresh(self, symbol: str, bars: Iterable[Any]) -> RegimeVerdict | None:
        verdict = self.classify(symbol, bars)
        with self._lock:
            self._verdicts[symbol] = verdict
            self._refreshed_at[symbol] = self._clock()
        return verdict

    def entry_allowed(self, symbol: str) -> bool:
        """True only for a fresh, confident, tradeable verdict. Everything else is no."""
        with self._lock:
            verdict = self._verdicts.get(symbol)
        if verdict is None:
            return False
        if self._clock() - verdict.at > self.ttl_seconds:
            return False
        if verdict.confidence < self.min_confidence:
            return False
        return verdict.tradeable

    def status(self) -> dict[str, dict]:
        with self._lock:
            verdicts = dict(self._verdicts)
            refreshed = dict(self._refreshed_at)
        out: dict[str, dict] = {}
        for sym in set(verdicts) | set(self._last_error):
            v = verdicts.get(sym)
            out[sym] = {
                "regime": v.regime if v else None,
                "confidence": v.confidence if v else None,
                "reason": v.reason if v else self._last_error.get(sym),
                "age_seconds": (self._clock() - v.at) if v else None,
                "entry_allowed": self.entry_allowed(sym),
                "refreshed_at": refreshed.get(sym),
                "model": self.model,
            }
        return out

    def _fail(self, symbol: str, why: str) -> None:
        self._last_error[symbol] = why
        log.warning("%s: regime advisor gave no verdict (%s); entries closed", symbol, why)
        self._record({"symbol": symbol, "model": self.model, "at": self._clock(),
                      "regime": None, "error": why})
        return None

    def _record(self, entry: dict) -> None:
        if self._journal_on:
            _journal(entry)
