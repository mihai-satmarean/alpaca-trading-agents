"""The SIXFOLD analyst scans before the open, once."""
from __future__ import annotations
import threading, time
from unittest.mock import MagicMock, patch


def _coordinator_stub():
    from src.agents.coordinator import Coordinator
    c = Coordinator.__new__(Coordinator)
    c._sixfold_thread = None
    c._sixfold_agent = MagicMock()
    c._sixfold_agent._universe = ["AAPL"] * 418
    started = threading.Event()
    def loop():
        started.set(); time.sleep(0.5)
    c._sixfold_agent.run_loop = loop
    return c, started


def test_prewarm_starts_the_analyst_once_and_start_does_not_double_it():
    c, started = _coordinator_stub()
    assert c.prewarm() is True
    assert started.wait(1.0)
    assert c.prewarm() is False, "a live analyst thread must not be duplicated"


def test_prewarm_restarts_a_dead_thread():
    c, _ = _coordinator_stub()
    c._sixfold_agent.run_loop = lambda: None
    assert c.prewarm() is True
    time.sleep(0.2)
    assert c.prewarm() is True, "a finished thread is not 'alive'; a fresh one may start"


def test_run_live_prewarms_before_waiting_for_the_open():
    import inspect, scripts.run_live as rl
    src = inspect.getsource(rl.main)
    assert "coord.prewarm()" in src
    assert src.index("coord.prewarm()") < src.index("while not market_is_open()"), (
        "prewarm must precede the wait-for-open loop or it is worthless"
    )
