"""
Tests for IOC_FILL_POLL_INTERVAL_SEC (fill-poll-fast patch, May 2026).

Verifies:
  1. Default interval is 0.05s (down from hard-coded 0.2s).
  2. _poll_fill_price honours the interval_sec parameter.
  3. _poll_fill_price returns immediately on first poll if fill is present
     (no sleep before first request).
  4. _poll_fill_price uses interval_sec between polls when no fill yet.
"""
import asyncio
import importlib

import pytest
from unittest.mock import AsyncMock


def test_default_interval_is_50ms():
    """Module-load default should be 0.05s in the absence of env override."""
    import src.execution.live_executor as le
    # Re-import to ensure we read current module state (don't rely on stale cache).
    importlib.reload(le)
    assert abs(le.IOC_FILL_POLL_INTERVAL_SEC - 0.05) < 1e-9, (
        f"Expected default 0.05, got {le.IOC_FILL_POLL_INTERVAL_SEC}"
    )


def test_env_override_takes_effect(monkeypatch):
    """IOC_FILL_POLL_INTERVAL_SEC=0.1 should yield 0.1 at module load."""
    monkeypatch.setenv("IOC_FILL_POLL_INTERVAL_SEC", "0.1")
    import src.execution.live_executor as le
    importlib.reload(le)
    try:
        assert abs(le.IOC_FILL_POLL_INTERVAL_SEC - 0.1) < 1e-9
    finally:
        # Restore default for other tests in this process.
        monkeypatch.delenv("IOC_FILL_POLL_INTERVAL_SEC", raising=False)
        importlib.reload(le)


@pytest.mark.asyncio
async def test_first_poll_no_sleep_when_filled_immediately():
    """If MEXC reports the fill on poll #1, return without sleeping."""
    from src.execution.live_executor import _poll_fill_price

    client = AsyncMock()
    client.get_open_positions = AsyncMock(return_value={
        "code": 0,
        "data": [{
            "symbol": "ZEC_USDT",
            "holdAvgPrice": 432.5,
            "holdVol": 10,
        }],
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
        fill_price, hold_vol = await _poll_fill_price(
            client, "ZEC_USDT", timeout_sec=1.0, interval_sec=0.05,
        )
    finally:
        le.asyncio.sleep = original_sleep  # type: ignore[assignment]

    # Fill found on first poll → no sleep inside the loop.
    assert fill_price > 0, "should have found fill"
    assert hold_vol == 10
    assert sleep_calls == [], (
        f"first-poll fill must not sleep, got {sleep_calls}"
    )


@pytest.mark.asyncio
async def test_interval_sec_used_between_polls():
    """When poll #1 returns no fill, the loop sleeps for exactly interval_sec.

    fast-poll (May 2026) note: _poll_fill_price now uses an adaptive first
    sleep (interval_first_sec). To test that the standard interval is what's
    used between non-first polls — and not the first-poll value — we pin
    interval_first_sec to the same value as interval_sec here. The dedicated
    adaptive-interval test is in test_fast_poll_deal_details.py.
    """
    from src.execution.live_executor import _poll_fill_price

    # First call returns empty (no fill), second returns the fill.
    client = AsyncMock()
    client.get_open_positions = AsyncMock(side_effect=[
        {"code": 0, "data": []},
        {"code": 0, "data": [{
            "symbol": "ZEC_USDT",
            "holdAvgPrice": 432.5,
            "holdVol": 10,
        }]},
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
        fill_price, hold_vol = await _poll_fill_price(
            client, "ZEC_USDT",
            timeout_sec=2.0,
            interval_sec=0.07,
            interval_first_sec=0.07,
        )
    finally:
        le.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert fill_price > 0
    assert hold_vol == 10
    # Exactly one sleep between poll #1 (empty) and poll #2 (fill), at interval_sec.
    assert len(sleep_calls) == 1, (
        f"expected 1 sleep between 2 polls, got {len(sleep_calls)}: {sleep_calls}"
    )
    assert abs(sleep_calls[0] - 0.07) < 1e-9, (
        f"sleep should be exactly interval_sec=0.07, got {sleep_calls[0]}"
    )
