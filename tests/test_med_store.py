"""Tests for MedicationStore — ownership stamping, the privacy read-filter, and
the dose log. The privacy logic is load-bearing: personal meds must never leak
to another household member (or to an unknown speaker)."""

from datetime import date, datetime, timezone

import pytest

from medication_shared.med_store import (
    InvalidMedicationError,
    MedicationStore,
    visible_to,
)

ALICE = 42
BOB = 99
NOW = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(backend):  # backend fixture comes from conftest.py
    return MedicationStore()


def _personal(store, owner=ALICE, name="Vitamin D", times=("08:00",)):
    return store.add_medication(
        name=name, dose="1 pill", dose_times=list(times),
        recurrence="daily", scope="personal", owner_user_id=owner, now=NOW,
    )


def _household(store, name="Dog Rimadyl", times=("07:00", "19:00")):
    return store.add_medication(
        name=name, dose="75mg", dose_times=list(times),
        recurrence="daily", scope="household", now=NOW,
    )


class TestVisibleTo:
    def test_household_visible_to_anyone(self):
        assert visible_to({"user_id": None}, ALICE)
        assert visible_to({"user_id": None}, None)  # even an unknown viewer

    def test_personal_only_owner(self):
        assert visible_to({"user_id": ALICE}, ALICE)
        assert not visible_to({"user_id": ALICE}, BOB)

    def test_personal_hidden_from_unknown_viewer(self):
        assert not visible_to({"user_id": ALICE}, None)


class TestAddMedication:
    def test_personal_stamps_owner(self, store):
        rec = _personal(store, owner=ALICE)
        assert rec["user_id"] == ALICE
        assert rec["scope"] == "personal"
        assert rec["active"] is True
        assert rec["id"].startswith("med-")
        assert rec["dose_times"] == ["08:00"]

    def test_household_has_null_owner(self, store):
        rec = _household(store)
        assert rec["user_id"] is None
        assert rec["dose_times"] == ["07:00", "19:00"]

    def test_personal_without_owner_fails_closed(self, store):
        with pytest.raises(InvalidMedicationError):
            store.add_medication(
                name="X", dose="", dose_times=["08:00"],
                scope="personal", owner_user_id=None,
            )

    def test_invalid_dose_time_raises(self, store):
        with pytest.raises(InvalidMedicationError):
            store.add_medication(name="X", dose="", dose_times=["08:00", "99:99"], scope="household")

    def test_invalid_recurrence_raises(self, store):
        with pytest.raises(InvalidMedicationError):
            store.add_medication(name="X", dose="", dose_times=["08:00"], recurrence="hourly", scope="household")

    def test_no_dose_times_raises(self, store):
        with pytest.raises(InvalidMedicationError):
            store.add_medication(name="X", dose="", dose_times=[], scope="household")

    def test_blank_name_raises(self, store):
        with pytest.raises(InvalidMedicationError):
            store.add_medication(name="   ", dose="", dose_times=["08:00"], scope="household")

    def test_invalid_scope_raises(self, store):
        with pytest.raises(InvalidMedicationError):
            store.add_medication(name="X", dose="", dose_times=["08:00"], scope="everyone")

    def test_times_normalized_deduped_sorted(self, store):
        rec = store.add_medication(
            name="X", dose="", dose_times=["7:00", "07:00", "20:00"],
            scope="household", now=NOW,
        )
        assert rec["dose_times"] == ["07:00", "20:00"]


class TestListAndGetPrivacy:
    def test_list_filters_by_viewer(self, store):
        _personal(store, owner=ALICE, name="Alice Med")
        _personal(store, owner=BOB, name="Bob Med")
        _household(store, name="Dog")
        names = sorted(r["name"] for r in store.list_medications(ALICE))
        assert names == ["Alice Med", "Dog"]  # Bob's personal med is NOT visible

    def test_unknown_viewer_sees_household_only(self, store):
        _personal(store, owner=ALICE)
        _household(store, name="Dog")
        assert [r["name"] for r in store.list_medications(None)] == ["Dog"]

    def test_get_denies_cross_user(self, store):
        rec = _personal(store, owner=ALICE)
        assert store.get_medication(rec["id"], ALICE) is not None
        assert store.get_medication(rec["id"], BOB) is None

    def test_agent_sees_all_active(self, store):
        _personal(store, owner=ALICE)
        _personal(store, owner=BOB)
        _household(store, name="Dog")
        assert len(store.all_active_medications()) == 3

    def test_deactivate_hides_from_list(self, store):
        rec = _personal(store, owner=ALICE)
        assert store.deactivate(rec["id"], ALICE) is True
        assert store.list_medications(ALICE) == []
        assert store.deactivate("med-nope", ALICE) is False


class TestDoseLog:
    def test_log_and_query(self, store):
        med = _household(store, name="Dog")
        store.log_dose(med, administered_by=ALICE, source="tap", now=NOW)
        doses = store.doses_on(med["id"], NOW.date())
        assert len(doses) == 1
        assert doses[0]["administered_by"] == ALICE
        assert doses[0]["user_id"] is None  # mirrors the household med's ownership
        assert doses[0]["source"] == "tap"

    def test_doses_isolated_by_day(self, store):
        med = _household(store, name="Dog")
        store.log_dose(med, administered_by=ALICE, now=datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc))
        assert store.doses_on(med["id"], date(2026, 6, 24)) == []
        assert len(store.doses_on(med["id"], date(2026, 6, 23))) == 1

    def test_evening_dose_lands_on_local_day(self, store):
        # Regression: a dose taken just past midnight UTC (i.e. the prior
        # evening in a negative-offset zone) must be found on its LOCAL day —
        # doses_on normalises taken_at to local before taking the date, so this
        # holds on any machine timezone, not just UTC.
        med = _household(store, name="Evening")
        instant = datetime(2026, 6, 25, 1, 30, tzinfo=timezone.utc)
        store.log_dose(med, administered_by=ALICE, now=instant)
        local_day = instant.astimezone().date()
        assert len(store.doses_on(med["id"], local_day)) == 1

    def test_default_timestamp_is_local_not_utc(self, store):
        # Regression: log_dose with no explicit `now` must stamp local
        # wall-clock (tz-aware), so the dose's calendar day matches the local
        # day doses_on / the agent query — not tomorrow's UTC day in the evening.
        from medication_shared.med_store import _iso

        parsed = datetime.fromisoformat(_iso(None))
        assert parsed.utcoffset() == datetime.now().astimezone().utcoffset()
