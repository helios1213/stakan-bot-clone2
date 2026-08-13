"""Self-review #1: reconcile must NOT orphan-close a position whose NORMAL close
is already in flight (is_closing) — doing so double-books its PnL into the $25
kill (the close path books it too). Regression guard for the backfill edit.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.safety.reconciliation import reconcile_once


@pytest.mark.asyncio
async def test_case_a_skips_a_position_being_closed_normally():
    # MEXC still lists the position (close not yet settled)
    mexc_pos = {"symbol": "SOXL_USDT", "positionType": 1, "holdVol": 3000,
                "leverage": 48, "openAvgPrice": 0.0027, "im": 90.0,
                "createTime": int((time.time() - 3600) * 1000)}
    client = MagicMock()
    client.get_open_positions = AsyncMock(return_value={"code": 0, "data": [mexc_pos]})
    executor = MagicMock()
    executor.slot_id = 2
    executor.client_pool = MagicMock()
    executor.client_pool.get = AsyncMock(return_value=client)
    executor.market_close_position = AsyncMock()

    pool = MagicMock()
    pool._executors = {2: executor}
    pool.active_executors = MagicMock(return_value={2: executor})
    pool.get_executor = MagicMock(return_value=executor)
    pool.slot_has_key = AsyncMock(return_value=True)
    pool.get_safety = MagicMock(return_value=MagicMock())

    # the engine IS tracking it, but it is mid normal-close (is_closing=True)
    closing = SimpleNamespace(mode="live", is_open=True, is_closing=True,
                              symbol="SOXLUSDT", elapsed_sec=100.0,
                              account_label="slot2")
    marked = AsyncMock()
    engine = SimpleNamespace(_open_positions={"SOXLUSDT": [closing]},
                             live_pool=pool, live_db=MagicMock(execute=AsyncMock()),
                             mark_position_closed_externally=marked)

    await reconcile_once(engine, pool, alerts=None)

    executor.market_close_position.assert_not_awaited()  # no orphan-close
    marked.assert_not_awaited()                          # and no external-close re-book
