"""Tests for the medication dose-schedule engine (pure logic, no SDK).

Date anchors used throughout (verified): 2026-06-24 is a Wednesday.
  Mon 06-22 · Tue 06-23 · Wed 06-24 · Thu 06-25 · Fri 06-26 · Sat 06-27 · Sun 06-28 · Mon 06-29
"""

from datetime import date, datetime

import pytest

from medication_shared.schedule_util import (
    InvalidScheduleError,
    coerce_dose_times,
    dose_states,
    due_doses,
    is_dose_due,
    next_due,
    parse_hhmm,
    recurrence_applies,
    slot_coverage,
)

WED = date(2026, 6, 24)
SAT = date(2026, 6, 27)
SUN = date(2026, 6, 28)


class TestParseHhmm:
    def test_valid(self):
        assert parse_hhmm("07:00") == (7, 0)
        assert parse_hhmm("23:59") == (23, 59)
        assert parse_hhmm("00:00") == (0, 0)
        assert parse_hhmm("7:05") == (7, 5)  # single-digit hour tolerated

    @pytest.mark.parametrize(
        "bad", ["24:00", "07:60", "7", "ab:cd", "", "07:00:00", "-1:00", "12:-5"]
    )
    def test_invalid(self, bad):
        with pytest.raises(InvalidScheduleError):
            parse_hhmm(bad)


class TestRecurrenceApplies:
    def test_daily_is_every_day(self):
        assert recurrence_applies("daily", WED)
        assert recurrence_applies("daily", SAT)

    def test_weekdays(self):
        assert recurrence_applies("weekdays", WED)
        assert not recurrence_applies("weekdays", SAT)
        assert not recurrence_applies("weekdays", SUN)

    def test_weekends(self):
        assert not recurrence_applies("weekends", WED)
        assert recurrence_applies("weekends", SAT)
        assert recurrence_applies("weekends", SUN)

    def test_invalid_recurrence(self):
        with pytest.raises(InvalidScheduleError):
            recurrence_applies("hourly", WED)


class TestIsDoseDue:
    def test_due_within_window(self):
        now = datetime(2026, 6, 24, 8, 2)  # 2 min after an 08:00 dose
        assert is_dose_due("08:00", now, window_minutes=5)

    def test_not_due_before_time(self):
        assert not is_dose_due("08:00", datetime(2026, 6, 24, 7, 58), window_minutes=5)

    def test_not_due_past_window(self):
        assert not is_dose_due("08:00", datetime(2026, 6, 24, 8, 6), window_minutes=5)

    def test_exactly_on_time_is_due(self):
        assert is_dose_due("08:00", datetime(2026, 6, 24, 8, 0), window_minutes=5)

    def test_recurrence_gates_the_day(self):
        sat_now = datetime(2026, 6, 27, 8, 2)  # Saturday
        assert not is_dose_due("08:00", sat_now, window_minutes=5, recurrence="weekdays")
        assert is_dose_due("08:00", sat_now, window_minutes=5, recurrence="weekends")


class TestDueDoses:
    def test_returns_only_due(self):
        now = datetime(2026, 6, 24, 8, 3)
        assert due_doses(["08:00", "20:00"], now, window_minutes=5) == ["08:00"]

    def test_empty(self):
        assert due_doses([], datetime(2026, 6, 24, 8, 3)) == []


class TestNextDue:
    def test_picks_later_dose_today(self):
        now = datetime(2026, 6, 24, 8, 3)
        assert next_due(["08:00", "20:00"], now) == datetime(2026, 6, 24, 20, 0)

    def test_rolls_to_tomorrow_when_all_passed(self):
        now = datetime(2026, 6, 24, 9, 0)
        assert next_due(["08:00"], now) == datetime(2026, 6, 25, 8, 0)

    def test_weekdays_skips_the_weekend(self):
        fri = datetime(2026, 6, 26, 9, 0)  # Friday, after the 08:00 dose
        assert next_due(["08:00"], fri, recurrence="weekdays") == datetime(2026, 6, 29, 8, 0)

    def test_unsorted_times_still_correct(self):
        now = datetime(2026, 6, 24, 12, 0)
        assert next_due(["20:00", "08:00"], now) == datetime(2026, 6, 24, 20, 0)

    def test_empty_returns_none(self):
        assert next_due([], datetime(2026, 6, 24, 9, 0)) is None


class TestCoerceDoseTimes:
    def test_list_passthrough(self):
        assert coerce_dose_times(["07:00", "19:00"]) == ["07:00", "19:00"]

    def test_comma_string_with_spaces(self):
        # the app's array-as-text edit path (the real dev-node corruption)
        assert coerce_dose_times("07:00,14:45, 14:48") == ["07:00", "14:45", "14:48"]

    def test_bracketed_quoted_string(self):
        assert coerce_dose_times('["07:00","19:00"]') == ["07:00", "19:00"]

    def test_none_and_empty(self):
        assert coerce_dose_times(None) == []
        assert coerce_dose_times("") == []
        assert coerce_dose_times([]) == []

    def test_dose_states_accepts_a_string(self):
        states = dose_states("08:00,20:00", datetime(2026, 6, 24, 8, 10), set())
        assert [(t, s) for t, s, _dt in states] == [("08:00", "due"), ("20:00", "upcoming")]


def untagged(n: int = 1) -> list[dict]:
    """n administration rows with no slot tag (legacy / pre-first-window)."""
    return [{"dose_time": None} for _ in range(n)]


class TestDoseStates:
    TIMES = ["08:00", "20:00"]

    def _states(self, now, rows=(), **kw):
        covered = slot_coverage(self.TIMES, list(rows))
        return [(t, s) for t, s, _dt in dose_states(self.TIMES, now, covered, **kw)]

    def test_all_upcoming_before_first(self):
        assert self._states(datetime(2026, 6, 24, 7, 0)) == [("08:00", "upcoming"), ("20:00", "upcoming")]

    def test_due_within_grace(self):
        # 08:10, 30-min grace -> first slot is "due"
        assert self._states(datetime(2026, 6, 24, 8, 10)) == [("08:00", "due"), ("20:00", "upcoming")]

    def test_overdue_past_grace(self):
        # 08:45 > 08:00 + 30min grace -> "overdue"
        assert self._states(datetime(2026, 6, 24, 8, 45)) == [("08:00", "overdue"), ("20:00", "upcoming")]

    def test_untagged_row_marks_earliest_slot_done(self):
        # one untagged administration today -> credits the 08:00 slot even late
        assert self._states(datetime(2026, 6, 24, 21, 0), rows=untagged(1)) == [
            ("08:00", "done"), ("20:00", "overdue"),
        ]

    def test_all_done_when_all_taken(self):
        assert self._states(datetime(2026, 6, 24, 21, 0), rows=untagged(2)) == [
            ("08:00", "done"), ("20:00", "done"),
        ]

    def test_recurrence_gates_the_day(self):
        sat = datetime(2026, 6, 27, 9, 0)  # Saturday
        assert dose_states(self.TIMES, sat, set(), recurrence="weekdays") == []
        assert self._states(sat, recurrence="weekends") == [("08:00", "overdue"), ("20:00", "upcoming")]

    def test_custom_grace(self):
        # 08:10 with only 5-min grace -> already overdue
        assert self._states(datetime(2026, 6, 24, 8, 10), grace_minutes=5) == [("08:00", "overdue"), ("20:00", "upcoming")]
