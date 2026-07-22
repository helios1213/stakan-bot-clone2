"""Tests for reconciliation fixes F-010 (multi-slot collision) and F-012
(age=0 grace bypass).
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.safety.reconciliation import reconcile_once


# Helper to build a mock executor that returns a fixed list of positions
def make_executor(positions_for_get_open):
    client = AsyncMock()
    client.get_open_positions = AsyncMock(
        return_value={"code": 0, "data": positions_for_get_open}
    )
    client_pool = AsyncMock()
    client_pool.get = AsyncMock(return_value=client)
    executor = MagicMock()
    executor.client_pool = client_pool
    executor.market_close_position = AsyncMock(return_value=MagicMock(
        success=True,
        realized_pnl_usdt=0.0,
        error_msg=None,
    ))
    return executor


# ────────────────────────────────────────────────────────────────────────
# F-010 — multi-slot orphan attribution
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_handles_same_symbol_in_two_slots_independently():
    """Two slots each holding an orphan SUI_USDT position must BOTH be
    detected and closed. Before the fix, the second slot's position
    overwrote the first in the symbol-keyed dict and the first was missed."""

    # Both slots have an orphan position in SUI_USDT with createTime old
    # enough to bypass the grace window.
    old_ts_ms = int((time.time() - 60) * 1000)  # 60s ago
    pos1 = {
        "symbol": "SUI_USDT",
        "positionType": 1, "holdVol": 10,
        "leverage": 50, "createTime": old_ts_ms,
    }
    pos2 = {
        "symbol": "SUI_USDT",
        "positionType": 2, "holdVol": 20,
        "leverage": 60, "createTime": old_ts_ms,
    }
    exec1 = make_executor([pos1])
    exec2 = make_executor([pos2])

    live_pool = MagicMock()
    live_pool._executors = {1: exec1, 2: exec2}
    live_pool.get_executor = MagicMock(
        side_effect=lambda sid: {1: exec1, 2: exec2}.get(sid)
    )

    shadow_engine = MagicMock()
    shadow_engine._open_positions = {}  # engine knows about NOTHING

    summary = await reconcile_once(shadow_engine, live_pool, alerts=None)

    # BOTH orphans must be closed — one per slot
    assert summary["orphans_closed"] == 2, (
        f"Expected 2 orphans closed (one per slot), got {summary}"
    )
    # Each executor must have been told to close
    exec1.market_close_position.assert_awaited_once()
    exec2.market_close_position.assert_awaited_once()


# ────────────────────────────────────────────────────────────────────────
# F-012 — age=0 must still apply grace
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_skips_orphan_when_createTime_unknown():
    """If MEXC returns createTime=0 (unknown age), grace MUST apply rather
    than be bypassed. Old code's `0 < age < GRACE` skipped grace at age==0,
    force-closing positions of unknown age."""

    pos = {
        "symbol": "SUI_USDT",
        "positionType": 1, "holdVol": 10,
        "leverage": 50, "createTime": 0,   # ← unknown / missing
    }
    executor = make_executor([pos])
    live_pool = MagicMock()
    live_pool._executors = {1: executor}
    live_pool.get_executor = MagicMock(return_value=executor)

    shadow_engine = MagicMock()
    shadow_engine._open_positions = {}

    summary = await reconcile_once(shadow_engine, live_pool, alerts=None)

    # Position has age=0 (unknown). Grace MUST apply → no close.
    assert summary["orphans_closed"] == 0, (
        f"Position of unknown age was closed despite grace: {summary}"
    )
    executor.market_close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_closes_when_position_is_old_enough():
    """Sanity check: old positions (createTime well before grace window)
    are still closed normally."""

    pos = {
        "symbol": "SUI_USDT",
        "positionType": 1, "holdVol": 10,
        "leverage": 50,
        "createTime": int((time.time() - 120) * 1000),  # 2 min ago
    }
    executor = make_executor([pos])
    live_pool = MagicMock()
    live_pool._executors = {1: executor}
    live_pool.get_executor = MagicMock(return_value=executor)

    shadow_engine = MagicMock()
    shadow_engine._open_positions = {}

    summary = await reconcile_once(shadow_engine, live_pool, alerts=None)

    assert summary["orphans_closed"] == 1, f"Old orphan not closed: {summary}"
    executor.market_close_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_skips_just_opened_position():
    """Positions opened within the grace window (15s) are skipped even
    when MEXC returns valid createTime."""

    pos = {
        "symbol": "SUI_USDT",
        "positionType": 1, "holdVol": 10,
        "leverage": 50,
        "createTime": int((time.time() - 5) * 1000),  # 5s ago, inside grace
    }
    executor = make_executor([pos])
    live_pool = MagicMock()
    live_pool._executors = {1: executor}
    live_pool.get_executor = MagicMock(return_value=executor)

    shadow_engine = MagicMock()
    shadow_engine._open_positions = {}

    summary = await reconcile_once(shadow_engine, live_pool, alerts=None)

    assert summary["orphans_closed"] == 0, (
        f"Fresh position closed despite grace: {summary}"
    )
    executor.market_close_position.assert_not_awaited()
