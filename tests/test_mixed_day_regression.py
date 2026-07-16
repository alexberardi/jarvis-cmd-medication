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
        # Within 2h of the first slot: tagged to it (edit-safe), not untagged.
        assert first is not None and first["dose_time"] == "08:00"
        assert store.log_dose(med, administered_by=ALEX, now=_at(6, 5)) is None

        covered = store.coverage_for(med, _at(6, 5).date())
        assert covered == {"08:00"}, "one early mark credits exactly one slot"
        states = dict(
            (t, s) for t, s, _ in dose_states(med["dose_times"], _at(20, 35), covered)
        )
        assert states["20:00"] == "overdue", "the evening dose is still owed"

    def test_far_early_mark_stays_untagged(self, store):
        # 03:00 for an 08:00 slot is not attributable to it — record reality,
        # untagged; coverage still credits the first slot (legacy semantics).
        med = _keppra(store, times=("08:00", "20:00"))
        rec = store.log_dose(med, administered_by=ALEX, now=_at(3, 0))
        assert rec is not None and rec["dose_time"] is None
        assert store.coverage_for(med, _at(3, 5).date()) == {"08:00"}
        assert store.log_dose(med, administered_by=ALEX, now=_at(3, 5)) is None

    def test_pre_window_mark_survives_a_slot_removal(self, store):
        # Review finding: an UNTAGGED 06:00 credit floats onto tonight's slot
        # if the morning slot is later edited away. Tagged to 08:00, the row
        # becomes a proximity-bounded orphan instead: tonight stays owed.
        med = _keppra(store, times=("08:00", "20:00"))
        store.log_dose(med, administered_by=ALEX, now=_at(6, 0))  # tagged 08:00
        med["dose_times"] = ["20:00"]  # app edit removes the morning slot
        store._meds.save(med["id"], med)
        med = store.get_medication(med["id"], ALEX)
        assert store.coverage_for(med, _at(12, 0).date()) == set(), (
            "the 06:00 administration must not cover tonight's 20:00 dose"
        )


class TestStaleButton:
    """Design-review findings B2/B3: a push button is a snapshot. Its slot may
    have been edited away, and the push may be tapped on a later day. Honoring
    the tag verbatim mints orphan credits or covers tomorrow's dose."""

    def test_edited_away_slot_records_a_bounded_orphan(self, store):
        med = _keppra(store, times=("07:30", "19:00"))  # 07:00 was renamed 07:30
        rec = store.log_dose(
            med, administered_by=ALEX, now=_at(7, 40), dose_time="07:00"
        )
        # The tag is kept as an orphan; the proximity bound credits the rename.
        assert rec is not None and rec["dose_time"] == "07:00"
        assert store.coverage_for(med, _at(7, 45).date()) == {"07:30"}
        # a second tap on the same stale push is a duplicate (row-level guard)
        assert (
            store.log_dose(med, administered_by=ALEX, now=_at(7, 41), dose_time="07:00")
            is None
        )

    def test_stale_tap_for_a_removed_slot_cannot_cover_tonight(self, store):
        # The morning slot was REMOVED (not renamed). A stale tap's orphan row
        # is proximity-bounded, so it must not credit the 19:00 dose 11h away.
        med = _keppra(store, times=("19:00",))
        rec = store.log_dose(
            med, administered_by=ALEX, now=_at(8, 0), dose_time="07:00"
        )
        assert rec is not None and rec["dose_time"] == "07:00"  # recorded reality
        assert store.coverage_for(med, _at(8, 5).date()) == set(), (
            "tonight's dose was never given — the orphan must stay inert"
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


class TestReviewFindings:
    """Pins for the remaining verified adversarial-review findings."""

    def test_duplicate_hint_never_names_a_covered_dose(self, store, cmd):
        # Early mark credits 08:00; the 06:10 duplicate must point at 20:00
        # (the next ACTIONABLE dose), not at the already-covered 08:00 —
        # "next dose is at 8:00 AM" is double-administration bait.
        med = _keppra(store, times=("08:00", "20:00"))
        store.log_dose(med, administered_by=ALEX, now=_at(6, 0))
        msg = cmd._already_recorded_message(store, med, _at(6, 10))
        assert "8:00 PM" in msg and "8:00 AM" not in msg

    def test_duplicate_button_tap_names_the_still_open_dose(self, store, cmd):
        # Double-tap on the 19:00 alert while 07:00 is still owed: the reply
        # must flag the open morning dose rather than imply everything's fine.
        med = _keppra(store, times=("07:00", "19:00"))
        store.log_dose(med, administered_by=ALEX, now=_at(19, 5))  # tags 19:00
        data = {"med_id": med["id"], "dose_time": "19:00", "date": "2026-07-15"}
        resp = cmd._mark_via_button(data, _req(), _at(19, 6))
        assert resp.context_data["recorded"] is False
        assert "7:00 AM" in resp.context_data["message"]

    def test_cascade_mark_names_the_credited_slot(self, store, cmd, inbox):
        # Repeated marks deliberately heal earlier open slots; the reply must
        # SAY which slot got credited so an echo can't silently mark a missed
        # dose without the user noticing.
        med = _keppra(store, times=("08:00", "14:00", "20:00"))
        first = store.log_dose(med, administered_by=ALEX, now=_at(20, 5))
        assert first is not None and first["dose_time"] == "20:00"
        recorded, _ = cmd._record_and_broadcast(store, med, ALEX, source="voice", now=_at(20, 6))
        assert recorded is not None and recorded["dose_time"] == "14:00"
        suffix = cmd._credited_suffix(med, recorded, _at(20, 6))
        assert "2:00 PM" in suffix

    def test_status_surfaces_unreadable_dose_times(self, store, cmd):
        med = _keppra(store, times=("19:00",))
        med["dose_times"] = ["8am", "19:00"]  # hand-edited garbage entry
        store._meds.save(med["id"], med)
        resp = cmd.run(_req(), action="status")
        assert "8am" in resp.context_data["message"], (
            "an unreadable dose time must be surfaced, not silently dropped"
        )

    def test_log_dose_uses_one_clock_instant(self, store):
        # The dedup/slot decision and the stored taken_at must come from the
        # same instant — two clock reads let a midnight-straddling mark tag
        # the next day's slot.
        med = _keppra(store, times=("08:00", "20:00"))
        stamp = _at(20, 5)
        rec = store.log_dose(med, administered_by=ALEX, now=stamp)
        assert rec is not None
        assert rec["taken_at"] == stamp.astimezone().isoformat()
