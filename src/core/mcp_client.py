from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from itertools import count
from queue import Empty, Queue
from typing import Any, Dict, List, Optional, Self, Union

logger = logging.getLogger(__name__)

ALLOWED_TOOLS = frozenset([
    "get_account_info",
    "get_all_positions",
    "get_open_position",
    "get_clock",
    "get_option_contracts",
    "get_option_latest_quote",
    "get_option_snapshot",
    "get_option_chain",
    "get_stock_latest_quote",
    "get_stock_snapshot",
    "get_orders",
])


class AlpacaMCPClient:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True,
                 timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper
        self.timeout = timeout
        self._process: Optional[subprocess.Popen] = None
        self._request_id_counter = count(1)
        self._request_id_lock = threading.Lock()
        self._lines: Queue[str | None] = Queue()
        self._reader: Optional[threading.Thread] = None

    def _stderr_tail(self, limit: int = 400) -> str:
        """The server's own last words, which decide what the fix is.

        stderr was routed to DEVNULL, so a server that died during startup
        reported only its exit code. The cause on 2026-08-31 was a dependency
        that had moved a module, stated plainly on the stream being discarded,
        and it cost a session of CSP scanning to find by hand.
        """
        proc = self._process
        if proc is None or proc.stderr is None:
            return "(no stderr captured)"
        try:
            text = proc.stderr.read() or ""
        except Exception:
            return "(stderr unreadable)"
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        return lines[-1][:limit] if lines else "(stderr empty)"

    def start(self) -> None:
        """Spawn the MCP server process and perform the handshake."""
        if self._process is not None:
            return

        env = dict(os.environ)
        env.update({
            "ALPACA_API_KEY": self.api_key,
            "ALPACA_SECRET_KEY": self.secret_key,
            "ALPACA_PAPER_TRADE": "true" if self.paper else "false",
        })

        # fastmcp 4.0 moved fastmcp.tools.tool, which alpaca-mcp-server 2.3.0
        # imports at module scope. uvx resolves the newest fastmcp on every cold
        # run, so the day 4.0.0 was published the server began exiting with
        # ModuleNotFoundError before it could answer initialize. Nothing in this
        # repo changed. CSP scanning was disabled from 14:47 on 2026-08-31 and
        # the only trace was one ERROR line per restart.
        #
        # Pin below 4 until alpaca-mcp-server supports the new layout.
        self._process = subprocess.Popen(
            ["uvx", "--with", "fastmcp<4",
             "alpaca-mcp-server", "--transport", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

        self._reader = threading.Thread(target=self._drain_stdout, daemon=True)
        self._reader.start()

        request_id = self._next_id()
        init_request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "productadvisors-agent",
                    "version": "0.1.0"
                }
            }
        }

        self._write_json(init_request)
        response = self._read_response(request_id, "initialize")
        if "result" not in response:
            raise RuntimeError("Initialize response missing 'result'")

        # Send initialized notification
        self._write_json({
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        })

    def stop(self) -> None:
        """Terminate the MCP server process gracefully."""
        if self._process is None:
            return

        try:
            self._process.terminate()
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        finally:
            self._process = None
            self._reader = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def _write_json(self, data: dict) -> None:
        """Write a JSON object to the subprocess stdin."""
        self._process.stdin.write(json.dumps(data) + "\n")
        self._process.stdin.flush()

    def _next_id(self) -> int:
        with self._request_id_lock:
            return next(self._request_id_counter)

    def _drain_stdout(self) -> None:
        """Move the server's stdout onto a queue so reads can honour a deadline.

        Reading the pipe directly cannot time out: readline() blocks, so a server
        that stops responding hangs the caller forever regardless of any deadline
        checked around it.
        """
        stream = self._process.stdout
        try:
            for line in iter(stream.readline, ""):
                self._lines.put(line)
        finally:
            self._lines.put(None)  # sentinel: stream closed

    def _read_response(self, expected_id: int, what: str = "request") -> dict:
        """Wait for the reply with this id, discarding unrelated lines."""
        deadline = time.time() + self.timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"timed out after {self.timeout}s waiting for {what}")
            try:
                line = self._lines.get(timeout=remaining)
            except Empty:
                raise TimeoutError(f"timed out after {self.timeout}s waiting for {what}") from None
            if line is None:
                code = self._process.poll() if self._process else None
                raise RuntimeError(
                f"MCP server exited (code {code}) while awaiting {what}: "
                f"{self._stderr_tail()}"
            )
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("id") != expected_id:
                continue
            if "error" in data:
                raise RuntimeError(f"MCP error on {what}: {data['error']}")
            return data

    def list_tools(self) -> list[str]:
        """List all available tools."""
        request_id = self._next_id()
        self._write_json({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/list"
        })
        response = self._read_response(request_id, "tools/list")
        return [t["name"] for t in response["result"]["tools"]]

    def call(self, tool: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Call a tool with given arguments."""
        if tool not in ALLOWED_TOOLS:
            raise ValueError(f"tool {tool!r} is not in the read-only allowlist")

        request_id = self._next_id()
        params = {"name": tool}
        if arguments is not None:
            params["arguments"] = arguments

        self._write_json({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": params
        })

        response = self._read_response(request_id, tool)
        content = response["result"].get("content", [])
        texts = [c["text"] for c in content if c.get("type") == "text"]
        return "".join(texts)

    def _maybe_json(self, text: str) -> Union[dict, list, str]:
        """Attempt to parse text as JSON, falling back to the original string."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def account(self) -> Union[dict, str]:
        """Get account information."""
        result = self.call("get_account_info")
        return self._maybe_json(result)

    def positions(self) -> Union[list, dict, str]:
        """Get all positions."""
        result = self.call("get_all_positions")
        return self._maybe_json(result)

    def clock(self) -> Union[dict, str]:
        """Get market clock."""
        result = self.call("get_clock")
        return self._maybe_json(result)

    def option_contracts(self, underlying: str, *,
                         expiration_date_gte: Optional[str] = None,
                         expiration_date_lte: Optional[str] = None,
                         strike_price_gte: Optional[float] = None,
                         strike_price_lte: Optional[float] = None,
                         contract_type: Optional[str] = None,
                         limit: int = 100) -> Union[dict, list, str]:
        """Get option contracts."""
        args = {"underlying_symbols": [underlying], "limit": limit}
        if expiration_date_gte is not None:
            args["expiration_date_gte"] = expiration_date_gte
        if expiration_date_lte is not None:
            args["expiration_date_lte"] = expiration_date_lte
        if strike_price_gte is not None:
            args["strike_price_gte"] = strike_price_gte
        if strike_price_lte is not None:
            args["strike_price_lte"] = strike_price_lte
        if contract_type is not None:
            args["type"] = contract_type

        result = self.call("get_option_contracts", args)
        return self._maybe_json(result)

    def option_quote(self, symbols: Union[str, List[str]]) -> Union[dict, list, str]:
        """Get latest option quotes."""
        if isinstance(symbols, list):
            symbols = ",".join(symbols)
        result = self.call("get_option_latest_quote", {"symbols": symbols})
        return self._maybe_json(result)

    def stock_quote(self, symbols: Union[str, List[str]]) -> Union[dict, list, str]:
        """Get latest stock quotes."""
        if isinstance(symbols, list):
            symbols = ",".join(symbols)
        result = self.call("get_stock_latest_quote", {"symbols": symbols})
        return self._maybe_json(result)
