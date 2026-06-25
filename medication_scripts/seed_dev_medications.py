#!/usr/bin/env python3
"""Dev seed: put sample medications on a running jarvis-node for testing.

Run ON THE NODE with the node venv (so the node storage backend is available):

    cd /opt/jarvis-node && .venv/bin/python /home/pi/seed_dev_medications.py [PERSONAL_USER_ID]

Seeds a HOUSEHOLD dog med (7am/7pm — every household member sees it) and, when a
user id is given, a PERSONAL morning med owned by that user (only they see it).
Idempotent by medication name. With no user id, prints the user_ids seen in
existing reminders so you can pick a real owner for the personal med.
"""

import sys
import uuid
from datetime import datetime, timezone

from services.storage_backend import init_storage_backend

init_storage_backend()

from jarvis_command_sdk import JarvisStorage

meds = JarvisStorage("medication")


def make(name: str, dose: str, times: list[str], scope: str, user_id: int | None) -> None:
    if any(m.get("name") == name for m in meds.get_all()):
        print(f"  exists, skip: {name!r}")
        return
    med_id = "med-" + uuid.uuid4().hex
    meds.save(
        med_id,
        {
            "id": med_id, "name": name, "dose": dose, "dose_times": times,
            "recurrence": "daily", "scope": scope, "user_id": user_id,
            "active": True, "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(f"  seeded {scope:9} {name!r} times={times} user_id={user_id} -> {med_id}")


print(f"existing medication records: {len(meds.get_all())}")
print("seeding:")
make("Dog Rimadyl", "75mg", ["07:00", "19:00"], "household", None)

personal_uid = int(sys.argv[1]) if len(sys.argv) > 1 else None
if personal_uid is not None:
    make("Morning Vitamins", "1 pill", ["08:00"], "personal", personal_uid)
else:
    reminders = JarvisStorage("set_reminder").get_all()
    uids = sorted({r.get("user_id") for r in reminders if r.get("user_id") is not None})
    print("\n(no personal med seeded — pass a user id as the first arg to seed one.)")
    print(f"user_ids seen in existing reminders: {uids or 'none'}")

print("\nfinal medication records:")
for m in meds.get_all():
    print(f"  - {m['name']} [{m['scope']}] times={m.get('dose_times')} owner={m.get('user_id')}")
