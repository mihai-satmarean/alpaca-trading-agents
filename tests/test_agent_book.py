"""Per-agent sleeve classification for the cockpit."""

from __future__ import annotations

from src.core.agent_book import build_books, classify_symbol


class TestClassify:
    def test_occ_put_is_csp(self):
        assert classify_symbol("MARA260911P00010000", {"QQQ"}) == "csp"

    def test_occ_call_is_covered_call(self):
        assert classify_symbol("AAPL260918C00200000", {"QQQ"}) == "covered_call"

    def test_picker_victim_is_vampire_not_sixfold(self):
        assert classify_symbol("IWM", {"QQQ", "IWM", "SQ", "XLI"}) == "vampire"

    def test_other_equity_is_sixfold(self):
        assert classify_symbol("NVDA", {"QQQ", "IWM"}) == "sixfold"

    def test_tlt_is_pendulum_not_sixfold(self):
        assert classify_symbol("TLT", {"QQQ"}) == "pendulum"


class TestBuildBooks:
    def test_csp_invested_is_collateral_not_mark(self):
        books = build_books(
            [{
                "symbol": "MARA260911P00010000",
                "qty": -1,
                "market_value": -32.0,
                "unrealized_pl": -4.0,
            }],
            equity=100_000.0,
            vampire_symbols={"QQQ"},
            snap={},
            fills={},
        )
        by = {b.agent: b for b in books}
        assert by["csp"].invested == 1000.0
        assert by["csp"].unrealized_pnl == -4.0
        assert by["csp"].positions == 1

    def test_vampire_and_sixfold_split(self):
        books = build_books(
            [
                {"symbol": "IWM", "qty": -8, "market_value": -2336.0, "unrealized_pl": 0.5},
                {"symbol": "NVDA", "qty": 2, "market_value": 350.0, "unrealized_pl": 1.2},
            ],
            equity=100_000.0,
            vampire_symbols={"IWM", "QQQ"},
            snap={"vampire": {"IWM": {"daily_pnl": 3.0}}},
            fills={},
        )
        by = {b.agent: b for b in books}
        assert by["vampire"].invested == 2336.0
        assert by["vampire"].unrealized_pnl == 0.5
        assert by["vampire"].realized_pnl == 3.0
        assert by["sixfold"].invested == 350.0
        assert by["sixfold"].unrealized_pnl == 1.2
        assert abs(by["vampire"].budget - 15_000) < 1
        assert abs(by["sixfold"].budget - 45_000) < 1
        assert abs(by["reserve"].budget - 10_000) < 1
        assert abs(by["pendulum"].budget - 15_000) < 1
