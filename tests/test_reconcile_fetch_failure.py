"""Reconcile must not tear down a live position on a transient MEXC read failure.

Group-A audit bug (2026-08-13): _fetch_mexc_positions_for_slot returned [] on
BOTH a failed read (510/timeout/exception) and a genuinely empty account. Case B
then marked an attributed live position "closed externally" — fabricating PnL,
tearing down the watcher/stop/trail (45-50x runs unmanaged), and double-counting
PnL into the $25 kill. Fix: None on failure; Case B acts only on slots read OK.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.safety.reconciliation import (
    _fetch_mexc_positions_for_slot,
    reconcile_once,
)


# ── unit: the fetch contract (None on failure, list on success) ──
def _exec(get_open_positions):
    client = MagicMock()
    client.get_open_positions = get_open_positions
    ex = MagicMock()
    ex.client_pool = MagicMock()
    ex.client_pool.get = AsyncMock(return_value=client)
    return ex


@pytest.mark.asyncio
async def test_fetch_none_on_code_error():
    ex = _exec(AsyncMock(return_value={"code": 510, "msg": "rate limit"}))
    assert await _fetch_mexc_positions_for_slot(ex, 1) is None


@pytest.mark.asyncio
async def test_fetch_none_on_exception():
    ex = _exec(AsyncMock(side_effect=RuntimeError("boom")))
    assert await _fetch_mexc_positions_for_slot(ex, 1) is None


@pytest.mark.asyncio
async def test_fetch_list_on_success():
    ex = _exec(AsyncMock(return_value={"code": 0, "data": [{"symbol": "X"}]}))
    assert await _fetch_mexc_positions_for_slot(ex, 1) == [{"symbol": "X"}]


@pytest.mark.asyncio
async def test_fetch_empty_list_on_success_empty():
    ex = _exec(AsyncMock(return_value={"code": 0, "data": []}))
    assert await _fetch_mexc_positions_for_slot(ex, 1) == []


# ── integration: Case B behaviour under fetch-failure vs confirmed-empty ──
def _pos(slot=1, sym="1000PEPEUSDT", elapsed=100.0):
    return SimpleNamespace(mode="live", is_open=True, is_closing=False,
                           symbol=sym, elapsed_sec=elapsed,
                           account_label=f"slot{slot}")


def _pool(get_open_positions):
    executor = _exec(get_open_positions)
    p = MagicMock()
    p._executors = {1: executor}
    p.active_executors = MagicMock(return_value={1: executor})
    p.get_executor = MagicMock(return_value=executor)
    p.get_safety = MagicMock(return_value=None)
    return p


def _engine(pool):
    return SimpleNamespace(
        _open_positions={"1000PEPEUSDT": [_pos()]},
        live_pool=pool, live_db=None,
        mark_position_closed_externally=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_case_b_does_NOT_teardown_on_fetch_failure():
    """THE BUG: a 510/timeout must NOT mark a live position closed-externally."""
    pool = _pool(AsyncMock(side_effect=RuntimeError("510 rate limit")))
    engine = _engine(pool)
    await reconcile_once(engine, pool, alerts=None)
    engine.mark_position_closed_externally.assert_not_awaited()


@pytest.mark.asyncio
async def test_case_b_still_marks_closed_on_confirmed_empty():
    """Behaviour preserved: a SUCCESSFUL empty read means the position really is
    gone -> still tear it down in the engine."""
    pool = _pool(AsyncMock(return_value={"code": 0, "data": []}))
    engine = _engine(pool)
    await reconcile_once(engine, pool, alerts=None)
    engine.mark_position_closed_externally.assert_awaited_once()
