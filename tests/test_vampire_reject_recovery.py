"""A refused order is broker truth, not a transient failure.

On 2026-08-31 the scalper submitted 4,700 refused orders in 29 minutes, roughly
200 per minute, until the account was rate-limited and the watchdog and the
scheduled reports went blind. Every refusal was identical:

    {"available":"9","code":40310000,"existing_qty":"9","held_for_orders":"0",
     "message":"insufficient qty available for order (requested: 10, available: 9)"}

The engine held 9 shares short and asked to buy 10 back. Its counter over-stated
the position, so the covering order was larger than the position it was closing,
and the venue refused it every single time.

The counter over-stated the position because of the rule written that same
morning to fix the opposite defect: an unknown fill is assumed FILLED, justified
on the grounds that "over-counting only makes the engine trade less." That is
true while opening a position and false while closing one. An over-stated
position asks for more than exists, and no number of retries makes the extra
share appear. It is a permanent deadlock.

Two things follow, and both are tested here. The refusal states the true
position and the size that may be sent, so the engine adopts them instead of
guessing. And a refusal costs the same rate-limit budget as a fill, so a symbol
the venue keeps refusing gets parked rather than hammered.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from alpaca.trading.enums import OrderSide

from src.strategies.vampire_engine import VampireConfig, VampireEngine

# Verbatim from logs/session-20260831.log.
HOOD_BODY = ('{"available":"9","code":40310000,"existing_qty":"9",'
             '"held_for_orders":"0","message":"insufficient qty available for '
             'order (requested: 10, available: 9)","symbol":"HOOD"}')
TQQQ_BODY = ('{"available":"4","code":40310000,"existing_qty":"17",'
             '"held_for_orders":"13","message":"insufficient qty available for '
             'order (requested: 10, available: 4)",'
             '"related_orders":["e7a84f20-e6d6-4922-adf8-9a261da866a1"],'
             '"symbol":"TQQQ"}')
WASH_BODY = ('{"code":40310000,"existing_order_id":"b52e654c-3058-4586-ba75-'
             'fed82c1a9a51","message":"potential wash trade detected. use '
             'complex orders","reject_reason":"opposite side market/stop '
             'order exists"}')


class Refused(Exception):
    """Stands in for alpaca.common.exceptions.APIError, which stringifies to the body."""


def _order(status="filled", filled="9", oid="o1"):
    o = MagicMock()
    o.status, o.filled_qty, o.id = status, filled, oid
    return o


def _engine(side_effect):
    cfg = VampireConfig(symbol="HOOD", tick_threshold=0.02, position_size=10,
                        max_position=21, max_daily_loss=1e9, max_trades_per_min=20)
    client = MagicMock()
    client.market_order.side_effect = side_effect
    e = VampireEngine(client, MagicMock(), MagicMock(), cfg)
    e._is_market_hours = lambda: True
    return e, client


class TestTheRefusalIsParsed:
    def test_existing_qty_and_available_are_read_separately(self):
        """They are not the same number when resting orders reserve shares."""
        facts = VampireEngine._reject_facts(Refused(TQQQ_BODY))
        assert facts["existing_qty"] == 17, "position is 17, not the sendable 4"
        assert facts["available"] == 4, "only 4 may be sent while 13 are held"

    def test_the_hood_refusal_that_looped_4700_times(self):
        assert VampireEngine._reject_facts(Refused(HOOD_BODY)) == {
            "available": 9, "existing_qty": 9,
        }

    def test_a_wash_trade_refusal_carries_no_quantities(self):
        """Nothing to adopt: this one needs spacing, not resizing."""
        assert VampireEngine._reject_facts(Refused(WASH_BODY)) == {}

    def test_an_unparseable_body_yields_nothing_rather_than_raising(self):
        assert VampireEngine._reject_facts(Refused("502 Bad Gateway")) == {}
        assert VampireEngine._reject_facts(Refused("{truncated")) == {}


class TestTheEngineAdoptsBrokerTruth:
    def test_an_oversized_cover_is_resized_and_succeeds(self):
        """The exact deadlock: believes it is short 12, actually short 9."""
        e, client = _engine([Refused(HOOD_BODY), _order("filled", "9")])
        e._net_position = -12

        filled = e._submit(10, 100.0, OrderSide.BUY)

        assert filled == 9, "the retry at the venue's size must fill"
        assert [c.args[1] for c in client.market_order.call_args_list] == [10, 9], \
            "first attempt 10 refused, second attempt resized to 9"

    def test_the_counter_is_corrected_to_the_venue_number(self):
        """_submit adopts the true position; the caller applies the fill to it."""
        e, _ = _engine([Refused(HOOD_BODY), _order("filled", "9")])
        e._net_position = -12
        e._submit(10, 100.0, OrderSide.BUY)
        assert e._net_position == -9, "the phantom 3 shares are gone"

    def test_correction_preserves_the_side_of_the_position(self):
        """A long that over-states itself must not be flipped short."""
        e, _ = _engine([Refused(HOOD_BODY), _order("filled", "9")])
        e._net_position = 12
        e._submit(10, 100.0, OrderSide.SELL)
        assert e._net_position == 9

    def test_a_correct_counter_is_left_alone(self):
        """Adoption fires only on disagreement, so a wash-trade refusal on an
        accurate counter must not perturb it."""
        e, _ = _engine([Refused(WASH_BODY)])
        e._net_position = -9
        e._submit(9, 100.0, OrderSide.BUY)
        assert e._net_position == -9

    def test_the_deadlock_clears_end_to_end(self):
        """The whole point: a tick that could never cover now covers.

        Believes it is short 12 while holding 9. Before the fix this asked for
        10 forever. It must now end flat, having sent one refused order and one
        that the venue accepted.
        """
        e, client = _engine([Refused(HOOD_BODY), _order("filled", "9")])
        e._net_position = -12
        e._avg_entry = 100.0
        e._last_fill_price = 100.0

        e.tick(99.90)   # price down against a short: cover

        assert e._net_position == 0, "flat, not deadlocked"
        assert client.market_order.call_count == 2

    def test_it_retries_once_not_forever(self):
        """Two refusals in a row must stop, or the loop is back."""
        e, client = _engine([Refused(HOOD_BODY), Refused(HOOD_BODY)])
        e._net_position = -12
        assert e._submit(10, 100.0, OrderSide.BUY) == 0
        assert client.market_order.call_count == 2

    def test_a_wash_trade_refusal_is_not_retried(self):
        """No quantity to resize to; resending the same order just repeats it."""
        e, client = _engine([Refused(WASH_BODY)])
        e._net_position = -9
        assert e._submit(9, 100.0, OrderSide.BUY) == 0
        assert client.market_order.call_count == 1

    def test_available_zero_is_not_submitted(self):
        """Nothing sendable means send nothing, not an order for zero shares."""
        body = HOOD_BODY.replace('"available":"9"', '"available":"0"')
        e, client = _engine([Refused(body)])
        e._net_position = -9
        assert e._submit(10, 100.0, OrderSide.BUY) == 0
        assert client.market_order.call_count == 1


class TestRefusalsCostBudget:
    def test_a_refusal_consumes_rate_limit_the_way_a_fill_does(self):
        """The hole that let 4,700 through a 20-per-minute limit.

        Only _record_bleed appended to the limiter, and a refused order never
        reaches it, so refusals were free and unbounded.
        """
        e, _ = _engine([Refused(WASH_BODY)] * 30)
        e._net_position = -9
        for _ in range(20):
            e._submit(9, 100.0, OrderSide.BUY)
        assert not e._check_rate_limit(), "20 refusals must exhaust a 20/min budget"

    def test_a_streak_parks_the_symbol(self):
        e, _ = _engine([Refused(WASH_BODY)] * 30)
        e._net_position = -9
        for _ in range(VampireEngine.REJECT_STREAK_TRIP):
            e._submit(9, 100.0, OrderSide.BUY)
        assert e._reject_cooldown_until > time.time()

    def test_a_parked_symbol_submits_nothing(self):
        e, client = _engine([_order("filled", "10")])
        e._reject_cooldown_until = time.time() + 60
        e.tick(100.0)
        assert client.market_order.call_count == 0, "cooldown must reach the venue call"

    def test_backoff_is_bounded(self):
        e, _ = _engine([Refused(WASH_BODY)] * 200)
        e._net_position = -9
        for _ in range(60):
            e._submit(9, 100.0, OrderSide.BUY)
        assert e._reject_cooldown_until - time.time() <= VampireEngine.REJECT_BACKOFF_MAX + 1

    def test_an_accepted_order_clears_the_streak(self):
        e, _ = _engine([Refused(WASH_BODY), Refused(WASH_BODY), _order("filled", "9")])
        e._net_position = -9
        e._submit(9, 100.0, OrderSide.BUY)
        e._submit(9, 100.0, OrderSide.BUY)
        assert e._reject_streak == 2
        e._submit(9, 100.0, OrderSide.BUY)
        assert e._reject_streak == 0
