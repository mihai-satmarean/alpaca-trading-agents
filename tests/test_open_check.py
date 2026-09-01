"""The post-open verification must count what actually happened today.

Yesterday I counted 4,182 order rejects in a window that contained 7, because
the grep matched untimestamped traceback lines. Every reject writes a multi-line
traceback, so an unanchored count multiplies each failure by its stack depth and
sweeps in the whole day's history besides. A monitor that inflates its own alarm
is worse than no monitor: it trains you to ignore it.
"""

from __future__ import annotations

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
        assert oc.collateral("QQQ", 3, 2145.81) == ("Scalper", 2145.81)

    def test_a_short_is_counted_by_size_not_sign(self):
        """A short is exposure, not negative exposure."""
        assert oc.collateral("HOOD", -20, -2083.20) == ("Scalper", 2083.20)

    def test_everything_else_is_sixfold(self):
        assert oc.collateral("NVDA", 22, 4796.99) == ("SixFold", 4796.99)

    def test_a_retired_scalper_symbol_is_still_the_scalpers(self):
        """SOXL was dropped from config but its position was still ours."""
        assert oc.collateral("SOXL", 12, 1338.48)[0] == "Scalper"


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

    def test_caps_match_the_configured_split_on_100k(self):
        assert oc.CAPS == {"Scalper": 10_000.0, "CSP": 20_000.0, "SixFold": 50_000.0}


class TestClosedMarketIsNotAnAlarm:
    """systemd OnCalendar knows weekdays; it does not know Thanksgiving.

    Zero fills is the loudest signal this check has on a trading day and
    meaningless on a holiday. A monitor that alarms every Christmas is one
    nobody reads by New Year.
    """

    def _run(self, monkeypatch, *, is_open, fills, units=None):
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
        calls["out"] = oc.build_report("09:30:00")
        return calls["out"]

    def test_zero_fills_on_a_holiday_is_not_a_problem(self, monkeypatch):
        from collections import Counter
        title, body, severity = self._run(monkeypatch, is_open=False, fills=(Counter(), {}))
        assert severity == "default"
        assert "PROBLEM" not in title and "PROBLEM" not in body
        assert "Market closed" in body

    def test_zero_fills_while_open_is_a_problem(self, monkeypatch):
        from collections import Counter
        title, body, severity = self._run(monkeypatch, is_open=True, fills=(Counter(), {}))
        assert severity == "high"
        assert "zero scalper fills" in body

    def test_an_unreadable_clock_prefers_the_false_alarm(self, monkeypatch):
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
        _, body, severity = oc.build_report("09:30:00")
        assert severity == "high" and "zero scalper fills" in body

    def test_a_dead_service_alarms_even_on_a_holiday(self, monkeypatch):
        """A closed market excuses no fills. It does not excuse a dead agent."""
        from collections import Counter
        title, body, severity = self._run(
            monkeypatch, is_open=False, fills=(Counter(), {}),
            units=(["alpaca-agent inactive restarts=3"], False))
        assert severity == "high"
        assert "service is down" in body
