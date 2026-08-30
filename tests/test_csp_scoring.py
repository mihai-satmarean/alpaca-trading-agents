"""Scoring cash-secured puts on what they actually pay.

The original scanner never read an option's price. It computed
    estimated_premium = underlying_price * min_premium_pct
    premium_pct       = estimated_premium / strike
which depends on the underlying and the strike but never on the contract, and
because strike is the denominator it RISES as strike falls. Deeper out-of-the-
money puts therefore scored highest while paying the least: the ranking was
inverted against real premium. `min_premium_pct` was also used as the definition
of premium rather than as a floor to reject on.

These tests are the specification. Anything that passes them prices a put on its
own bid and ranks by return on the capital the trade actually ties up.
"""

from __future__ import annotations

import pytest

from src.strategies.csp_scoring import QuotedPut, ScoringConfig, evaluate, rank


def _put(symbol="SPY261218P00450000", strike=450.0, dte=30, bid=4.50, ask=4.70,
         open_interest=1000, delta=-0.28) -> QuotedPut:
    return QuotedPut(symbol=symbol, strike=strike, days_to_expiry=dte, bid=bid,
                     ask=ask, open_interest=open_interest, delta=delta)


def _cfg(**kw) -> ScoringConfig:
    base = dict(min_premium_pct=0.005, min_open_interest=100,
                max_delta=-0.30, min_dte=7, max_dte=45)
    base.update(kw)
    return ScoringConfig(**base)


class TestPremiumComesFromTheContract:
    def test_credit_is_the_bid_times_one_hundred(self):
        """We are selling, so we receive the bid. Using mid or ask books a credit
        we were never offered."""
        e = evaluate(_put(bid=4.50, ask=4.70), _cfg())
        assert e.credit == pytest.approx(450.0)

    def test_collateral_is_strike_times_one_hundred(self):
        e = evaluate(_put(strike=450.0), _cfg())
        assert e.collateral == pytest.approx(45_000.0)

    def test_return_on_capital_uses_collateral_not_underlying_price(self):
        e = evaluate(_put(strike=450.0, bid=4.50), _cfg())
        assert e.return_on_capital == pytest.approx(450.0 / 45_000.0)

    def test_two_puts_at_the_same_strike_are_separated_by_their_bids(self):
        """The old formula gave these an identical score: it never saw the bid."""
        rich = evaluate(_put(bid=6.00), _cfg())
        poor = evaluate(_put(bid=2.00), _cfg())
        assert rich.score > poor.score


class TestRankingIsNotInverted:
    def test_the_put_that_pays_more_wins(self):
        """Both clear the premium floor, so this isolates ranking from filtering."""
        near = _put(symbol="NEAR", strike=445.0, bid=5.00)   # 1.12% on capital
        far = _put(symbol="FAR", strike=430.0, bid=2.60)     # 0.60%, still above the floor
        ordered = rank([far, near], _cfg())
        assert [e.put.symbol for e in ordered] == ["NEAR", "FAR"]

    def test_the_exact_inversion_the_old_scorer_produced(self):
        """Old formula: score rose as strike fell, independent of the bid. On this
        pair it preferred FAR, which returns a third as much on more capital."""
        near = _put(symbol="NEAR", strike=445.0, bid=5.00)   # 1.12% on capital
        far = _put(symbol="FAR", strike=430.0, bid=1.20)     # 0.28% on capital
        old_near = (450.0 * 0.005) / near.strike
        old_far = (450.0 * 0.005) / far.strike
        assert old_far > old_near                            # the inversion
        assert evaluate(near, _cfg()).score > evaluate(far, _cfg()).score

    def test_shorter_dated_wins_at_equal_return_on_capital(self):
        """Same money back sooner is a better annualized rate."""
        quick = _put(symbol="Q", dte=14, bid=4.50)
        slow = _put(symbol="S", dte=42, bid=4.50)
        assert evaluate(quick, _cfg()).annualized > evaluate(slow, _cfg()).annualized
        assert [e.put.symbol for e in rank([slow, quick], _cfg())] == ["Q", "S"]


class TestAnnualisation:
    def test_thirty_day_put_annualises_by_the_dte_ratio(self):
        e = evaluate(_put(strike=450.0, bid=4.50, dte=30), _cfg())
        assert e.annualized == pytest.approx((450.0 / 45_000.0) * (365 / 30))

    def test_zero_dte_does_not_divide_by_zero(self):
        """dte=0 is explicitly permitted here to isolate the arithmetic."""
        e = evaluate(_put(dte=0), _cfg(min_dte=0))
        assert e.annualized == 0.0

    def test_zero_dte_is_rejected_under_the_normal_window(self):
        assert evaluate(_put(dte=0), _cfg()).rejected is not None


class TestRejection:
    def test_no_bid_is_rejected_as_unsellable(self):
        """A contract with no bid cannot be sold at any price we would accept."""
        assert evaluate(_put(bid=0.0), _cfg()).rejected is not None

    def test_negative_or_missing_bid_is_rejected(self):
        assert evaluate(_put(bid=-1.0), _cfg()).rejected is not None

    def test_min_premium_pct_is_a_floor_not_a_definition(self):
        """0.10 credit on a 450 strike is 0.02% on capital, under the 0.5% floor."""
        e = evaluate(_put(bid=0.10), _cfg(min_premium_pct=0.005))
        assert e.rejected is not None
        assert e.return_on_capital == pytest.approx(10.0 / 45_000.0)

    def test_premium_exactly_at_the_floor_is_accepted(self):
        bid = 0.005 * 450.0
        assert evaluate(_put(bid=bid), _cfg(min_premium_pct=0.005)).rejected is None

    def test_thin_open_interest_is_rejected(self):
        assert evaluate(_put(open_interest=5), _cfg(min_open_interest=100)).rejected is not None

    def test_delta_beyond_the_configured_limit_is_rejected(self):
        """max_delta -0.30 means do not sell puts more deltas than that. -0.45 is
        further in the money and must be refused. This filter was configured in
        strategies.yml and implemented nowhere."""
        assert evaluate(_put(delta=-0.45), _cfg(max_delta=-0.30)).rejected is not None
        assert evaluate(_put(delta=-0.20), _cfg(max_delta=-0.30)).rejected is None

    def test_missing_delta_does_not_reject(self):
        """Alpaca does not always return greeks. Absent is not disqualifying."""
        assert evaluate(_put(delta=None), _cfg()).rejected is None

    def test_dte_outside_the_window_is_rejected(self):
        assert evaluate(_put(dte=3), _cfg(min_dte=7)).rejected is not None
        assert evaluate(_put(dte=90), _cfg(max_dte=45)).rejected is not None

    def test_rejection_reason_is_a_readable_string(self):
        r = evaluate(_put(bid=0.0), _cfg()).rejected
        assert isinstance(r, str) and r


class TestRank:
    def test_rank_drops_rejected_candidates(self):
        good = _put(symbol="GOOD", bid=4.50)
        dead = _put(symbol="DEAD", bid=0.0)
        thin = _put(symbol="THIN", open_interest=1)
        assert [e.put.symbol for e in rank([good, dead, thin], _cfg())] == ["GOOD"]

    def test_rank_is_descending_by_score(self):
        puts = [_put(symbol=f"P{i}", bid=b) for i, b in enumerate([2.0, 6.0, 4.0])]
        scores = [e.score for e in rank(puts, _cfg())]
        assert scores == sorted(scores, reverse=True)

    def test_empty_input_gives_empty_output(self):
        assert rank([], _cfg()) == []

    def test_all_rejected_gives_empty_output(self):
        assert rank([_put(bid=0.0), _put(bid=0.0)], _cfg()) == []


class TestScoreProperties:
    def test_score_is_monotonic_in_the_bid(self):
        prev = None
        for bid in [3.0, 4.0, 6.0, 8.0]:
            s = evaluate(_put(bid=bid), _cfg()).score
            if prev is not None:
                assert s > prev
            prev = s

    def test_liquidity_breaks_a_tie_between_equal_returns(self):
        deep = _put(symbol="DEEP", open_interest=5000)
        shallow = _put(symbol="SHALLOW", open_interest=150)
        assert evaluate(deep, _cfg()).score > evaluate(shallow, _cfg()).score

    def test_score_is_zero_for_a_rejected_candidate(self):
        assert evaluate(_put(bid=0.0), _cfg()).score == 0.0


class TestStrategyRefusesToGuess:
    """The scanner must not trade a contract whose price it does not know.

    The original code answered "what does this pay?" with
    `underlying_price * min_premium_pct` and sold on that. These assert the
    replacement refuses instead.
    """

    def _strategy(self, quote_provider):
        from unittest.mock import MagicMock
        from src.strategies.csp import CashSecuredPutStrategy

        client, chain, data, tracker = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        data.get_latest_quote.return_value = MagicMock(mid=470.0)
        contract = MagicMock()
        contract.symbol = "SPY261218P00450000"
        contract.strike_price = 450.0
        contract.days_to_expiry = 30
        contract.open_interest = 1000
        chain.get_puts.return_value = [contract]
        chain.filter_by_otm_pct.return_value = [contract]

        return CashSecuredPutStrategy(
            client, chain, data, tracker, quote_provider=quote_provider
        )

    def test_no_quote_provider_means_no_opportunities(self):
        assert self._strategy(None).scan(["SPY"]) == []

    def test_a_contract_with_no_quote_is_skipped_not_guessed(self):
        assert self._strategy(lambda syms: {}).scan(["SPY"]) == []

    def test_a_failing_quote_provider_does_not_raise_or_trade(self):
        def boom(_):
            raise RuntimeError("venue down")

        assert self._strategy(boom).scan(["SPY"]) == []

    def test_a_priced_contract_is_scored_from_its_bid(self):
        provider = lambda syms: {s: {"bid": 5.00, "ask": 5.20, "delta": -0.28} for s in syms}
        opps = self._strategy(provider).scan(["SPY"])
        assert len(opps) == 1
        assert opps[0].cash_required == pytest.approx(45_000.0)
        assert opps[0].premium_pct == pytest.approx(500.0 / 45_000.0)

    def test_a_bid_below_the_floor_produces_no_opportunity(self):
        provider = lambda syms: {s: {"bid": 0.10, "ask": 0.20, "delta": -0.10} for s in syms}
        assert self._strategy(provider).scan(["SPY"]) == []
