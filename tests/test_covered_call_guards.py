"""Shares that back a short call must never be sold or re-written against.

Found on 2026-09-02, the afternoon the first real covered call went on the
book (KO, 100 shares, one Oct-2 $93 call). Two automated paths would have
undone it: SIXFOLD's disposal sells the shares when a name scores below 50
(a blank fundamentals feed scores 0), leaving the call naked; and the
covered-call scanner, which re-scans every five minutes for positions of 100+
shares, would have sold a second call against the same shares, at market.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.strategies.covered_call import CoveredCallStrategy
from src.strategies.sixfold_executor import SixfoldExecutor

CALL = "KO261002C00093000"


def _snapshot(positions):
    snap = MagicMock(); snap.positions = positions; snap.equity = 100_000.0
    return snap


class TestSixfoldDisposalGuard:
    def _executor(self, positions, flagged):
        tracker = MagicMock(); tracker.get_snapshot.return_value = _snapshot(positions)
        analyst = MagicMock(); analyst.get_disposal_candidates.return_value = flagged
        analyst.scores = {}
        return SixfoldExecutor(MagicMock(), MagicMock(), tracker, MagicMock(),
                               MagicMock(), analyst, excluded=set())

    def test_short_calls_are_found_with_the_real_parser(self):
        """The first version of this guard read occ.underlying; the field is
        occ.root. The AttributeError was caught, the guard returned an empty
        set, and every name read as uncovered: fail-open through a typo."""
        ex = self._executor({"KO": {"qty": 100, "market_value": 8_886.0},
                             CALL: {"qty": -1, "market_value": -45.0}}, [])
        assert ex._underlyings_with_short_calls() == {"KO"}

    def test_a_name_with_a_covered_call_is_not_disposed(self):
        ex = self._executor({"KO": {"qty": 100, "market_value": 8_886.0},
                             CALL: {"qty": -1, "market_value": -45.0}}, ["KO"])
        assert ex.run_disposals() == []
        ex._client.close_position.assert_not_called()
        assert any("naked" in r["reason"] for r in ex.last_rejections)

    def test_a_name_without_a_call_is_still_disposed(self):
        ex = self._executor({"PG": {"qty": 34, "market_value": 5_000.0}}, ["PG"])
        ex.run_disposals()
        ex._client.close_position.assert_called_once_with("PG")

    def test_a_long_call_does_not_count_as_cover(self):
        ex = self._executor({"KO": {"qty": 100, "market_value": 8_886.0},
                             CALL: {"qty": +1, "market_value": 45.0}}, ["KO"])
        ex.run_disposals()
        ex._client.close_position.assert_called_once_with("KO")


class TestCoveredCallScannerGuard:
    def _strategy(self, positions):
        tracker = MagicMock(); tracker.get_snapshot.return_value = _snapshot(positions)
        chain = MagicMock()
        cand = MagicMock(); cand.strike_price = 93.0; cand.days_to_expiry = 30; cand.symbol = CALL
        chain.get_calls.return_value = [cand]; chain.select_best_expiry.return_value = [cand]
        return CoveredCallStrategy(MagicMock(), chain, MagicMock(), tracker)

    def _long(self, qty, px=88.86):
        return {"qty": qty, "side": "long", "current_price": px, "market_value": qty * px}

    def test_shares_already_backing_a_call_are_not_written_against_again(self):
        st = self._strategy({"KO": self._long(100), CALL: {"qty": -1, "side": "short",
                                                             "current_price": 0.45, "market_value": -45.0}})
        assert st.scan() == []

    def test_free_shares_beyond_the_call_can_be_written(self):
        st = self._strategy({"KO": self._long(200), CALL: {"qty": -1, "side": "short",
                                                             "current_price": 0.45, "market_value": -45.0}})
        opps = st.scan()
        assert len(opps) == 1 and opps[0].contracts_possible == 1

    def test_an_uncovered_round_lot_still_scans(self):
        st = self._strategy({"KO": self._long(100)})
        opps = st.scan()
        assert len(opps) == 1 and opps[0].contracts_possible == 1

    def test_option_positions_are_never_treated_as_shares(self):
        st = self._strategy({CALL: {"qty": 150, "side": "long", "current_price": 0.45, "market_value": 6_750.0}})
        assert st.scan() == []
