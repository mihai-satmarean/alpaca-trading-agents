"""A book this strategy does not trade must not set its trigger.

At 15:25 on 2026-08-31 HOOD returned a median spread of $3.87 on a $104 stock,
3.7% wide against its usual 4 cents. The derived trigger came out at $9.675, so
the symbol needed a 9.3% move before it would act: configured, running, and
silently unable to trade for the rest of the session.

There was a floor on the threshold (MIN_TICK_THRESHOLD) and no ceiling. The
floor is the dangerous direction and was guarded; the ceiling is the direction
that quietly retires a symbol, and was not. A wide quote is now discarded as
unrepresentative rather than trusted, and if every read is wide the configured
threshold stands instead of a derived one.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from src.agents.vampire import (
    MAX_SPREAD_FRACTION,
    MIN_TICK_THRESHOLD,
    SPREAD_MULTIPLE,
    VampireAgent,
)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    async def instant(_delay=0):
        return None
    monkeypatch.setattr("src.agents.vampire.asyncio.sleep", instant)


def _apply(a):
    asyncio.run(a._apply_spread_thresholds())


def _agent(quotes, configured=0.02):
    """quotes: list of (bid, ask) served in order, last one repeating."""
    a = VampireAgent.__new__(VampireAgent)
    eng = MagicMock()
    eng.cfg = MagicMock()
    eng.cfg.tick_threshold = configured
    a._engines = {"HOOD": eng}
    a._data = MagicMock()
    seq = list(quotes)

    def _q(_sym):
        bid, ask = seq.pop(0) if len(seq) > 1 else seq[0]
        q = MagicMock()
        q.bid, q.ask = bid, ask
        return q

    a._data.get_latest_quote.side_effect = _q
    return a, eng


class TestWideQuotesDoNotSetTheTrigger:
    def test_a_normal_book_still_derives_a_threshold(self):
        a, eng = _agent([(104.88, 104.92)])       # 4c on ~$105
        _apply(a)
        assert eng.cfg.tick_threshold == round(0.04 * SPREAD_MULTIPLE, 4)

    def test_the_hood_book_that_disabled_the_symbol_is_rejected(self):
        """$3.87 wide on $104. Previously produced a 9.675 trigger."""
        a, eng = _agent([(103.0, 106.87)], configured=0.05)
        _apply(a)
        assert eng.cfg.tick_threshold == 0.05, "configured value must stand"

    def test_it_never_produces_the_9_dollar_trigger(self):
        a, eng = _agent([(103.0, 106.87)], configured=0.05)
        _apply(a)
        assert eng.cfg.tick_threshold < 1.0

    def test_a_single_wide_read_is_discarded_not_the_whole_sample(self):
        """Four good reads and one blowout: use the four."""
        a, eng = _agent([(104.88, 104.92), (104.88, 104.92), (103.0, 106.87),
                         (104.88, 104.92), (104.88, 104.92)])
        _apply(a)
        assert eng.cfg.tick_threshold == round(0.04 * SPREAD_MULTIPLE, 4)

    def test_the_floor_still_binds_on_a_very_tight_book(self):
        """The dangerous direction stays guarded."""
        a, eng = _agent([(700.000, 700.001)])
        _apply(a)
        assert eng.cfg.tick_threshold == MIN_TICK_THRESHOLD

    def test_the_boundary_is_inclusive(self):
        price, spread = 100.0, 100.0 * MAX_SPREAD_FRACTION
        a, eng = _agent([(price - spread / 2, price + spread / 2)])
        _apply(a)
        assert eng.cfg.tick_threshold == round(spread * SPREAD_MULTIPLE, 4)
