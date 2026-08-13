"""slot_has_key must read the REAL WebkeySlot field.

Regression for the -$42 orphan (2026-08-13): slot_has_key checked
`slot.webkey_blob`, a field WebkeySlot does NOT have (the decrypted key lives in
`slot.webkey`). getattr(..., "webkey_blob", None) was therefore ALWAYS None ->
slot_has_key ALWAYS False -> reconcile logged "без вебкея" on every orphan and
NEVER closed one. The whole orphan-close safety net was dead.

The existing reconcile tests MOCK slot_has_key, so they never exercised this
implementation. These drive the real method with real WebkeySlot objects.
"""
from __future__ import annotations

import pytest

from src.execution.live_pool import LiveExecutorPool
from src.execution.webkey.credentials import WebkeySlot


class FakeStore:
    def __init__(self, result=None, raises=False):
        self._result = result
        self._raises = raises

    async def get(self, slot_id):
        if self._raises:
            raise RuntimeError("db down")
        return self._result


def _pool(store):
    p = LiveExecutorPool.__new__(LiveExecutorPool)   # bypass __init__
    p.webkey_store = store
    return p


def _slot(**kw):
    kw.setdefault("slot_id", 1)
    kw.setdefault("label", "x")
    kw.setdefault("enabled", True)
    kw.setdefault("webkey", "WEB" + "0" * 64)
    kw.setdefault("visitor_id", "v" * 20)
    return WebkeySlot(**kw)


@pytest.mark.asyncio
async def test_enabled_keyed_slot_HAS_key():
    """The bug: this returned False for a fully-keyed, enabled slot -> orphan
    left unmanaged (the -$42 SOXL long)."""
    assert await _pool(FakeStore(_slot())).slot_has_key(1) is True


@pytest.mark.asyncio
async def test_deleted_key_has_no_key():
    assert await _pool(FakeStore(_slot(webkey=None, visitor_id=None))).slot_has_key(1) is False


@pytest.mark.asyncio
async def test_disabled_slot_has_no_key():
    assert await _pool(FakeStore(_slot(enabled=False))).slot_has_key(1) is False


@pytest.mark.asyncio
async def test_missing_slot_has_no_key():
    assert await _pool(FakeStore(None)).slot_has_key(1) is False


@pytest.mark.asyncio
async def test_store_error_is_safe_false():
    assert await _pool(FakeStore(raises=True)).slot_has_key(1) is False
