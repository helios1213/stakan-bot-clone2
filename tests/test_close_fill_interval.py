"""
Tests for CLOSE_FILL_POLL_INTERVAL_SEC (close-fill-fast patch, May 2026).

Symmetric to test_fill_poll_interval.py but covers the exit-side poller
_poll_close_fill (which queries /position/list/history_positions after
close_all_positions to retrieve authoritative closeAvgPrice/realised).

Verifies:
  1. Default interval is 0.05s (down from hard-coded 0.25s).
  2. Env override (CLOSE_FILL_POLL_INTERVAL_SEC=0.10) takes effect.
  3. _poll_close_fill returns immediately on first poll if matching closed
     position is already in history.
  4. _poll_close_fill uses interval_sec between polls when match not found yet.
  5. Filter logic (state==3, updateTime >= close_after_ts_ms - 5000ms,
     positionId match) preserved across patch.
"""
import asyncio
import importlib

import pytest
from unittest.mock import AsyncMock


def _reload_le_clean(monkeypatch, **env_overrides):
    """Reload live_executor with controlled env (mirrors test_ioc_retry_config)."""
    for key in ("IOC_MAX_ATTEMPTS", "IOC_RETRY_DELAY_MS",
                "IOC_FILL_POLL_INTERVAL_SEC", "CLOSE_FILL_POLL_INTERVAL_SEC"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env_overrides.items():
        monkeypatch.setenv(key, val)
    import src.execution.live_executor as le
    importlib.reload(le)
    return le


def test_close_fill_default_interval_is_50ms(monkeypatch):
    """Module-load default should be 0.05s in the absence of env override."""
    le = _reload_le_clean(monkeypatch)
    try:
        assert abs(le.CLOSE_FILL_POLL_INTERVAL_SEC - 0.05) < 1e-9, (
            f"Expected default 0.05, got {le.CLOSE_FILL_POLL_INTERVAL_SEC}"
        )
    finally:
        importlib.reload(le)


def test_close_fill_env_override_takes_effect(monkeypatch):
    """CLOSE_FILL_POLL_INTERVAL_SEC=0.10 should yield 0.10 at module load."""
    le = _reload_le_clean(monkeypatch, CLOSE_FILL_POLL_INTERVAL_SEC="0.1")
    try:
        assert abs(le.CLOSE_FILL_POLL_INTERVAL_SEC - 0.1) < 1e-9
    finally:
        # Restore to default for other tests in this process.
        monkeypatch.delenv("CLOSE_FILL_POLL_INTERVAL_SEC", raising=False)
        importlib.reload(le)


def _matching_row(symbol="ZEC_USDT", position_id=12345, update_ts=2_000_000):
    """A history_positions row that satisfies all filters."""
    return {
        "symbol": symbol,
        "positionId": position_id,
        "state": 3,
        "updateTime": update_ts,
        "closeAvgPrice": 432.5,
        "openAvgPrice": 430.0,
        "realised": 2.5,
    }


@pytest.mark.asyncio
async def test_close_fill_first_poll_no_sleep_when_matched():
    """If history shows the closed position on poll #1, return without sleeping."""
    from src.execution.live_executor import _poll_close_fill

    client = AsyncMock()
    client.get_history_positions = AsyncMock(return_value={
        "code": 0,
        "data": [_matching_row()],
    })

    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def tracking_sleep(d):
        sleep_calls.append(d)
        await real_sleep(0)

    import src.execution.live_executor as le
    original_sleep = le.asyncio.sleep
    le.asyncio.sleep = tracking_sleep  # type: ignore[assignment]
    try:
        close_avg, realised, open_avg = await _poll_close_fill(
            client,
            symbol="ZEC_USDT",
            position_id=12345,
            close_after_ts_ms=1_999_000,  # row.updateTime > this - 5000
            timeout_sec=5.0,
            interval_sec=0.05,
        )
    finally:
        le.asyncio.sleep = original_sleep  # type: ignore[assignment]

    # ZEC scale=1: returned prices equal raw values.
    assert close_avg == 432.5
    assert open_avg == 430.0
    assert realised == 2.5
    assert sleep_calls == [], (
        f"match on poll #1 must not sleep, got {sleep_calls}"
    )


@pytest.mark.asyncio
async def test_close_fill_interval_sec_used_between_polls():
    """When poll #1 returns no match, the loop sleeps for exactly interval_sec."""
    from src.execution.live_executor import _poll_close_fill

    # First call: empty data (MEXC hasn't surfaced the closed position yet).
    # Second call: matching row appears.
    client = AsyncMock()
    client.get_history_positions = AsyncMock(side_effect=[
        {"code": 0, "data": []},
        {"code": 0, "data": [_matching_row()]},
    ])

    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def tracking_sleep(d):
        sleep_calls.append(d)
        await real_sleep(0)

    import src.execution.live_executor as le
    original_sleep = le.asyncio.sleep
    le.asyncio.sleep = tracking_sleep  # type: ignore[assignment]
    try:
        close_avg, realised, open_avg = await _poll_close_fill(
            client,
            symbol="ZEC_USDT",
            position_id=12345,
            close_after_ts_ms=1_999_000,
            timeout_sec=5.0,
            interval_sec=0.07,
        )
    finally:
        le.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert close_avg == 432.5
    # Exactly one sleep between poll #1 (empty) and poll #2 (match), at interval_sec.
    assert len(sleep_calls) == 1, (
        f"expected 1 sleep between 2 polls, got {len(sleep_calls)}: {sleep_calls}"
    )
    assert abs(sleep_calls[0] - 0.07) < 1e-9, (
        f"sleep should be exactly interval_sec=0.07, got {sleep_calls[0]}"
    )


@pytest.mark.asyncio
async def test_close_fill_skips_row_with_stale_update_time():
    """A row with updateTime older than (close_after_ts - 5000ms) must be skipped.

    Guards against regression of the 5s tolerance window for MEXC's
    second-rounded updateTime field (closefill-fix). Patch must not have
    altered this filter.
    """
    from src.execution.live_executor import _poll_close_fill

    # Row updateTime way before our close window — should be rejected.
    stale_row = _matching_row(update_ts=1_000_000)
    fresh_row = _matching_row(update_ts=2_000_000)

    client = AsyncMock()
    # Poll #1: only stale row → no match → loop continues
    # Poll #2: fresh row → match
    client.get_history_positions = AsyncMock(side_effect=[
        {"code": 0, "data": [stale_row]},
        {"code": 0, "data": [fresh_row]},
    ])

    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def tracking_sleep(d):
        sleep_calls.append(d)
        await real_sleep(0)

    import src.execution.live_executor as le
    original_sleep = le.asyncio.sleep
    le.asyncio.sleep = tracking_sleep  # type: ignore[assignment]
    try:
        close_avg, _, _ = await _poll_close_fill(
            client,
            symbol="ZEC_USDT",
            position_id=12345,
            close_after_ts_ms=1_999_000,  # stale row's 1_000_000 < 1_999_000 - 5000
            timeout_sec=5.0,
            interval_sec=0.05,
        )
    finally:
        le.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert close_avg == 432.5, "should have matched fresh row on poll #2"
    assert len(sleep_calls) == 1, (
        "stale row should cause exactly one sleep before fresh row found"
    )


@pytest.mark.asyncio
async def test_close_fill_skips_row_with_wrong_position_id():
    """When position_id filter is provided, rows with different positionId skipped."""
    from src.execution.live_executor import _poll_close_fill

    wrong_pid_row = _matching_row(position_id=99999)
    right_pid_row = _matching_row(position_id=12345)

    client = AsyncMock()
    client.get_history_positions = AsyncMock(side_effect=[
        {"code": 0, "data": [wrong_pid_row]},
        {"code": 0, "data": [wrong_pid_row, right_pid_row]},
    ])

    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def tracking_sleep(d):
        sleep_calls.append(d)
        await real_sleep(0)

    import src.execution.live_executor as le
    original_sleep = le.asyncio.sleep
    le.asyncio.sleep = tracking_sleep  # type: ignore[assignment]
    try:
        close_avg, _, _ = await _poll_close_fill(
            client,
            symbol="ZEC_USDT",
            position_id=12345,
            close_after_ts_ms=1_999_000,
            timeout_sec=5.0,
            interval_sec=0.05,
        )
    finally:
        le.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert close_avg == 432.5, "should have matched the row with correct positionId"
    assert len(sleep_calls) == 1, "exactly one sleep between the two poll calls"
