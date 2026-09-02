"""Fixed reporting times for the session, in Eastern.

A fixed clock rather than an interval, because the useful moments are tied to
the session and not to a stopwatch: what we intend before the open, what
happened at it, whether the first cycle did anything, where we stand at midday,
what is about to be flattened, and where the day finished.

Times are Eastern explicitly. Reading the machine clock works until the machine
moves or the code runs anywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Checkpoint:
    at: dt_time
    label: str
    blurb: str
    severity: str = "default"
    closing: bool = False


CHECKPOINTS: tuple[Checkpoint, ...] = (
    Checkpoint(dt_time(7, 0), "PRE-MARKET",
               "Plan for the session. Options cannot trade before 09:30."),
    Checkpoint(dt_time(9, 30), "OPEN",
               "Market open. Agents live."),
    Checkpoint(dt_time(9, 45), "FIRST CYCLE",
               "Fifteen minutes in: what the first scan did, and what it refused."),
    Checkpoint(dt_time(12, 0), "MIDDAY",
               "Half-session status."),
    Checkpoint(dt_time(15, 30), "PRE-CLOSE",
               "Twenty minutes before the Vampire is flattened. Options stay on."),
    Checkpoint(dt_time(16, 15), "CLOSED",
               "Session finished.", severity="high", closing=True),
)


def next_checkpoint(now: datetime) -> tuple[datetime, Checkpoint]:
    """The next checkpoint at or after `now`, rolling to the next weekday.

    Weekends roll to Monday: reporting an empty Saturday six times says nothing
    that Friday's close did not already say.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)

    day = now.date()
    for _ in range(8):
        if day.weekday() < 5:
            for cp in CHECKPOINTS:
                when = datetime.combine(day, cp.at, tzinfo=ET)
                if when > now:
                    return when, cp
        day += timedelta(days=1)
        now = datetime.combine(day, dt_time(0, 0), tzinfo=ET) - timedelta(seconds=1)
    raise RuntimeError("no checkpoint found within a week")


def seconds_until(when: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(ET)
    return max(0.0, (when - now).total_seconds())


def checkpoints_for(day: date) -> list[Checkpoint]:
    return list(CHECKPOINTS) if day.weekday() < 5 else []
