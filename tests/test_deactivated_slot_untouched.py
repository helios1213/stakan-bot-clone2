"""A slot whose webkey was deleted must never be touched again.

Deleting the key is how the operator says "this account is mine now". It did not
work: the pool logged "deactivating slot" while keeping the executor —
credentials included — in `_executors`, and reconcile read that dict directly.
A manually opened GRVT_USDT SHORT was closed as an orphan for -$1.48.

Two guarantees, tested separately because they fail independently:
  1. `active_executors()` excludes a deactivated slot;
  2. reconcile skips a close when the slot has no key, even if the pool's view
     is stale (rebuild runs on a timer, so there is always such a window).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.execution.live_pool import LiveExecutorPool
from src.safety.reconciliation import reconcile_once


def _executor_holding_a_position():
    """An executor whose account really does hold a position.

    Positions come through executor.client_pool.get(slot) -> get_open_positions,
    not off the executor directly. Mocking the wrong path made the negative test
    pass for the wrong reason — there was simply nothing to close.
    """
    client = MagicMock()
    client.get_open_positions = AsyncMock(return_value={
        "code": 0,
        # createTime well in the past: a position of unknown age gets the
        # grace period applied, not bypassed, so without this BOTH tests pass
        # vacuously — nothing is ever eligible to close.
        "data": [{"symbol": "GRVT_USDT", "positionType": 2, "holdVol": 300,
                  "leverage": 20, "openAvgPrice": 0.24, "im": 3.6,
                  "createTime": int((time.time() - 3600) * 1000)}],
    })
    executor = MagicMock()
    executor.client_pool = MagicMock()
    executor.client_pool.get = AsyncMock(return_value=client)
    return executor


def _pool_with(active_ids: set[int], executors: dict) -> LiveExecutorPool:
    p = LiveExecutorPool.__new__(LiveExecutorPool)
    p._executors = executors
    p._active_slot_ids = active_ids
    p._pair_to_slots = {}
    p._safety_controllers = {}
    return p


class TestActiveExecutors:
    def test_deactivated_slot_is_excluded(self):
        ex1, ex2 = MagicMock(), MagicMock()
        pool = _pool_with({1}, {1: ex1, 2: ex2})
        active = pool.active_executors()
        assert set(active) == {1}, "slot 2 lost its key and must not be usable"

    def test_executor_object_is_kept_for_stats(self):
        """The original comment wanted stats to survive deactivation — they do;
        what changes is that the slot may no longer reach the exchange."""
        ex1, ex2 = MagicMock(), MagicMock()
        pool = _pool_with({1}, {1: ex1, 2: ex2})
        pool.active_executors()
        assert 2 in pool._executors

    def test_no_active_ids_means_nothing_is_usable(self):
        pool = _pool_with(set(), {1: MagicMock(), 2: MagicMock()})
        assert pool.active_executors() == {}


@pytest.mark.asyncio
async def test_reconcile_will_not_close_on_a_slot_without_a_key():
    """The stale-view case: the pool still lists the slot as active, but the key
    is already gone. Nothing may be closed."""
    executor = _executor_holding_a_position()
    executor.market_close_position = AsyncMock()

    pool = MagicMock()
    pool._executors = {1: executor}
    pool.active_executors = MagicMock(return_value={1: executor})
    pool.get_executor = MagicMock(return_value=executor)
    # The key is gone even though the pool has not rebuilt yet.
    pool.slot_has_key = AsyncMock(return_value=False)

    engine = SimpleNamespace(_open_positions={}, live_pool=pool, db=None)
    await reconcile_once(engine, pool, alerts=None)

    executor.market_close_position.assert_not_awaited(), (
        "a position on a keyless slot was closed — this is the reported bug")


@pytest.mark.asyncio
async def test_reconcile_still_closes_a_real_orphan_when_the_key_is_present():
    """The guard must not strand genuine orphans — that is what reconcile is
    for, and an unmanaged position with no stop is how the -$25.82 liquidation
    happened."""
    executor = _executor_holding_a_position()
    executor.market_close_position = AsyncMock(
        return_value=SimpleNamespace(success=True, realised_pnl=0.0,
                                     exit_price=0.24, latency_ms=200,
                                     error_msg=None))

    pool = MagicMock()
    pool._executors = {1: executor}
    pool.active_executors = MagicMock(return_value={1: executor})
    pool.get_executor = MagicMock(return_value=executor)
    pool.slot_has_key = AsyncMock(return_value=True)

    engine = SimpleNamespace(_open_positions={}, live_pool=pool, db=None)
    await reconcile_once(engine, pool, alerts=None)

    assert executor.market_close_position.await_count == 1
