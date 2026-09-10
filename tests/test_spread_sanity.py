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

from unittest.mock import MagicMock

from src.agents.vampire import (
    MAX_SPREAD_FRACTION,
    MIN_TICK_THRESHOLD,
    SPREAD_MULTIPLE,
    VampireAgent,
)


def _agent(quotes, configured=0.02):
    """quotes: list of (bid, ask). Since 2026-09-04 the trigger is derived from
    a WINDOW of the book (MarketDataService.recent_spread) rather than five
    latest-quote polls, so the fixture serves the same book as a distribution.
    The assertions below are unchanged on purpose: the behaviour contract has
    to survive the change of mechanism."""
    a = VampireAgent.__new__(VampireAgent)
    eng = MagicMock()
    eng.cfg = MagicMock()
    eng.cfg.tick_threshold = configured
    a._engines = {"HOOD": eng}
    a._data = MagicMock()
    spreads = sorted(ask - bid for bid, ask in quotes)
    mids = sorted((ask + bid) / 2 for bid, ask in quotes)
    a._data.recent_spread.return_value = {
        "n": 5000,
        "median": spreads[len(spreads) // 2],
        "p90": spreads[-1],
        "price": mids[len(mids) // 2],
        "window_minutes": 20,
    }
    return a, eng


class TestWideQuotesDoNotSetTheTrigger:
    def test_a_normal_book_still_derives_a_threshold(self):
        a, eng = _agent([(104.88, 104.92)])       # 4c on ~$105
        a._apply_spread_thresholds()
        assert eng.cfg.tick_threshold == round(0.04 * SPREAD_MULTIPLE, 4)

    def test_the_hood_book_that_disabled_the_symbol_is_rejected(self):
        """$3.87 wide on $104. Previously produced a 9.675 trigger."""
        a, eng = _agent([(103.0, 106.87)], configured=0.05)
        a._apply_spread_thresholds()
        assert eng.cfg.tick_threshold == 0.05, "configured value must stand"

    def test_it_never_produces_the_9_dollar_trigger(self):
        a, eng = _agent([(103.0, 106.87)], configured=0.05)
        a._apply_spread_thresholds()
        assert eng.cfg.tick_threshold < 1.0

    def test_one_wide_quote_in_the_window_does_not_move_the_trigger(self):
        """Was: median-of-5 discards one wide read. Now the median is taken over
        thousands of quotes, so a single wide print cannot move it at all -- a
        strictly stronger version of the same guarantee."""
        tight = [(104.88, 104.92)] * 9
        a, eng = _agent(tight + [(103.0, 106.87)])
        a._apply_spread_thresholds()
        assert eng.cfg.tick_threshold == round(0.04 * SPREAD_MULTIPLE, 4)

    def test_the_floor_still_binds_on_a_very_tight_book(self):
        """The dangerous direction stays guarded."""
        a, eng = _agent([(700.000, 700.001)])
        a._apply_spread_thresholds()
        assert eng.cfg.tick_threshold == MIN_TICK_THRESHOLD

    def test_the_boundary_is_inclusive(self):
        price, spread = 100.0, 100.0 * MAX_SPREAD_FRACTION
        a, eng = _agent([(price - spread / 2, price + spread / 2)])
        a._apply_spread_thresholds()
        assert eng.cfg.tick_threshold == round(spread * SPREAD_MULTIPLE, 4)
