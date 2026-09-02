"""SIXFOLD could open positions and never close one.

The analyst has always computed disposal candidates - action "dispose" below
a 50 composite, "avoid" below 40 - and nothing in the codebase ever called
get_disposal_candidates(). The executor read get_buy_candidates() only, so
the largest sleeve was structurally buy-only: a scoring system whose sell
signal was unreachable.

Scope note, because it was the reason this took verification rather than
typing: SPEC 3.8 (exit) is tagged "[UNKNOWN. Second most important gap]",
and the spec's own convention states [UNKNOWN] means implementation is
blocked on it, while [P] means "proposed default that Tashi must confirm or
replace." Every rule inside 3.8 is [P]. So the +35% target, gap-close exit
and 6-month time stop are deliberately NOT implemented - they are the
document author's placeholders, not Tashi's rules. What IS implemented is
the analyst's own disposition bands, which already exist in this codebase
and already drive the buy side.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.core.finance_advisor import AdvisorOpinion, CouncilDecision
from src.strategies.sixfold_executor import SixfoldExecutor

COUNCIL_PATCH = "src.strategies.sixfold_executor.evaluate_equity_buy"


def _approve(symbol, score, fundamentals=None):
    return CouncilDecision(
        action="buy", symbol=symbol, approved=True,
        votes_for=3, votes_against=0, abstentions=0,
        opinions=[AdvisorOpinion("m", "Finance Specialist", "approve", "ok", True)],
        summary="approved",
    )


def _score(value):
    s = MagicMock()
    s.composite_score = value
    return s


def _exec(*, disposals=(), buys=(), positions=None, scores=None,
          excluded=("SPY", "QQQ", "HOOD", "TQQQ"), sleeve=50_000.0):
    client, data, tracker, breaker, allocator, analyst = (MagicMock() for _ in range(6))
    data.get_latest_quote.return_value = MagicMock(mid=100.0)
    tracker.get_snapshot.return_value = MagicMock(
        equity=100_000.0,
        positions={s: {"market_value": v} for s, v in (positions or {}).items()},
    )
    allocator.get_budget.return_value = MagicMock(sixfold_budget=sleeve)
    breaker.check.return_value = True
    breaker.can_trade.return_value = True
    breaker.limits = MagicMock(max_single_trade_pct=0.05)
    analyst.get_disposal_candidates.return_value = list(disposals)
    analyst.get_buy_candidates.return_value = list(buys)
    analyst.scores = MagicMock()
    analyst.scores.get.side_effect = lambda s: (scores or {}).get(s)
    ex = SixfoldExecutor(client, data, tracker, breaker, allocator, analyst,
                         excluded=set(excluded))
    return ex, client


@patch(COUNCIL_PATCH, side_effect=_approve)
class TestADowngradedHoldingIsSold:
    def test_a_flagged_holding_is_closed(self, _m):
        ex, client = _exec(disposals=["JPM"], positions={"JPM": 4_800.0},
                           scores={"JPM": _score(39.0)})
        result = ex.run_cycle()
        client.close_position.assert_called_once_with("JPM")
        assert [d["symbol"] for d in result["disposals"]] == ["JPM"]

    def test_the_disposal_records_the_score_that_triggered_it(self, _m):
        ex, _ = _exec(disposals=["JPM"], positions={"JPM": 4_800.0},
                      scores={"JPM": _score(39.0)})
        d = ex.run_cycle()["disposals"][0]
        assert d["score"] == 39.0 and d["side"] == "sell"

    def test_only_the_intersection_of_flagged_and_held_is_sold(self, _m):
        """The analyst scans 18 names; most flagged ones are not owned.
        Selling a name we do not hold is an unwanted short, not an exit."""
        ex, client = _exec(disposals=["JPM", "INTC", "XOM"],
                           positions={"JPM": 4_800.0},
                           scores={"JPM": _score(39.0)})
        ex.run_cycle()
        client.close_position.assert_called_once_with("JPM")

    def test_nothing_flagged_means_nothing_sold(self, _m):
        ex, client = _exec(disposals=[], positions={"NVDA": 4_800.0})
        ex.run_cycle()
        client.close_position.assert_not_called()

    def test_a_healthy_holding_is_untouched(self, _m):
        ex, client = _exec(disposals=["JPM"], positions={"NVDA": 4_800.0},
                           scores={"NVDA": _score(78.5)})
        ex.run_cycle()
        client.close_position.assert_not_called()


@patch(COUNCIL_PATCH, side_effect=_approve)
class TestItNeverSellsAnotherSleevesPosition:
    def test_an_excluded_symbol_is_not_sold_even_when_flagged(self, _m):
        """The scalper owns HOOD. SIXFOLD closing it would flatten a position
        this strategy never opened, and make the P&L unattributable."""
        ex, client = _exec(disposals=["HOOD"], positions={"HOOD": 3_000.0},
                           scores={"HOOD": _score(30.0)})
        ex.run_cycle()
        client.close_position.assert_not_called()

    def test_that_refusal_is_recorded_rather_than_silent(self, _m):
        ex, _ = _exec(disposals=["HOOD"], positions={"HOOD": 3_000.0},
                      scores={"HOOD": _score(30.0)})
        ex.run_cycle()
        assert any(r["symbol"] == "HOOD" and "another sleeve" in r["reason"]
                   for r in ex.last_rejections)


@patch(COUNCIL_PATCH, side_effect=_approve)
class TestFailuresDoNotCascade:
    def test_a_broker_refusal_on_one_name_does_not_stop_the_others(self, _m):
        """Disposals are attempted in sorted order, so INTC goes first here.
        Its refusal must not prevent JPM from being closed."""
        ex, client = _exec(disposals=["JPM", "INTC"],
                           positions={"JPM": 4_800.0, "INTC": 3_000.0},
                           scores={"JPM": _score(39.0), "INTC": _score(35.0)})
        client.close_position.side_effect = [RuntimeError("rejected"), None]
        result = ex.run_cycle()
        assert client.close_position.call_count == 2
        assert [d["symbol"] for d in result["disposals"]] == ["JPM"]
        assert any(r["symbol"] == "INTC" for r in ex.last_rejections)

    def test_an_unavailable_analyst_disposes_nothing_and_does_not_raise(self, _m):
        ex, client = _exec(positions={"JPM": 4_800.0})
        ex._analyst.get_disposal_candidates.side_effect = RuntimeError("cluster down")
        result = ex.run_cycle()          # must not raise
        client.close_position.assert_not_called()
        assert result["disposals"] == []

    def test_disposals_are_reported_even_when_there_is_no_buy_budget(self, _m):
        """An exit is not conditional on having budget left to buy with."""
        ex, client = _exec(disposals=["JPM"], positions={"JPM": 4_800.0},
                           scores={"JPM": _score(39.0)}, sleeve=0.0)
        result = ex.run_cycle()
        client.close_position.assert_called_once_with("JPM")
        assert result["status"] == "no_sleeve"
        assert [d["symbol"] for d in result["disposals"]] == ["JPM"]


@patch(COUNCIL_PATCH, side_effect=_approve)
class TestOrderingAndGating:
    def test_a_tripped_breaker_blocks_disposals_too(self, _m):
        ex, client = _exec(disposals=["JPM"], positions={"JPM": 4_800.0},
                           scores={"JPM": _score(39.0)})
        ex._breaker.check.return_value = False
        assert ex.run_cycle()["status"] == "breaker_active"
        client.close_position.assert_not_called()

    def test_selling_happens_before_buying(self, _m):
        """A name downgraded this cycle must not be re-bought in the same
        pass, and freed capital should be available to the buy side."""
        ex, client = _exec(disposals=["JPM"], buys=["KO"],
                           positions={"JPM": 4_800.0},
                           scores={"JPM": _score(39.0), "KO": _score(66.0)})
        calls = []
        client.close_position.side_effect = lambda s: calls.append(("sell", s))
        def _buy(req):
            calls.append(("buy", req.symbol))
            return MagicMock(id="1")
        client.submit_order.side_effect = _buy
        ex.run_cycle()
        assert calls[0][0] == "sell" and any(c[0] == "buy" for c in calls)

    def test_an_exit_does_not_require_council_approval(self, _m):
        """The council is a buy gate. Making an exit wait on AI approval means
        a cluster outage traps the system in a deteriorating position."""
        ex, client = _exec(disposals=["JPM"], positions={"JPM": 4_800.0},
                           scores={"JPM": _score(39.0)})
        with patch(COUNCIL_PATCH, side_effect=RuntimeError("council down")):
            ex.run_cycle()
        client.close_position.assert_called_once_with("JPM")
