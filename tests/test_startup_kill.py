"""Group B #3: a startup force-close must feed the $25 kill (record_close).

startup_reconcile previously only logged the close — the realized loss never
reached record_close nor live_trades, so the peak-drawdown kill resumed after a
restart blind to that loss (the periodic path persisted; startup did not).
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.safety.reconciliation import startup_reconcile


@pytest.mark.asyncio
async def test_startup_close_feeds_the_kill():
    position = {"symbol": "SOXL_USDT", "positionType": 1, "holdVol": 3000,
                "leverage": 48, "openAvgPrice": 144.0, "im": 90.0,
                "createTime": int((time.time() - 3600) * 1000)}
    client = MagicMock()
    client.get_open_positions = AsyncMock(return_value={"code": 0, "data": [position]})
    executor = MagicMock()
    executor.slot_id = 1
    executor.client_pool = MagicMock()
    executor.client_pool.get = AsyncMock(return_value=client)
    executor.market_close_position = AsyncMock(return_value=SimpleNamespace(
        success=True, exit_price=144.0, realized_pnl_usdt=-20.0,
        entry_price_confirmed=144.0, error_msg=None))

    safety = MagicMock()
    pool = MagicMock()
    pool._executors = {1: executor}
    pool.active_executors = MagicMock(return_value={1: executor})
    pool.webkey_store = MagicMock()
    pool.webkey_store.list_enabled_complete = AsyncMock(
        return_value=[SimpleNamespace(slot_id=1)])
    pool.get_or_create_executor = MagicMock(return_value=executor)
    pool.get_executor = MagicMock(return_value=executor)
    pool.slot_has_key = AsyncMock(return_value=True)
    pool.get_safety = MagicMock(return_value=safety)

    shadow_engine = SimpleNamespace(live_db=MagicMock(execute=AsyncMock()))

    summary = await startup_reconcile(pool, None, shadow_engine)

    assert summary["closed_successfully"] == 1
    safety.record_close.assert_called_once()
    assert safety.record_close.call_args.args[1] == -20.0   # the real loss
