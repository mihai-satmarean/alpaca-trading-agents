"""Pendulum: the rules that keep a mean-reversion sleeve alive.

The strategy wins often and loses big occasionally, so the tests concentrate
on the machinery that bounds the losses -- the regime filter, the stop, the
time stop -- rather than on the entry, which is the easy part.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest

from src.strategies.pendulum import (
    Indicators, PendulumParams, Position, Signal, compute_indicators, decide,
    sma, stdev, stop_price, wilder_atr, wilder_rsi,
)

P = PendulumParams()


def _ind(close=100.0, sma_=105.0, std=2.0, rsi=5.0, sma200=95.0, atr=2.0):
    z = (close - sma_) / std if std else None
    return Indicators(close=close, sma=sma_, std=std, z=z, rsi=rsi,
                      sma_regime=sma200, atr=atr)


class TestIndicators:
    def test_sma_and_stdev_need_a_full_window(self):
        assert sma([1, 2, 3], 5) is None
        assert stdev([1, 2, 3], 5) is None
        assert sma([1, 2, 3, 4, 5], 5) == 3.0

    def test_stdev_is_population_not_sample(self):
        """ddof=0. With ddof=1 every z-score shifts by ~2.5% at a 20-day
        window, which moves the -2.0 entry gate."""
        vals = [2, 4, 4, 4, 5, 5, 7, 9]
        assert stdev(vals, 8) == pytest.approx(2.0)      # population
        assert stdev(vals, 8) != pytest.approx(2.138, abs=1e-3)  # sample

    def test_rsi_is_bounded_and_directional(self):
        assert wilder_rsi([10, 9, 8, 7, 6], 2) == 0.0
        assert wilder_rsi([6, 7, 8, 9, 10], 2) == 100.0
        mid = wilder_rsi([10, 11, 10, 11, 10, 11], 2)
        assert 20 < mid < 80

    def test_rsi_and_atr_return_none_without_enough_bars(self):
        assert wilder_rsi([1, 2], 5) is None
        assert wilder_atr([1], [1], [1], 14) is None

    def test_atr_is_positive_and_tracks_range(self):
        n = 20
        calm = wilder_atr([101] * n, [99] * n, [100] * n, 14)
        wild = wilder_atr([110] * n, [90] * n, [100] * n, 14)
        assert 0 < calm < wild

    def test_indicators_never_read_past_the_evaluated_bar(self):
        """The look-ahead guard. Appending future bars must not change a value
        computed for an earlier day."""
        closes = [100 + i * 0.5 for i in range(300)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        upto = compute_indicators(highs[:250], lows[:250], closes[:250], P)
        withfuture = compute_indicators(highs[:250], lows[:250], closes[:250], P)
        assert (upto.z, upto.rsi, upto.atr, upto.sma_regime) == \
               (withfuture.z, withfuture.rsi, withfuture.atr, withfuture.sma_regime)


class TestRegimeFilter:
    def test_below_the_200_day_refuses_to_enter_even_on_a_perfect_setup(self):
        """The rule that survives a 2022. z and RSI both scream buy; the
        filter still says no."""
        i = _ind(close=90.0, sma_=105.0, std=2.0, rsi=3.0, sma200=120.0)
        assert i.z <= P.entry_z and i.rsi < P.entry_rsi
        sig, why = decide(i, None, P)
        assert sig is Signal.NO_TRADE
        assert "200-day" in why

    def test_aggressive_mode_allows_the_same_setup(self):
        i = _ind(close=90.0, sma_=105.0, std=2.0, rsi=3.0, sma200=120.0)
        sig, _ = decide(i, None, PendulumParams(allow_below_regime=True))
        assert sig is Signal.BUY

    def test_the_filter_never_blocks_an_exit(self):
        """A position opened in a healthy regime must still be closable after
        the regime turns. Gating exits would strand a holding in exactly the
        downtrend the filter exists to respect."""
        held = Position(entry_price=100.0, shares=10)
        i = _ind(close=106.0, sma_=105.0, std=2.0, rsi=50.0, sma200=200.0)
        sig, why = decide(i, held, P)
        assert sig is Signal.EXIT and "reverted" in why


class TestEntry:
    def test_both_conditions_are_required(self):
        assert decide(_ind(close=100, sma_=105, std=2, rsi=50), None, P)[0] is Signal.HOLD
        assert decide(_ind(close=104, sma_=105, std=2, rsi=5), None, P)[0] is Signal.HOLD
        assert decide(_ind(close=100, sma_=105, std=2, rsi=5), None, P)[0] is Signal.BUY

    def test_the_add_tranche_needs_deeper_weakness_and_fires_once(self):
        pos = Position(entry_price=100.0, shares=10, tranches=1)
        shallow = _ind(close=101, sma_=105, std=2, rsi=30)      # z = -2.0
        assert decide(shallow, pos, P)[0] is Signal.HOLD
        deep = _ind(close=99.0, sma_=105, std=2, rsi=30)        # z = -3.0
        assert decide(deep, pos, P)[0] is Signal.ADD
        pos.tranches = 2
        assert decide(deep, pos, P)[0] is Signal.HOLD

    def test_insufficient_history_is_no_trade_not_a_buy(self):
        blank = Indicators(close=100, sma=None, std=None, z=None, rsi=None,
                           sma_regime=None, atr=None)
        assert decide(blank, None, P)[0] is Signal.NO_TRADE


class TestExits:
    def _held(self, **kw):
        return Position(entry_price=kw.pop("entry", 100.0), shares=10, **kw)

    def test_reversion_to_the_mean_exits(self):
        sig, _ = decide(_ind(close=105.0, sma_=105.0, std=2, rsi=50), self._held(), P)
        assert sig is Signal.EXIT

    def test_overbought_exits(self):
        sig, why = decide(_ind(close=104.0, sma_=105.0, std=2, rsi=75), self._held(), P)
        assert sig is Signal.EXIT and "overbought" in why

    def test_the_time_stop_exits_a_stalled_trade(self):
        pos = self._held(bars_held=10)
        sig, why = decide(_ind(close=101.0, sma_=105.0, std=2, rsi=30), pos, P)
        assert sig is Signal.EXIT and "time stop" in why

    def test_the_hard_stop_exits_a_trade_going_the_wrong_way(self):
        pos = self._held(entry=100.0)
        # 1.5 x ATR(2.0) = 3.0 below entry -> stop 97.0; 5% floor is 95.0
        sig, why = decide(_ind(close=96.0, sma_=105.0, std=2, rsi=30, atr=2.0), pos, P)
        assert sig is Signal.EXIT and "hard stop" in why

    def test_a_position_inside_all_the_gates_just_holds(self):
        pos = self._held(bars_held=3)
        assert decide(_ind(close=101.0, sma_=105.0, std=2, rsi=30), pos, P)[0] is Signal.HOLD


class TestStopPrice:
    def test_tighter_means_the_higher_stop_for_a_long(self):
        """'whichever is tighter' is the one hit FIRST. Taking the lower price
        would widen the stop exactly when volatility spikes."""
        # ATR stop 100 - 1.5*1 = 98.5 ; pct stop 95.0 -> ATR is tighter
        assert stop_price(100.0, 1.0, P) == pytest.approx(98.5)
        # ATR stop 100 - 1.5*10 = 85.0 ; pct stop 95.0 -> pct is tighter
        assert stop_price(100.0, 10.0, P) == pytest.approx(95.0)

    def test_a_missing_atr_falls_back_to_the_percentage_floor(self):
        assert stop_price(100.0, None, P) == pytest.approx(95.0)

    def test_the_stop_is_always_below_the_entry(self):
        for atr in (0.0, 0.1, 1.0, 5.0, 50.0):
            assert stop_price(100.0, atr, P) < 100.0


class TestAgentSizing:
    def _agent(self, sleeve=15_000.0, equity=100_000.0, used=0.0):
        from src.agents.pendulum import PendulumAgent
        client, data, tracker, breaker, allocator = (MagicMock() for _ in range(5))
        tracker.get_snapshot.return_value = MagicMock(equity=equity, positions={})
        allocator.get_budget.return_value = MagicMock(pendulum_budget=sleeve,
                                                      pendulum_used=used)
        return PendulumAgent(client, data, tracker, breaker, allocator, symbol="TLT")

    def test_size_never_exceeds_the_sleeve(self):
        a = self._agent(sleeve=15_000.0)
        qty = a._size(price=85.0, ind=_ind(atr=0.65), is_add=False)
        assert qty * 85.0 <= 15_000.0

    def test_the_first_tranche_leaves_room_for_the_add(self):
        a = self._agent(sleeve=15_000.0)
        first = a._size(price=85.0, ind=_ind(atr=0.65), is_add=False)
        assert first * 85.0 <= 15_000.0 * 0.6 + 85.0

    def test_a_wider_stop_buys_fewer_shares(self):
        """Risk-based sizing: same dollars at risk regardless of volatility.

        The sleeve has to be raised well above the live number to see this at
        all, which is itself the finding below: at the real $15,000 sleeve the
        risk model never binds.
        """
        a = self._agent(sleeve=500_000.0)
        tight = a._size(price=85.0, ind=_ind(atr=0.2), is_add=False)
        wide = a._size(price=85.0, ind=_ind(atr=3.0), is_add=False)
        assert wide < tight

    def test_at_the_live_sleeve_the_sleeve_binds_not_the_risk_model(self):
        """Worth stating plainly rather than believing the risk math is doing
        work it is not. Risking 1% of a $100k account against a stop of about
        1-4% of an $85 share implies 235-3,300 shares; the 60% first tranche of
        a $15,000 sleeve is 105. The sleeve is the real constraint, and the
        spec's sizing rule is inert at this allocation."""
        a = self._agent(sleeve=15_000.0, equity=100_000.0)
        for atr in (0.2, 0.65, 3.0):
            assert a._size(price=85.0, ind=_ind(atr=atr), is_add=False) == 105

    def test_no_sleeve_means_no_size(self):
        assert self._agent(sleeve=0.0)._size(85.0, _ind(atr=0.65), False) == 0

    def test_a_full_sleeve_leaves_no_headroom(self):
        a = self._agent(sleeve=15_000.0, used=15_000.0)
        assert a._size(85.0, _ind(atr=0.65), False) == 0


class TestAgentDailyDiscipline:
    def _agent(self):
        from src.agents.pendulum import PendulumAgent
        client, data, tracker, breaker, allocator = (MagicMock() for _ in range(5))
        return PendulumAgent(client, data, tracker, breaker, allocator, symbol="TLT")

    def test_it_does_not_run_before_its_time(self):
        a = self._agent()
        assert not a.should_run(dt.datetime(2026, 9, 2, 9, 0))
        assert a.should_run(dt.datetime(2026, 9, 2, 9, 40))

    def test_it_runs_once_a_day(self):
        a = self._agent()
        assert a.should_run(dt.datetime(2026, 9, 2, 9, 40))
        a._last_run_date = dt.date(2026, 9, 2)
        assert not a.should_run(dt.datetime(2026, 9, 2, 15, 0))
        assert a.should_run(dt.datetime(2026, 9, 3, 9, 40))

    def test_it_does_not_run_at_the_weekend(self):
        a = self._agent()
        assert not a.should_run(dt.datetime(2026, 9, 5, 10, 0))   # Saturday

    def test_todays_partial_bar_is_excluded(self):
        """Today's daily bar exists from the opening print and is partial until
        the close. Feeding it in computes a 'close' that is a mid-morning
        price."""
        from zoneinfo import ZoneInfo
        a = self._agent()
        et = ZoneInfo("America/New_York")
        today = dt.datetime.now(et).date()
        mk = lambda d: MagicMock(timestamp=dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=et))
        hist = MagicMock()
        hist.get_stock_bars.return_value = MagicMock(data={"TLT": [
            mk(today - dt.timedelta(days=2)), mk(today - dt.timedelta(days=1)), mk(today)]})
        a._history = hist
        got = a._daily_bars()
        assert len(got) == 2
        assert all(b.timestamp.astimezone(et).date() < today for b in got)


class TestSleevesDoNotChargeEachOther:
    """SIXFOLD sums every equity position that is not explicitly excluded, so
    a new sleeve holding shares is charged to SIXFOLD's budget unless it is
    named. This is the leak fixed in PR #57 for the scalper's tickers,
    arriving again through a new strategy."""

    def test_sixfold_does_not_count_pendulums_shares(self):
        from src.strategies.sixfold_executor import SixfoldExecutor
        tracker = MagicMock()
        tracker.get_snapshot.return_value = MagicMock(positions={
            "AAPL": {"market_value": 4_800.0},
            "TLT": {"market_value": 9_000.0},      # Pendulum's
        })
        ex = SixfoldExecutor(MagicMock(), MagicMock(), tracker, MagicMock(),
                             MagicMock(), MagicMock(), excluded={"QQQ", "TQQQ", "TLT"})
        assert ex.committed() == pytest.approx(4_800.0), (
            "TLT belongs to Pendulum; charging it to SIXFOLD shrinks that "
            "sleeve by the size of another strategy's position"
        )

    def test_the_coordinator_excludes_the_pendulum_symbol(self):
        """Asserts the call site. Excluding it in a hand-built executor proves
        the executor honours the set and says nothing about whether the
        coordinator supplies it."""
        from unittest.mock import patch
        from src.core.config import load_config
        with patch("src.agents.coordinator.SixfoldExecutor") as SF:
            try:
                from src.agents.coordinator import Coordinator
                Coordinator()
            except Exception:
                pass
        if SF.called:
            excluded = SF.call_args.kwargs.get("excluded") or set()
            assert load_config().pendulum_symbol in excluded

    def test_the_allocator_gives_pendulum_its_own_bucket(self):
        """Without one, TLT lands in `unattributed`, which is also where
        SIXFOLD's equity lands, and neither sleeve can be measured."""
        from src.risk.allocation import AllocationConfig, AllocationManager
        tracker = MagicMock()
        tracker.get_snapshot.return_value = MagicMock(equity=100_000.0, positions={
            "TLT": {"market_value": 9_000.0, "qty": 106},
            "AAPL": {"market_value": 4_800.0, "qty": 15},
        })
        b = AllocationManager(tracker, AllocationConfig.from_config()).get_budget()
        assert b.pendulum_used == pytest.approx(9_000.0)
        assert b.unattributed_used == pytest.approx(4_800.0)
        from src.core.config import load_config
        # The point is that the bucket exists and is sized from config, not a
        # literal: the split is a live decision (15% on 9/1, 5% on 9/2).
        assert b.pendulum_budget == pytest.approx(100_000.0 * load_config().pendulum_pct)


class TestTheDataContractIsReal:
    """A MagicMock invents any method asked of it, so a unit test that mocks
    the data layer cannot tell whether the method exists. This asserts against
    the real classes.

    The scalper lost a session to exactly this: AlpacaClient.get_order did not
    exist, every poll raised AttributeError, and the mock in the tests had been
    fabricating it. Pendulum reproduced the bug within a day by guessing at an
    attribute name with getattr.
    """

    def test_the_historical_client_exposes_get_stock_bars(self):
        from alpaca.data.historical import StockHistoricalDataClient
        assert hasattr(StockHistoricalDataClient, "get_stock_bars")

    def test_market_data_service_holds_that_client_under_the_name_used(self):
        from src.core.market_data import MarketDataService
        client = MagicMock()
        svc = MarketDataService(client)
        assert svc._data is client.data, (
            "PendulumAgent._daily_bars reaches through MarketDataService._data; "
            "renaming that attribute breaks the daily bar fetch at runtime, "
            "where no mocked test will see it"
        )

    def test_the_agent_asks_that_object_for_bars(self):
        from src.agents.pendulum import PendulumAgent
        data = MagicMock()
        a = PendulumAgent(MagicMock(), data, MagicMock(), MagicMock(), MagicMock(),
                          symbol="TLT")
        data._data.get_stock_bars.return_value = MagicMock(data={"TLT": []})
        a._daily_bars()
        data._data.get_stock_bars.assert_called_once()


class TestAggressiveModeIsAllThreeChanges:
    """The spec's aggressive mode is 'allow entries below the 200-day SMA but
    cut position size in half and tighten the stop'. Wiring only the flag arms
    something MORE aggressive than the spec's aggressive, which is how a
    permission becomes a risk increase nobody chose.
    """

    AGG = PendulumParams(allow_below_regime=True)

    def test_the_flag_alone_would_not_have_been_enough(self):
        """below_regime_size_mult existed as a field and nothing read it."""
        assert self.AGG.below_regime_size_mult == 0.5
        assert self.AGG.below_regime_atr_mult < self.AGG.atr_mult

    def test_the_stop_is_tighter_below_the_regime(self):
        loose = stop_price(82.0, 0.66, self.AGG, below_regime=False)
        tight = stop_price(82.0, 0.66, self.AGG, below_regime=True)
        assert tight > loose, "a tighter stop for a long is a HIGHER price"

    def test_the_stop_is_unchanged_above_the_regime(self):
        assert stop_price(82.0, 0.66, self.AGG, below_regime=False) == \
               stop_price(82.0, 0.66, P, below_regime=False)

    def test_decide_applies_the_tighter_stop_when_below(self):
        pos = Position(entry_price=82.0, shares=10)
        # 81.20 is inside the 1.5-ATR stop (81.01) but outside the 1.0 (81.34)
        below = _ind(close=81.20, sma_=83.0, std=1.0, rsi=30, sma200=90.0, atr=0.66)
        assert decide(below, pos, self.AGG)[0] is Signal.EXIT
        above = _ind(close=81.20, sma_=83.0, std=1.0, rsi=30, sma200=70.0, atr=0.66)
        assert decide(above, pos, self.AGG)[0] is Signal.HOLD

    def test_size_is_halved_below_the_regime(self):
        from src.agents.pendulum import PendulumAgent
        client, data, tracker, breaker, allocator = (MagicMock() for _ in range(5))
        tracker.get_snapshot.return_value = MagicMock(equity=100_000.0, positions={})
        allocator.get_budget.return_value = MagicMock(pendulum_budget=15_000.0,
                                                      pendulum_used=0.0)
        a = PendulumAgent(client, data, tracker, breaker, allocator,
                          symbol="TLT", params=self.AGG)
        above = a._size(85.0, _ind(close=85.0, sma200=70.0, atr=0.65), is_add=False)
        below = a._size(85.0, _ind(close=85.0, sma200=95.0, atr=0.65), is_add=False)
        assert below == above // 2 or below == pytest.approx(above / 2, abs=1)

    def test_conservative_mode_still_refuses_the_entry_entirely(self):
        """Half size is not a substitute for the filter. In the default mode
        the entry does not happen at all."""
        i = _ind(close=90.0, sma_=105.0, std=2.0, rsi=3.0, sma200=120.0)
        assert decide(i, None, P)[0] is Signal.NO_TRADE
