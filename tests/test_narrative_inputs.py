"""The narrative can only describe what something records.

CSP refusals were computed and thrown away, and the scalper's activity lived
inside per-symbol engines that nothing asked. Both are the interesting half of a
session: a report saying "no trades" is far more useful when it can say why, and
a report covering only the options sleeve silently omits a fifth of the account.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.strategies.vampire_engine import VampireConfig, VampireEngine


class TestCSPRecordsItsRefusals:
    def _strategy(self, quotes):
        from datetime import date, timedelta

        from src.core.options_chain import OptionCandidate
        from src.strategies.csp import CashSecuredPutStrategy

        client, chain, data, tracker = (MagicMock() for _ in range(4))
        data.get_latest_quote.return_value = MagicMock(mid=11.60)
        contracts = [
            OptionCandidate(symbol=s, underlying="CLF", contract_type="put",
                            strike_price=11.0, expiration=date.today() + timedelta(days=30),
                            open_interest=oi, premium_estimate=None, days_to_expiry=30)
            for s, oi in [("CLF261016P00011000", 900), ("CLF261016P00010000", 5)]
        ]
        chain.get_puts.return_value = contracts
        chain.filter_by_otm_pct.return_value = contracts
        return CashSecuredPutStrategy(client, chain, data, tracker,
                                      quote_provider=lambda syms: quotes)

    def test_refusals_are_recorded_with_a_reason(self):
        s = self._strategy({
            "CLF261016P00011000": {"bid": 0.01, "ask": 0.05},   # under the floor
            "CLF261016P00010000": {"bid": 0.40, "ask": 0.45},   # thin open interest
        })
        s.scan(["CLF"])
        assert s.last_rejections
        for r in s.last_rejections:
            assert r["symbol"] and r["reason"]

    def test_an_accepted_candidate_is_not_listed_as_refused(self):
        s = self._strategy({"CLF261016P00011000": {"bid": 0.40, "ask": 0.45}})
        s.scan(["CLF"])
        assert all(r["symbol"] != "CLF261016P00011000" for r in s.last_rejections)

    def test_refusals_reset_between_scans(self):
        s = self._strategy({"CLF261016P00011000": {"bid": 0.01, "ask": 0.05}})
        s.scan(["CLF"])
        first = len(s.last_rejections)
        s.scan(["CLF"])
        assert len(s.last_rejections) == first

    def test_refusal_list_is_capped(self):
        """A wide chain can refuse hundreds. A notification cannot carry them."""
        s = self._strategy({})
        s.last_rejections = [{"symbol": f"X{i}", "reason": "r"} for i in range(500)]
        s.scan(["CLF"])
        assert len(s.last_rejections) <= 20

    def test_no_quote_provider_records_that_as_the_reason(self):
        from src.strategies.csp import CashSecuredPutStrategy

        s = CashSecuredPutStrategy(*(MagicMock() for _ in range(4)), quote_provider=None)
        s.scan(["CLF"])
        assert s.last_rejections and "quote" in s.last_rejections[0]["reason"].lower()


class TestVampireReportsItsActivity:
    def _agent(self, symbols=("SPY", "QQQ")):
        from src.agents.vampire import VampireAgent

        client, data, tracker, breaker, allocator = (MagicMock() for _ in range(5))
        data.get_latest_quote.return_value = MagicMock(mid=700.0)
        allocator.get_budget.return_value = MagicMock(vampire_budget=20_000.0)
        return VampireAgent(client, data, tracker, breaker, allocator, symbols=list(symbols))

    def test_summary_covers_every_symbol(self):
        a = self._agent()
        summary = a.activity_summary()
        assert {row["symbol"] for row in summary} == {"SPY", "QQQ"}

    def test_summary_reports_trades_and_position(self):
        a = self._agent(["SPY"])
        e = a._engines["SPY"]
        e._is_market_hours = lambda: True
        e.cfg.max_notional = None
        e.tick(699.0, vwap=700.0)          # opens a long
        row = a.activity_summary()[0]
        assert row["trades"] >= 1
        assert row["net_position"] != 0
        assert "realized_pnl" in row and "state" in row

    def test_a_quiet_engine_reports_zeroes_not_absence(self):
        row = self._agent(["SPY"]).activity_summary()[0]
        assert row["trades"] == 0 and row["net_position"] == 0

    def test_summary_never_raises_on_a_broken_engine(self):
        a = self._agent(["SPY"])
        a._engines["SPY"] = object()       # nothing a report can read
        assert a.activity_summary() == [] or a.activity_summary()[0]["symbol"] == "SPY"
