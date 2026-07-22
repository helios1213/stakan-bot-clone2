"""
Tests for LiveExecutor.place_ioc_open — IOC limit entry path.

Critical invariants:
  - For PEPE (scale=1000), API receives RAW price (limit_scaled / 1000)
  - All-expired = success=False, error_msg='ioc_all_expired' (NO market fallback)
  - On fill, fill_price returned to caller is in SCALED domain
  - Pre-retry position check prevents duplicate opens after partial fill
  - We submit REAL IOC (type=3), not GTC LIMIT (type=1)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.execution.live_executor import (
    LiveExecutor,
    ORDER_TYPE_IOC_LIMIT,
    ORDER_TYPE_IOC,
    ORDER_TYPE_LIMIT,
    SIDE_OPEN_LONG,
    SIDE_OPEN_SHORT,
)


def test_real_ioc_constant_is_type_3():
    """MEXC contract API: type=3 means IOC. type=1 means GTC LIMIT.
    We were previously sending type=1 thinking it was IOC. Fixed now.
    """
    assert ORDER_TYPE_IOC == "3"
    assert ORDER_TYPE_LIMIT == "1"
    # Back-compat alias used throughout the bot now points to real IOC
    assert ORDER_TYPE_IOC_LIMIT == "3"
    assert ORDER_TYPE_IOC_LIMIT == ORDER_TYPE_IOC


def _make_ob(bid_price: float, ask_price: float, synced: bool = True):
    """Build a fake OrderBook with best_bid/best_ask returning Level-like objects."""
    ob = MagicMock()
    ob.is_synced = synced

    bid_lvl = MagicMock()
    bid_lvl.price = bid_price
    ask_lvl = MagicMock()
    ask_lvl.price = ask_price

    ob.best_bid = MagicMock(return_value=bid_lvl)
    ob.best_ask = MagicMock(return_value=ask_lvl)
    return ob


def _make_executor():
    """Build LiveExecutor with mocked client_pool."""
    client = AsyncMock()
    # Safe defaults for the fill-poll endpoints. Without these, a bare
    # AsyncMock returns AsyncMock children and `resp.get(...)` yields
    # never-awaited coroutines (RuntimeWarning). Individual tests override
    # get_open_positions when they need a specific fill.
    _empty = {"code": 0, "data": []}
    client.get_order_deals = AsyncMock(return_value=dict(_empty))
    client.get_open_positions = AsyncMock(return_value=dict(_empty))
    client.get_history_positions = AsyncMock(return_value=dict(_empty))
    pool = MagicMock()

    async def get(slot_id):
        return client

    pool.get = get
    return LiveExecutor(client_pool=pool, slot_id=1), client


@pytest.mark.asyncio
async def test_pepe_long_ioc_sends_raw_price_to_api():
    """
    PEPE scale=1000. OB has scaled prices (e.g. ask=0.004144).
    MEXC API must receive RAW price (0.000004144 raw) regardless of bot internals.

    Passive-entry mode (IOC_PASSIVE_TICK_OFFSET=0): LONG limit = best_ask
    exactly (at touch), no aggressive offset. raw = best_ask / scale.
    """
    executor, client = _make_executor()

    # Scaled OB: ask 0.004144 → raw 0.000004144
    ob = _make_ob(bid_price=0.004143, ask_price=0.004144)

    # Mock submit_order success + fill polling returns scaled fill price
    client.submit_order = AsyncMock(return_value={
        "code": 0,
        "data": {"orderId": "12345"},
    })
    # _poll_fill_price reads holdAvgPrice raw and multiplies by scale internally.
    # Mock the open_positions response so it returns RAW = 0.000004144.
    # (holdVol>0 required for fill confirmation since the realfill-A patch.)
    client.get_open_positions = AsyncMock(return_value={
        "code": 0,
        "data": [{"symbol": "PEPE_USDT", "holdAvgPrice": 0.000004144, "holdVol": 100}],
    })

    result = await executor.place_ioc_open(
        symbol="PEPE_USDT",
        direction="long",
        notional_usdt=1000.0,
        leverage=48,
        mexc_ob=ob,
    )

    assert result.success is True
    assert result.order_id == "12345"
    # fill_price returned to caller is SCALED (Binance-equivalent)
    assert abs(result.fill_price - 0.004144) < 1e-9, f"fill_price should be scaled, got {result.fill_price}"

    # Verify API call: order_type=IOC_LIMIT, price in RAW form
    call_kwargs = client.submit_order.await_args.kwargs
    assert call_kwargs["order_type"] == ORDER_TYPE_IOC_LIMIT
    assert call_kwargs["side"] == SIDE_OPEN_LONG
    # Passive at-touch: limit price = best_ask = 0.004144 (scaled).
    # raw = scaled / 1000 = 0.000004144.
    raw_price_sent = float(call_kwargs["price"])
    expected_raw = 0.004144 / 1000.0
    # Tolerance: 10-decimal formatting truncates around 5-6 significant figures.
    # For PEPE raw (~4e-6), ~1ppm of expected is plenty.
    assert abs(raw_price_sent - expected_raw) < expected_raw * 1e-4, \
        f"raw price sent {raw_price_sent} != expected {expected_raw}"


@pytest.mark.asyncio
async def test_zec_long_ioc_no_scale_change():
    """
    ZEC scale=1.0 (not in SYMBOL_SCALE_TO_BINANCE). Raw == scaled.
    """
    executor, client = _make_executor()

    ob = _make_ob(bid_price=432.0, ask_price=432.5)
    client.submit_order = AsyncMock(return_value={
        "code": 0, "data": {"orderId": "z1"},
    })
    client.get_open_positions = AsyncMock(return_value={
        # F-022 fix (test patch May 2026): after the realfill-A patch
        # (also May 2026), _poll_fill_price requires BOTH holdAvgPrice>0 AND
        # holdVol>0 to confirm a fill. Mocks need holdVol present.
        # Value is irrelevant to this test (only fill_price is asserted on).
        "code": 0, "data": [{"symbol": "ZEC_USDT", "holdAvgPrice": 432.5, "holdVol": 100}],
    })

    result = await executor.place_ioc_open(
        symbol="ZEC_USDT", direction="long",
        notional_usdt=250.0, leverage=10, mexc_ob=ob,
    )
    assert result.success is True
    # No scaling for ZEC: raw == scaled
    assert abs(result.fill_price - 432.5) < 1e-6


@pytest.mark.asyncio
async def test_short_uses_bid_at_touch():
    """SHORT in passive mode: limit price = best_bid (at touch).

    IOC_PASSIVE_TICK_OFFSET=0 → no offset; the legacy aggressive
    `best_bid × (1 - offset_bps/10000)` formula is gone (offset_bps is
    ignored in passive mode). ENA scale=1 → raw == scaled.
    """
    executor, client = _make_executor()

    ob = _make_ob(bid_price=100.0, ask_price=100.1)
    client.submit_order = AsyncMock(return_value={
        "code": 0, "data": {"orderId": "s1"},
    })
    client.get_open_positions = AsyncMock(return_value={
        "code": 0, "data": [{"symbol": "ENA_USDT", "holdAvgPrice": 100.0, "holdVol": 100}],
    })

    result = await executor.place_ioc_open(
        symbol="ENA_USDT", direction="short",
        notional_usdt=250.0, leverage=10, mexc_ob=ob,
    )
    assert result.success is True

    call_kwargs = client.submit_order.await_args.kwargs
    assert call_kwargs["side"] == SIDE_OPEN_SHORT
    # Passive at-touch: limit = best_bid = 100.0 (ENA scale=1 → raw == scaled).
    raw_price = float(call_kwargs["price"])
    assert abs(raw_price - 100.0) < 1e-6


@pytest.mark.asyncio
async def test_all_attempts_expired_returns_failure_no_market_fallback():
    """
    All N attempts expire (no fill). Must return success=False with
    error_msg='ioc_all_expired' (or similar). NO market order should be placed.
    """
    executor, client = _make_executor()
    ob = _make_ob(bid_price=0.004143, ask_price=0.004144)

    # API accepts each order but no fill ever observed
    client.submit_order = AsyncMock(return_value={
        "code": 0, "data": {"orderId": "ord_expired"},
    })
    # _poll_fill_price returns 0 (no holdAvgPrice)
    client.get_open_positions = AsyncMock(return_value={
        "code": 0, "data": [],  # no open positions
    })

    result = await executor.place_ioc_open(
        symbol="PEPE_USDT", direction="long",
        notional_usdt=1000.0, leverage=48, mexc_ob=ob,
        max_attempts=2,
        retry_delay_ms=1,  # speed up test
    )

    assert result.success is False
    assert "expired" in (result.error_msg or "").lower()
    # API was called max_attempts times (each attempt sends one IOC)
    assert client.submit_order.await_count == 2

    # CRITICAL: no order_type=MARKET was ever sent — only IOC
    for call in client.submit_order.await_args_list:
        assert call.kwargs["order_type"] == ORDER_TYPE_IOC_LIMIT, \
            f"Market fallback detected! All entries must be IOC. Got: {call.kwargs}"


@pytest.mark.asyncio
async def test_pre_retry_position_check_aborts_duplicate():
    """
    Attempt 1 partially fills (we don't observe it within poll timeout, so
    we think it expired). Before attempt 2, position check sees an open
    position and aborts retry — preventing 1.5x size.
    """
    executor, client = _make_executor()
    ob = _make_ob(bid_price=0.004143, ask_price=0.004144)

    client.submit_order = AsyncMock(return_value={
        "code": 0, "data": {"orderId": "p1"},
    })

    # Sequence of get_open_positions:
    #   call 1: during _poll_fill_price after attempt 1 → empty (no fill yet)
    #   ... (multiple polls during timeout)
    #   call N: pre-retry check before attempt 2 → has position!
    call_count = {"n": 0}

    async def open_positions_side_effect():
        call_count["n"] += 1
        # First few calls: no positions (poll_fill returns 0)
        if call_count["n"] <= 3:
            return {"code": 0, "data": []}
        # After: position appears (partial fill landed late)
        return {
            "code": 0,
            "data": [{"symbol": "PEPE_USDT", "holdVol": 100, "holdAvgPrice": 0.000004144}],
        }

    client.get_open_positions = AsyncMock(side_effect=open_positions_side_effect)

    result = await executor.place_ioc_open(
        symbol="PEPE_USDT", direction="long",
        notional_usdt=1000.0, leverage=48, mexc_ob=ob,
        max_attempts=3,
        retry_delay_ms=1,
    )

    # Only ONE submit_order call — attempt 2 was aborted by position check
    assert client.submit_order.await_count == 1, \
        f"Expected 1 submit (attempt 2 aborted), got {client.submit_order.await_count}"


@pytest.mark.asyncio
async def test_orderbook_not_synced_fails_fast():
    """If OB is not synced, return failure immediately — don't submit anything."""
    executor, client = _make_executor()
    ob = _make_ob(bid_price=0, ask_price=0, synced=False)

    client.submit_order = AsyncMock()
    result = await executor.place_ioc_open(
        symbol="PEPE_USDT", direction="long",
        notional_usdt=1000, leverage=48, mexc_ob=ob,
    )
    assert result.success is False
    assert "synced" in (result.error_msg or "").lower()
    assert client.submit_order.await_count == 0  # never called API


@pytest.mark.asyncio
async def test_passive_long_places_at_ask_touch():
    """
    PASSIVE MODE LONG: limit = best_ask (taker-or-cancel at touch).
    For ENA_USDT, ask=0.10376 → limit=0.10376.
    """
    executor, client = _make_executor()
    # Non-PEPE symbol: scale=1, OB prices are raw
    ob = _make_ob(bid_price=0.10374, ask_price=0.10376)

    client.submit_order = AsyncMock(return_value={
        "code": 0, "data": {"orderId": "p1"},
    })
    client.get_open_positions = AsyncMock(return_value={
        # F-022 fix: _poll_fill_price requires holdVol > 0 (realfill-A patch).
        "code": 0, "data": [{"symbol": "ENA_USDT", "holdAvgPrice": 0.10376, "holdVol": 100}],
    })

    result = await executor.place_ioc_open(
        symbol="ENA_USDT", direction="long",
        notional_usdt=250, leverage=50, mexc_ob=ob,
    )
    assert result.success is True

    call_kwargs = client.submit_order.await_args.kwargs
    assert call_kwargs["side"] == SIDE_OPEN_LONG
    raw_price = float(call_kwargs["price"])
    # ask 0.10376 - 0 tick = 0.10376
    assert abs(raw_price - 0.10376) < 1e-7


@pytest.mark.asyncio
async def test_passive_short_places_at_bid_touch():
    """
    PASSIVE MODE SHORT: limit = best_bid (taker-or-cancel at touch).
    For ENA, bid=0.10374 → limit=0.10374.
    """
    executor, client = _make_executor()
    ob = _make_ob(bid_price=0.10374, ask_price=0.10376)

    client.submit_order = AsyncMock(return_value={
        "code": 0, "data": {"orderId": "p2"},
    })
    client.get_open_positions = AsyncMock(return_value={
        # F-022 fix: _poll_fill_price requires holdVol > 0 (realfill-A patch).
        "code": 0, "data": [{"symbol": "ENA_USDT", "holdAvgPrice": 0.10374, "holdVol": 100}],
    })

    result = await executor.place_ioc_open(
        symbol="ENA_USDT", direction="short",
        notional_usdt=250, leverage=50, mexc_ob=ob,
    )
    assert result.success is True

    call_kwargs = client.submit_order.await_args.kwargs
    assert call_kwargs["side"] == SIDE_OPEN_SHORT
    raw_price = float(call_kwargs["price"])
    # bid 0.10374 + 0 tick = 0.10374
    assert abs(raw_price - 0.10374) < 1e-7
