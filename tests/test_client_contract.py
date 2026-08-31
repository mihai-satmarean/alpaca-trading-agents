"""Every broker method the strategies call must exist on the real client.

The scalper polls the broker to confirm a fill. AlpacaClient had no get_order,
so every poll raised AttributeError, was swallowed by the surrounding except,
and fell through to "assume it filled": 101 times on 2026-08-31, with zero
successful polls all session. The engine counted every submission as a full
fill, over-stated its position, and then asked the venue to buy back more than
it held, which the venue refused 4,700 times.

Nothing caught it. The unit tests inject MagicMock, which fabricates any
attribute asked of it, so a call to a method that does not exist passes
happily and returns another Mock. A mock cannot tell you the real object is
missing a method; only the real object can.

This test asks the class itself.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from src.core.alpaca_client import AlpacaClient

SRC = Path(__file__).resolve().parents[1] / "src"
CALL = re.compile(r"self\._client\.([a-z_][a-z0-9_]*)\s*\(")


def _called_methods() -> set[str]:
    """Every self._client.<name>( in the strategy and agent layers."""
    names: set[str] = set()
    for path in list(SRC.rglob("*.py")):
        names |= set(CALL.findall(path.read_text()))
    return names


class TestTheClientHonoursItsCallers:
    def test_at_least_one_call_site_was_found(self):
        """Guards the regex: an empty set would make this suite vacuous."""
        assert len(_called_methods()) >= 3

    @pytest.mark.parametrize("name", sorted(_called_methods()))
    def test_the_real_client_exposes_it(self, name):
        assert hasattr(AlpacaClient, name), (
            f"strategy code calls self._client.{name}(), which AlpacaClient does "
            f"not define. Under MagicMock this passes and returns a Mock; against "
            f"the broker it raises AttributeError."
        )

    def test_get_order_specifically(self):
        """Named because its absence cost a session."""
        assert callable(getattr(AlpacaClient, "get_order", None))
        sig = inspect.signature(AlpacaClient.get_order)
        assert "order_id" in sig.parameters
