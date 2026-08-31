"""The quote normaliser must never invent a price.

An unreadable quote has to disappear, not default to zero: a zero bid that
reaches the scanner is a fabricated premium, which is the defect this whole
path exists to remove.
"""

from __future__ import annotations

import json

from src.core.option_quotes import _rows


def test_mapping_keyed_by_symbol():
    out = _rows({"SPY261218P00450000": {"bid_price": 4.5, "ask_price": 4.7}})
    assert out["SPY261218P00450000"]["bid"] == 4.5


def test_list_of_quote_objects():
    out = _rows([{"symbol": "A", "bid_price": 1.0, "ask_price": 1.1}])
    assert out["A"]["bid"] == 1.0


def test_wrapped_in_a_quotes_key():
    out = _rows({"quotes": {"A": {"bid": 2.0, "ask": 2.1}}})
    assert out["A"]["bid"] == 2.0


def test_short_field_names():
    assert _rows({"A": {"bp": 3.0, "ap": 3.2}})["A"]["bid"] == 3.0


def test_json_string_is_parsed():
    assert _rows(json.dumps({"A": {"bid": 1.5, "ask": 1.6}}))["A"]["bid"] == 1.5


def test_greeks_delta_is_lifted():
    assert _rows({"A": {"bid": 1.0, "greeks": {"delta": -0.28}}})["A"]["delta"] == -0.28


def test_flat_delta_is_lifted():
    assert _rows({"A": {"bid": 1.0, "delta": -0.31}})["A"]["delta"] == -0.31


def test_missing_delta_is_simply_absent():
    assert "delta" not in _rows({"A": {"bid": 1.0}})["A"]


def test_a_quote_with_no_bid_is_dropped_not_zeroed():
    assert _rows({"A": {"ask": 5.0}}) == {}


def test_unreadable_bid_is_dropped():
    assert _rows({"A": {"bid": "n/a", "ask": 1.0}}) == {}


def test_ask_falls_back_to_bid_rather_than_zero():
    assert _rows({"A": {"bid": 2.0}})["A"]["ask"] == 2.0


def test_garbage_shapes_yield_nothing():
    for junk in ["not json", 42, None, [], {}]:
        assert _rows(junk) == {}
