"""2026-09-03, Mihai from the live dashboard: two of three council advisors
"rejected: Could not parse YAML allocation block". Reproduced with the live
prompt: dell4-qwen38 spent its entire 4,096-token budget reasoning on every
call (146 s, finish_reason "length", no visible answer) and dell4-chat used
3,951 of 4,096, failing whenever it thought slightly longer. The panel then
handed the empty answer (or the thinking stream) to the parser and blamed
the format. Fix: enable_thinking=false for the two Qwen3 models (measured:
4.5 s and 21 s with clean blocks), an honest budget error instead of a
parse failure when a model does run out, and a council context that no
longer describes an expired pause as current."""

from __future__ import annotations

import json

import dashboard.council as C


class _Resp:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _capture(monkeypatch, body: dict):
    seen = {}

    def fake_urlopen(req, timeout=0, context=None):
        seen["payload"] = json.loads(req.data.decode())
        return _Resp(body)
    monkeypatch.setattr(C.urllib.request, "urlopen", fake_urlopen)
    return seen


OK = {"choices": [{"finish_reason": "stop", "message": {"content": "```yaml\nallocation:\n  sixfold_pct: 0.6\n  options_pct: 0.15\n  vampire_pct: 0.1\n  pendulum_pct: 0.1\n  reserve_pct: 0.05\n```"}}]}


class TestThinkingIsSwitchedOffForTheQwen3Advisors:
    def test_a_no_think_model_sends_the_chat_template_switch(self, monkeypatch):
        seen = _capture(monkeypatch, OK)
        C._query_model({"id": "m", "label": "m", "base_url": "http://h", "max_tokens": 10, "no_think": True}, "ctx")
        assert seen["payload"]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_a_model_without_the_flag_is_left_alone(self, monkeypatch):
        seen = _capture(monkeypatch, OK)
        C._query_model({"id": "m", "label": "m", "base_url": "http://h", "max_tokens": 10}, "ctx")
        assert "chat_template_kwargs" not in seen["payload"]

    def test_the_two_reasoning_models_are_flagged_and_finance_is_not(self):
        flags = {m["id"]: bool(m.get("no_think")) for m in C.COUNCIL_MODELS}
        assert flags == {"dell4-finance": False, "dell4-chat": True, "dell4-qwen38": True}


class TestRunningOutOfBudgetIsReportedAsSuch:
    def test_length_with_no_answer_is_an_error_not_a_parse_failure(self, monkeypatch):
        _capture(monkeypatch, {"choices": [{"finish_reason": "length",
                                            "message": {"content": None, "reasoning_content": "still thinking about"}}]})
        r = C._query_model({"id": "m", "label": "m", "base_url": "http://h", "max_tokens": 4096}, "ctx")
        assert r["error"] and "4096-token budget" in r["error"]
        assert r["content"] == ""

    def test_a_clean_finish_with_content_in_reasoning_still_uses_it(self, monkeypatch):
        _capture(monkeypatch, {"choices": [{"finish_reason": "stop",
                                            "message": {"content": None, "reasoning_content": "```yaml\nsixfold_pct: 0.6\n```"}}]})
        r = C._query_model({"id": "m", "label": "m", "base_url": "http://h", "max_tokens": 10}, "ctx")
        assert r["error"] is None and "sixfold_pct" in r["content"]

    def test_a_normal_answer_is_untouched(self, monkeypatch):
        _capture(monkeypatch, OK)
        r = C._query_model({"id": "m", "label": "m", "base_url": "http://h", "max_tokens": 10}, "ctx")
        assert r["error"] is None and C._parse_yaml_block(r["content"])["sixfold_pct"] == 0.6


class TestParserAcceptsYmlFences:
    def test_yml_fence(self):
        text = "## Proposed Changes\n```yml\nallocation:\n  sixfold_pct: 0.5\n  options_pct: 0.2\n  vampire_pct: 0.15\n  pendulum_pct: 0.1\n  reserve_pct: 0.05\n```"
        assert C._parse_yaml_block(text)["reserve_pct"] == 0.05


class TestTheContextTellsTheTruthAboutThePause:
    def test_an_expired_pause_reads_as_active(self):
        assert C._vampire_status_phrase("2026-09-02") == "active (a pause through 2026-09-02 has expired)"

    def test_a_future_pause_reads_as_paused(self):
        assert C._vampire_status_phrase("2099-01-01") == "paused until 2099-01-01"

    def test_no_pause_reads_as_active(self):
        assert C._vampire_status_phrase(None) == "active"

    def test_the_context_builder_uses_the_phrase(self):
        src = open(C.__file__, encoding="utf-8").read()
        assert "pause = _vampire_status_phrase(scfg.vampire_paused_until)" in src
        assert 'f"Status: {pause}"' in src


class TestAutoRefreshDefaultsOn:
    def test_the_checkbox_defaults_to_true(self):
        import dashboard.app as app
        src = open(app.__file__, encoding="utf-8").read()
        assert 'st.checkbox("Auto-refresh (15s)", value=True)' in src
