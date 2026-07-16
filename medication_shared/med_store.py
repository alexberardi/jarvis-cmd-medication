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

import threading
import uuid
from datetime import date, datetime
from typing import Any

from jarvis_command_sdk import JarvisStorage

from medication_shared.schedule_util import (
    VALID_RECURRENCES,
    InvalidScheduleError,
    canon_slot,
    canon_slots,
    coerce_dose_times,
    eligible_slots,
    parse_hhmm,
    recurrence_applies,
    resolve_slot_for_mark,
    slot_coverage,
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
    """Coerce ``dose_times`` to a clean, zero-padded list.

    The app's array-as-text edit can round-trip ``["07:00"]`` back as the string
    ``"7:00, 19:00"`` — and the edit path saves the record without going through
    ``add_medication``'s validation, so unpadded times reach storage. All slot
    comparisons canonicalize independently (``canon_slot``), so this is
    hardening, not the load-bearing fix; an entry that won't parse is kept as-is
    rather than making the med unreadable.
    """
    if "dose_times" in record:
        cleaned: list[str] = []
        for raw in coerce_dose_times(record.get("dose_times")):
            slot = canon_slot(raw)
            cleaned.append(slot if slot is not None else raw)
        record["dose_times"] = sorted(set(cleaned))
    return record


def _iso(now: datetime | None) -> str:
    # Local wall-clock (tz-aware) so a dose's calendar day matches how doses_on
    # and the agent query it (both use the local date). Defaulting to UTC here
    # mislabels evening doses with tomorrow's date once UTC rolls past midnight.
    return (now or datetime.now().astimezone()).isoformat()


# log_dose is read-coverage-then-write; a voice mark and a push-button callback
# for the same med can interleave in the node process (agent thread + MQTT
# listener). Serialize per med so both can't pass the duplicate guard and
# double-record. Process-local is sufficient: all writers live in the node's
# main process.
_LOCKS_GUARD = threading.Lock()
_MED_LOCKS: dict[str, threading.Lock] = {}


def _med_lock(med_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _MED_LOCKS.setdefault(str(med_id), threading.Lock())


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
        dose_time: str | None = None,
    ) -> dict | None:
        """Record that a dose of ``med`` was administered, tagged with the slot
        it covers. Mirrors the med's ``user_id`` onto the log row so the browser
        filter treats it identically.

        Returns ``None`` — no row, and callers must say so honestly — when the
        mark is a duplicate: everything markable right now is already covered.
        Duplicate marks are not hypothetical (double-taps and voice+tap pairs
        seconds apart are in the prod dose log), and an uncaught duplicate is
        how a later dose used to be silently rendered "done".

        ``dose_time`` is the slot a reminder push's Mark button was fired for.
        It is honored only when it is a current slot whose window has opened
        today — a stale push (schedule edited, or tapped after midnight) falls
        back to behaving like a plain voice mark, because writing its tag
        verbatim would cover a different day's (or nobody's) dose.

        The dedup guard resolves against :func:`slot_coverage` — the same
        coverage the reminder agent alerts on. v0.2.1 keyed this guard on the
        latest open slot regardless of coverage, which both swallowed marks the
        agent was actively demanding (the 2026-07-15 lockout) and reached
        forward onto a not-yet-taken future dose.
        """
        stamp = (now or datetime.now()).astimezone()
        med_id = med["id"]
        # On a day the recurrence doesn't apply, nothing is scheduled: the mark
        # still records (untagged), exactly like an as-needed med.
        active = recurrence_applies(med.get("recurrence", "daily"), stamp.date())
        times = coerce_dose_times(med.get("dose_times")) if active else []

        with _med_lock(med_id):
            covered = slot_coverage(times, self.doses_on(med_id, stamp.date()))
            eligible = eligible_slots(times, stamp)

            slot = canon_slot(dose_time) if dose_time is not None else None
            if slot is not None and (slot not in canon_slots(times) or slot not in eligible):
                # Stale button: the slot was edited off the schedule, or its
                # window hasn't opened today (yesterday's push tapped after
                # midnight). Tagging it verbatim would cover the wrong dose.
                slot = None
            if slot is not None:
                if slot in covered:
                    return None
            else:
                slot = resolve_slot_for_mark(times, stamp, covered)
                if slot is None:
                    if eligible:
                        # Everything markable right now is already covered.
                        return None
                    # Nothing markable yet today (before the first window, an
                    # off-recurrence day, or an as-needed med): a real
                    # administration, recorded untagged — slot_coverage credits
                    # it to the next slot. At most one such credit can be
                    # outstanding: a second early mark is a duplicate.
                    upcoming = canon_slots(times)
                    if upcoming and upcoming[0] in covered:
                        return None

            taken_at = _iso(now)
            key = f"dose-{med_id}-{taken_at}"
            record = {
                "med_id": med_id,
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

    def coverage_for(self, med: dict, day: date, *, med_id: str | None = None) -> set[str]:
        """Slots covered on ``day`` per :func:`slot_coverage` over the med's rows.

        The one coverage view shared by the agent, the status query, the
        pending check, and (via ``log_dose``) the duplicate guard.
        """
        mid = med_id or med.get("id") or med.get("_data_key")
        if not mid:
            return set()
        times = coerce_dose_times(med.get("dose_times"))
        return slot_coverage(times, self.doses_on(str(mid), day))

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
