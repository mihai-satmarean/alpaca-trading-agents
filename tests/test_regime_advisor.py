"""Regime advisor: the contract, the gate, the exit invariant, and the wiring.

The advisor is the only model in the Vampire's path. These tests pin what
makes it safe: an answer that is not a verdict never opens the gate; the gate
is consulted on entries only; and the coordinator really builds and passes it
(a call-site test, per the wiring lesson in tests/test_vampire_pause.py).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from alpaca.trading.enums import OrderSide

from src.strategies import regime_advisor as ra
from src.strategies.regime_advisor import (
    RegimeAdvisor, build_user_prompt, extract_json_object, format_bars,
    parse_verdict, read_regime_journal,
)
from src.strategies.vampire_engine import VampireConfig, VampireEngine

# Verbatim answers captured against the Dell4 proxy on 2026-09-02.
FINANCE_REAL = ('{"regime": "chop", "trade": false, "confidence": 0.8, '
                '"reason": "Price oscillates without clear trend, low volume suggests choppy market."}')
CHAT_FENCED = ('```json\n{"regime":"trend_down","confidence":0.7,'
               '"reason":"lower highs, lower lows"}\n```')
CHAT_BUDGET_EXHAUSTED = ""   # finish_reason "length": empty content, drafts in the thinking stream


def _bars(n=30, start_hour=14, price=100.0):
    out = []
    for i in range(n):
        t = datetime(2026, 9, 2, start_hour, i, tzinfo=timezone.utc)
        out.append({"timestamp": t, "open": price, "high": price + 0.1, "low": price - 0.1,
                    "close": price + (0.05 if i % 2 else -0.05), "volume": 1000 + i})
    return out


class _FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _advisor(answer=FINANCE_REAL, *, clock=None, **kw):
    clock = clock or _FakeClock()
    calls: list = []

    def llm(model, system, user):
        calls.append((model, system, user))
        if isinstance(answer, Exception):
            raise answer
        return answer

    adv = RegimeAdvisor("dell4-test", llm_call=llm, ttl_seconds=1200,
                        journal=False, clock=clock, **kw)
    adv.calls = calls
    return adv, clock


class TestVerdictParsing:
    def test_the_finance_answer_parses_and_its_trade_boolean_is_ignored(self):
        v = parse_verdict(FINANCE_REAL, "QQQ", "dell4-finance", now=1.0)
        assert v.regime == "chop" and v.confidence == 0.8 and v.tradeable

    def test_markdown_fences_are_tolerated(self):
        v = parse_verdict(CHAT_FENCED, "QQQ")
        assert v.regime == "trend_down" and not v.tradeable

    def test_empty_content_is_no_verdict(self):
        assert parse_verdict(CHAT_BUDGET_EXHAUSTED, "QQQ") is None
        assert parse_verdict(None, "QQQ") is None

    def test_prose_without_json_is_no_verdict(self):
        assert parse_verdict("The market looks choppy this morning.", "QQQ") is None

    def test_a_regime_outside_the_contract_is_no_verdict(self):
        assert parse_verdict('{"regime":"sideways","confidence":0.9}', "QQQ") is None

    def test_confidence_is_coerced_and_clamped(self):
        assert parse_verdict('{"regime":"chop","confidence":"high"}', "Q").confidence == 0.0
        assert parse_verdict('{"regime":"chop","confidence":1.7}', "Q").confidence == 1.0

    def test_braces_inside_strings_do_not_end_the_object(self):
        raw = extract_json_object('{"a":"x}y","b":{"c":1}} trailing {"d":2}')
        assert json.loads(raw) == {"a": "x}y", "b": {"c": 1}}

    def test_the_first_object_wins_over_a_trailing_one(self):
        v = parse_verdict('{"regime":"chop","confidence":0.5} {"regime":"news","confidence":0.9}', "Q")
        assert v.regime == "chop"


class TestPrompt:
    def test_bars_are_formatted_oldest_first_in_eastern_time(self):
        lines = format_bars(_bars(3, start_hour=14))      # 14:00 UTC is 10:00 ET in September
        assert lines[0].startswith("10:00 ") and lines[2].startswith("10:02 ")
        assert lines[0].endswith(" o=100.00 h=100.10 l=99.90 c=99.95 v=1000")

    def test_only_the_last_thirty_bars_are_sent(self):
        assert len(format_bars(_bars(45))) == 30

    def test_alpaca_bar_objects_are_accepted(self):
        bar = MagicMock()
        bar.timestamp = datetime(2026, 9, 2, 14, 5, tzinfo=timezone.utc)
        bar.open, bar.high, bar.low, bar.close, bar.volume = 1.0, 2.0, 0.5, 1.5, 7.0
        assert format_bars([bar]) == ["10:05 o=1.00 h=2.00 l=0.50 c=1.50 v=7"]

    def test_the_user_prompt_names_the_symbol(self):
        assert build_user_prompt("TQQQ", ["a", "b"]).startswith("Symbol TQQQ.")


class TestTheGate:
    def test_no_verdict_yet_closes_entries(self):
        adv, _ = _advisor()
        assert adv.entry_allowed("QQQ") is False

    def test_a_fresh_chop_verdict_opens_entries(self):
        adv, _ = _advisor()
        adv.refresh("QQQ", _bars())
        assert adv.entry_allowed("QQQ") is True

    def test_a_trend_verdict_closes_entries(self):
        adv, _ = _advisor(CHAT_FENCED)
        adv.refresh("QQQ", _bars())
        assert adv.entry_allowed("QQQ") is False

    def test_a_stale_verdict_closes_entries(self):
        adv, clock = _advisor()
        adv.refresh("QQQ", _bars())
        clock.t += 1201
        assert adv.entry_allowed("QQQ") is False

    def test_a_verdict_inside_the_ttl_still_opens(self):
        adv, clock = _advisor()
        adv.refresh("QQQ", _bars())
        clock.t += 1199
        assert adv.entry_allowed("QQQ") is True

    def test_an_unreachable_model_closes_entries_without_raising(self):
        adv, _ = _advisor(ConnectionError("proxy down"))
        assert adv.refresh("QQQ", _bars()) is None
        assert adv.entry_allowed("QQQ") is False
        assert "model unavailable" in adv.status()["QQQ"]["reason"]

    def test_an_unparseable_answer_closes_entries(self):
        adv, _ = _advisor("I think it is choppy.")
        adv.refresh("QQQ", _bars())
        assert adv.entry_allowed("QQQ") is False

    def test_an_empty_answer_closes_entries(self):
        adv, _ = _advisor(CHAT_BUDGET_EXHAUSTED)
        adv.refresh("QQQ", _bars())
        assert adv.entry_allowed("QQQ") is False

    def test_a_regime_outside_the_contract_closes_entries(self):
        adv, _ = _advisor('{"regime":"sideways","confidence":0.9}')
        adv.refresh("QQQ", _bars())
        assert adv.entry_allowed("QQQ") is False

    def test_min_confidence_is_enforced(self):
        adv, _ = _advisor(min_confidence=0.9)
        adv.refresh("QQQ", _bars())
        assert adv.entry_allowed("QQQ") is False

    def test_too_few_bars_never_calls_the_model(self):
        adv, _ = _advisor()
        adv.refresh("QQQ", _bars(5))
        assert adv.calls == [] and adv.entry_allowed("QQQ") is False

    def test_a_failed_refresh_replaces_an_earlier_permission(self):
        """A chop verdict must not outlive the next failed refresh."""
        adv, _ = _advisor()
        adv.refresh("QQQ", _bars())
        assert adv.entry_allowed("QQQ") is True

        def down(*_):
            raise ConnectionError("proxy down")
        adv._llm = down
        adv.refresh("QQQ", _bars())
        assert adv.entry_allowed("QQQ") is False

    def test_symbols_are_independent(self):
        adv, _ = _advisor()
        adv.refresh("QQQ", _bars())
        assert adv.entry_allowed("TQQQ") is False

    def test_the_prompt_sent_is_the_contract(self):
        adv, _ = _advisor()
        adv.refresh("QQQ", _bars())
        model, system, user = adv.calls[0]
        assert model == "dell4-test" and system == ra.SYSTEM_PROMPT
        assert user.startswith("Symbol QQQ.") and user.count("\n") == 30


class TestJournal:
    def test_every_verdict_and_every_failure_is_written(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ra, "JOURNAL_PATH", str(tmp_path / "regime.jsonl"))
        adv = RegimeAdvisor("m", llm_call=lambda *a: FINANCE_REAL, journal=True, clock=_FakeClock())
        adv.refresh("QQQ", _bars())

        def down(*_):
            raise RuntimeError("boom")
        adv._llm = down
        adv.refresh("TQQQ", _bars())

        recs = read_regime_journal()
        assert recs[0]["symbol"] == "TQQQ" and recs[0]["regime"] is None
        assert "model unavailable" in recs[0]["error"]
        assert recs[1]["symbol"] == "QQQ" and recs[1]["regime"] == "chop"


def _engine(gate):
    c = MagicMock()

    def _order(symbol, qty, side, tif=None):
        o = MagicMock()
        o.status, o.filled_qty, o.id = "filled", str(qty), "t"
        return o

    c.market_order.side_effect = _order
    e = VampireEngine(c, MagicMock(), MagicMock(),
                      VampireConfig(symbol="QQQ", tick_threshold=0.02, position_size=10,
                                    max_position=100, max_daily_loss=1e9, entry_gate=gate))
    e._is_market_hours = lambda: True
    return e


class TestTheEngineConsultsTheGateOnEntriesOnly:
    def test_a_closed_gate_opens_no_lot_in_either_direction(self):
        e = _engine(lambda: False)
        e.tick(99.0, vwap=100.0)    # dip: would buy
        e.tick(101.0, vwap=100.0)   # rip: would short
        assert e.net_position == 0 and e._client.market_order.call_count == 0

    def test_an_open_gate_changes_nothing(self):
        e = _engine(lambda: True)
        e.tick(99.0, vwap=100.0)
        assert e.net_position == 10

    def test_no_gate_is_the_old_behaviour(self):
        e = _engine(None)
        e.tick(99.0, vwap=100.0)
        assert e.net_position == 10

    def test_a_closed_gate_never_blocks_a_long_exit(self):
        """The invariant: the advisor can stop the Vampire entering, never leaving."""
        e = _engine(lambda: True)
        e.tick(99.0, vwap=100.0)
        assert e.net_position == 10
        e.cfg.entry_gate = lambda: False
        e.tick(101.0, vwap=100.0)
        assert e.net_position == 0
        assert e._client.market_order.call_args.args[2] == OrderSide.SELL

    def test_a_closed_gate_never_blocks_a_short_cover(self):
        e = _engine(lambda: True)
        e.tick(101.0, vwap=100.0)
        assert e.net_position == -10
        e.cfg.entry_gate = lambda: False
        e.tick(99.0, vwap=100.0)
        assert e.net_position == 0
        assert e._client.market_order.call_args.args[2] == OrderSide.BUY

    def test_a_gate_that_raises_is_closed(self):
        def boom():
            raise RuntimeError("proxy")
        e = _engine(boom)
        e.tick(99.0, vwap=100.0)
        assert e.net_position == 0

    def test_the_gate_is_not_asked_on_an_exit(self):
        asked: list = []
        e = _engine(lambda: asked.append(1) or True)
        e.tick(99.0, vwap=100.0)
        n = len(asked)
        e.tick(101.0, vwap=100.0)
        assert e.net_position == 0 and len(asked) == n


class TestWiring:
    def test_the_agent_binds_each_engine_to_its_own_symbol(self):
        from src.agents.vampire import VampireAgent
        adv, _ = _advisor()
        a = VampireAgent(*(MagicMock() for _ in range(5)), symbols=["QQQ", "TQQQ"],
                         regime_advisor=adv)
        adv.refresh("QQQ", _bars())
        assert a._engines["QQQ"].cfg.entry_gate() is True
        assert a._engines["TQQQ"].cfg.entry_gate() is False

    def test_without_an_advisor_engines_have_no_gate(self):
        from src.agents.vampire import VampireAgent
        a = VampireAgent(*(MagicMock() for _ in range(5)), symbols=["QQQ", "TQQQ"])
        assert all(e.cfg.entry_gate is None for e in a._engines.values())

    def test_refresh_reads_iex_minute_bars_and_hands_them_to_the_advisor(self):
        from src.agents.vampire import VampireAgent
        data = MagicMock()
        data.get_recent_minute_bars.return_value = _bars()
        adv = MagicMock()
        a = VampireAgent(MagicMock(), data, MagicMock(), MagicMock(), MagicMock(),
                         symbols=["QQQ", "TQQQ"], regime_advisor=adv)
        a._refresh_regimes()
        assert sorted(c.args[0] for c in data.get_recent_minute_bars.call_args_list) == ["QQQ", "TQQQ"]
        assert adv.refresh.call_count == 2
        assert adv.refresh.call_args.args[1] == _bars()

    def test_a_bars_read_failure_still_refreshes_with_nothing(self):
        """No bars means no verdict means closed, and the advisor must be told."""
        from src.agents.vampire import VampireAgent
        data = MagicMock()
        data.get_recent_minute_bars.side_effect = RuntimeError("feed down")
        adv = MagicMock()
        a = VampireAgent(MagicMock(), data, MagicMock(), MagicMock(), MagicMock(),
                         symbols=["QQQ"], regime_advisor=adv)
        a._refresh_regimes()
        adv.refresh.assert_called_once_with("QQQ", [])

    def test_the_regime_loop_runs_on_its_own_thread_and_stops_with_stop_all(self):
        from src.agents.vampire import VampireAgent
        data = MagicMock()
        data.get_recent_minute_bars.return_value = _bars()
        adv, _ = _advisor()
        adv.window_seconds = 0.01
        a = VampireAgent(MagicMock(), data, MagicMock(), MagicMock(), MagicMock(),
                         symbols=["QQQ"], regime_advisor=adv)
        a._start_regime_loop()
        deadline = time.time() + 2
        while data.get_recent_minute_bars.call_count < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert data.get_recent_minute_bars.call_count >= 2
        a.stop_all()
        a._regime_thread.join(timeout=2)
        assert not a._regime_thread.is_alive()

    def test_the_advisor_runs_in_shadow_mode_when_the_sleeve_is_unfunded(self):
        """At 0% the engines idle but the verdicts must still be produced, or the
        gate can never show the skill that would justify funding it."""
        import asyncio

        from src.agents.vampire import VampireAgent
        data = MagicMock()
        data.get_recent_minute_bars.return_value = _bars()
        allocator = MagicMock()
        allocator.get_budget.return_value = MagicMock(vampire_budget=0.0, vampire_available=0.0)
        adv, _ = _advisor()
        adv.window_seconds = 60
        a = VampireAgent(MagicMock(), data, MagicMock(), MagicMock(), allocator,
                         symbols=["QQQ"], regime_advisor=adv)
        asyncio.run(a.run())          # returns at the zero-budget check
        deadline = time.time() + 2
        while data.get_recent_minute_bars.call_count < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert a._regime_thread is not None and a._regime_thread.is_alive()
        assert adv.entry_allowed("QQQ") is True      # a verdict was produced
        assert a._client.market_order.call_count == 0
        a.stop_all()

    def test_the_repo_config_enables_the_advisor_on_dell4_chat(self):
        from src.core.config import load_config
        cfg = load_config()
        adv = cfg.vampire_regime_advisor
        assert adv["model"] == "dell4-chat" and adv["window_minutes"] == 15
        # VampireConfig(**overrides) would raise on an unknown key.
        assert "regime_advisor" not in cfg.vampire_engine_overrides

    def test_the_coordinator_actually_builds_and_passes_it(self):
        from src.agents.coordinator import Coordinator
        with patch("src.agents.coordinator.VampireAgent") as VA:
            try:
                Coordinator()
            except Exception:
                pass
        assert VA.called, "the coordinator never built a VampireAgent"
        adv = VA.call_args.kwargs.get("regime_advisor")
        assert isinstance(adv, RegimeAdvisor), "the yml's regime_advisor block reaches nothing"
        assert adv.model == "dell4-chat" and adv.window_seconds == 900 and adv.ttl_seconds == 1200


class TestRecentMinuteBars:
    def test_requests_the_iex_feed_and_returns_the_symbols_bars(self):
        from alpaca.data.enums import DataFeed

        from src.core.market_data import MarketDataService
        client = MagicMock()
        client.data.get_stock_bars.return_value = {"QQQ": [1, 2, 3]}
        svc = MarketDataService(client)
        assert svc.get_recent_minute_bars("QQQ", minutes=90) == [1, 2, 3]
        req = client.data.get_stock_bars.call_args.args[0]
        assert req.feed == DataFeed.IEX
        assert (req.end - req.start).total_seconds() == 90 * 60
