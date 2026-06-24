"""Tests for the MedicationCommand voice/tap surface — privacy-scoped reads,
voice mark + household broadcast, the mark_administered callback, and status."""

from datetime import datetime

import pytest
from jarvis_command_sdk import RequestInformation

from commands.medication.command import MedicationCommand
from medication_shared.med_store import MedicationStore

ALICE = 42
BOB = 99


@pytest.fixture
def store(backend):
    return MedicationStore()


@pytest.fixture
def cmd():
    return MedicationCommand()


def _req(user_id=ALICE):
    return RequestInformation(voice_command="x", conversation_id="c1", user_id=user_id)


def _personal(store, owner=ALICE, name="Vitamin D", times=("08:00",)):
    return store.add_medication(
        name=name, dose="1 pill", dose_times=list(times),
        scope="personal", owner_user_id=owner,
    )


def _household(store, name="Dog Rimadyl", times=("07:00", "19:00")):
    return store.add_medication(
        name=name, dose="75mg", dose_times=list(times), scope="household",
    )


class TestList:
    def test_empty_points_to_app(self, store, cmd):
        resp = cmd.run(_req(), action="list")
        assert resp.success
        assert "app" in resp.context_data["message"].lower()
        assert resp.context_data["medications"] == []

    def test_list_is_privacy_scoped(self, store, cmd):
        _personal(store, owner=ALICE, name="Alice Med")
        _personal(store, owner=BOB, name="Bob Med")
        _household(store, name="Dog")
        resp = cmd.run(_req(ALICE), action="list")
        names = sorted(m["name"] for m in resp.context_data["medications"])
        assert names == ["Alice Med", "Dog"]  # never Bob's personal med


class TestMark:
    def test_household_mark_broadcasts(self, store, cmd, inbox):
        med = _household(store, name="Dog")
        resp = cmd.run(_req(BOB), action="mark", name="dog")
        assert resp.success
        # dose logged
        assert len(store.doses_on(med["id"], datetime.now().astimezone().date())) == 1
        # household broadcast posted
        assert len(inbox.posts) == 1
        post = inbox.posts[0]
        assert post["target_type"] == "household"
        assert post["push"] is True
        assert "Dog" in post["title"]

    def test_personal_mark_does_not_broadcast(self, store, cmd, inbox):
        med = _personal(store, owner=ALICE, name="Vitamin D")
        resp = cmd.run(_req(ALICE), action="mark", name="vitamin")
        assert resp.success
        assert len(store.doses_on(med["id"], datetime.now().astimezone().date())) == 1
        assert inbox.posts == []

    def test_mark_matches_reordered_name(self, store, cmd, inbox):
        # The LLM extracts "Keppra for Leo" for a med stored as "Leo Keppra";
        # word-overlap must still match (the real dev-node failure).
        store.add_medication(
            name="Leo Keppra", dose="1 tablet", dose_times=["07:00", "19:00"], scope="household",
        )
        resp = cmd.run(_req(BOB), action="mark", name="Keppra for Leo")
        assert resp.success
        assert len(inbox.posts) == 1

    def test_mark_not_found(self, store, cmd, inbox):
        _household(store, name="Dog")
        resp = cmd.run(_req(ALICE), action="mark", name="aspirin")
        assert not resp.success
        assert "couldn't find" in resp.context_data["message"].lower()
        assert inbox.posts == []

    def test_mark_no_medications(self, store, cmd, inbox):
        resp = cmd.run(_req(ALICE), action="mark", name="anything")
        assert not resp.success
        assert resp.context_data["error"] == "no_medications"

    def test_mark_ambiguous_asks(self, store, cmd, inbox):
        _personal(store, owner=ALICE, name="Morning Vitamin")
        _personal(store, owner=ALICE, name="Morning Aspirin")
        resp = cmd.run(_req(ALICE), action="mark", name="morning")
        assert not resp.success
        assert resp.context_data["error"] == "ambiguous"
        assert resp.wait_for_input is True
        assert inbox.posts == []

    def test_cannot_mark_another_users_personal_med(self, store, cmd, inbox):
        _personal(store, owner=ALICE, name="Alice Secret Med")
        resp = cmd.run(_req(BOB), action="mark", name="secret")
        assert not resp.success  # Bob can't even see it


class TestMarkMineBySpeaker:
    def test_marks_speakers_pending_personal_meds(self, store, cmd, inbox):
        _personal(store, owner=ALICE, name="Vitamin D")
        resp = cmd.run(_req(ALICE), action="mark")  # generic "I took my meds", no name
        assert resp.success
        assert resp.context_data["marked"] == ["Vitamin D"]

    def test_ignores_other_users_and_household(self, store, cmd, inbox):
        _personal(store, owner=ALICE, name="Alice Med")
        _personal(store, owner=BOB, name="Bob Med")
        _household(store, name="Dog")
        resp = cmd.run(_req(ALICE), action="mark")
        assert resp.context_data["marked"] == ["Alice Med"]  # not Bob's, not the dog's

    def test_unknown_speaker_is_denied(self, store, cmd, inbox):
        _personal(store, owner=ALICE)
        resp = cmd.run(_req(None), action="mark")
        assert not resp.success
        assert resp.context_data["error"] == "unknown_speaker"

    def test_already_taken_today(self, store, cmd, inbox):
        med = _personal(store, owner=ALICE, name="Vitamin D", times=("08:00",))
        store.log_dose(med, administered_by=ALICE, now=datetime.now().astimezone())
        resp = cmd.run(_req(ALICE), action="mark")
        assert resp.context_data["marked"] == []
        assert "already taken" in resp.context_data["message"].lower()

    def test_no_personal_meds(self, store, cmd, inbox):
        _household(store, name="Dog")  # only a household med exists
        resp = cmd.run(_req(ALICE), action="mark")
        assert resp.context_data["marked"] == []


class TestMarkAdministeredCallback:
    def test_callback_logs_and_broadcasts(self, store, cmd, inbox):
        med = _household(store, name="Dog")
        resp = cmd.mark_administered({"med_id": med["id"]}, _req(BOB))
        assert resp.success
        assert len(store.doses_on(med["id"], datetime.now().astimezone().date())) == 1
        assert len(inbox.posts) == 1

    def test_callback_unknown_med(self, store, cmd, inbox):
        resp = cmd.mark_administered({"med_id": "med-nope"}, _req(ALICE))
        assert not resp.success
        assert inbox.posts == []

    def test_callback_registered(self, cmd):
        assert "mark_administered" in cmd.get_callbacks()


class TestStatus:
    def test_status_lists_remaining(self, store, cmd):
        _household(store, name="Dog", times=("07:00", "19:00"))
        resp = cmd.run(_req(ALICE), action="status")
        assert resp.success
        pending = resp.context_data["pending"]
        assert len(pending) == 1
        assert pending[0]["remaining"] == ["07:00", "19:00"]  # nothing taken yet

    def test_status_reflects_taken_doses(self, store, cmd):
        med = _household(store, name="Dog", times=("07:00", "19:00"))
        store.log_dose(med, administered_by=ALICE, now=datetime.now().astimezone())
        resp = cmd.run(_req(ALICE), action="status")
        pending = resp.context_data["pending"]
        assert pending[0]["remaining"] == ["19:00"]  # first dose accounted for

    def test_status_all_caught_up(self, store, cmd):
        med = _household(store, name="Dog", times=("07:00",))
        store.log_dose(med, administered_by=ALICE, now=datetime.now().astimezone())
        resp = cmd.run(_req(ALICE), action="status")
        assert resp.context_data["pending"] == []
        assert "caught up" in resp.context_data["message"].lower()
