# jarvis-cmd-medication

Medication tracking for [Jarvis](https://github.com/alexberardi/jarvis-node-setup).

Track **personal** and **household** medications, get a push + voice reminder when a
dose is due (and an overdue warning if it slips), and mark doses administered by tapping
the notification or by voice. Marking a household med (e.g. the dog's meds) tells the
rest of the household it was given.

Medications are added and edited **in the app** — voice is for marking doses and quick
queries, not data entry.

## Install

```bash
python scripts/command_store.py install --url https://github.com/alexberardi/jarvis-cmd-medication
```

## Privacy model

Each medication record carries its own `user_id`:

| Scope | `user_id` | Who can see / mark it | Reminders go to |
|-------|-----------|------------------------|-----------------|
| **personal** | the owner | only the owner | the owner |
| **household** | `None` | everyone in the household | the whole household |

Personal meds are fail-closed: if the owner can't be resolved at add time, the record is
*not* made household-visible. One member never sees another member's personal meds.

## Voice

- "I took my meds" → uses **speaker recognition** to mark the speaker's own pending
  personal doses (unknown speaker is denied, never guessed).
- "Mark the dog's Rimadyl as given" → marks a named med; household meds broadcast to
  everyone. Name matching is word-overlap, so "Keppra for Leo" still finds "Leo Keppra".
- "What medications are still due?" → privacy-scoped status of remaining doses today.

## Components

| Component | Type | Description |
|-----------|------|-------------|
| `medication` | command | List / status / mark doses; `mark_administered` callback for the notification button |
| `medication_reminders` | agent | Polls every 5 min; reminds when a dose is due, warns when one is overdue (30-min grace, re-warns hourly) |

## Layout

```
commands/medication/command.py          # voice/tap surface, privacy-scoped, speaker-aware mark
agents/medication_reminders/agent.py    # due-dose reminders + overdue warnings
medication_shared/
  schedule_util.py                       # pure dose-schedule logic (states, due/overdue, recurrence)
  med_store.py                           # JarvisStorage-backed records + doses, visibility rules
scripts/seed_dev_medications.py          # dev convenience: seed a few sample meds
tests/                                   # unit tests (pure logic + command + agent + store)
```

## Tests

```bash
pytest -q
```

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
