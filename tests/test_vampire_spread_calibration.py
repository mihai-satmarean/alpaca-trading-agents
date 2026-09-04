"""2026-09-04: the Vampire's trigger was calibrated on the opening auction and
then frozen for the session.

run_live.py starts the agent within ~30s of the opening bell and
_apply_spread_thresholds ran ~38s in, sampling the book five times a fifth of
a second apart -- one second of evidence, drawn from the widest moment of the
day. QQQ's IEX median spread on 2026-09-03 was $0.740 in the first minute,
$0.110 by 09:35 and $0.050 by 11:10, so the trigger froze at $2.05 against a
book that traded at $0.05. QQQ produced 3 realized round trips that session;
TQQQ, whose penny spread barely widens at the auction, produced 85. It
reproduced on a second day: 09:30:51 read $1.060 -> $2.6500, while that same
day's 12:25 restart read $0.040 -> $0.1000.

The read itself was never wrong -- in the exact 4-second window it sampled,
the true IEX median was $0.750 against the $0.820 it logged. Feed mismatch and
caching were both ruled out. The defect was WHEN it sampled and that it never
sampled again.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.agents.vampire import VampireAgent


def _agent(sample, symbols=("QQQ",), configured=0.05):
    data = MagicMock()
    data.recent_spread.return_value = sample
    a = VampireAgent(MagicMock(), data, MagicMock(), MagicMock(), MagicMock(),
                     symbols=list(symbols),
                     config_overrides={"tick_threshold": configured})
    return a


def _sample(median, p90=None, n=5000, price=700.0, window=20):
    return {"n": n, "median": median, "p90": p90 if p90 is not None else median * 2,
            "price": price, "window_minutes": window}


class TestTheThresholdComesFromAWindowNotFiveReads:
    def test_a_normal_book_sets_the_trigger_at_two_and_a_half_spreads(self):
        a = _agent(_sample(0.05))
        a._apply_spread_thresholds()
        assert a._engines["QQQ"].cfg.tick_threshold == 0.125

    def test_it_reads_a_window_rather_than_polling_the_latest_quote(self):
        """The old routine called get_latest_quote five times. If that returns,
        the regression is back."""
        a = _agent(_sample(0.05))
        a._apply_spread_thresholds()
        a._data.recent_spread.assert_called_with("QQQ")
        assert a._data.get_latest_quote.call_count == 0

    def test_the_opening_auction_no_longer_decides_the_session(self):
        """$0.740 was the real first-minute median on 2026-09-03. Under the old
        code that became a $1.85 trigger for the whole day. The window
        measurement returns the session's own spread instead."""
        opening = _agent(_sample(0.740))
        opening._apply_spread_thresholds()
        auction_threshold = opening._engines["QQQ"].cfg.tick_threshold

        settled = _agent(_sample(0.050))
        settled._apply_spread_thresholds()
        assert settled._engines["QQQ"].cfg.tick_threshold < auction_threshold / 10


class TestFailingSafe:
    def test_no_sample_keeps_the_configured_threshold(self):
        """A wide stale trigger only stops the symbol trading. A trigger that
        is too NARROW trades constantly at negative edge, which is the
        expensive direction, so the fallback must never widen participation."""
        a = _agent(None, configured=0.05)
        a._apply_spread_thresholds()
        assert a._engines["QQQ"].cfg.tick_threshold == 0.05

    def test_a_raising_data_layer_does_not_stop_the_agent(self):
        a = _agent(_sample(0.05))
        a._data.recent_spread.side_effect = RuntimeError("feed down")
        a._apply_spread_thresholds()
        assert a._engines["QQQ"].cfg.tick_threshold == 0.05

    def test_the_minimum_threshold_floor_still_applies(self):
        a = _agent(_sample(0.001))
        a._apply_spread_thresholds()
        assert a._engines["QQQ"].cfg.tick_threshold == 0.02

    def test_a_book_wider_than_the_max_fraction_of_price_is_refused(self):
        """The HOOD case: a 3.7%-wide book is not the book this strategy
        trades, and deriving a trigger from it retires the symbol silently."""
        a = _agent(_sample(5.0, price=100.0), configured=0.05)
        a._apply_spread_thresholds()
        assert a._engines["QQQ"].cfg.tick_threshold == 0.05


class TestRecalibrationDuringTheSession:
    def test_the_threshold_is_re_derived_not_frozen_at_startup(self):
        a = _agent(_sample(0.740))
        a._apply_spread_thresholds()
        assert a._engines["QQQ"].cfg.tick_threshold == 1.85
        a._data.recent_spread.return_value = _sample(0.050)
        a._recalibrate_spreads()
        assert a._engines["QQQ"].cfg.tick_threshold == 0.125

    def test_recalibration_never_raises(self):
        a = _agent(_sample(0.05))
        a._data.recent_spread.side_effect = RuntimeError("boom")
        a._recalibrate_spreads()

    def test_the_regime_loop_recalibrates(self):
        """Wiring, asserted at the call site: deleting the call from the loop
        must fail a test, or the fix silently reverts to startup-only."""
        import inspect
        src = inspect.getsource(VampireAgent._regime_loop)
        assert "_recalibrate_spreads" in src


class TestTheDataLayerWindow:
    def test_recent_spread_requests_the_iex_feed_and_returns_a_distribution(self):
        from alpaca.data.enums import DataFeed
        from src.core.market_data import MarketDataService

        class Q:
            def __init__(self, b, a):
                self.bid_price, self.ask_price = b, a

        client = MagicMock()
        client.data.get_stock_quotes.return_value = {"QQQ": [Q(700.00, 700.05)] * 500}
        svc = MarketDataService(client)
        out = svc.recent_spread("QQQ", minutes=20)
        assert out["n"] == 500
        assert round(out["median"], 4) == 0.05
        assert round(out["price"], 2) == 700.02 or round(out["price"], 3) == 700.025
        assert client.data.get_stock_quotes.call_args.args[0].feed == DataFeed.IEX

    def test_too_few_quotes_widens_the_window_then_gives_up(self):
        from src.core.market_data import MarketDataService
        client = MagicMock()
        client.data.get_stock_quotes.return_value = {"QQQ": []}
        svc = MarketDataService(client)
        assert svc.recent_spread("QQQ") is None
        assert client.data.get_stock_quotes.call_count == 3   # 20m, 2h, then a day

    def test_crossed_and_zero_quotes_are_discarded(self):
        from src.core.market_data import MarketDataService

        class Q:
            def __init__(self, b, a):
                self.bid_price, self.ask_price = b, a

        client = MagicMock()
        client.data.get_stock_quotes.return_value = {
            "QQQ": ([Q(700.00, 700.05)] * 300) + ([Q(0, 0)] * 50) + ([Q(701, 700)] * 50)}
        svc = MarketDataService(client)
        out = svc.recent_spread("QQQ")
        assert out["n"] == 300

    def test_a_failing_request_returns_none_rather_than_raising(self):
        from src.core.market_data import MarketDataService
        client = MagicMock()
        client.data.get_stock_quotes.side_effect = RuntimeError("403")
        svc = MarketDataService(client)
        assert svc.recent_spread("QQQ") is None
