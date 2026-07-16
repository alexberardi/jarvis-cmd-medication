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
    "canon_slot",
    "canon_slots",
    "recurrence_applies",
    "is_dose_due",
    "due_doses",
    "eligible_slots",
    "due_slot",
    "resolve_slot_for_mark",
    "slot_coverage",
    "next_due",
    "dose_states",
    "coerce_dose_times",
]

VALID_RECURRENCES = ("daily", "weekdays", "weekends")

# How far ahead next_due will search before giving up (covers any weekly gap).
_HORIZON_DAYS = 8

# A dose may be marked this many minutes before its scheduled time.
EARLY_MINUTES = 30

# An orphan-tagged row (its slot was edited off the schedule) may credit an
# uncovered slot at most this far from its tag. Close enough to heal a slot
# *rename* (07:00 → 07:30), far enough that a *removed* slot's already-consumed
# administration can't leak onto tonight's dose.
ORPHAN_CREDIT_WINDOW_MINUTES = 120


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


def canon_slot(value: Any) -> str | None:
    """Canonical slot identity: ``"7:00"`` → ``"07:00"``; ``None`` if unparseable.

    Slot values reach comparisons from three independently-produced sources —
    the stored schedule (the app's array-as-text edit persists unpadded times
    like ``"7:00"``), row tags written by this module (always padded), and push
    button payloads. Every membership test in this module goes through this
    canonical form; comparing raw strings makes tagged rows invisible the moment
    a schedule is edited by hand.
    """
    try:
        return "%02d:%02d" % parse_hhmm(str(value))
    except InvalidScheduleError:
        return None


def canon_slots(dose_times: Any) -> list[str]:
    """The schedule as canonical slots, chronological, deduped.

    Chronological, not lexical: sorted raw, ``"7:00"`` lands *after* ``"19:00"``
    and every earliest-first walk below would run backwards.
    """
    out = {canon_slot(t) for t in coerce_dose_times(dose_times)}
    out.discard(None)
    return sorted(out)  # type: ignore[arg-type]  # None discarded above


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


def eligible_slots(
    dose_times: list[str],
    now: datetime,
    *,
    early_minutes: int = EARLY_MINUTES,
) -> list[str]:
    """Canonical slots whose mark window has opened by ``now``, chronological.

    A slot is markable from ``early_minutes`` before its scheduled time (taking
    a dose slightly early is normal) through the end of the day. Eligibility is
    monotone within a day: once a slot's window opens it never closes.
    """
    window = timedelta(minutes=early_minutes)
    return [t for t in canon_slots(dose_times) if now >= _scheduled_on(now, t) - window]


def due_slot(
    dose_times: list[str],
    now: datetime,
    *,
    early_minutes: int = EARLY_MINUTES,
) -> str | None:
    """The latest slot whose window has opened as of ``now``, ignoring coverage.
    ``None`` if no dose is markable yet today.

    Callers use the None/not-None distinction to tell "nothing is scheduled
    around now" (a mark records an untagged, real administration) from "a dose
    is in play" (a mark either covers a slot or is a duplicate).
    """
    eligible = eligible_slots(dose_times, now, early_minutes=early_minutes)
    return eligible[-1] if eligible else None


def resolve_slot_for_mark(
    dose_times: list[str],
    now: datetime,
    covered_slots: set[str] | None = None,
    *,
    early_minutes: int = EARLY_MINUTES,
) -> str | None:
    """Which dose slot is a mark *answering* right now? ``None`` if none is.

    The latest slot whose window has opened and that is not yet covered —
    confirming at 20:10 marks the evening dose rather than resurrecting an
    unmarked morning one, but when the evening dose is already covered a mark
    falls back to the still-open earlier slot instead of being swallowed.

    That fallback is load-bearing: v0.2.1 keyed its duplicate guard on the
    latest open slot regardless of coverage, so once the evening dose was
    marked, the system could keep alerting for the morning slot while refusing
    every mark that would have covered it (the 2026-07-15 lockout). Alerts and
    marks must agree on what is owed.

    A slot whose window hasn't opened is never returned: a mark can't reach
    forward past ``early_minutes`` to a future dose.
    """
    covered = covered_slots or set()
    uncovered = [
        t
        for t in eligible_slots(dose_times, now, early_minutes=early_minutes)
        if t not in covered
    ]
    return uncovered[-1] if uncovered else None


def slot_coverage(dose_times: list[str], rows: list[dict]) -> set[str]:
    """Map one day's administration rows onto the slots they cover.

    THE single source of truth for "which doses are done today": the reminder
    agent, the status query, the pending-dose check, and ``log_dose``'s
    duplicate guard must all derive coverage from here. The 2026-07-15 incident
    was two views of coverage disagreeing — the agent demanding a slot that the
    mark path structurally refused to accept a mark for.

    Three kinds of row, in decreasing specificity:

    - **Tagged** (``dose_time`` is a current slot): covers exactly that slot.
      Duplicate tags for one slot are inert — they never spill onto another.
    - **Orphan-tagged** (``dose_time`` parses but was edited off the schedule):
      credits the nearest uncovered slot within ``ORPHAN_CREDIT_WINDOW_MINUTES``
      — heals a slot rename without letting a *removed* slot's consumed dose
      leak onto tonight's. No slot in range → the row records history, covers
      nothing. An unparseable tag is treated the same as no slot in range.
    - **Untagged** (no ``dose_time``: pre-slot-tagging legacy rows, or a mark
      before the day's first window): each credits the earliest uncovered slot.
      For an all-untagged day this reproduces the pre-tagging counting exactly,
      so existing installs keep their behaviour.
    """
    slots = canon_slots(dose_times)
    covered: set[str] = set()
    orphans: list[str] = []
    credits = 0
    for rec in rows:
        raw = rec.get("dose_time")
        if not raw:
            credits += 1
            continue
        tag = canon_slot(raw)
        if tag in slots:
            covered.add(tag)  # type: ignore[arg-type]
        elif tag is not None:
            orphans.append(tag)
    for tag in sorted(orphans):
        tag_min = _minutes(tag)
        in_range = [
            t
            for t in slots
            if t not in covered
            and abs(_minutes(t) - tag_min) <= ORPHAN_CREDIT_WINDOW_MINUTES
        ]
        if in_range:
            covered.add(min(in_range, key=lambda t: (abs(_minutes(t) - tag_min), t)))
    for t in slots:
        if credits <= 0:
            break
        if t not in covered:
            covered.add(t)
            credits -= 1
    return covered


def _minutes(slot: str) -> int:
    hour, minute = parse_hhmm(slot)
    return hour * 60 + minute


def dose_states(
    dose_times: list[str],
    now: datetime,
    covered: set[str],
    *,
    recurrence: str = "daily",
    grace_minutes: int = 30,
) -> list[tuple[str, str, datetime | None]]:
    """Per-slot state for today: ``(dose_time, state, scheduled_dt)``.

    States: ``"done"`` (an administration covers this slot), ``"upcoming"``
    (before its time), ``"due"`` (from its time through the grace window),
    ``"overdue"`` (past the grace window, still unmarked).

    ``covered`` MUST come from :func:`slot_coverage` over the day's rows. There
    is deliberately no count parameter and no fallback: v0.2.1 kept a legacy
    count fallback that engaged only when *zero* rows were slot-tagged, so the
    day's first tagged row retroactively flipped every untagged row's slot to
    "overdue" (the 2026-07-15 incident). Untagged-row handling lives in
    ``slot_coverage``, in one place.

    Returns ``[]`` on days the recurrence doesn't apply.
    """
    if not recurrence_applies(recurrence, now.date()):
        return []
    grace = timedelta(minutes=grace_minutes)
    out: list[tuple[str, str, datetime | None]] = []
    for dose_time in canon_slots(dose_times):
        if dose_time in covered:
            out.append((dose_time, "done", None))
            continue
        scheduled = _scheduled_on(now, dose_time)
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
