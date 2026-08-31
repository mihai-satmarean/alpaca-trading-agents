"""Shutdown must reach the flatten, whatever happens above it.

On 2026-08-31 a duplicate of the SixfoldExecutor construction from __init__ was
pasted into stop(), where the `cfg` it referenced does not exist. Every shutdown
raised NameError before reaching this:

    self._running = False
    self._sixfold_agent.stop()
    self._vampire_agent.stop_all()      # <- flattens the scalper
    self.cancel_intraday_orders()       # <- kills resting orders

So the scalper was never flattened and its orders were never cancelled. It went
unnoticed because the traceback is logged by the caller and the process was
exiting anyway. It surfaced when a short position survived a restart that should
have closed it.

The end-of-day flatten and the EC2 cutover both run this path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.agents.coordinator import Coordinator


def _coordinator():
    c = Coordinator.__new__(Coordinator)
    c._running = True
    c._sixfold_agent = MagicMock()
    c._vampire_agent = MagicMock()
    c.cancel_intraday_orders = MagicMock(return_value=[])
    return c


class TestShutdownReachesTheFlatten:
    def test_it_completes_without_raising(self):
        _coordinator().stop()

    def test_the_scalper_is_flattened(self):
        c = _coordinator()
        c.stop()
        c._vampire_agent.stop_all.assert_called_once()

    def test_resting_orders_are_cancelled(self):
        c = _coordinator()
        c.stop()
        c.cancel_intraday_orders.assert_called_once()

    def test_the_run_loop_is_told_to_stop(self):
        c = _coordinator()
        c.stop()
        assert c._running is False

    def test_stop_does_not_build_a_sixfold_executor(self):
        """Construction belongs in __init__. Building an order-placing object
        while shutting down is what dragged the out-of-scope name in here."""
        c = _coordinator()
        c._sixfold_executor = "untouched"
        c.stop()
        assert c._sixfold_executor == "untouched"

    def test_shutdown_is_idempotent(self):
        """Cutover, EOD flatten and a signal handler can all land on it."""
        c = _coordinator()
        c.stop()
        c.stop()
        assert c._vampire_agent.stop_all.call_count == 2
        assert c._running is False
