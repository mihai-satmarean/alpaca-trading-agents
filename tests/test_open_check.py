"""The post-open verification must count what actually happened today.

Yesterday I counted 4,182 order rejects in a window that contained 7, because
the grep matched untimestamped traceback lines. Every reject writes a multi-line
traceback, so an unanchored count multiplies each failure by its stack depth and
sweeps in the whole day's history besides. A monitor that inflates its own alarm
is worse than no monitor: it trains you to ignore it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import scripts.open_check as oc


class TestCollateralBySleeve:
    def test_a_short_put_ties_up_strike_not_market_value(self):
        """The mark is a few hundred dollars; the obligation is thousands."""
        sleeve, amount = oc.collateral("MARA260911P00010000", -5, -145.0)
        assert sleeve == "CSP"
        assert amount == 5000.0          # $10 strike x 100 x 5

    def test_strike_is_read_from_the_last_eight_digits(self):
        _, amount = oc.collateral("RIVN260911P00016000", -3, -183.0)
        assert amount == 4800.0          # $16 x 100 x 3

    def test_scalper_symbols_are_their_market_value(self):
        assert oc.collateral("QQQ", 3, 2145.81) == ("Vampire", 2145.81)

    def test_a_short_is_counted_by_size_not_sign(self):
        """A short is exposure, not negative exposure."""
        assert oc.collateral("HOOD", -20, -2083.20) == ("Vampire", 2083.20)

    def test_everything_else_is_sixfold(self):
        assert oc.collateral("NVDA", 22, 4796.99) == ("SixFold", 4796.99)

    def test_a_retired_scalper_symbol_is_still_the_scalpers(self):
        """SOXL was dropped from config but its position was still ours."""
        assert oc.collateral("SOXL", 12, 1338.48)[0] == "Vampire"


class TestLogSignaturesCountsOnlyToday:
    LOG = (
        "09:15:02 INFO run_live: idling\n"
        "09:31:10 WARNING vampire_engine: BUY submit rejected for HOOD (streak 1): {...}\n"
        "Traceback (most recent call last):\n"
        '  File "/opt/alpaca-agent/src/strategies/vampire_engine.py", line 289\n'
        "    order = self._client.market_order(\n"
        "alpaca.common.exceptions.APIError: submit rejected blah\n"
        "09:31:11 WARNING vampire_engine: BUY submit rejected for HOOD (streak 2): {...}\n"
        "09:31:12 ERROR vampire_engine: HOOD: 5 consecutive rejects, pausing 2s\n"
        "09:32:00 INFO options_income: MCP quote source ready (72 tools)\n"
    )

    def _counts(self, tmp_path, monkeypatch, since="09:30:00"):
        p = tmp_path / "session.err"
        p.write_text(self.LOG)
        monkeypatch.setattr(oc, "LOG", str(p))
        return oc.log_signatures(since)

    def test_tracebacks_do_not_inflate_the_reject_count(self, tmp_path, monkeypatch):
        """Two rejects, each trailing a stack. The answer is two."""
        assert self._counts(tmp_path, monkeypatch)["rejects"] == 2

    def test_lines_before_the_window_are_excluded(self, tmp_path, monkeypatch):
        c = self._counts(tmp_path, monkeypatch, since="09:32:00")
        assert "rejects" not in c
        assert c["mcp_ready"] == 1

    def test_the_backoff_trip_is_seen(self, tmp_path, monkeypatch):
        assert self._counts(tmp_path, monkeypatch)["backoff_trips"] == 1

    def test_a_clean_log_reports_nothing_rather_than_failing(self, tmp_path, monkeypatch):
        p = tmp_path / "session.err"
        p.write_text("09:31:00 INFO run_live: all good\n")
        monkeypatch.setattr(oc, "LOG", str(p))
        assert oc.log_signatures("09:30:00") == {}

    def test_a_missing_log_is_reported_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(oc, "LOG", str(tmp_path / "nope.err"))
        assert oc.log_signatures("09:30:00") == {"log_missing": 1}

    def test_a_failed_poll_is_counted(self, tmp_path, monkeypatch):
        p = tmp_path / "session.err"
        p.write_text("09:31:00 WARNING vampire_engine: HOOD: cannot read order abc; assuming it filled\n")
        monkeypatch.setattr(oc, "LOG", str(p))
        assert oc.log_signatures("09:30:00")["failed_polls"] == 1


class TestThresholds:
    def test_the_storm_threshold_sits_between_normal_and_the_incident(self):
        """19 rejects was a healthy afternoon; 4,700 was the outage."""
        assert 19 < oc.REJECT_STORM < 4700

    def test_caps_are_derived_from_the_live_config_not_a_frozen_snapshot(self):
        """The prior version of this test pinned a hardcoded dict. It kept
        passing for two allocation changes after the split it was written
        against, because it verified nothing about the live config -- it just
        verified oc.CAPS still equaled itself. That is how "SixFold over cap"
        fired every morning while SixFold sat under its real 75% target."""
        from src.core.config import load_config
        cfg = load_config()
        caps = oc.sleeve_caps(100_000.0)
        assert caps == pytest.approx({
            "Vampire": 100_000.0 * cfg.vampire_pct,
            "CSP": 100_000.0 * cfg.options_pct,
            "SixFold": 100_000.0 * cfg.sixfold_pct,
        })

    def test_caps_scale_with_equity(self):
        caps = oc.sleeve_caps(200_000.0)
        assert caps["SixFold"] == pytest.approx(oc.sleeve_caps(100_000.0)["SixFold"] * 2)


class TestTheRegimeGateExplainsZeroFills:
    """2026-09-03: PR #85/#86 gave the Vampire an LLM entry gate that only
    opens in "chop". A trending morning correctly produces zero fills, and the
    old unconditional "zero Vampire fills" alarm could not tell that apart
    from the engine being dead. These pin the three-way split."""

    def _run(self, monkeypatch, tmp_path, *, regime_lines):
        from collections import Counter

        def fake_api(path):
            if path == "/v2/clock":
                return {"is_open": True}
            if path == "/v2/account":
                return {"equity": "100000", "last_equity": "100000"}
            if path.startswith("/v2/positions"):
                return []
            raise AssertionError(path)

        if regime_lines is not None:
            p = tmp_path / "regime.jsonl"
            p.write_text("\n".join(regime_lines) + ("\n" if regime_lines else ""))
            monkeypatch.setattr(oc, "REGIME_LOG", str(p))
        else:
            monkeypatch.setattr(oc, "REGIME_LOG", str(tmp_path / "nope.jsonl"))

        monkeypatch.setattr(oc, "_api", fake_api)
        monkeypatch.setattr(oc, "log_signatures", lambda since: {})
        monkeypatch.setattr(oc, "units_status",
                            lambda: (["alpaca-agent active restarts=0"], True))
        monkeypatch.setattr(oc, "fills_today", lambda d: (Counter(), {}))
        return oc.build_report("09:30:00")

    @staticmethod
    def _at(days_ago: int = 0) -> float:
        """A real epoch timestamp on today (or `days_ago` before it), 09:45 ET --
        real, moving dates rather than a frozen 2026-09-03, so this keeps
        passing on every future run instead of failing the day after."""
        target = datetime.now(oc.ET) - timedelta(days=days_ago)
        return target.replace(hour=9, minute=45, second=0, microsecond=0).timestamp()

    def test_a_trending_morning_with_no_chop_verdicts_is_not_a_problem(self, monkeypatch, tmp_path):
        ts = self._at()
        lines = [f'{{"symbol": "QQQ", "regime": "trend_up", "at": {ts}}}',
                 f'{{"symbol": "TQQQ", "regime": "trend_up", "at": {ts}}}']
        title, body, severity = self._run(monkeypatch, tmp_path, regime_lines=lines)
        assert severity == "default", body
        assert "PROBLEM" not in title

    def test_a_chop_verdict_with_zero_fills_is_still_a_problem(self, monkeypatch, tmp_path):
        ts = self._at()
        lines = [f'{{"symbol": "QQQ", "regime": "chop", "at": {ts}}}']
        _, body, severity = self._run(monkeypatch, tmp_path, regime_lines=lines)
        assert severity == "high"
        assert "zero Vampire fills despite an open regime gate" in body

    def test_no_regime_file_at_all_is_still_a_problem(self, monkeypatch, tmp_path):
        _, body, severity = self._run(monkeypatch, tmp_path, regime_lines=None)
        assert severity == "high"
        assert "no regime verdicts today" in body

    def test_yesterdays_chop_verdict_does_not_excuse_today(self, monkeypatch, tmp_path):
        """A verdict from the wrong calendar day must not count -- the exact
        bare-time-matching mistake documented in this project's own memory."""
        lines = [f'{{"symbol": "QQQ", "regime": "chop", "at": {self._at(days_ago=1)}}}']
        _, body, severity = self._run(monkeypatch, tmp_path, regime_lines=lines)
        assert severity == "high"
        assert "no regime verdicts today" in body


class TestClosedMarketIsNotAnAlarm:
    """systemd OnCalendar knows weekdays; it does not know Thanksgiving.

    Zero fills is the loudest signal this check has on a trading day and
    meaningless on a holiday. A monitor that alarms every Christmas is one
    nobody reads by New Year.
    """

    def _run(self, monkeypatch, tmp_path, *, is_open, fills, units=None):
        calls = {}

        def fake_api(path):
            if path == "/v2/clock":
                return {"is_open": is_open}
            if path == "/v2/account":
                return {"equity": "100000", "last_equity": "100000"}
            if path.startswith("/v2/positions"):
                return []
            raise AssertionError(path)

        monkeypatch.setattr(oc, "_api", fake_api)
        monkeypatch.setattr(oc, "log_signatures", lambda since: {})
        monkeypatch.setattr(oc, "units_status",
                            lambda: units or (["alpaca-agent active restarts=0"], True))
        monkeypatch.setattr(oc, "fills_today", lambda day: fills)
        # This class predates the regime gate and asserts on its absence: no
        # verdicts anywhere means "advisor status unknown", which stays a
        # problem. Isolated to a path that can never exist, so these tests
        # cannot accidentally read whatever machine they happen to run on --
        # which is exactly how the box's real logs/regime.jsonl silently
        # flipped two of these from "high" to "default" the first time this
        # ran somewhere that had genuine trend_up-only verdicts on file.
        monkeypatch.setattr(oc, "REGIME_LOG", str(tmp_path / "no-such-regime-log.jsonl"))
        calls["out"] = oc.build_report("09:30:00")
        return calls["out"]

    def test_zero_fills_on_a_holiday_is_not_a_problem(self, monkeypatch, tmp_path):
        from collections import Counter
        title, body, severity = self._run(monkeypatch, tmp_path, is_open=False, fills=(Counter(), {}))
        assert severity == "default"
        assert "PROBLEM" not in title and "PROBLEM" not in body
        assert "Market closed" in body

    def test_zero_fills_while_open_is_a_problem(self, monkeypatch, tmp_path):
        from collections import Counter
        title, body, severity = self._run(monkeypatch, tmp_path, is_open=True, fills=(Counter(), {}))
        assert severity == "high"
        assert "zero Vampire fills" in body

    def test_an_unreadable_clock_prefers_the_false_alarm(self, monkeypatch, tmp_path):
        """Silence is the worse failure: alarm when the state is unknown."""
        from collections import Counter

        def fake_api(path):
            if path == "/v2/clock":
                raise RuntimeError("clock unreachable")
            if path == "/v2/account":
                return {"equity": "100000", "last_equity": "100000"}
            if path.startswith("/v2/positions"):
                return []
            raise AssertionError(path)

        monkeypatch.setattr(oc, "_api", fake_api)
        monkeypatch.setattr(oc, "log_signatures", lambda since: {})
        monkeypatch.setattr(oc, "units_status", lambda: (["x active restarts=0"], True))
        monkeypatch.setattr(oc, "fills_today", lambda day: (Counter(), {}))
        monkeypatch.setattr(oc, "REGIME_LOG", str(tmp_path / "no-such-regime-log.jsonl"))
        _, body, severity = oc.build_report("09:30:00")
        assert severity == "high" and "zero Vampire fills" in body

    def test_a_dead_service_alarms_even_on_a_holiday(self, monkeypatch, tmp_path):
        """A closed market excuses no fills. It does not excuse a dead agent."""
        from collections import Counter
        title, body, severity = self._run(
            monkeypatch, tmp_path, is_open=False, fills=(Counter(), {}),
            units=(["alpaca-agent inactive restarts=3"], False))
        assert severity == "high"
        assert "service is down" in body
