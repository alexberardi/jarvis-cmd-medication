"""Medication tracking command.

Medications are ADDED and EDITED in the mobile app (the command-data browser),
not by voice. This command is the lightweight voice + tap surface:

- ``mark``   — record a dose administered ("I gave the dog his meds", or the
               "Mark administered" button on a reminder push). For a *household*
               med it also broadcasts to the household so nobody double-doses.
- ``list``   — "what medications do I have".
- ``status`` — "what's left to take today" / "did I take my morning meds".

Ownership/privacy lives in :class:`MedicationStore`: voice/tap reads are scoped
to the speaker (own + household), so one member never sees another's personal med.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, List

from jarvis_command_sdk import (
    CommandExample,
    CommandResponse,
    FieldSpec,
    IJarvisCommand,
    IJarvisParameter,
    IJarvisSecret,
    JarvisInbox,
    JarvisParameter,
    RecordSummary,
    RequestInformation,
    callback,
)

from medication_shared.med_store import VALID_SCOPES, MedicationStore, record_owner
from medication_shared.schedule_util import (
    VALID_RECURRENCES,
    coerce_dose_times,
    recurrence_applies,
)

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


_logger = JarvisLogger(service="jarvis-node")

_ACTIONS = ["mark", "list", "status"]

# Filler words ignored when fuzzy-matching a spoken name against the stored
# medication name. The LLM phrases the name many ways ("Keppra for Leo",
# "Leo's Keppra", "my morning meds"), so match on significant-word overlap
# rather than a strict substring.
_NAME_STOPWORDS = {
    "for", "the", "my", "his", "her", "their", "our", "a", "an", "to", "of",
    "s", "is", "it", "i", "gave", "give", "given", "took", "take", "taken",
    "mark", "marked", "as", "and", "dose", "doses", "med", "meds", "medicine",
    "medication", "medications", "pill", "pills",
}


def _significant_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _NAME_STOPWORDS}


def _match_by_name(meds: list[dict], name: str | None) -> list[dict]:
    """Filter meds by a spoken name using significant-word overlap.

    Returns every med tied for the highest overlap, ``[]`` if nothing overlaps,
    or the input unchanged when no name was given (the caller decides).
    """
    if not name:
        return meds
    query = _significant_tokens(name)
    if not query:
        return meds
    scored: list[tuple[int, dict]] = []
    for med in meds:
        med_words = set(re.findall(r"[a-z0-9]+", str(med.get("name", "")).lower()))
        overlap = len(query & med_words)
        if overlap:
            scored.append((overlap, med))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [med for score, med in scored if score == best]


def _spoken_time(dt: datetime) -> str:
    """'07:02' -> '7:02 AM' (portable; no %-I glibc extension)."""
    return dt.strftime("%I:%M %p").lstrip("0")


class MedicationCommand(IJarvisCommand):
    """Mark medication doses and check today's medications."""

    @property
    def command_name(self) -> str:
        return "medication"

    @property
    def description(self) -> str:
        return (
            "Mark a medication dose as taken, or check today's medications. "
            "Use for 'I took my pills', 'mark the dog's meds as given', "
            "'what medications do I have', 'what's left to take today', "
            "'did I take my morning meds'. "
            "Medications are added and edited in the app, not by voice."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "medication", "medications", "medicine", "meds", "pill", "pills",
            "dose", "took my", "take my", "administered", "gave",
        ]

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                "action", "string", required=False,
                description=(
                    "mark=record a dose taken, list=show my medications, "
                    "status=what's left to take today"
                ),
                enum_values=_ACTIONS,
            ),
            JarvisParameter(
                "name", "string", required=False,
                description="Which medication (fuzzy match), e.g. 'the dog's meds', 'my vitamin D'",
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def rules(self) -> List[str]:
        return [
            "Medications are created in the app — never try to add a new medication by voice; "
            "if asked to add one, say it's managed in the app.",
            "'I took/gave X' or 'mark X (as taken/given)' -> action='mark', name=X",
            "'what meds / what medications / list my meds' -> action='list'",
            "'what's left today / did I take X / what do I still need to take' -> action='status'",
        ]

    # ── Mobile command-data browser ───────────────────────────────────────

    def editable_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec("id", "id", label="ID", editable=False),
            FieldSpec("name", "string", label="Medication", required=True),
            FieldSpec("dose", "string", label="Dose", placeholder="e.g. 1 pill, 75mg"),
            FieldSpec("dose_times", "array", item_type="time", label="Dose times"),
            FieldSpec("recurrence", "enum", enum_values=list(VALID_RECURRENCES), label="Repeat"),
            # scope is chosen ONCE at create time ("Visible to": Just me vs the
            # household) and must NOT be editable afterwards — re-scoping would
            # change who can see a personal med. create_only=True makes the add
            # form offer it while the edit form leaves it read-only. The owner
            # (user_id) is always stamped server-side from the authenticated
            # caller, never sent by the client.
            FieldSpec(
                "scope",
                "enum",
                enum_values=list(VALID_SCOPES),
                label="Visible to",
                editable=False,
                create_only=True,
            ),
            FieldSpec("user_id", "user_ref", label="Owner", editable=False),
            FieldSpec("active", "bool", label="Active", editable=False),
            FieldSpec("created_at", "datetime", label="Added", editable=False),
        ]

    def display_summary(self, record: dict) -> RecordSummary:
        name = record.get("name") or "Medication"
        bits: list[str] = []
        if record.get("dose"):
            bits.append(str(record["dose"]))
        times = coerce_dose_times(record.get("dose_times"))
        if times:
            bits.append(", ".join(times))
        if record.get("scope") == "household":
            bits.append("household")
        return RecordSummary(title=str(name), subtitle=" • ".join(bits) or None, icon="pill")

    @property
    def data_browser_supports_create(self) -> bool:
        """Medications are added in the app — enable the create ("+") flow."""
        return True

    def data_browser_create(
        self, fields: dict[str, Any], requesting_user_id: int | None
    ) -> tuple[str, dict[str, Any]]:
        """Create a medication from the app's add form.

        Routes through ``MedicationStore.add_medication`` so the fail-closed
        owner rule, dose-time normalization, scope→user_id stamping, and id
        minting all apply. ``scope`` ("Visible to") is the create-only field
        that decides ownership; the owner is always the authenticated caller
        (``requesting_user_id``), never client-supplied. ``add_medication``
        raises ``InvalidMedicationError`` (a ``ValueError``) on bad input,
        which the node surfaces as a 400.
        """
        record = MedicationStore().add_medication(
            name=fields.get("name", ""),
            dose=fields.get("dose", ""),
            dose_times=fields.get("dose_times") or [],
            recurrence=fields.get("recurrence") or "daily",
            scope=fields.get("scope") or "personal",
            owner_user_id=requesting_user_id,
        )
        return record["id"], record

    # ── Examples ──────────────────────────────────────────────────────────

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample("I gave the dog his meds", {"action": "mark", "name": "dog"}, is_primary=True),
            CommandExample("I took my meds", {"action": "mark"}),  # no name → speaker's personal meds
            CommandExample("I took my morning pills", {"action": "mark", "name": "morning"}),
            CommandExample("What medications do I have?", {"action": "list"}),
            CommandExample("What do I still need to take today?", {"action": "status"}),
            CommandExample("Did I take my vitamin D?", {"action": "status", "name": "vitamin d"}),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        items: list[tuple[str, dict[str, Any], bool]] = [
            ("I gave the dog his medicine", {"action": "mark", "name": "dog"}, True),
            ("Mark the dog's meds as given", {"action": "mark", "name": "dog"}, False),
            ("I took my morning meds", {"action": "mark", "name": "morning"}, False),
            ("I took my vitamin D", {"action": "mark", "name": "vitamin d"}, False),
            ("I just took my blood pressure pill", {"action": "mark", "name": "blood pressure"}, False),
            ("Mark my evening dose as taken", {"action": "mark", "name": "evening"}, False),
            ("I gave Rimadyl to the dog", {"action": "mark", "name": "rimadyl"}, False),
            ("Done with my pills", {"action": "mark"}, False),
            ("What medications do I have?", {"action": "list"}, False),
            ("List my meds", {"action": "list"}, False),
            ("Show me my medications", {"action": "list"}, False),
            ("What pills do I take?", {"action": "list"}, False),
            ("What's left to take today?", {"action": "status"}, False),
            ("What do I still need to take?", {"action": "status"}, False),
            ("Did I take my morning meds?", {"action": "status", "name": "morning"}, False),
            ("Did I give the dog his medicine yet?", {"action": "status", "name": "dog"}, False),
            ("Have I taken my vitamin D today?", {"action": "status", "name": "vitamin d"}, False),
        ]
        return [CommandExample(voice, params, is_primary) for voice, params, is_primary in items]

    def post_process_tool_call(self, args: dict[str, Any], voice_command: str) -> dict[str, Any]:
        if not args.get("action"):
            args["action"] = "list"
        return args

    # ── Execution ─────────────────────────────────────────────────────────

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        action: str = kwargs.get("action") or "list"
        viewer = request_info.user_id if request_info is not None else None
        store = MedicationStore()
        if action == "mark":
            return self._run_mark(store, viewer, kwargs.get("name"), request_info)
        if action == "status":
            return self._run_status(store, viewer, kwargs.get("name"))
        return self._run_list(store, viewer)

    def _run_list(self, store: MedicationStore, viewer: int | None) -> CommandResponse:
        meds = store.list_medications(viewer)
        if not meds:
            return CommandResponse.success_response(
                context_data={
                    "message": "You don't have any medications set up. You can add them in the app.",
                    "medications": [],
                },
                wait_for_input=False,
            )
        names = ", ".join(m["name"] for m in meds)
        formatted = [
            {
                "id": m["id"], "name": m["name"], "dose": m.get("dose"),
                "dose_times": m.get("dose_times"), "recurrence": m.get("recurrence"),
                "scope": m.get("scope"),
            }
            for m in meds
        ]
        return CommandResponse.success_response(
            context_data={
                "message": f"You have {len(meds)} medication(s): {names}.",
                "medications": formatted, "count": len(meds),
            },
            wait_for_input=False,
        )

    def _run_status(self, store: MedicationStore, viewer: int | None, name: str | None) -> CommandResponse:
        now = datetime.now().astimezone()
        meds = store.list_medications(viewer)
        if name:
            meds = _match_by_name(meds, name)

        if not meds:
            msg = (
                f"I couldn't find a medication matching '{name}'."
                if name
                else "You don't have any medications set up. You can add them in the app."
            )
            return CommandResponse.success_response(
                context_data={"message": msg, "pending": []}, wait_for_input=False
            )

        pending: list[tuple[dict, list[str]]] = []
        for med in meds:
            if not recurrence_applies(med.get("recurrence", "daily"), now.date()):
                continue
            scheduled = list(med.get("dose_times") or [])
            covered = store.covered_slots(med["id"], now.date())
            if covered:
                # Slot-tagged: what's left is what wasn't actually administered.
                # (Slicing by a count marked the evening dose as taken whenever
                # the morning one was confirmed twice.)
                remaining = [t for t in scheduled if t not in covered]
            else:
                taken = len(store.doses_on(med["id"], now.date()))
                remaining = scheduled[taken:]  # legacy untagged rows
            if remaining:
                pending.append((med, remaining))

        if not pending:
            return CommandResponse.success_response(
                context_data={
                    "message": "You're all caught up on your medications for today.",
                    "pending": [],
                },
                wait_for_input=False,
            )

        parts = [f"{med['name']} ({', '.join(rem)})" for med, rem in pending]
        return CommandResponse.success_response(
            context_data={
                "message": "Still to take today: " + "; ".join(parts) + ".",
                "pending": [
                    {"id": med["id"], "name": med["name"], "remaining": rem}
                    for med, rem in pending
                ],
            },
            wait_for_input=False,
        )

    def _run_mark(
        self,
        store: MedicationStore,
        viewer: int | None,
        name: str | None,
        request_info: RequestInformation,
    ) -> CommandResponse:
        # Generic "I took my meds" (no specific drug named) → use the recognized
        # speaker to mark THEIR pending personal meds. Handled first so an
        # unknown speaker gets "who are you?" rather than "no medications"
        # (their personal meds aren't visible to an unidentified viewer).
        if not _significant_tokens(name or ""):
            return self._run_mark_mine(store, viewer)

        meds = store.list_medications(viewer)
        if not meds:
            msg = "You don't have any medications set up yet. You can add them in the app."
            return CommandResponse.error_response(
                error_details=msg, context_data={"message": msg, "error": "no_medications"}
            )

        matches = _match_by_name(meds, name)
        _logger.info(
            "medication mark attempt", spoken_name=name, viewer=viewer,
            visible=[m.get("name") for m in meds], matched=[m.get("name") for m in matches],
        )

        if not matches:
            msg = f"I couldn't find a medication matching '{name}'."
            return CommandResponse.error_response(
                error_details=msg, context_data={"message": msg, "error": "not_found"}
            )
        if len(matches) > 1:
            names = ", ".join(m["name"] for m in matches)
            msg = f"Which one — {names}?"
            return CommandResponse.error_response(
                error_details=msg,
                context_data={"message": msg, "error": "ambiguous", "candidates": [m["name"] for m in matches]},
                wait_for_input=True,
            )

        med = matches[0]
        _logger.info(
            "medication mark resolved", spoken_name=name, matched=med.get("name"),
            scope=med.get("scope"), viewer=viewer,
        )
        administered_by = request_info.user_id if request_info is not None else None
        broadcast = self._record_and_broadcast(store, med, administered_by, source="voice")
        if med.get("scope") == "household" and broadcast:
            msg = f"Marked {med['name']} as given, and let the household know."
        else:
            msg = f"Marked {med['name']} as taken."
        return CommandResponse.success_response(
            context_data={"message": msg, "med_id": med["id"], "name": med["name"]},
            wait_for_input=False,
        )

    def _run_mark_mine(self, store: MedicationStore, viewer: int | None) -> CommandResponse:
        """Mark the recognized speaker's pending personal meds ("I took my meds").

        Scoped to the speaker's OWN meds (household meds like the dog's are not
        "mine"). Denies when the speaker is unknown — we can't attribute personal
        meds to an unidentified voice.
        """
        if viewer is None:
            msg = (
                "I'm not sure who's speaking, so I can't tell which medications are "
                "yours — try saying the medication name, like 'I took my vitamin D'."
            )
            return CommandResponse.error_response(
                error_details=msg, context_data={"message": msg, "error": "unknown_speaker"}
            )
        now = datetime.now().astimezone()
        mine = [m for m in store.list_medications(viewer) if record_owner(m) == viewer]
        if not mine:
            msg = "You don't have any personal medications set up. You can add them in the app."
            return CommandResponse.success_response(
                context_data={"message": msg, "marked": []}, wait_for_input=False
            )
        pending = [m for m in mine if self._has_pending_dose(store, m, now)]
        if not pending:
            msg = "You've already taken all your medications for today."
            return CommandResponse.success_response(
                context_data={"message": msg, "marked": []}, wait_for_input=False
            )
        marked: list[str] = []
        for med in pending:
            self._record_and_broadcast(store, med, viewer, source="voice", now=now)
            marked.append(med["name"])
        msg = (
            f"Marked {marked[0]} as taken."
            if len(marked) == 1
            else "Marked your medications: " + ", ".join(marked) + "."
        )
        _logger.info("medication marked mine", viewer=viewer, marked=marked)
        return CommandResponse.success_response(
            context_data={"message": msg, "marked": marked}, wait_for_input=False
        )

    def _has_pending_dose(self, store: MedicationStore, med: dict, now: datetime) -> bool:
        """True if the med still has an unmarked dose scheduled for today."""
        if not recurrence_applies(med.get("recurrence", "daily"), now.date()):
            return False
        med_id = med.get("id") or med.get("_data_key")
        if not med_id:
            return True
        slots = coerce_dose_times(med.get("dose_times"))
        covered = store.covered_slots(med_id, now.date())
        if covered:
            return any(t not in covered for t in slots)
        # Legacy untagged rows: fall back to counting.
        return len(store.doses_on(med_id, now.date())) < len(slots)

    @callback("mark_administered")
    def mark_administered(self, data: dict, request_info: RequestInformation) -> CommandResponse:
        """Tap handler for the 'Mark administered' button on a reminder push."""
        med_id = (data or {}).get("med_id")
        viewer = request_info.user_id if request_info is not None else None
        store = MedicationStore()
        med = store.get_medication(med_id, viewer) if med_id else None
        if med is None:
            return CommandResponse.error_response(
                error_details="That medication is no longer available.",
                context_data={"message": "That medication is no longer available."},
            )
        self._record_and_broadcast(store, med, viewer, source="tap")
        return CommandResponse.final_response(
            context_data={"message": f"Marked {med['name']} as administered.", "med_id": med["id"]}
        )

    # ── Shared mark + broadcast ───────────────────────────────────────────

    def _record_and_broadcast(
        self,
        store: MedicationStore,
        med: dict,
        administered_by: int | None,
        *,
        source: str,
        now: datetime | None = None,
    ) -> bool:
        """Log the dose; for a household med, broadcast it so nobody double-doses.

        Returns True if a household broadcast was posted successfully.
        """
        recorded = store.log_dose(
            med, administered_by=administered_by, source=source, now=now
        )
        if recorded is None:
            # Every slot for today is already covered — this is a duplicate
            # confirmation (double-tap, or a tap plus a spoken "I took it").
            # Don't log it and don't broadcast: a second "administered" push is
            # how a household member gets told twice, and the extra row is what
            # used to silently mark the next dose as taken.
            _logger.info(
                "medication mark ignored (dose already recorded today)",
                med=med.get("name"),
                source=source,
            )
            return False
        if med.get("scope") != "household":
            _logger.info("medication marked (personal — no broadcast)", med=med.get("name"), source=source)
            return False
        when = _spoken_time((now or datetime.now()).astimezone())
        dose = med.get("dose")
        detail = f"{med['name']} ({dose})" if dose else med["name"]
        tag = JarvisInbox(self.command_name).post(
            title=f"{med['name']} administered",
            summary=f"Given at {when}",
            body=f"{detail} was administered at {when}.",
            category="medication",
            create_push_notification=True,
            target_type="household",
        )
        _logger.info("medication household broadcast", med=med.get("name"), source=source, post_tag=tag)
        return tag == "ok"
