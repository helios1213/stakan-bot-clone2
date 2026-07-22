"""Tests for live-executor patches:
  F-008 — market_close_position must fail-CLOSED on verification exception
  F-007 — IOC pre-retry safety branch must poll fill and return success
          (not fall through to all-expired failure)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.live_executor import (
    LiveExecutor, SIDE_CLOSE_LONG, SIDE_CLOSE_SHORT,
)


@pytest.mark.asyncio
async def test_market_close_uses_exchange_side_not_caller():
    """The 2026-07-19 PEPE orphan: caller believed SHORT but the exchange
    position was LONG, so a 'close short' hit MEXC api_error_2009 and orphaned.
    The close must target the ACTUAL exchange side (CLOSE_LONG) and use the
    exchange holdVol, not the caller's (inflated) qty."""
    client = AsyncMock()
    snapshot_resp = {"code": 0, "data": [{
        "symbol": "PEPE_USDT", "positionId": 999,
        "holdVol": 20, "positionType": 1,   # 1 = LONG on the exchange
    }]}
    submit_resp = {"code": 0, "data": {"orderId": "1"}}
    call_log = []

    async def gop():
        call_log.append("c")
        # 1st = pre-close snapshot (LONG present); later = verify (gone)
        return snapshot_resp if len(call_log) == 1 else {"code": 0, "data": []}

    client.get_open_positions = AsyncMock(side_effect=gop)
    client.submit_order = AsyncMock(return_value=submit_resp)
    client.get_history_positions = AsyncMock(return_value={"code": 0, "data": []})
    client_pool = AsyncMock()
    client_pool.get = AsyncMock(return_value=client)

    ex = LiveExecutor(client_pool=client_pool, slot_id=1, close_timeout_sec=5.0)
    result = await ex.market_close_position(
        symbol="PEPE_USDT", direction="short",   # WRONG belief
        qty_contracts=200000, leverage=66,        # inflated (base units)
    )
    assert client.submit_order.await_count == 1
    kw = client.submit_order.await_args.kwargs
    assert kw["side"] == SIDE_CLOSE_LONG, "must close the real LONG, not caller's SHORT"
    assert kw["vol"] == 20, "must use exchange holdVol, not caller's inflated qty"
    assert result.success is True


@pytest.mark.asyncio
async def test_market_close_skips_order_when_already_flat():
    """Snapshot shows the symbol absent → already closed → NO order submitted,
    success returned (prevents the false ORPHAN alert)."""
    client = AsyncMock()
    client.get_open_positions = AsyncMock(return_value={"code": 0, "data": []})
    client.submit_order = AsyncMock(return_value={"code": 0, "data": {}})
    client.get_history_positions = AsyncMock(return_value={"code": 0, "data": []})
    client_pool = AsyncMock()
    client_pool.get = AsyncMock(return_value=client)

    ex = LiveExecutor(client_pool=client_pool, slot_id=1, close_timeout_sec=5.0)
    result = await ex.market_close_position(
        symbol="PEPE_USDT", direction="short", qty_contracts=20, leverage=66,
    )
    assert client.submit_order.await_count == 0, "must not submit when already flat"
    assert result.success is True


# ────────────────────────────────────────────────────────────────────────
# F-008 — market_close fail-CLOSED on verify exception
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_market_close_fails_when_verify_raises():
    """If post-close verification raises (timeout, network), the close
    must be reported as FAILED so the caller can alert and reconcile.
    Before the fix, `still_open=False` initial → exception silently
    treated as "closed", losing the orphan."""

    client = AsyncMock()
    # Pre-close snapshot: there's a position (we need its qty for the order)
    snapshot_resp = {
        "code": 0,
        "data": [{
            "symbol": "SUI_USDT", "positionId": 999,
            "holdVol": 10,  # match qty_contracts caller passes
        }],
    }
    # Order submit: MEXC says OK
    submit_resp = {"code": 0, "data": {"orderId": "12345"}}
    # _poll_close_fill: returns 0,0,0 (no row yet)
    # get_open_positions call #2 (the verify): raises
    call_log = []

    async def get_open_positions_side_effect():
        call_log.append("get_open_positions")
        if len(call_log) == 1:
            return snapshot_resp
        # On verify call: raise
        raise TimeoutError("simulated MEXC timeout")

    client.get_open_positions = AsyncMock(side_effect=get_open_positions_side_effect)
    client.submit_order = AsyncMock(return_value=submit_resp)
    # _poll_close_fill calls get_history_positions; make it return empty
    client.get_history_positions = AsyncMock(
        return_value={"code": 0, "data": []}
    )

    client_pool = AsyncMock()
    client_pool.get = AsyncMock(return_value=client)

    executor = LiveExecutor(client_pool=client_pool, slot_id=1, close_timeout_sec=5.0)

    result = await executor.market_close_position(
        symbol="SUI_USDT",
        direction="long",
        qty_contracts=10,
        leverage=50,
    )

    # F-008: verify raised, so we must NOT report success
    assert result.success is False, (
        "market_close_position should fail-CLOSED when verify raises"
    )
    assert "still_open" in (result.error_msg or "")


@pytest.mark.asyncio
async def test_market_close_succeeds_when_position_truly_gone():
    """Sanity: when /open_positions returns an empty list (position truly
    gone), market_close_position should report success."""

    client = AsyncMock()
    snapshot_resp = {
        "code": 0,
        "data": [{
            "symbol": "SUI_USDT", "positionId": 999,
            "holdVol": 10,
        }],
    }
    submit_resp = {"code": 0, "data": {"orderId": "12345"}}
    empty_resp = {"code": 0, "data": []}

    call_log = []

    async def get_open_positions_side_effect():
        call_log.append("call")
        # First call: snapshot (returns position)
        # Subsequent calls: verify (returns empty = closed)
        if len(call_log) == 1:
            return snapshot_resp
        return empty_resp

    client.get_open_positions = AsyncMock(side_effect=get_open_positions_side_effect)
    client.submit_order = AsyncMock(return_value=submit_resp)
    client.get_history_positions = AsyncMock(
        return_value={"code": 0, "data": []}  # no history row yet
    )

    client_pool = AsyncMock()
    client_pool.get = AsyncMock(return_value=client)

    executor = LiveExecutor(client_pool=client_pool, slot_id=1, close_timeout_sec=5.0)

    result = await executor.market_close_position(
        symbol="SUI_USDT", direction="long",
        qty_contracts=10, leverage=50,
    )

    assert result.success is True, (
        f"Position truly gone, but market_close reported failure: {result.error_msg}"
    )


@pytest.mark.asyncio
async def test_market_close_fails_when_position_still_listed():
    """If verify call succeeds but the position is STILL in the open list,
    report failure (existing behaviour preserved by patch)."""

    client = AsyncMock()
    snapshot_resp = {
        "code": 0,
        "data": [{
            "symbol": "SUI_USDT", "positionId": 999,
            "holdVol": 10,
        }],
    }
    submit_resp = {"code": 0, "data": {"orderId": "12345"}}

    call_log = []

    async def get_open_positions_side_effect():
        call_log.append("call")
        # All calls return the same: position present
        return snapshot_resp

    client.get_open_positions = AsyncMock(side_effect=get_open_positions_side_effect)
    client.submit_order = AsyncMock(return_value=submit_resp)
    client.get_history_positions = AsyncMock(
        return_value={"code": 0, "data": []}
    )

    client_pool = AsyncMock()
    client_pool.get = AsyncMock(return_value=client)

    executor = LiveExecutor(client_pool=client_pool, slot_id=1, close_timeout_sec=5.0)

    result = await executor.market_close_position(
        symbol="SUI_USDT", direction="long",
        qty_contracts=10, leverage=50,
    )

    assert result.success is False
    assert "still_open" in (result.error_msg or "")


# ────────────────────────────────────────────────────────────────────────
# F-007 — IOC pre-retry branch polls fill data and returns success
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ioc_pre_retry_returns_success_when_position_filled():
    """When IOC pre-retry check detects an already-open position (i.e. a
    prior attempt's order filled between the submit and the retry), the
    function MUST poll the fill data and return success — not break to
    the post-loop failure path that reports ioc_all_expired."""

    # Patch the function-level constants to enable retries.

    # Build a mock orderbook
    mexc_ob = MagicMock()
    mexc_ob.is_synced = True
    best_ask = MagicMock(price=2.0, size=100)
    best_bid = MagicMock(price=1.99, size=100)
    mexc_ob.best_ask = MagicMock(return_value=best_ask)
    mexc_ob.best_bid = MagicMock(return_value=best_bid)

    client = AsyncMock()
    # First attempt: submit returns OK but no fill confirmed; second
    # attempt's pre-retry check sees the position now exists.
    submit_resp = {"code": 0, "data": {"orderId": "111"}}
    # Position exists with avg price 2.0, vol 50
    position_present_resp = {
        "code": 0,
        "data": [{
            "symbol": "SUI_USDT",
            "holdAvgPrice": 2.0,  # raw price (will be * scale → scaled)
            "holdVol": 50,
        }],
    }
    # _poll_fill_price calls get_open_positions repeatedly. First few
    # before second attempt: empty. After: contains position.
    open_positions_calls = []

    async def get_open_positions_side():
        open_positions_calls.append(1)
        # All subsequent calls: position present
        return position_present_resp

    client.get_open_positions = AsyncMock(side_effect=get_open_positions_side)
    client.submit_order = AsyncMock(return_value=submit_resp)
    # _poll_fill_price tries get_order_deals first; give it an empty dict so
    # it falls through to get_open_positions (and so resp.get() isn't called
    # on an unawaited AsyncMock coroutine).
    client.get_order_deals = AsyncMock(return_value={"code": 0, "data": []})

    client_pool = AsyncMock()
    client_pool.get = AsyncMock(return_value=client)

    executor = LiveExecutor(
        client_pool=client_pool, slot_id=1,
        order_timeout_sec=5.0,
    )

    # Force max_attempts=2 to exercise the retry branch
    result = await executor.place_ioc_open(
        symbol="SUI_USDT",
        direction="long",
        notional_usdt=100.0,
        leverage=50,
        mexc_ob=mexc_ob,
        offset_ticks=1,
        max_attempts=2,
        retry_delay_ms=10,
    )

    # F-007: result must be success when pre-retry detects filled position
    assert result.success is True, (
        f"IOC pre-retry should return success when fill detected; got: "
        f"success={result.success} error={result.error_msg}"
    )
    assert result.fill_qty_contracts == 50
    assert result.fill_price > 0


# ────────────────────────────────────────────────────────────────────────
# Phantom-fill guard: believed-expired IOC that actually filled → flatten
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phantom_check_flattens_when_order_actually_filled():
    """A believed-expired IOC whose deal_details shows a real fill must be
    flattened (surgical close of exactly the filled qty on the close side)."""
    import src.execution.live_executor as le
    le.PHANTOM_FILL_CHECK_DELAY_SEC = 0.0  # no sleep in test

    client = AsyncMock()
    # deal_details: order actually filled 620 contracts
    client.get_order_deals = AsyncMock(return_value={"code": 0, "data": [{"vol": 620, "price": 0.0028735}]})
    client.submit_order = AsyncMock(return_value={"code": 0, "data": {"orderId": "close1"}})
    client_pool = AsyncMock(); client_pool.get = AsyncMock(return_value=client)

    ex = le.LiveExecutor(client_pool=client_pool, slot_id=2, close_timeout_sec=5.0)
    await ex._phantom_open_check("ord123", "PEPE_USDT", "short", 69)

    assert client.submit_order.await_count == 1, "must flatten the phantom fill"
    kw = client.submit_order.await_args.kwargs
    assert kw["side"] == le.SIDE_CLOSE_SHORT, "close a phantom SHORT with CLOSE_SHORT side"
    assert kw["vol"] == 620, "flatten exactly the filled qty"
    assert kw["order_type"] == "5", "market close"


@pytest.mark.asyncio
async def test_phantom_check_noop_when_genuinely_no_fill():
    """The normal case: deal_details empty (truly expired) → NO close order."""
    import src.execution.live_executor as le
    le.PHANTOM_FILL_CHECK_DELAY_SEC = 0.0

    client = AsyncMock()
    client.get_order_deals = AsyncMock(return_value={"code": 0, "data": []})
    client.submit_order = AsyncMock(return_value={"code": 0, "data": {}})
    client_pool = AsyncMock(); client_pool.get = AsyncMock(return_value=client)

    ex = le.LiveExecutor(client_pool=client_pool, slot_id=2, close_timeout_sec=5.0)
    await ex._phantom_open_check("ord123", "PEPE_USDT", "short", 69)

    assert client.submit_order.await_count == 0, "genuinely no fill → must NOT submit a close"


@pytest.mark.asyncio
async def test_phantom_check_noop_when_deals_unreadable():
    """deal_details returns a non-zero code (e.g. 401) → do NOT act (reconcile backstop)."""
    import src.execution.live_executor as le
    le.PHANTOM_FILL_CHECK_DELAY_SEC = 0.0

    client = AsyncMock()
    client.get_order_deals = AsyncMock(return_value={"code": 401, "message": "expired"})
    client.submit_order = AsyncMock()
    client_pool = AsyncMock(); client_pool.get = AsyncMock(return_value=client)

    ex = le.LiveExecutor(client_pool=client_pool, slot_id=2, close_timeout_sec=5.0)
    await ex._phantom_open_check("ord123", "PEPE_USDT", "short", 69)

    assert client.submit_order.await_count == 0, "unreadable deals → no blind close"
