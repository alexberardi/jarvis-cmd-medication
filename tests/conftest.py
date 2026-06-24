"""Shared test fixtures: an in-memory StorageBackend so JarvisStorage works
without a real node DB (mirrors the SDK's tests/test_storage.py pattern)."""

import pytest
from jarvis_command_sdk.inbox import InboxBackend, get_inbox_backend, set_inbox_backend
from jarvis_command_sdk.storage import StorageBackend, get_backend, set_backend


class FakeStorageBackend(StorageBackend):
    """In-memory (command_name, key) -> dict store. Copies on read/write so
    callers can't mutate stored records by reference (matches the real DB)."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict] = {}
        self.secrets: dict[tuple[str, str, int | None], str] = {}

    def save(self, command_name, data_key, data, expires_at=None):
        self.data[(command_name, data_key)] = dict(data)

    def get(self, command_name, data_key):
        rec = self.data.get((command_name, data_key))
        return dict(rec) if rec is not None else None

    def get_all(self, command_name):
        return [dict(v) for (c, _k), v in self.data.items() if c == command_name]

    def delete(self, command_name, data_key):
        return self.data.pop((command_name, data_key), None) is not None

    def delete_all(self, command_name):
        keys = [(c, k) for (c, k) in self.data if c == command_name]
        for key in keys:
            del self.data[key]
        return len(keys)

    def get_secret(self, key, scope, user_id=None):
        return self.secrets.get((key, scope, user_id))

    def set_secret(self, key, value, scope, value_type="string", user_id=None):
        self.secrets[(key, scope, user_id)] = value

    def delete_secret(self, key, scope, user_id=None):
        self.secrets.pop((key, scope, user_id), None)


@pytest.fixture
def backend():
    prev = get_backend()
    fake = FakeStorageBackend()
    set_backend(fake)
    yield fake
    set_backend(prev)  # type: ignore[arg-type]


class FakeInboxBackend(InboxBackend):
    """Captures posted inbox items so tests can assert the household broadcast."""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    def post_inbox_item(
        self, command_name, *, title, summary="", body="", category="general",
        metadata=None, user_id=None, create_push_notification=False, target_type="household",
    ):
        self.posts.append({
            "command_name": command_name, "title": title, "summary": summary,
            "body": body, "category": category, "metadata": metadata,
            "user_id": user_id, "push": create_push_notification, "target_type": target_type,
        })
        return "ok"


@pytest.fixture
def inbox():
    prev = get_inbox_backend()
    fake = FakeInboxBackend()
    set_inbox_backend(fake)
    yield fake
    set_inbox_backend(prev)  # type: ignore[arg-type]
