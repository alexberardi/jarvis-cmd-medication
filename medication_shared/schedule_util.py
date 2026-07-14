"""Dose-schedule math for the medication tracker — pure logic, no SDK/IO.

A medication carries a list of dose times (``"HH:MM"`` strings) and a
``recurrence`` (which days it applies). This module answers the two questions
the reminder agent and the query command need:

- "Is a dose due *right now*?" — :func:`is_dose_due` / :func:`due_doses`
  (used by the agent each poll tick, gated by a short window).
- "When is the next dose?" — :func:`next_due`
  (used for the "when's my next dose / what's left today" query).

There is no RRULE engine anywhere in Jarvis; this is deliberately small and
deterministic. All functions operate on whatever ``datetime`` the caller
passes (naive or tz-aware) and never call ``datetime.now()`` themselves, so
they are fully testable.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

__all__ = [
    "VALID_RECURRENCES",
    "InvalidScheduleError",
    "parse_hhmm",
    "recurrence_applies",
    "is_dose_due",
    "due_doses",
    "next_due",
    "dose_states",
    "coerce_dose_times",
]

VALID_RECURRENCES = ("daily", "weekdays", "weekends")

# How far ahead next_due will search before giving up (covers any weekly gap).
_HORIZON_DAYS = 8


class InvalidScheduleError(ValueError):
    """A dose time or recurrence value could not be understood."""


def coerce_dose_times(value: Any) -> list[str]:
    """Normalise dose times to a list of strings.

    Accepts a real list, or a comma/whitespace-separated string — the mobile
    command-data browser renders an ``array`` field as a text box, so an edit
    can round-trip ``["07:00","19:00"]`` back as the string ``"07:00,19:00"``.
    Strips stray brackets/quotes defensively (e.g. a JSON-ish ``'["07:00"]'``).
    """
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip().strip("[]")
        parts = re.split(r"[,\n]+", cleaned)
        return [p.strip().strip("\"'") for p in parts if p.strip().strip("\"'")]
    return [str(t).strip() for t in value if str(t).strip()]


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse a ``"HH:MM"`` 24-hour time into ``(hour, minute)``.

    Tolerates a single-digit hour (``"7:05"``). Raises
    :class:`InvalidScheduleError` for anything not a valid 24h time.
    """
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise InvalidScheduleError(f"dose time must be 'HH:MM', got {value!r}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        raise InvalidScheduleError(f"dose time must be numeric 'HH:MM', got {value!r}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise InvalidScheduleError(f"dose time out of range, got {value!r}")
    return hour, minute


def recurrence_applies(recurrence: str, day: date) -> bool:
    """Does ``recurrence`` fire on ``day``?

    ``daily`` → every day; ``weekdays`` → Mon–Fri; ``weekends`` → Sat–Sun.
    """
    if recurrence == "daily":
        return True
    weekday = day.weekday()  # Monday == 0 ... Sunday == 6
    if recurrence == "weekdays":
        return weekday < 5
    if recurrence == "weekends":
        return weekday >= 5
    raise InvalidScheduleError(
        f"unknown recurrence {recurrence!r}; expected one of {VALID_RECURRENCES}"
    )


def _scheduled_on(day_dt: datetime, dose_time: str) -> datetime:
    """The dose's scheduled datetime on ``day_dt``'s date (tzinfo preserved)."""
    hour, minute = parse_hhmm(dose_time)
    return day_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)


def is_dose_due(
    dose_time: str,
    now: datetime,
    *,
    window_minutes: int = 5,
    recurrence: str = "daily",
) -> bool:
    """Is ``dose_time`` due at ``now`` (within the last ``window_minutes``)?

    True when ``recurrence`` applies on ``now``'s date and the scheduled time
    has arrived but not yet aged past the window — i.e.
    ``0 <= now - scheduled < window_minutes``. The window matches the agent's
    poll cadence so a dose fires once shortly after its time.
    """
    if not recurrence_applies(recurrence, now.date()):
        return False
    scheduled = _scheduled_on(now, dose_time)
    elapsed = (now - scheduled).total_seconds()
    return 0 <= elapsed < window_minutes * 60


def due_doses(
    dose_times: list[str],
    now: datetime,
    *,
    window_minutes: int = 5,
    recurrence: str = "daily",
) -> list[str]:
    """The subset of ``dose_times`` that are due at ``now`` (document order)."""
    return [
        t
        for t in coerce_dose_times(dose_times)
        if is_dose_due(t, now, window_minutes=window_minutes, recurrence=recurrence)
    ]


def resolve_slot_for_mark(
    dose_times: list[str],
    now: datetime,
    covered_slots: set[str] | None = None,
    *,
    early_minutes: int = 30,
) -> str | None:
    """Which dose slot is a mark *answering* right now? ``None`` if none is.

    A mark may only cover a dose that has actually come due — the latest such
    uncovered slot, so confirming at 20:10 marks the evening dose rather than
    resurrecting an unmarked morning one. Taking a dose slightly early is normal,
    so a slot is eligible from ``early_minutes`` before its scheduled time.

    Crucially it must NEVER reach forward to a *future* dose. An earlier cut of
    this function fell back to "the next upcoming slot" when nothing was due,
    which meant a duplicate tap at 08:07 marked the 20:00 dose as taken — the very
    silent-skip this change exists to remove, reintroduced from the other side.
    When nothing is due and nothing is uncovered, the right answer is to record
    nothing.
    """
    slot = due_slot(dose_times, now, early_minutes=early_minutes)
    if slot is None:
        return None
    return None if slot in (covered_slots or set()) else slot


def due_slot(
    dose_times: list[str],
    now: datetime,
    *,
    early_minutes: int = 30,
) -> str | None:
    """The dose slot that has come due as of ``now`` (latest one), ignoring
    whether it's already been taken. ``None`` if no dose is due yet today.

    Separate from ``resolve_slot_for_mark`` so callers can tell the difference
    between a *duplicate* confirmation of a dose that's due, and a dose taken at
    a time when nothing is scheduled — which is a real event that must still be
    recorded, just not credited against a slot.
    """
    times = sorted({"%02d:%02d" % parse_hhmm(t) for t in coerce_dose_times(dose_times)})
    window = timedelta(minutes=early_minutes)
    eligible = [t for t in times if now >= _scheduled_on(now, t) - window]
    return eligible[-1] if eligible else None


def dose_states(
    dose_times: list[str],
    now: datetime,
    doses_taken_today: int,
    *,
    covered_slots: set[str] | None = None,
    recurrence: str = "daily",
    grace_minutes: int = 30,
) -> list[tuple[str, str, datetime | None]]:
    """Per-slot state for today: ``(dose_time, state, scheduled_dt)``.

    States: ``"done"`` (an administration covers this slot), ``"upcoming"``
    (before its time), ``"due"`` (from its time through the grace window),
    ``"overdue"`` (past the grace window, still unmarked).

    ``covered_slots`` is the set of slots actually administered today. Prefer it:
    administrations used to be untagged, so this function counted rows and marked
    the first N slots done, in time order. Two taps on the 08:00 dose therefore
    marked the 20:00 dose "done" as well — no reminder, no error, nobody took it.
    A count cannot distinguish "both doses taken" from "the morning one confirmed
    twice".

    ``doses_taken_today`` remains the fallback for legacy rows written before
    slot-tagging (``covered_slots=None``), so existing installs keep their current
    behaviour rather than resurrecting old doses as overdue.

    Returns ``[]`` on days the recurrence doesn't apply.
    """
    if not recurrence_applies(recurrence, now.date()):
        return []
    times = sorted({"%02d:%02d" % parse_hhmm(t) for t in coerce_dose_times(dose_times)})
    grace = timedelta(minutes=grace_minutes)
    out: list[tuple[str, str, datetime | None]] = []
    for index, dose_time in enumerate(times):
        if covered_slots is not None:
            covered = dose_time in covered_slots
        else:
            covered = index < doses_taken_today  # legacy, untagged rows
        if covered:
            out.append((dose_time, "done", None))
            continue
        hour, minute = parse_hhmm(dose_time)
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < scheduled:
            state = "upcoming"
        elif now < scheduled + grace:
            state = "due"
        else:
            state = "overdue"
        out.append((dose_time, state, scheduled))
    return out


def next_due(
    dose_times: list[str],
    now: datetime,
    *,
    recurrence: str = "daily",
) -> datetime | None:
    """The soonest dose scheduled at or after ``now``, or ``None`` if empty.

    Searches forward day by day (skipping days the recurrence doesn't apply),
    returning the earliest scheduled datetime ``>= now``.
    """
    times_in = coerce_dose_times(dose_times)
    if not times_in:
        return None
    times = sorted(parse_hhmm(t) for t in times_in)  # validates + orders
    for offset in range(_HORIZON_DAYS):
        day_dt = now + timedelta(days=offset)
        if not recurrence_applies(recurrence, day_dt.date()):
            continue
        for hour, minute in times:
            candidate = day_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate >= now:
                return candidate
    return None
