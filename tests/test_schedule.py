"""The reporting clock."""

from __future__ import annotations

from datetime import date, datetime, time as dt_time

import pytest

from src.core.schedule import CHECKPOINTS, ET, next_checkpoint, seconds_until


def _at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


class TestCheckpointSet:
    def test_the_six_agreed_times(self):
        assert [c.at for c in CHECKPOINTS] == [
            dt_time(7, 0), dt_time(9, 30), dt_time(9, 45),
            dt_time(12, 0), dt_time(15, 30), dt_time(16, 15),
        ]

    def test_every_checkpoint_has_a_label_and_blurb(self):
        for c in CHECKPOINTS:
            assert c.label and c.blurb

    def test_only_the_last_is_a_closing_report(self):
        assert [c.closing for c in CHECKPOINTS] == [False] * 5 + [True]


class TestNextCheckpoint:
    def test_before_the_first_gives_seven_am(self):
        when, cp = next_checkpoint(_at(2026, 8, 31, 6, 0))     # Monday
        assert cp.at == dt_time(7, 0) and when.hour == 7

    def test_mid_session_gives_the_following_one(self):
        _, cp = next_checkpoint(_at(2026, 8, 31, 10, 0))
        assert cp.at == dt_time(12, 0)

    def test_exactly_on_a_checkpoint_gives_the_next_not_the_same(self):
        """Otherwise a report fires and immediately re-fires."""
        _, cp = next_checkpoint(_at(2026, 8, 31, 12, 0))
        assert cp.at == dt_time(15, 30)

    def test_after_the_last_rolls_to_the_next_morning(self):
        when, cp = next_checkpoint(_at(2026, 8, 31, 17, 0))
        assert cp.at == dt_time(7, 0) and when.day == 1 and when.month == 9

    def test_friday_evening_rolls_to_monday(self):
        when, _ = next_checkpoint(_at(2026, 9, 4, 17, 0))      # Friday
        assert when.weekday() == 0 and when.day == 7

    def test_saturday_rolls_to_monday(self):
        when, _ = next_checkpoint(_at(2026, 9, 5, 10, 0))
        assert when.weekday() == 0

    def test_naive_input_is_treated_as_eastern(self):
        when, cp = next_checkpoint(datetime(2026, 8, 31, 6, 0))
        assert cp.at == dt_time(7, 0) and when.tzinfo is not None


class TestSecondsUntil:
    def test_counts_forward(self):
        assert seconds_until(_at(2026, 8, 31, 12, 0), _at(2026, 8, 31, 11, 30)) == 1800

    def test_never_negative(self):
        assert seconds_until(_at(2026, 8, 31, 9, 0), _at(2026, 8, 31, 12, 0)) == 0
