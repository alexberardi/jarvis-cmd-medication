"""A double-marked dose must not silently consume a later one.

Administrations were not slot-tagged: ``dose_states`` received a COUNT
(``len(store.doses_on(...))``) and marked the first N slots done, in time order.
So two taps on the 08:00 dose produced two log rows, the count became 2, and the
**20:00 slot was marked "done" without anyone taking it** — no reminder, no
error, no signal. The UI shows it as taken.

The mark path had no idempotency guard at all (``_record_and_broadcast`` calls
``log_dose`` unconditionally), and duplicate marks were observed in production:
two "administered" pushes 2.5 minutes apart, and a double-tap whose second
confirmation was swallowed by the notification dedup window.

This is the failure mode that matters for the people most likely to want this —
someone managing an elderly parent on several daily medications, where nobody is
standing there to notice the pill still in the cup.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from medication_shared.med_store import MedicationStore
from medication_shared.schedule_util import dose_states, slot_coverage

TZ = timezone.utc


def tagged(*slots: str) -> list[dict]:
    return [{"dose_time": s} for s in slots]


def untagged(n: int = 1) -> list[dict]:
    return [{"dose_time": None} for _ in range(n)]


@pytest.fixture
def store(backend):  # `backend` = in-memory StorageBackend from conftest
    return MedicationStore()


def _at(hour: int, minute: int = 0) -> datetime:
    # Local, not UTC: the store normalises administrations with .astimezone(),
    # and dose_times ("08:00") are wall-clock times in the household's timezone.
    return datetime(2026, 7, 14, hour, minute).astimezone()


class TestDoubleMarkDoesNotConsumeLaterDose:
    TIMES = ["08:00", "20:00"]

    def test_one_mark_covers_only_the_dose_it_answers(self):
        # Morning taken at 08:05. Evening must still be pending at 20:30.
        states = dose_states(
            self.TIMES,
            now=_at(20, 30),
            covered=slot_coverage(self.TIMES, tagged("08:00")),
        )
        by_slot = {slot: state for slot, state, _ in states}
        assert by_slot["08:00"] == "done"
        assert by_slot["20:00"] == "overdue", "evening dose was never taken"

    def test_double_marking_the_morning_does_not_mark_the_evening(self):
        # The bug: two rows for 08:00 → count == 2 → both slots "done".
        # Slot-tagged, duplicate tags for one slot are inert: the evening dose
        # is still owed regardless of how many times the morning was confirmed.
        states = dose_states(
            self.TIMES,
            now=_at(20, 30),
            covered=slot_coverage(self.TIMES, tagged("08:00", "08:00")),
        )
        by_slot = {slot: state for slot, state, _ in states}
        assert by_slot["08:00"] == "done"
        assert by_slot["20:00"] == "overdue", (
            "a double-tap on the morning dose silently cancelled the evening reminder"
        )

    def test_both_slots_covered_is_still_done(self):
        states = dose_states(
            self.TIMES,
            now=_at(21, 0),
            covered=slot_coverage(self.TIMES, tagged("08:00", "20:00")),
        )
        assert all(state == "done" for _, state, _ in states)

    def test_legacy_untagged_rows_fall_back_to_counting(self):
        # Rows written before slot-tagging have no dose_time. They must keep
        # behaving exactly as before rather than resurrecting old doses as
        # "overdue" on every existing install.
        states = dose_states(
            self.TIMES,
            now=_at(20, 30),
            covered=slot_coverage(self.TIMES, untagged(1)),
        )
        by_slot = {slot: state for slot, state, _ in states}
        assert by_slot["08:00"] == "done"
        assert by_slot["20:00"] == "overdue"


class TestMarkIsIdempotent:
    """Marking the same slot twice must not create a second administration."""

    def test_second_mark_of_the_same_slot_is_a_no_op(self, store):
        from medication_shared.schedule_util import resolve_slot_for_mark

        med = store.add_medication(
            name="Keppra",
            dose="500mg",
            dose_times=["08:00", "20:00"],
            recurrence="daily",
            scope="household",
        )

        first = store.log_dose(med, administered_by=1, now=_at(8, 5))
        assert first is not None
        assert first.get("dose_time") == "08:00", "administration must record its slot"

        # A double-tap, or a tap plus a voice confirmation.
        second = store.log_dose(med, administered_by=1, now=_at(8, 7))
        assert second is None, "a second mark of an already-covered slot must be a no-op"

        rows = store.doses_on(med["id"], _at(8, 7).date())
        assert len(rows) == 1, f"expected one administration, got {len(rows)}"

        # And the evening dose is still owed.
        covered = {r["dose_time"] for r in rows if r.get("dose_time")}
        assert resolve_slot_for_mark(["08:00", "20:00"], _at(20, 30), covered) == "20:00"

    def test_marking_the_evening_after_the_morning_records_the_evening_slot(self, store):
        med = store.add_medication(
            name="Keppra",
            dose="500mg",
            dose_times=["08:00", "20:00"],
            recurrence="daily",
            scope="household",
        )
        store.log_dose(med, administered_by=1, now=_at(8, 5))
        evening = store.log_dose(med, administered_by=1, now=_at(20, 10))

        assert evening is not None
        assert evening["dose_time"] == "20:00"
        assert len(store.doses_on(med["id"], _at(20, 10).date())) == 2
