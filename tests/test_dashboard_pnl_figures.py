"""The dashboard shows two P&L figures and says which is which."""

from __future__ import annotations

import sys

sys.argv = ["streamlit", "run"]
from dashboard.app import STARTING_EQUITY, pnl_figures  # noqa: E402


def test_today_is_against_the_prior_close_and_total_against_the_100k_start():
    f = pnl_figures(equity=99_647.0, last_equity=99_017.56)
    assert round(f["today"], 2) == 629.44
    assert round(f["since_start"], 2) == -353.0
    assert STARTING_EQUITY == 100_000.0


def test_percentages_use_their_own_bases():
    f = pnl_figures(equity=101_000.0, last_equity=100_000.0, start=100_000.0)
    assert f["today_pct"] == 1.0 and f["since_start_pct"] == 1.0
    g = pnl_figures(equity=101_000.0, last_equity=99_000.0, start=100_000.0)
    assert abs(g["today_pct"] - 2.0202) < 1e-3 and g["since_start_pct"] == 1.0


def test_a_missing_prior_close_does_not_divide_by_zero():
    f = pnl_figures(equity=100.0, last_equity=0.0)
    assert f["today_pct"] == 0.0
