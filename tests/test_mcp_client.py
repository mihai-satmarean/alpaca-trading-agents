from __future__ import annotations

import json
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from src.core.mcp_client import ALLOWED_TOOLS, AlpacaMCPClient



def _client(timeout: float = 1.0, reply: dict | None = None):
    """Client with the subprocess bypassed.

    `_write_json` is replaced with an auto-responder: whatever id the client
    sends, the canned reply comes back on that id. Tests that predict ids by
    hand break as soon as a method makes more than one call.
    """
    c = AlpacaMCPClient("k", "s", paper=True, timeout=timeout)
    c._process = MagicMock()
    c._written = []

    def _write(d):
        c._written.append(d)
        if reply is not None and "id" in d:
            c._lines.put(json.dumps({"jsonrpc": "2.0", "id": d["id"], **reply}) + "\n")

    c._write_json = _write
    return c


def _text_reply(text: str) -> dict:
    return {"result": {"content": [{"type": "text", "text": text}]}}


def test_allowed_tools_are_readonly():
    assert isinstance(ALLOWED_TOOLS, frozenset)
    assert all(not name.startswith(("place_", "cancel_", "close_", "replace_", "exercise_", "update_", "create_", "delete_", "remove_", "add_")) for name in ALLOWED_TOOLS)


@pytest.mark.parametrize("tool_name", [
    "place_stock_order",
    "place_option_order",
    "place_crypto_order",
    "cancel_order_by_id",
    "cancel_all_orders",
    "close_position",
    "close_all_positions",
    "replace_order_by_id",
    "exercise_options_position",
    "update_account_config",
    "create_watchlist",
])
def test_call_rejects_non_readonly_tools(tool_name):
    c = _client()
    with pytest.raises(ValueError, match="allowlist"):
        c.call(tool_name, {})
    assert c._written == []


def test_call_returns_concatenated_text_content():
    c = _client()
    c._lines = queue.Queue()
    c._lines.put(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "text", "text": " "},
                {"type": "text", "text": "World"},
                {"type": "image", "url": "http://example.com/image.png"},
                {"type": "text", "text": "!"},
            ]
        }
    }) + "\n")

    result = c.call("get_account_info", {})
    assert result == "Hello World!"


def test_read_response_skips_non_matching_ids():
    """A reply for another id must be discarded, not returned or fatal."""
    c = _client()
    c._lines.put("not json\n")
    c._lines.put(json.dumps({"jsonrpc": "2.0", "id": 99, "result": {"data": "wrong"}}) + "\n")
    c._lines.put(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"data": "right"}}) + "\n")

    envelope = c._read_response(1, "probe")
    assert envelope["result"]["data"] == "right"


def test_read_response_handles_json_errors():
    c = _client()
    c._lines = queue.Queue()
    c._lines.put("invalid json\n")
    c._lines.put(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"message": "Test error"}}) + "\n")

    with pytest.raises(RuntimeError, match="Test error"):
        c._read_response(1, "test_tool")


def test_read_response_raises_on_exit_sentinel():
    c = _client()
    c._lines = queue.Queue()
    c._lines.put(None)

    with pytest.raises(RuntimeError, match="exited"):
        c._read_response(1, "test_tool")


def test_timeout_raises_correctly():
    c = _client(timeout=0.2)
    c._lines = queue.Queue()

    start = time.time()
    with pytest.raises(TimeoutError, match="test_tool"):
        c._read_response(1, "test_tool")
    end = time.time()

    assert end - start < 0.5


def test_next_id_increments():
    c = _client()
    assert c._next_id() == 1
    assert c._next_id() == 2
    assert c._next_id() == 3


def test_next_id_thread_safety():
    c = _client()

    def get_id():
        return c._next_id()

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(get_id) for _ in range(100)]
        ids = set(future.result() for future in futures)

    assert len(ids) == 100


def test_option_contracts_filters_none_values():
    """None-valued options are omitted rather than sent as JSON null."""
    c = _client(reply=_text_reply("{}"))
    c.option_contracts(
        "SPY",
        contract_type="put",
        expiration_date_gte="2026-09-01",
        strike_price_gte=None,
        strike_price_lte=200.0,
        expiration_date_lte=None,
    )

    args = c._written[0]["params"]["arguments"]
    assert args["underlying_symbols"] == ["SPY"]
    assert args["type"] == "put"
    assert args["expiration_date_gte"] == "2026-09-01"
    assert args["strike_price_lte"] == 200.0
    assert "strike_price_gte" not in args
    assert "expiration_date_lte" not in args
    assert None not in args.values()


def test_option_quote_joins_symbols():
    c = _client(reply=_text_reply("{}"))
    c.option_quote(["A", "B"])
    assert c._written[0]["params"]["arguments"]["symbols"] == "A,B"


def test_option_quote_accepts_a_bare_string():
    c = _client(reply=_text_reply("{}"))
    c.option_quote("SPY241220P00450000")
    assert c._written[0]["params"]["arguments"]["symbols"] == "SPY241220P00450000"


def test_list_tools_returns_plain_strings():
    """The server returns tool dicts; callers want names."""
    c = _client(reply={"result": {"tools": [{"name": "a", "description": "x"},
                                            {"name": "b", "description": "y"}]}})
    assert c.list_tools() == ["a", "b"]


class TestTheServerIsLaunchedWithAWorkingDependencySet:
    """fastmcp 4.0 moved fastmcp.tools.tool, which alpaca-mcp-server 2.3.0
    imports at module scope. uvx resolves the newest fastmcp on every cold run,
    so the server began dying at startup with no repo change at all. CSP
    scanning was off from 14:47 on 2026-08-31 and the only evidence was one
    ERROR line per restart, because the server's stderr went to DEVNULL.
    """

    def _popen_args(self, monkeypatch):
        from unittest.mock import MagicMock
        import src.core.mcp_client as mod

        seen = {}

        def fake_popen(args, **kw):
            seen["args"], seen["kw"] = args, kw
            raise RuntimeError("stop before the handshake")

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        c = mod.AlpacaMCPClient("k", "s")
        try:
            c.start()
        except Exception:
            pass
        return seen

    def test_fastmcp_is_pinned_below_4(self, monkeypatch):
        args = self._popen_args(monkeypatch)["args"]
        assert "--with" in args, "no dependency pin: uvx will resolve fastmcp 4"
        assert args[args.index("--with") + 1] == "fastmcp<4"

    def test_the_pin_precedes_the_package(self, monkeypatch):
        """uvx reads options before the command; order is not cosmetic."""
        args = self._popen_args(monkeypatch)["args"]
        assert args.index("--with") < args.index("alpaca-mcp-server")

    def test_stderr_is_captured_not_discarded(self, monkeypatch):
        import subprocess as sp
        kw = self._popen_args(monkeypatch)["kw"]
        assert kw["stderr"] is sp.PIPE, "DEVNULL hides why the server died"

    def test_the_failure_message_carries_the_servers_own_words(self):
        from unittest.mock import MagicMock
        from src.core.mcp_client import AlpacaMCPClient
        c = AlpacaMCPClient("k", "s")
        proc = MagicMock()
        proc.stderr.read.return_value = (
            "Traceback (most recent call last):\n"
            "ModuleNotFoundError: No module named 'fastmcp.tools.tool'\n"
        )
        c._process = proc
        assert "fastmcp.tools.tool" in c._stderr_tail()

    def test_stderr_tail_never_raises(self):
        from src.core.mcp_client import AlpacaMCPClient
        c = AlpacaMCPClient("k", "s")
        assert c._stderr_tail() == "(no stderr captured)"
