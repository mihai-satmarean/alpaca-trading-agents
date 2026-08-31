"""Retiring a symbol must not strand the position it was holding.

stop_all and the end-of-day flatten both iterate self._engines. A position
whose engine has been removed is therefore unreachable by every exit path the
agent has, and carries overnight with nothing able to close it.

SOXL was dropped from the scalper on 2026-08-31 for having no borrow, while it
held 12 shares. Nothing closed them and nothing would have: the symbol produced
zero log lines after the restart. They were closed by hand at 14:55 ET.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.agents.vampire import VampireAgent


def _agent(assets: dict, engines: dict):
    a = VampireAgent.__new__(VampireAgent)
    a._engines = dict(engines)
    a._client = MagicMock()
    a._client.trading.get_asset.side_effect = lambda s: assets[s]
    return a


def _asset(shortable: bool):
    x = MagicMock()
    x.shortable = shortable
    return x


def _engine():
    e = MagicMock()
    e._flatten_all = MagicMock()
    return e


class TestUnshortableSymbolsAreRetired:
    def test_a_shortable_symbol_is_kept_and_untouched(self):
        eng = _engine()
        a = _agent({"HOOD": _asset(True)}, {"HOOD": eng})
        a._drop_unshortable()
        assert "HOOD" in a._engines
        eng._flatten_all.assert_not_called()

    def test_an_unshortable_symbol_is_dropped(self):
        a = _agent({"SOXL": _asset(False)}, {"SOXL": _engine()})
        a._drop_unshortable()
        assert "SOXL" not in a._engines

    def test_it_is_flattened_before_it_is_dropped(self):
        """The whole point. Dropping first strands the position."""
        eng = _engine()
        a = _agent({"SOXL": _asset(False)}, {"SOXL": eng})
        a._drop_unshortable()
        eng._flatten_all.assert_called_once()
        assert "SOXL" not in a._engines

    def test_a_failed_flatten_keeps_the_engine(self):
        """Unreachable is worse than unwanted: if it cannot be closed, keep the
        engine so the exit paths can still see the position."""
        eng = _engine()
        eng._flatten_all.side_effect = RuntimeError("venue down")
        a = _agent({"SOXL": _asset(False)}, {"SOXL": eng})
        a._drop_unshortable()
        assert "SOXL" in a._engines, "must stay reachable when it cannot be closed"

    def test_an_unreadable_asset_keeps_the_symbol(self):
        a = _agent({}, {"HOOD": _engine()})
        a._client.trading.get_asset.side_effect = RuntimeError("timeout")
        a._drop_unshortable()
        assert "HOOD" in a._engines, "a failed borrow check must not retire a symbol"

    def test_only_the_unshortable_one_goes(self):
        hood, soxl = _engine(), _engine()
        a = _agent({"HOOD": _asset(True), "SOXL": _asset(False)},
                   {"HOOD": hood, "SOXL": soxl})
        a._drop_unshortable()
        assert set(a._engines) == {"HOOD"}
        hood._flatten_all.assert_not_called()
        soxl._flatten_all.assert_called_once()
