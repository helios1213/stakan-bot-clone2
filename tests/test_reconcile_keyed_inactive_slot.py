"""Reconcile must reach a keyed slot even when it is NOT live-active.

Audit #7 / the -$42 class: a slot that holds a key (and a real 45-50x position)
but is not live-active — after a restart, or deactivated while a position was
open — had no executor, so reconcile never fetched its account and the position
bled unmanaged, invisible to the $25 kill too. Fix: reconcile enumerates ALL
keyed slots (list_enabled_complete) via get_or_create_executor; slot_has_key
still gates the close.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.safety.reconciliation import reconcile_once


def _executor_holding_orphan():
    client = MagicMock()
    client.get_open_positions = AsyncMock(return_value={
        "code": 0,
        "data": [{"symbol": "SOXL_USDT", "positionType": 1, "holdVol": 3122,
                  "leverage": 49, "openAvgPrice": 144.0, "im": 95.0,
                  "createTime": int((time.time() - 3600) * 1000)}],
    })
    ex = MagicMock()
    ex.client_pool = MagicMock()
    ex.client_pool.get = AsyncMock(return_value=client)
    ex.market_close_position = AsyncMock(return_value=SimpleNamespace(
        success=True, exit_price=144.0, realized_pnl_usdt=-1.0,
        entry_price_confirmed=144.0, error_msg=None))
    return ex


@pytest.mark.asyncio
async def test_keyed_but_not_active_slot_orphan_is_closed():
    """slot 2 is keyed (list_enabled_complete) but NOT live-active
    (active_executors empty). Its orphan must still be found and closed."""
    executor = _executor_holding_orphan()

    pool = MagicMock()
    pool._executors = {}
    pool.active_executors = MagicMock(return_value={})            # NOT live-active
    pool.webkey_store = MagicMock()
    pool.webkey_store.list_enabled_complete = AsyncMock(
        return_value=[SimpleNamespace(slot_id=2)])                # but IS keyed
    pool.get_or_create_executor = MagicMock(return_value=executor)
    pool.get_executor = MagicMock(return_value=executor)
    pool.slot_has_key = AsyncMock(return_value=True)
    pool.get_safety = MagicMock(return_value=None)

    engine = SimpleNamespace(_open_positions={}, live_pool=pool, live_db=None)
    await reconcile_once(engine, pool, alerts=None)

    executor.market_close_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_keyless_slot_still_not_touched():
    """The safety gate survives: an enumerated slot whose key is gone
    (slot_has_key False) must NOT be closed."""
    executor = _executor_holding_orphan()

    pool = MagicMock()
    pool._executors = {}
    pool.active_executors = MagicMock(return_value={})
    pool.webkey_store = MagicMock()
    pool.webkey_store.list_enabled_complete = AsyncMock(
        return_value=[SimpleNamespace(slot_id=2)])
    pool.get_or_create_executor = MagicMock(return_value=executor)
    pool.get_executor = MagicMock(return_value=executor)
    pool.slot_has_key = AsyncMock(return_value=False)             # key gone
    pool.get_safety = MagicMock(return_value=None)

    engine = SimpleNamespace(_open_positions={}, live_pool=pool, live_db=None)
    await reconcile_once(engine, pool, alerts=None)

    executor.market_close_position.assert_not_awaited()
