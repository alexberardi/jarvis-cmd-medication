"""MedicationStore — persistence + ownership/privacy for the medication tracker.

Wraps :class:`jarvis_command_sdk.JarvisStorage`. There is no row-level security
in command storage, so ownership is encoded in the record itself: a top-level
int ``user_id`` means a *personal* med (visible only to that user); ``None``
means a *household* med (visible to everyone). This exact shape is what the node
data-browser filter (``_user_can_see``) keys off, so the same record is private
in voice, the agent, and the mobile browser.

Two storage namespaces:
- ``"medication"``       — medication definitions (browsed/edited in the app)
- ``"medication_doses"`` — the administration log (internal; powers "what's left today")

Fail-closed rule: a *personal* med cannot be created without a known owner — an
unresolved speaker must never silently produce a household-visible "personal" med.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from jarvis_command_sdk import JarvisStorage

from medication_shared.schedule_util import (
    VALID_RECURRENCES,
    InvalidScheduleError,
    coerce_dose_times,
    parse_hhmm,
    due_slot,
    resolve_slot_for_mark,
)

MEDS_STORAGE = "medication"
DOSES_STORAGE = "medication_doses"

VALID_SCOPES = ("personal", "household")

__all__ = [
    "MEDS_STORAGE",
    "DOSES_STORAGE",
    "VALID_SCOPES",
    "InvalidMedicationError",
    "MedicationStore",
    "visible_to",
    "record_owner",
]


class InvalidMedicationError(ValueError):
    """A medication could not be created (bad scope, missing owner, bad schedule)."""


def record_owner(record: dict) -> int | None:
    """The owner user_id of a record, or None for a household record."""
    owner = record.get("user_id")
    return None if owner is None else int(owner)


def visible_to(record: dict, viewer_user_id: int | None) -> bool:
    """Command/agent read filter.

    Household records (``user_id is None``) are visible to everyone, including an
    unknown viewer. Personal records are visible only to their owner — an unknown
    viewer (``viewer_user_id is None``) sees household records *only*.
    """
    owner = record.get("user_id")
    if owner is None:
        return True
    return viewer_user_id is not None and int(owner) == int(viewer_user_id)


def _normalized(record: dict) -> dict:
    """Coerce ``dose_times`` to a clean list (the app's array-as-text edit can
    round-trip it back as a comma-separated string)."""
    if "dose_times" in record:
        record["dose_times"] = coerce_dose_times(record.get("dose_times"))
    return record


def _iso(now: datetime | None) -> str:
    # Local wall-clock (tz-aware) so a dose's calendar day matches how doses_on
    # and the agent query it (both use the local date). Defaulting to UTC here
    # mislabels evening doses with tomorrow's date once UTC rolls past midnight.
    return (now or datetime.now().astimezone()).isoformat()


def _clean_dose_times(dose_times: Any) -> list[str]:
    """Validate, normalise to ``HH:MM``, de-dup, and sort dose times.

    Accepts a list or a comma-separated string (the app's array-as-text edit).
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in coerce_dose_times(dose_times):
        if raw is None or str(raw).strip() == "":
            continue
        hour, minute = parse_hhmm(str(raw))  # raises InvalidScheduleError
        norm = f"{hour:02d}:{minute:02d}"
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return sorted(out)


class MedicationStore:
    """Domain wrapper over JarvisStorage for medications + dose log."""

    def __init__(self) -> None:
        self._meds = JarvisStorage(MEDS_STORAGE)
        self._doses = JarvisStorage(DOSES_STORAGE)

    # -- medications -----------------------------------------------------

    def add_medication(
        self,
        *,
        name: str,
        dose: str,
        dose_times: list[str],
        scope: str,
        recurrence: str = "daily",
        owner_user_id: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Create a medication, stamping ownership from ``scope``.

        ``scope="personal"`` requires ``owner_user_id`` (fail-closed) and stamps
        ``user_id`` to it; ``scope="household"`` stamps ``user_id=None``.
        """
        name = (name or "").strip()
        if not name:
            raise InvalidMedicationError("medication name is required")
        if scope not in VALID_SCOPES:
            raise InvalidMedicationError(f"scope must be one of {VALID_SCOPES}, got {scope!r}")
        if recurrence not in VALID_RECURRENCES:
            raise InvalidMedicationError(
                f"recurrence must be one of {VALID_RECURRENCES}, got {recurrence!r}"
            )
        try:
            times = _clean_dose_times(dose_times)
        except InvalidScheduleError as exc:
            raise InvalidMedicationError(str(exc)) from exc
        if not times:
            raise InvalidMedicationError("at least one dose time is required")

        if scope == "personal":
            if owner_user_id is None:
                raise InvalidMedicationError(
                    "cannot create a personal medication without a known user (fail closed)"
                )
            user_id: int | None = int(owner_user_id)
        else:  # household
            user_id = None

        med_id = "med-" + uuid.uuid4().hex
        record = {
            "id": med_id,
            "name": name,
            "dose": (dose or "").strip(),
            "dose_times": times,
            "recurrence": recurrence,
            "scope": scope,
            "user_id": user_id,
            "active": True,
            "created_at": _iso(now),
        }
        self._meds.save(med_id, record)
        return record

    def list_medications(
        self, viewer_user_id: int | None, *, active_only: bool = True
    ) -> list[dict]:
        """Medications visible to ``viewer_user_id`` (own + household)."""
        meds = [_normalized(r) for r in self._meds.get_all() if visible_to(r, viewer_user_id)]
        if active_only:
            meds = [r for r in meds if r.get("active", True)]
        return meds

    def get_medication(self, med_id: str, viewer_user_id: int | None) -> dict | None:
        """A single medication, or None if absent or not visible to the viewer."""
        rec = self._meds.get(med_id)
        if rec is None or not visible_to(rec, viewer_user_id):
            return None
        return _normalized(rec)

    def all_active_medications(self) -> list[dict]:
        """Every active medication, unfiltered — for the reminder agent, which
        is a system actor that routes each med to its own owner/household."""
        return [_normalized(r) for r in self._meds.get_all() if r.get("active", True)]

    def deactivate(self, med_id: str, viewer_user_id: int | None) -> bool:
        """Soft-delete a medication the viewer owns. Returns False if not visible."""
        rec = self.get_medication(med_id, viewer_user_id)
        if rec is None:
            return False
        rec["active"] = False
        self._meds.save(med_id, rec)
        return True

    # -- dose log --------------------------------------------------------

    def log_dose(
        self,
        med: dict,
        *,
        administered_by: int | None,
        source: str = "voice",
        now: datetime | None = None,
    ) -> dict | None:
        """Record that a dose of ``med`` was administered, tagged with the slot it
        covers. Mirrors the med's ``user_id`` onto the log row so the browser
        filter treats it identically.

        Returns ``None`` when that slot is ALREADY covered today — i.e. this is a
        duplicate confirmation (a double-tap, or a tap plus a voice "I took it").

        The guard is load-bearing. Administrations used to be untagged and merely
        counted, so a second mark of the morning dose pushed the count to 2 and
        the evening slot was rendered "done" — silently, with no reminder and no
        error. Duplicate marks are not hypothetical: two "administered" pushes
        2.5 minutes apart were observed in production, and a double-tap whose
        second confirmation was swallowed by the notification dedup window.
        """
        stamp = (now or datetime.now()).astimezone()
        times = coerce_dose_times(med.get("dose_times"))
        covered = self.covered_slots(med["id"], stamp.date())

        slot = due_slot(times, stamp)
        if slot is not None and slot in covered:
            # A dose IS due, and it's already been marked — this is a duplicate
            # confirmation (double-tap, or a tap plus a spoken "I took it").
            return None
        # slot is None => nothing is scheduled around now. That's still a real
        # administration and must be recorded, but it credits no slot: silently
        # dropping it would be the same class of bug from the other direction.

        taken_at = _iso(now)
        key = f"dose-{med['id']}-{taken_at}"
        record = {
            "med_id": med["id"],
            "med_name": med.get("name"),
            "administered_by": None if administered_by is None else int(administered_by),
            "taken_at": taken_at,
            "dose_time": slot,
            "source": source,
            "scope": med.get("scope"),
            "user_id": med.get("user_id"),
        }
        self._doses.save(key, record)
        return record

    def covered_slots(self, med_id: str, day: date) -> set[str]:
        """Dose slots actually administered on ``day``.

        Only slot-tagged rows count. Legacy rows (written before tagging) have no
        ``dose_time``; callers fall back to counting for those so existing installs
        don't suddenly resurrect old doses as overdue.
        """
        return {
            rec["dose_time"]
            for rec in self.doses_on(med_id, day)
            if rec.get("dose_time")
        }

    def doses_on(self, med_id: str, day: date) -> list[dict]:
        """All administration records for ``med_id`` on ``day`` (local to taken_at)."""
        out: list[dict] = []
        for rec in self._doses.get_all():
            if rec.get("med_id") != med_id:
                continue
            taken_at = rec.get("taken_at")
            # Normalise to local before taking the calendar day so a dose stored
            # in any offset (incl. legacy UTC rows) lands on the right local day.
            if taken_at and datetime.fromisoformat(taken_at).astimezone().date() == day:
                out.append(rec)
        return out
