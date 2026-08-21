"""Tests for Stage 16 — env-configurable IOC retry behaviour.

Background:
  Stage 15 cut Patch C from live trades, dropping signal_to_pickup_ms 344→177.
  Post-Stage 15 SQL on 73 live PENGU trades revealed retry pathology:
    - 20% filled on attempt 1 (good signal entries)
    - 80% needed retries → median total latency 5+ seconds
    - Max observed 13386ms (3 retries × 1500ms backoff)

  Static-gap signals have a half-life of 200-800ms; retries enter on stale
  market state. Stage 16 ships new defaults (max=1, delay=300ms) and exposes
  both knobs as env vars so the user can A/B test from `.env` without rebuild.

Tests verify:
  1. New defaults take effect when env vars unset.
  2. Env vars override the defaults at module load time.
  3. Malformed env values fall back to the default (not crash).
  4. With max_attempts=1, place_ioc_open performs no retry sleep on expiry.
"""
from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.live_executor import (
    LiveExecutor,
)


def _reload_le_with_env(monkeypatch, **env_overrides):
    """Reload live_executor module with controlled env state.

    Removes the IOC_* env vars (so module-level defaults take effect),
    applies any provided overrides, reloads, and returns the reloaded
    module so the caller can read its constants.

    Without this, tests that import IOC_DEFAULT_* at the top of the file
    read whatever was in the user's actual .env at module load — and the
    tests below claim to test "defaults" but were really testing the
    deployer's .env. .env=0 broke test_new_defaults_retry_delay_is_300ms;
    .env=1 silently let test_new_defaults_max_attempts_is_one pass for
    the wrong reason.
    """
    for key in ("IOC_MAX_ATTEMPTS", "IOC_RETRY_DELAY_MS",
                "IOC_FILL_POLL_INTERVAL_SEC"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env_overrides.items():
        monkeypatch.setenv(key, val)
    import src.execution.live_executor as le
    importlib.reload(le)
    return le


# ──────────────────────────────────────────────────────────────────────
# 1. Default values (env unset) — verified with env isolation + reload
# ──────────────────────────────────────────────────────────────────────

def test_new_defaults_max_attempts_is_one(monkeypatch):
    """Stage 16 default: 1 attempt, no retry. Verified with env unset."""
    le = _reload_le_with_env(monkeypatch)
    try:
        assert le.IOC_DEFAULT_MAX_ATTEMPTS == 1, (
            f"Expected default IOC_MAX_ATTEMPTS=1, got {le.IOC_DEFAULT_MAX_ATTEMPTS}"
        )
    finally:
        # Restore module state from the actual deployer env so other tests
        # in the file (which import from module top) see consistent values.
        importlib.reload(le)


def test_new_defaults_retry_delay_is_zero(monkeypatch):
    """Built-in default: 0ms retry delay — retry sleeps are disabled to match
    the max_attempts=1 default (no retry). Was 300ms; the env-override path was
    removed, so this is now the hardcoded module default.
    """
    le = _reload_le_with_env(monkeypatch)
    try:
        assert le.IOC_DEFAULT_RETRY_DELAY_MS == 0, (
            f"Expected default IOC_RETRY_DELAY_MS=0, got {le.IOC_DEFAULT_RETRY_DELAY_MS}"
        )
    finally:
        importlib.reload(le)


# NOTE: the _read_int_env helper + its 7 tests were removed — IOC retry config
# is no longer env-driven (it's per-pair: pair YAML execution.ioc_max_attempts /
# ioc_attempt_interval_ms via ConfigLoader).


# ──────────────────────────────────────────────────────────────────────
# 3. place_ioc_open with max_attempts=1 does NOT sleep on expiry
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_max_attempts_one_skips_retry_sleep_on_expiry(monkeypatch):
    """With max_attempts=1, an expired IOC must NOT trigger a retry sleep.

    This is the central behavioural guarantee of Stage 16: when a passive IOC
    fails to fill (most common case at 80%), we exit immediately rather than
    sleeping 1500ms and retrying on stale market state.
    """
    sleep_calls = []

    async def fake_sleep(d):
        sleep_calls.append(d)

    monkeypatch.setattr(
        "src.execution.live_executor.asyncio.sleep", fake_sleep
    )

    # Build a minimal LiveExecutor without invoking __init__.
    executor = LiveExecutor.__new__(LiveExecutor)
    executor.client_pool = MagicMock()
    executor.slot_id = 1
    executor.opens_attempted = 0
    executor.opens_succeeded = 0
    executor.opens_failed = 0
    executor.last_error = None
    executor.slot_level_error = None
    executor.alerts = None
    executor.webkey_store = None
    executor.order_timeout_sec = 15.0
    # Phantom-gate state (2026-08-21). __new__ skips __init__, so anything
    # added there must be mirrored here or every open raises AttributeError.
    executor._phantom_unknown = set()
    executor._phantom_tasks = set()

    # Mock client: submit accepts (code=0, orderId), but poll never finds fill.
    client = MagicMock()
    client.submit_order = AsyncMock(return_value={
        "code": 0, "data": {"orderId": "p_test"},
    })
    client.get_open_positions = AsyncMock(return_value={
        "code": 0, "data": [],  # no positions → poll keeps spinning, eventually expires
    })
    executor.client_pool.get = AsyncMock(return_value=client)

    # Synthetic orderbook with best_bid/best_ask
    mexc_ob = MagicMock()
    mexc_ob.is_synced = True
    best_bid = MagicMock()
    best_bid.price = 0.10000
    best_ask = MagicMock()
    best_ask.price = 0.10001
    mexc_ob.best_bid = MagicMock(return_value=best_bid)
    mexc_ob.best_ask = MagicMock(return_value=best_ask)

    result = await executor.place_ioc_open(
        symbol="PENGU_USDT",
        direction="long",
        notional_usdt=250.0,
        leverage=50,
        mexc_ob=mexc_ob,
        max_attempts=1,
        retry_delay_ms=300,
    )

    # Result: did not fill (poll returned no positions).
    assert not result.success, "Expected ioc_expired_no_fill, got success"

    # Critical: no retry sleep happened. The only sleeps allowed are inside
    # _poll_fill_price (0.2s polling cadence). Retry sleep would be 0.3s exactly.
    retry_sleeps = [d for d in sleep_calls if abs(d - 0.3) < 0.001]
    assert retry_sleeps == [], (
        f"max_attempts=1 should never sleep retry_delay; got {retry_sleeps}"
    )


@pytest.mark.asyncio
async def test_max_attempts_two_does_sleep_retry_once_on_expiry(monkeypatch):
    """Sanity: max_attempts=2 still triggers one retry sleep on expiry.

    Confirms that the retry mechanism is intact when user opts in via env.
    """
    sleep_calls = []

    async def fake_sleep(d):
        sleep_calls.append(d)

    monkeypatch.setattr(
        "src.execution.live_executor.asyncio.sleep", fake_sleep
    )

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.client_pool = MagicMock()
    executor.slot_id = 1
    executor.opens_attempted = 0
    executor.opens_succeeded = 0
    executor.opens_failed = 0
    executor.last_error = None
    executor.slot_level_error = None
    executor.alerts = None
    executor.webkey_store = None
    executor.order_timeout_sec = 15.0
    # Phantom-gate state (2026-08-21). __new__ skips __init__, so anything
    # added there must be mirrored here or every open raises AttributeError.
    executor._phantom_unknown = set()
    executor._phantom_tasks = set()

    client = MagicMock()
    client.submit_order = AsyncMock(return_value={
        "code": 0, "data": {"orderId": "p_test"},
    })
    client.get_open_positions = AsyncMock(return_value={
        "code": 0, "data": [],
    })
    executor.client_pool.get = AsyncMock(return_value=client)

    mexc_ob = MagicMock()
    mexc_ob.is_synced = True
    best_bid = MagicMock()
    best_bid.price = 0.10000
    best_ask = MagicMock()
    best_ask.price = 0.10001
    mexc_ob.best_bid = MagicMock(return_value=best_bid)
    mexc_ob.best_ask = MagicMock(return_value=best_ask)

    result = await executor.place_ioc_open(
        symbol="PENGU_USDT",
        direction="long",
        notional_usdt=250.0,
        leverage=50,
        mexc_ob=mexc_ob,
        max_attempts=2,
        retry_delay_ms=300,
    )

    assert not result.success

    # With max_attempts=2 we expect exactly ONE 0.3s retry sleep between
    # the two attempts.
    retry_sleeps = [d for d in sleep_calls if abs(d - 0.3) < 0.001]
    assert len(retry_sleeps) == 1, (
        f"max_attempts=2 should sleep retry_delay once; got {retry_sleeps}"
    )
