"""Config values must reach the objects that act on them.

Three of the four wirings here were found broken on 2026-09-02, each with the
same shape: a value in strategies.yml, a dataclass or hardcoded default that
happened to match it, and nothing in between. Every test asserts the CALL
SITE (what the coordinator passes), not that a hand-built object would honour
the value if it were passed. Deleting the wiring must fail these.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from src.core.config import load_config


def _coordinator_with(patched: dict):
    """Construct the coordinator with the named classes patched; ignore any
    later failure since only the constructor calls matter."""
    from src.agents import coordinator as C
    ctx = {name: patch.object(C, name) for name in patched}
    mocks = {name: cm.__enter__() for name, cm in ctx.items()}
    try:
        try:
            C.Coordinator()
        except Exception:
            pass
    finally:
        for cm in ctx.values():
            cm.__exit__(None, None, None)
    return mocks


class TestSixfoldIsConfiguredFromTheFile:
    def test_the_analyst_gets_the_yml_universe_and_bands(self):
        cfg = load_config()
        m = _coordinator_with({"SixfoldAnalystAgent": None, "SixfoldExecutor": None,
                               "VampireAgent": None, "PendulumAgent": None,
                               "OptionsIncomeAgent": None, "RiskManagerAgent": None,
                               "AlpacaClient": None, "MarketDataService": None,
                               "PositionTracker": None})
        kw = m["SixfoldAnalystAgent"].call_args.kwargs
        assert kw.get("universe") == cfg.sixfold_universe
        assert "KO" in kw["universe"] and "SPY" not in kw["universe"], (
            "the hardcoded list carried SPY/QQQ (score 0) and dropped KO/PEP"
        )
        assert kw.get("buy_threshold") == cfg.sixfold_buy_threshold == 60.0
        assert kw.get("dispose_threshold") == cfg.sixfold_dispose_threshold

    def test_the_executor_gets_the_yml_concurrency_limit(self):
        cfg = load_config()
        m = _coordinator_with({"SixfoldAnalystAgent": None, "SixfoldExecutor": None,
                               "VampireAgent": None, "PendulumAgent": None,
                               "OptionsIncomeAgent": None, "RiskManagerAgent": None,
                               "AlpacaClient": None, "MarketDataService": None,
                               "PositionTracker": None})
        assert m["SixfoldExecutor"].call_args.kwargs.get("max_concurrent") \
            == cfg.sixfold_max_concurrent == 14

    def test_the_analyst_honours_its_thresholds(self):
        from src.agents.sixfold_analyst import SixfoldAnalystAgent
        a = SixfoldAnalystAgent(MagicMock(), buy_threshold=60.0)
        s = MagicMock(); s.in_scope = True; s.composite_score = 61.9
        s.lens_results = []; s.confidence = MagicMock(value="screening"); s.symbol = "AMZN"
        assert a._score_to_recommendation(s).action == "buy_candidate"
        a65 = SixfoldAnalystAgent(MagicMock(), buy_threshold=65.0)
        assert a65._score_to_recommendation(s).action == "hold"


class TestVampireConfigReachesTheEngine:
    def test_the_coordinator_passes_every_yml_key_not_just_the_pause(self):
        cfg = load_config()
        m = _coordinator_with({"SixfoldAnalystAgent": None, "SixfoldExecutor": None,
                               "VampireAgent": None, "PendulumAgent": None,
                               "OptionsIncomeAgent": None, "RiskManagerAgent": None,
                               "AlpacaClient": None, "MarketDataService": None,
                               "PositionTracker": None})
        ov = m["VampireAgent"].call_args.kwargs.get("config_overrides") or {}
        for k in ("tick_threshold", "position_size", "max_position", "max_daily_loss"):
            assert k in ov, f"{k} from strategies.yml never reached VampireConfig"
        assert ov["max_daily_loss"] == cfg.vampire["max_daily_loss"]


class TestDailyPnlBaselineIsTheBrokersNotTheProcessBoot:
    def _tracker(self, equity, last_equity):
        from src.core.position_tracker import PositionTracker
        client = MagicMock()
        acct = MagicMock(); acct.equity = equity; acct.cash = 1000.0
        acct.buying_power = 1.0; acct.last_equity = last_equity
        client.get_account.return_value = acct
        client.get_positions.return_value = []
        return PositionTracker(client)

    def test_a_restart_mid_day_does_not_zero_the_days_pnl(self):
        """Live on 2026-09-02: tracker -$0.03, broker +$502. The breaker was
        measuring loss since the last restart."""
        t = self._tracker(equity=99_519.55, last_equity=99_017.56)
        assert t.get_snapshot().daily_pnl == pytest.approx(502.0, abs=0.01)

    def test_the_baseline_rolls_with_the_session_date(self):
        t = self._tracker(equity=100.0, last_equity=90.0)
        assert t.get_snapshot().daily_pnl == pytest.approx(10.0)
        t._client.get_account.return_value.last_equity = 100.0
        t._client.get_account.return_value.equity = 103.0
        with patch("src.core.position_tracker.datetime") as D:
            D.now.return_value = dt.datetime(2099, 1, 1, 10, 0)
            assert t.get_snapshot().daily_pnl == pytest.approx(3.0), (
                "a process running across midnight must re-baseline"
            )

    def test_reset_daily_uses_the_previous_close_too(self):
        t = self._tracker(equity=105.0, last_equity=100.0)
        t.reset_daily()
        assert t._daily_start_equity == 100.0
