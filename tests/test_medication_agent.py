"""Tests for MedicationReminderAgent — due reminders, overdue cadence,
mark-cancellation, and household-vs-personal push targeting. The sync `_tick`
is driven with explicit `now` values for determinism."""

from datetime import datetime

import pytest

from agents.medication_reminders.agent import MedicationReminderAgent
from medication_shared.med_store import MedicationStore

ALICE = 42


@pytest.fixture
def store(backend):
    return MedicationStore()


@pytest.fixture
def agent():
    return MedicationReminderAgent()


def _now(hour, minute, *, month=6, day=24):  # 2026-06-24 is a Wednesday
    return datetime(2026, month, day, hour, minute).astimezone()


def _household(store, times=("08:00", "20:00")):
    return store.add_medication(name="Dog Med", dose="1 tab", dose_times=list(times), scope="household")


def _personal(store, owner=ALICE, times=("08:00",)):
    return store.add_medication(
        name="My Vitamin", dose="1", dose_times=list(times), scope="personal", owner_user_id=owner,
    )


class TestDueReminder:
    def test_fires_once_when_due(self, store, agent, inbox):
        _household(store)
        agent._tick(_now(8, 10))  # within the 30-min grace after 08:00
        alerts = agent.get_alerts()
        assert len(alerts) == 1
        assert alerts[0].priority == 3
        assert len(inbox.posts) == 1
        # still within the due window -> must not re-fire
        agent._tick(_now(8, 20))
        assert len(inbox.posts) == 1

    def test_push_carries_mark_administered_button(self, store, agent, inbox):
        _household(store)
        agent._tick(_now(8, 10))
        elements = inbox.posts[0]["metadata"]["interactive_elements"]
        assert elements[0]["callback"] == "mark_administered"
        assert elements[0]["command"] == "medication"
        assert inbox.posts[0]["push"] is True

    def test_not_due_before_time(self, store, agent, inbox):
        _household(store)
        agent._tick(_now(7, 0))
        assert agent.get_alerts() == []
        assert inbox.posts == []


class TestOverdue:
    def test_overdue_fires_then_rewarns_hourly(self, store, agent, inbox):
        _household(store, times=("08:00",))
        agent._tick(_now(8, 45))  # past 08:00 + 30-min grace -> overdue
        assert len(inbox.posts) == 1
        assert "overdue" in inbox.posts[0]["title"].lower()
        agent._tick(_now(9, 30))  # < 1h since last warn -> quiet
        assert len(inbox.posts) == 1
        agent._tick(_now(9, 50))  # > 1h since last warn -> re-warn
        assert len(inbox.posts) == 2

    def test_marking_the_dose_cancels(self, store, agent, inbox):
        med = _household(store, times=("08:00",))
        store.log_dose(med, administered_by=ALICE, now=_now(8, 5))  # slot now "done"
        agent._tick(_now(8, 45))
        assert agent.get_alerts() == []
        assert inbox.posts == []


class TestScopingAndRecurrence:
    def test_household_targets_everyone(self, store, agent, inbox):
        _household(store)
        agent._tick(_now(8, 10))
        assert inbox.posts[0]["target_type"] == "household"
        assert inbox.posts[0]["user_id"] is None

    def test_personal_targets_owner(self, store, agent, inbox):
        _personal(store, owner=ALICE)
        agent._tick(_now(8, 10))
        assert inbox.posts[0]["target_type"] == "user"
        assert inbox.posts[0]["user_id"] == ALICE

    def test_weekend_med_skips_a_weekday(self, store, agent, inbox):
        store.add_medication(
            name="Weekend Med", dose="1", dose_times=["08:00"], scope="household", recurrence="weekends",
        )
        agent._tick(_now(8, 10))  # Wednesday
        assert inbox.posts == []
