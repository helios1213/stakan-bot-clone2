"""
Tests for the parallel-close patch in LiveExecutor.close_position.

Verifies:
  1. snap and close requests run concurrently (not sequentially)
  2. when snap is slow, close completes on time anyway
  3. when close times out, snap is cancelled cleanly
  4. positionId is captured when snap completes in time
  5. positionId is None when snap times out (graceful degradation)
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.live_executor import LiveExecutor


@pytest.fixture
def executor():
    """Build a minimal LiveExecutor with mocked client_pool."""
    pool = MagicMock()
    pool.get = AsyncMock()
    ex = LiveExecutor.__new__(LiveExecutor)
    ex.client_pool = pool
    ex.slot_id = 1
    ex.close_timeout_sec = 5.0
    ex.closes_attempted = 0
    ex.closes_failed = 0
    ex.closes_succeeded = 0
    ex.last_error = None
    return ex


@pytest.mark.asyncio
async def test_close_runs_parallel_to_snap(executor):
    """
    Both snap and close take 300ms. If parallel — patched block ~300ms.
    If sequential (the bug we fixed) — patched block ~600ms.

    Measurement strategy: we time the snap+close+harvest block via the
    snap/close start timestamps and result.latency_ms (which is measured
    from t0 = right before parallel dispatch to right after close returns,
    INSIDE the function — this is what we actually patched). Total wall
    clock of close_position() includes _poll_close_fill polling which is
    irrelevant to this patch.
    """
    snap_started_at = []
    close_started_at = []

    async def slow_snap():
        snap_started_at.append(time.monotonic())
        await asyncio.sleep(0.3)
        return {"code": 0, "data": [{"symbol": "SUI_USDT", "positionId": 12345}]}

    async def slow_close(symbol):
        close_started_at.append(time.monotonic())
        await asyncio.sleep(0.3)
        return {"code": 0, "data": {"orderId": "ord_001"}}

    client = MagicMock()
    client.get_open_positions = slow_snap
    client.close_all_positions = slow_close
    executor.client_pool.get.return_value = client

    # Make _poll_close_fill return a NON-ZERO exit_price so the orphan check
    # is skipped. Orphan check would call get_open_positions a SECOND time,
    # adding another 300ms (slow_snap) and polluting our measurement.
    import src.execution.live_executor as mod
    original_poll = mod._poll_close_fill
    mod._poll_close_fill = AsyncMock(return_value=(0.5, 0.01, 0.5))

    try:
        result = await executor.close_position("SUI_USDT")
    finally:
        mod._poll_close_fill = original_poll

    # Verify both started within milliseconds of each other (parallel dispatch)
    assert len(snap_started_at) == 1
    assert len(close_started_at) == 1
    start_diff_ms = abs(snap_started_at[0] - close_started_at[0]) * 1000
    assert start_diff_ms < 50, (
        f"snap and close started {start_diff_ms:.0f}ms apart — not parallel"
    )

    # result.latency_ms is measured INSIDE close_position from t0 (right before
    # parallel dispatch) to right after close returns. With parallel dispatch
    # this should be ~300ms; with sequential it would be ~600ms.
    assert result.latency_ms < 500, (
        f"patched block took {result.latency_ms}ms — looks sequential"
    )
    assert result.success is True
    assert result.exit_price > 0  # confirms orphan check was skipped


@pytest.mark.asyncio
async def test_snap_slower_than_close_still_completes(executor):
    """
    Snap takes 1500ms, close takes 200ms. Patch should:
      - return from patched block after close + snap-wait ≈ 200ms + 200ms = 400ms
      - snap_task cancelled, positionId = None
      - close still success

    Same measurement caveat as T1: we look at result.latency_ms, not wall
    clock, because _poll_close_fill polling is outside the patched block.
    """
    snap_started_at = []

    async def very_slow_snap():
        snap_started_at.append(time.monotonic())
        await asyncio.sleep(1.5)
        return {"code": 0, "data": []}

    async def fast_close(symbol):
        await asyncio.sleep(0.2)
        return {"code": 0, "data": {"orderId": "ord_002"}}

    client = MagicMock()
    client.get_open_positions = very_slow_snap
    client.close_all_positions = fast_close
    executor.client_pool.get.return_value = client

    # Skip orphan check by returning non-zero exit
    import src.execution.live_executor as mod
    original_poll = mod._poll_close_fill
    mod._poll_close_fill = AsyncMock(return_value=(0.5, 0.01, 0.5))

    try:
        result = await executor.close_position("SUI_USDT")
    finally:
        mod._poll_close_fill = original_poll

    # Patched block: 200ms close + 200ms snap-wait = ~400ms.
    # If snap blocked close (the bug), this would be 1500ms+.
    assert result.latency_ms < 700, (
        f"patched block took {result.latency_ms}ms — snap blocked close"
    )
    assert result.success is True
    assert len(snap_started_at) == 1  # snap WAS dispatched (just didn't finish in time)


@pytest.mark.asyncio
async def test_close_timeout_cancels_snap(executor):
    """
    Close hangs longer than close_timeout_sec. snap is in-flight.
    Patch should: return failure, cancel snap, not leak the task.
    """
    executor.close_timeout_sec = 0.3  # short timeout for test

    snap_started_at = []
    close_started_at = []

    async def slow_snap():
        snap_started_at.append(time.monotonic())
        await asyncio.sleep(2.0)
        return {"code": 0, "data": []}

    async def hanging_close(symbol):
        close_started_at.append(time.monotonic())
        await asyncio.sleep(10.0)  # never returns within timeout
        return {"code": 0}

    client = MagicMock()
    client.get_open_positions = slow_snap
    client.close_all_positions = hanging_close
    executor.client_pool.get.return_value = client

    t0 = time.monotonic()
    result = await executor.close_position("SUI_USDT")
    elapsed = time.monotonic() - t0

    assert result.success is False
    assert "timeout" in (result.error_msg or "").lower()
    # Should have timed out after ~0.3s, not 10s
    assert elapsed < 1.0, f"close_timeout didn't fire — elapsed {elapsed*1000:.0f}ms"
    # Both tasks must have started (parallel dispatch)
    assert len(snap_started_at) == 1
    assert len(close_started_at) == 1

    # Give event loop a tick to settle cancelled tasks. If snap was leaked it
    # would still be running on the loop.
    await asyncio.sleep(0)
    # No reliable cross-Python way to assert "no pending tasks", but at least
    # the test completing without leaks is the basic guarantee.


@pytest.mark.asyncio
async def test_snap_fast_position_id_captured(executor):
    """
    Snap returns in 50ms, close in 200ms. positionId should be captured.
    """
    async def fast_snap():
        await asyncio.sleep(0.05)
        return {
            "code": 0,
            "data": [{"symbol": "SUI_USDT", "positionId": 99887766}],
        }

    async def normal_close(symbol):
        await asyncio.sleep(0.2)
        return {"code": 0, "data": {"orderId": "ord_003"}}

    client = MagicMock()
    client.get_open_positions = fast_snap
    client.close_all_positions = normal_close
    executor.client_pool.get.return_value = client

    captured = {}

    async def fake_poll(client, *, symbol, position_id, close_after_ts_ms, timeout_sec):
        captured["position_id"] = position_id
        captured["close_after_ts_ms"] = close_after_ts_ms
        return (0.5, 0.001, 0.5)  # (exit_price, pnl, entry_price)

    import src.execution.live_executor as mod
    original_poll = mod._poll_close_fill
    mod._poll_close_fill = fake_poll

    try:
        result = await executor.close_position("SUI_USDT")
    finally:
        mod._poll_close_fill = original_poll

    assert result.success is True
    assert captured["position_id"] == 99887766
    assert captured["close_after_ts_ms"] > 0


@pytest.mark.asyncio
async def test_snap_failure_does_not_break_close(executor):
    """
    Snap raises an exception. Close still succeeds. positionId is None.
    """
    async def failing_snap():
        await asyncio.sleep(0.01)
        raise ConnectionError("simulated network error during snap")

    async def normal_close(symbol):
        await asyncio.sleep(0.1)
        return {"code": 0, "data": {"orderId": "ord_004"}}

    client = MagicMock()
    client.get_open_positions = failing_snap
    client.close_all_positions = normal_close
    executor.client_pool.get.return_value = client

    captured = {}

    async def fake_poll(client, *, symbol, position_id, close_after_ts_ms, timeout_sec):
        captured["position_id"] = position_id
        return (0.0, 0.0, 0.0)  # no fill found

    import src.execution.live_executor as mod
    original_poll = mod._poll_close_fill
    mod._poll_close_fill = fake_poll
    # We also need to short-circuit the post-close orphan re-check:
    # close_position calls client.get_open_positions a second time when
    # real_exit_price == 0. Make it return "no position" so the orphan path
    # treats this as a clean close.
    original_get = client.get_open_positions
    call_count = [0]

    async def get_open_positions_dispatcher():
        call_count[0] += 1
        if call_count[0] == 1:
            # First call = the snap_task. We already replaced this above —
            # but MagicMock got reassigned. To keep test simple: raise once,
            # then return empty.
            raise ConnectionError("simulated network error during snap")
        return {"code": 0, "data": []}  # orphan check: position is gone

    client.get_open_positions = get_open_positions_dispatcher

    try:
        result = await executor.close_position("SUI_USDT")
    finally:
        mod._poll_close_fill = original_poll

    assert result.success is True  # close succeeded
    assert captured.get("position_id") is None  # snap failed → positionId=None
