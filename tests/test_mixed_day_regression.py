"""Regression pins for the 2026-07-15 prod incident and its adversarial-review
findings (v0.2.2).

The incident: the package was upgraded to v0.2.1 mid-day, so Jul 15 held one
UNTAGGED morning row (written by v0.2.0) and one slot-TAGGED evening row. The
first tagged row disabled the legacy count fallback, retroactively flipping the
morning slot to "overdue" — and the duplicate guard keyed on the latest open
slot regardless of coverage, so every subsequent mark (voice and app) was
silently swallowed while reporting success. Alerts repeated until midnight.

The fix routes every reader AND the duplicate guard through one coverage
function (``slot_coverage``), so the system can never demand a slot while
refusing marks for it. The design was adversarially reviewed before
implementation; the scenarios that broke the first draft are pinned here too:

- pre-first-window double-mark must not credit two slots (the v0.2.0 bug
  reborn through untagged credits),
- a stale push button (schedule edited / tapped after midnight) must never
  tag the wrong day's or a nonexistent slot,
- orphan tags (slot edited away) heal a *rename* but must not leak a consumed
  dose onto a far-away slot,
- unpadded schedules from the app's array-as-text edit must not orphan
  every tagged row.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from jarvis_command_sdk import RequestInformation

from agents.medication_reminders.agent import MedicationReminderAgent
from commands.medication.command import MedicationCommand
from medication_shared.med_store import MedicationStore
from medication_shared.schedule_util import dose_states, slot_coverage

ALEX = 4


@pytest.fixture
def store(backend):
    return MedicationStore()


@pytest.fixture
def cmd():
    return MedicationCommand()


@pytest.fixture
def agent():
    return MedicationReminderAgent()


def _at(hour: int, minute: int = 0) -> datetime:
    # The incident day. Local wall-clock, tz-aware — how the node stamps rows.
    return datetime(2026, 7, 15, hour, minute).astimezone()


def _req(user_id=ALEX):
    return RequestInformation(voice_command="x", conversation_id="c1", user_id=user_id)


def _keppra(store, times=("07:00", "19:00")):
    return store.add_medication(
        name="Leo kepra", dose="1", dose_times=list(times), scope="household"
    )


def _legacy_row(store, med, when: datetime) -> None:
    """A pre-slot-tagging administration row, byte-shaped like prod v0.2.0 rows
    (no ``dose_time`` key at all)."""
    taken_at = when.isoformat()
    store._doses.save(
        f"dose-{med['id']}-{taken_at}",
        {
            "med_id": med["id"],
            "med_name": med.get("name"),
            "administered_by": ALEX,
            "taken_at": taken_at,
            "source": "voice",
            "scope": med.get("scope"),
            "user_id": med.get("user_id"),
        },
    )


class TestIncidentReplay:
    """2026-07-15, minute by minute — must stay quiet this time."""

    def test_mixed_day_does_not_resurrect_the_morning_slot(self, store):
        med = _keppra(store)
        _legacy_row(store, med, _at(6, 44))  # v0.2.0 morning row, untagged

        evening = store.log_dose(med, administered_by=ALEX, now=_at(19, 0))
        assert evening is not None and evening["dose_time"] == "19:00"

        covered = store.coverage_for(med, _at(19, 5).date())
        assert covered == {"07:00", "19:00"}, (
            "the untagged morning row must keep crediting 07:00 after the "
            "day's first tagged row appears — this flip WAS the incident"
        )
        states = dict(
            (t, s) for t, s, _ in dose_states(med["dose_times"], _at(19, 5), covered)
        )
        assert states == {"07:00": "done", "19:00": "done"}

    def test_agent_stays_quiet_after_the_evening_mark(self, store, agent, inbox):
        med = _keppra(store)
        _legacy_row(store, med, _at(6, 44))
        store.log_dose(med, administered_by=ALEX, now=_at(19, 0))

        # The incident: ticks at 19:03, 20:04, 21:05 each pushed "Leo kepra
        # overdue — (07:00) hasn't been given yet."
        for hour, minute in ((19, 3), (20, 4), (21, 5)):
            agent._tick(_at(hour, minute))
        assert inbox.posts == []
        assert agent.get_alerts() == []

    def test_re_marks_are_duplicates_but_say_so(self, store, cmd, inbox):
        med = _keppra(store)
        _legacy_row(store, med, _at(6, 44))
        store.log_dose(med, administered_by=ALEX, now=_at(19, 0))
        inbox.posts.clear()

        # Alex's 20:06 tap on the alert. Everything is covered: nothing new
        # may be recorded — and the reply must not claim otherwise.
        resp = cmd._mark_via_button({"med_id": med["id"]}, _req(), _at(20, 6))
        assert resp.context_data["recorded"] is False
        assert "already" in resp.context_data["message"].lower()
        assert len(store.doses_on(med["id"], _at(20, 6).date())) == 2
        assert inbox.posts == [], "a duplicate must not re-broadcast to the household"


class TestLockoutHealed:
    """The other half of the incident: an alerting slot must always be
    coverable by a mark. v0.2.1 swallowed every mark once the LATEST open slot
    was covered, locking the earlier one open until midnight."""

    def test_mark_falls_back_to_the_earlier_uncovered_slot(self, store):
        med = _keppra(store, times=("08:00", "20:00"))
        # 19:35 — inside 20:00's early window; nothing else marked today.
        first = store.log_dose(med, administered_by=ALEX, now=_at(19, 35))
        assert first is not None and first["dose_time"] == "20:00"
        # 19:40 — the evening is covered; the mark must heal 08:00, not vanish.
        second = store.log_dose(med, administered_by=ALEX, now=_at(19, 40))
        assert second is not None and second["dose_time"] == "08:00"
        # 19:45 — now everything is covered; THIS one is the duplicate.
        assert store.log_dose(med, administered_by=ALEX, now=_at(19, 45)) is None

    def test_alert_button_heals_exactly_the_slot_it_named(self, store, cmd, agent, inbox):
        med = _keppra(store, times=("07:00", "19:00"))
        store.log_dose(med, administered_by=ALEX, now=_at(19, 0))  # tags 19:00

        agent._tick(_at(19, 40))  # 07:00 never marked -> genuinely overdue
        assert len(inbox.posts) == 1
        element = inbox.posts[0]["metadata"]["interactive_elements"][0]
        assert element["data"]["dose_time"] == "07:00"
        assert element["data"]["date"] == "2026-07-15"

        resp = cmd._mark_via_button(dict(element["data"]), _req(), _at(19, 45))
        assert resp.context_data["recorded"] is True
        rows = store.doses_on(med["id"], _at(19, 40).date())
        assert {r.get("dose_time") for r in rows} == {"07:00", "19:00"}

        inbox.posts.clear()
        agent._tick(_at(20, 45))  # next re-warn window — must be quiet now
        assert inbox.posts == []


class TestPreWindowDedup:
    """Design-review finding B1: without a duplicate guard on the
    before-first-window branch, two pre-dawn 'I took my meds' wrote two
    untagged credits and silently covered the evening slot — the original
    v0.2.0 bug reborn."""

    def test_second_early_mark_is_a_duplicate(self, store):
        med = _keppra(store, times=("08:00", "20:00"))
        first = store.log_dose(med, administered_by=ALEX, now=_at(6, 0))
        assert first is not None and first["dose_time"] is None  # real, untagged
        assert store.log_dose(med, administered_by=ALEX, now=_at(6, 5)) is None

        covered = store.coverage_for(med, _at(6, 5).date())
        assert covered == {"08:00"}, "one early mark credits exactly one slot"
        states = dict(
            (t, s) for t, s, _ in dose_states(med["dose_times"], _at(20, 35), covered)
        )
        assert states["20:00"] == "overdue", "the evening dose is still owed"


class TestStaleButton:
    """Design-review findings B2/B3: a push button is a snapshot. Its slot may
    have been edited away, and the push may be tapped on a later day. Honoring
    the tag verbatim mints orphan credits or covers tomorrow's dose."""

    def test_edited_away_slot_degrades_to_a_normal_mark(self, store):
        med = _keppra(store, times=("07:30", "19:00"))  # 07:00 was edited away
        rec = store.log_dose(
            med, administered_by=ALEX, now=_at(7, 40), dose_time="07:00"
        )
        assert rec is not None and rec["dose_time"] == "07:30", (
            "a stale tag must resolve like a plain mark, not write an orphan"
        )
        # a second tap on the same stale push is now a duplicate
        assert (
            store.log_dose(med, administered_by=ALEX, now=_at(7, 41), dose_time="07:00")
            is None
        )

    def test_yesterdays_push_does_not_tag_tonights_slot(self, store, cmd):
        med = _keppra(store, times=("21:00",))
        # Yesterday's overdue push tapped at 00:30: the date check nulls the tag.
        with_time = {"med_id": med["id"], "dose_time": "21:00", "date": "2026-07-14"}
        now = _at(0, 30)
        resp = cmd._mark_via_button(with_time, _req(), now)
        rows = store.doses_on(med["id"], now.date())
        assert resp.context_data["recorded"] is True
        assert len(rows) == 1 and rows[0]["dose_time"] is None, (
            "an after-midnight tap records the real administration untagged; "
            "tagging '21:00' would silently cover TONIGHT's dose"
        )

    def test_future_slot_guard_without_a_date_payload(self, store):
        # Old pushes carry no date. 21:00's window hasn't opened at 00:30, so
        # the tag must be refused on eligibility alone.
        med = _keppra(store, times=("21:00",))
        rec = store.log_dose(
            med, administered_by=ALEX, now=_at(0, 30), dose_time="21:00"
        )
        assert rec is not None and rec["dose_time"] is None


class TestOrphanCredits:
    """A tag whose slot was edited off the schedule: near misses heal (a slot
    *rename*), far ones must not leak coverage (a slot *removal*)."""

    def test_rename_heals(self):
        # 07:00 -> 07:30 rename after the dose was taken and tagged.
        assert slot_coverage(["07:30", "19:00"], [{"dose_time": "07:00"}]) == {"07:30"}

    def test_removed_slot_does_not_cover_the_evening(self):
        # 12:00 was taken, then removed from the schedule. Its row must not
        # cover 19:00 (7 hours away) — that dose was never given.
        covered = slot_coverage(
            ["07:00", "19:00"], [{"dose_time": "07:00"}, {"dose_time": "12:00"}]
        )
        assert covered == {"07:00"}


class TestUnpaddedSchedule:
    """Design-review finding B4/B5: the app's array-as-text edit can persist
    '7:00' unpadded. Raw string comparison would orphan every tagged row —
    hourly alerts no mark can silence, plus a falsely covered slot."""

    def test_marks_match_unpadded_schedule_entries(self, store):
        med = _keppra(store)
        med["dose_times"] = ["7:00", "19:00"]  # what an app edit can persist
        store._meds.save(med["id"], med)
        med = store.get_medication(med["id"], ALEX)

        rec = store.log_dose(med, administered_by=ALEX, now=_at(7, 5))
        assert rec is not None and rec["dose_time"] == "07:00"
        assert store.coverage_for(med, _at(7, 10).date()) == {"07:00"}
        assert store.log_dose(med, administered_by=ALEX, now=_at(7, 10)) is None

    def test_coverage_canonicalizes_both_sides(self):
        assert slot_coverage(["7:00", "19:00"], [{"dose_time": "07:00"}]) == {"07:00"}
        states = dict(
            (t, s)
            for t, s, _ in dose_states(
                ["7:00", "19:00"], _at(8, 0), slot_coverage(["7:00"], [{"dose_time": "07:00"}])
            )
        )
        assert states["07:00"] == "done"


class TestHonestReplies:
    """A mark that records nothing must say so on every surface. The incident
    stayed invisible for two hours because voice and the app both said
    'Marked as administered' while writing nothing."""

    def test_voice_duplicate_reply(self, store, cmd, inbox):
        # Single slot so the second mark is a duplicate at ANY wall-clock time
        # (run() has no injectable clock; with two slots an evening run would
        # legitimately heal the morning slot instead).
        _keppra(store, times=("07:00",))
        first = cmd.run(_req(), action="mark", name="kepra")
        assert first.context_data["recorded"] is True
        second = cmd.run(_req(), action="mark", name="kepra")
        assert second.context_data["recorded"] is False
        msg = second.context_data["message"].lower()
        assert "already" in msg
        assert "marked" not in msg.split("already")[0], (
            f"the duplicate reply must not open with a success claim: {msg!r}"
        )

    def test_mark_mine_reports_only_what_recorded(self, store, cmd, inbox):
        store.add_medication(
            name="Vitamin D", dose="1", dose_times=["08:00"],
            scope="personal", owner_user_id=ALEX,
        )
        med_b = store.add_medication(
            name="Fish Oil", dose="1", dose_times=["08:00", "21:00"],
            scope="personal", owner_user_id=ALEX,
        )
        # Fish Oil's 08:00 already covered; 21:00 pending but not markable at 09:00.
        store.log_dose(med_b, administered_by=ALEX, now=_at(8, 5))

        resp = cmd._run_mark_mine(store, ALEX, _at(9, 0))
        assert resp.context_data["marked"] == ["Vitamin D"]
        assert resp.context_data["already_recorded"] == ["Fish Oil"]
        msg = resp.context_data["message"]
        assert "Vitamin D" in msg and "Fish Oil" in msg


class TestOffScheduleReality:
    """Real administrations outside the schedule still get recorded."""

    def test_off_recurrence_day_records_untagged(self, store):
        med = store.add_medication(
            name="Weekday Med", dose="1", dose_times=["08:00"],
            scope="household", recurrence="weekdays",
        )
        saturday = datetime(2026, 7, 18, 8, 5).astimezone()
        rec = store.log_dose(med, administered_by=ALEX, now=saturday)
        assert rec is not None and rec["dose_time"] is None
