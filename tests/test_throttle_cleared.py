"""Deleting a webkey must take the open-rate latch with it.

An open-rate refusal (10014/9082/2036) latches the slot into throttled mode for
six hours, persisted to webkey_slots.open_throttle_until so it survives a
restart. That is correct for the account that earned it and wrong for the next
one, because MEXC enforces the limit per account, not per slot.

Seen on the primary: slot 1 took a 9082, the webkey was replaced two hours
later, and the fresh account inherited the throttle — 5 requests in an hour
against an unthrottled slot's 104, since under the latch a request costs ~72s
even when the IOC never fills.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from src.execution.webkey.credentials import WebkeyStore
from src.storage.db import Database, init_db

SAMPLE_WEBKEY = "WEB" + "a" * 64
SAMPLE_WEBKEY_2 = "WEB" + "b" * 64


@pytest_asyncio.fixture
async def store():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    await init_db(db_path)
    db = Database(db_path)
    await db.connect()
    s = WebkeyStore(db, Fernet.generate_key().decode())
    await s.ensure_slots_seeded()
    yield s
    await db.close()
    Path(db_path).unlink(missing_ok=True)


async def _latched(store, slot_id: int = 1) -> int:
    """Put a slot under a six-hour throttle, the way a refusal would."""
    await store.set_webkey(slot_id, SAMPLE_WEBKEY)
    until = int(time.time()) + 6 * 3600
    await store.set_open_throttle_until(slot_id, until)
    return until


@pytest.mark.asyncio
async def test_delete_clears_the_open_throttle(store):
    await _latched(store)
    row = await store.db.fetchone(
        "SELECT open_throttle_until FROM webkey_slots WHERE slot_id=1")
    assert row["open_throttle_until"] is not None, "setup did not latch"

    await store.delete(1)

    row = await store.db.fetchone(
        "SELECT open_throttle_until FROM webkey_slots WHERE slot_id=1")
    assert row["open_throttle_until"] is None, \
        "the next account would inherit the previous one's rate limit"


@pytest.mark.asyncio
async def test_a_replacement_webkey_starts_unthrottled(store):
    """The whole point: delete + add leaves the new account free."""
    await _latched(store)
    await store.delete(1)
    await store.set_webkey(1, SAMPLE_WEBKEY_2)

    slot = await store.get(1)
    assert getattr(slot, "open_throttle_until", None) in (None, 0)


@pytest.mark.asyncio
async def test_delete_does_not_touch_another_slots_throttle(store):
    """Slot 2 earned its own limit and must keep it."""
    await _latched(store, 1)
    until2 = await _latched(store, 2)

    await store.delete(1)

    row = await store.db.fetchone(
        "SELECT open_throttle_until FROM webkey_slots WHERE slot_id=2")
    assert row["open_throttle_until"] == until2


@pytest.mark.asyncio
async def test_delete_nulls_the_webkey_stamp(store):
    """The engine watches this field to notice the account changed.

    It is a whole-second stamp, so a delete and an add inside one second leave
    it at the same value — which is exactly why the engine also clears on the
    stored deadline disappearing. What must hold here is that delete() puts it
    through NULL rather than leaving the old value in place.
    """
    await _latched(store)
    assert (await store.get(1)).webkey_refreshed_at is not None

    await store.delete(1)
    assert (await store.get(1)).webkey_refreshed_at is None

    await store.set_webkey(1, SAMPLE_WEBKEY_2)
    assert (await store.get(1)).webkey_refreshed_at is not None


@pytest.mark.asyncio
async def test_a_same_second_swap_still_leaves_no_stored_latch(store):
    """The case the timestamp cannot catch — the deadline must still be gone."""
    await _latched(store)
    await store.delete(1)
    await store.set_webkey(1, SAMPLE_WEBKEY_2)

    row = await store.db.fetchone(
        "SELECT open_throttle_until, webkey_refreshed_at FROM webkey_slots WHERE slot_id=1")
    assert row["open_throttle_until"] is None
    assert row["webkey_refreshed_at"] is not None
