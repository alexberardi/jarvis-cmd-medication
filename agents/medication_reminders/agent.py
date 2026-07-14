"""MedicationReminderAgent — dose-time reminders and overdue warnings.

Polls every 5 minutes. For each active medication, per dose slot today
(see schedule_util.dose_states):

- ``due``     — at the dose time, within a 30-min grace → a reminder, fired once.
- ``overdue`` — grace passed and still unmarked → an escalating warning, re-fired
                at most hourly until the dose is marked or the next dose is due.

Each fire is pushed to the right audience (household med → everyone; personal
med → the owner) with a "Mark administered" button. Household meds are ALSO
spoken on the node (priority-3 ``Alert``); personal meds are push-only and are
never announced aloud — broadcasting someone's medication in a shared room is a
privacy leak. Marking the dose logs it, so the slot becomes ``done`` and the
warning stops — the dose log gates the whole state machine.

The agent is a system actor (no ambient user), so it reads every active med via
``MedicationStore.all_active_medications()`` and routes each by its own
top-level ``user_id`` (None = household).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

try:
    from jarvis_log_client import JarvisLogger
except ImportError:  # unit tests / non-node contexts

    import logging

    class JarvisLogger:  # minimal shim accepting structured kwargs
        def __init__(self, **kw: Any) -> None:
            self._log = logging.getLogger(kw.get("service", __name__))

        def info(self, msg: str, **kw: Any) -> None:
            self._log.info("%s %s", msg, kw or "")

        def warning(self, msg: str, **kw: Any) -> None:
            self._log.warning("%s %s", msg, kw or "")

        def error(self, msg: str, **kw: Any) -> None:
            self._log.error("%s %s", msg, kw or "")

        def debug(self, msg: str, **kw: Any) -> None:
            self._log.debug("%s %s", msg, kw or "")


from jarvis_command_sdk import (
    AgentSchedule,
    Alert,
    IJarvisAgent,
    IJarvisSecret,
    JarvisInbox,
)

from medication_shared.med_store import MedicationStore, record_owner
from medication_shared.schedule_util import dose_states

logger = JarvisLogger(service="jarvis-node")

POLL_SECONDS = 300
GRACE_MINUTES = 30
REWARN_MINUTES = 60
ALERT_TTL_MINUTES = 15


class MedicationReminderAgent(IJarvisAgent):
    """Fires dose-time reminders and overdue warnings for active medications."""

    def __init__(self) -> None:
        self._alerts: List[Alert] = []
        # dose-time reminders fired once per (med, slot, date)
        self._reminded: set[str] = set()
        # overdue warnings: last-warned time per (med, slot, date) for hourly cadence
        self._overdue_warned: Dict[str, datetime] = {}

    @property
    def name(self) -> str:
        return "medication_reminders"

    @property
    def description(self) -> str:
        return "Reminds when a medication dose is due and warns when one is overdue."

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(interval_seconds=POLL_SECONDS, run_on_startup=True)

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def include_in_context(self) -> bool:
        return False

    def get_context_data(self) -> Dict[str, Any]:
        return {}

    def get_alerts(self) -> List[Alert]:
        return list(self._alerts)

    async def run(self) -> None:
        self._tick(datetime.now().astimezone())

    # Sync core so it's deterministically testable (run() just supplies "now").
    def _tick(self, now: datetime) -> None:
        self._alerts = []
        self._prune(now)
        try:
            store = MedicationStore()
            meds = store.all_active_medications()
        except Exception as exc:  # never let one bad run kill the scheduler
            logger.error("medication agent: failed to load medications", error=str(exc))
            return
        for med in meds:
            try:
                self._process_med(store, med, now)
            except Exception as exc:
                logger.error("medication agent: medication failed", med=med.get("name"), error=str(exc))

    def _process_med(self, store: MedicationStore, med: dict, now: datetime) -> None:
        med_id = med.get("id") or med.get("_data_key")
        if not med_id:
            return
        name = med.get("name") or "your medication"
        # Pass the slots ACTUALLY administered, not a count. Counting made a
        # second confirmation of the morning dose mark the evening one "done" —
        # so the evening reminder silently never fired. covered_slots is empty for
        # legacy untagged rows, which is why the count is still passed as the
        # fallback.
        taken_today = len(store.doses_on(med_id, now.date()))
        covered = store.covered_slots(med_id, now.date()) or None
        states = dose_states(
            med.get("dose_times") or [],
            now,
            taken_today,
            covered_slots=covered,
            recurrence=med.get("recurrence", "daily"),
            grace_minutes=GRACE_MINUTES,
        )
        date_key = now.strftime("%Y-%m-%d")
        for dose_time, state, _scheduled in states:
            slot_key = f"{med_id}:{dose_time}:{date_key}"
            if state == "due":
                if slot_key in self._reminded:
                    continue
                self._reminded.add(slot_key)
                self._fire(
                    med, dose_time, now,
                    title=f"Time for {name}",
                    summary=f"{name} is due ({dose_time}).",
                )
            elif state == "overdue":
                last = self._overdue_warned.get(slot_key)
                if last is not None and (now - last) < timedelta(minutes=REWARN_MINUTES):
                    continue
                self._overdue_warned[slot_key] = now
                self._fire(
                    med, dose_time, now,
                    title=f"{name} overdue",
                    summary=f"{name} ({dose_time}) hasn't been given yet.",
                )

    def _fire(self, med: dict, dose_time: str, now: datetime, *, title: str, summary: str) -> None:
        is_household = med.get("scope") == "household"
        owner = record_owner(med)
        # Spoken on the node announces in a shared space — so it's only for
        # HOUSEHOLD meds (shared, the household already knows). A PERSONAL med
        # is push-only: the owner gets a private notification on their phone,
        # but the node never broadcasts someone's medication aloud to the room
        # (a health-privacy leak to anyone nearby).
        if is_household:
            self._alerts.append(
                Alert(
                    source_agent=self.name,
                    title=title,
                    summary=summary,
                    priority=3,
                    created_at=now,
                    expires_at=now + timedelta(minutes=ALERT_TTL_MINUTES),
                )
            )
        # Push with a "Mark administered" button, scoped to the right audience.
        element = {
            "id": f"mark-{med.get('id')}-{dose_time}",
            "label": "Mark administered",
            "command": "medication",
            "callback": "mark_administered",
            "data": {"med_id": med.get("id")},
            "navigation_type": "stack",
        }
        tag = JarvisInbox("medication").post(
            title=title,
            summary=summary,
            body=summary,
            category="medication",
            create_push_notification=True,
            target_type="household" if is_household else "user",
            user_id=None if is_household else owner,
            interactive_elements=[element],
        )
        logger.info(
            "medication reminder fired", med=med.get("name"), dose_time=dose_time,
            title=title, target="household" if is_household else f"user:{owner}", post_tag=tag,
        )

    def _prune(self, now: datetime) -> None:
        """Drop dedup state from previous days so each day re-arms cleanly."""
        today = now.strftime("%Y-%m-%d")
        self._reminded = {k for k in self._reminded if k.endswith(today)}
        self._overdue_warned = {k: v for k, v in self._overdue_warned.items() if k.endswith(today)}
