"""
Tests for fast-poll patch (May 2026).

Verifies that _poll_fill_price:
  1. Default IOC_FILL_POLL_INTERVAL_FIRST_SEC is 0.025s.
  2. Env override for first-interval works.
  3. With order_id: primary read is get_order_deals (NOT get_open_positions).
  4. With order_id: aggregates multi-deal partials into weighted-avg price.
  5. With order_id: falls back to get_open_positions if deal_details empty.
  6. Without order_id: legacy behaviour (open_positions only).
  7. First sleep uses interval_first_sec; subsequent sleeps use interval_sec.
  8. Returns scaled price (raw × get_binance_scale(symbol)).
"""
import asyncio
import importlib

import pytest
from unittest.mock import AsyncMock


def test_default_first_interval_is_25ms():
    """Module-load default should be 0.025s."""
    import src.execution.live_executor as le
    importlib.reload(le)
    assert abs(le.IOC_FILL_POLL_INTERVAL_FIRST_SEC - 0.025) < 1e-9, (
        f"Expected default 0.025, got {le.IOC_FILL_POLL_INTERVAL_FIRST_SEC}"
    )


def test_first_interval_env_override(monkeypatch):
    """IOC_FILL_POLL_INTERVAL_FIRST_SEC=0.01 should yield 0.01."""
    monkeypatch.setenv("IOC_FILL_POLL_INTERVAL_FIRST_SEC", "0.01")
    import src.execution.live_executor as le
    importlib.reload(le)
    try:
        assert abs(le.IOC_FILL_POLL_INTERVAL_FIRST_SEC - 0.01) < 1e-9
    finally:
        monkeypatch.delenv("IOC_FILL_POLL_INTERVAL_FIRST_SEC", raising=False)
        importlib.reload(le)


@pytest.mark.asyncio
async def test_uses_deal_details_when_order_id_provided():
    """With order_id, deal_details is the primary read; open_positions is NOT called."""
    from src.execution.live_executor import _poll_fill_price

    client = AsyncMock()
    # deal_details returns a single-fill IOC
    client.get_order_deals = AsyncMock(return_value={
        "code": 0,
        "data": [{
            "orderId": "ORD1",
            "symbol": "ZEC_USDT",
            "price": 432.5,
            "vol": 10,
        }],
    })
    client.get_open_positions = AsyncMock(return_value={"code": 0, "data": []})

    fill_price, hold_vol = await _poll_fill_price(
        client, "ZEC_USDT", order_id="ORD1", timeout_sec=1.0,
    )

    assert fill_price > 0, "should have found fill via deal_details"
    assert hold_vol == 10
    # Critical: open_positions should NOT have been called when deal_details
    # returned a fill on poll #1.
    client.get_open_positions.assert_not_called()
    client.get_order_deals.assert_awaited_once_with("ORD1")


@pytest.mark.asyncio
async def test_aggregates_partial_fills():
    """deal_details with 2 partial deals → weighted-avg price by vol."""
    from src.execution.live_executor import _poll_fill_price
    from src.exchanges.mexc_rest import get_binance_scale

    client = AsyncMock()
    # 6 contracts @ 100.0 + 4 contracts @ 110.0 → vw avg = (600 + 440) / 10 = 104.0
    client.get_order_deals = AsyncMock(return_value={
        "code": 0,
        "data": [
            {"orderId": "ORD2", "symbol": "ZEC_USDT", "price": 100.0, "vol": 6},
            {"orderId": "ORD2", "symbol": "ZEC_USDT", "price": 110.0, "vol": 4},
        ],
    })

    fill_price, hold_vol = await _poll_fill_price(
        client, "ZEC_USDT", order_id="ORD2", timeout_sec=1.0,
    )

    scale = get_binance_scale("ZEC_USDT")
    expected = 104.0 * scale
    assert abs(fill_price - expected) < 1e-6, (
        f"Expected weighted-avg {expected}, got {fill_price}"
    )
    assert hold_vol == 10


@pytest.mark.asyncio
async def test_falls_back_to_open_positions_on_deal_details_empty():
    """If deal_details returns no fills, fall back to open_positions in same poll cycle."""
    from src.execution.live_executor import _poll_fill_price

    client = AsyncMock()
    # deal_details returns empty (trade table hasn't surfaced yet)
    client.get_order_deals = AsyncMock(return_value={"code": 0, "data": []})
    # open_positions has the fill (position aggregator beat the trade table — rare but possible)
    client.get_open_positions = AsyncMock(return_value={
        "code": 0,
        "data": [{
            "symbol": "ZEC_USDT",
            "holdAvgPrice": 432.5,
            "holdVol": 10,
        }],
    })

    fill_price, hold_vol = await _poll_fill_price(
        client, "ZEC_USDT", order_id="ORD3", timeout_sec=1.0,
    )

    assert fill_price > 0, "fallback to open_positions should have found fill"
    assert hold_vol == 10
    client.get_order_deals.assert_awaited()
    client.get_open_positions.assert_awaited()


@pytest.mark.asyncio
async def test_falls_back_to_open_positions_on_deal_details_exception():
    """If deal_details raises (network blip), fall back gracefully."""
    from src.execution.live_executor import _poll_fill_price

    client = AsyncMock()
    client.get_order_deals = AsyncMock(side_effect=RuntimeError("temporary 5xx"))
    client.get_open_positions = AsyncMock(return_value={
        "code": 0,
        "data": [{
            "symbol": "ZEC_USDT",
            "holdAvgPrice": 432.5,
            "holdVol": 10,
        }],
    })

    fill_price, hold_vol = await _poll_fill_price(
        client, "ZEC_USDT", order_id="ORD4", timeout_sec=1.0,
    )

    assert fill_price > 0, "fallback should kick in on exception"
    assert hold_vol == 10


@pytest.mark.asyncio
async def test_legacy_path_when_no_order_id():
    """order_id=None → legacy open_positions-only path; deal_details NOT called."""
    from src.execution.live_executor import _poll_fill_price

    client = AsyncMock()
    client.get_order_deals = AsyncMock(return_value={"code": 0, "data": []})
    client.get_open_positions = AsyncMock(return_value={
        "code": 0,
        "data": [{
            "symbol": "ZEC_USDT",
            "holdAvgPrice": 432.5,
            "holdVol": 10,
        }],
    })

    fill_price, hold_vol = await _poll_fill_price(
        client, "ZEC_USDT", order_id=None, timeout_sec=1.0,
    )

    assert fill_price > 0
    assert hold_vol == 10
    # Critical: with order_id=None we must NOT touch deal_details.
    client.get_order_deals.assert_not_called()


@pytest.mark.asyncio
async def test_adaptive_interval_first_then_steady():
    """First sleep = interval_first_sec; subsequent sleeps = interval_sec."""
    from src.execution.live_executor import _poll_fill_price

    client = AsyncMock()
    # First two polls find nothing, third finds the fill.
    client.get_order_deals = AsyncMock(side_effect=[
        {"code": 0, "data": []},
        {"code": 0, "data": []},
        {"code": 0, "data": [{
            "orderId": "ORD5", "symbol": "ZEC_USDT", "price": 432.5, "vol": 10,
        }]},
    ])
    client.get_open_positions = AsyncMock(return_value={"code": 0, "data": []})

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
            order_id="ORD5",
            timeout_sec=2.0,
            interval_sec=0.05,
            interval_first_sec=0.025,
        )
    finally:
        le.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert fill_price > 0
    assert hold_vol == 10
    # Two sleeps between three polls.
    assert len(sleep_calls) == 2, (
        f"expected 2 sleeps between 3 polls, got {len(sleep_calls)}: {sleep_calls}"
    )
    # First sleep = adaptive 25ms, second = standard 50ms.
    assert abs(sleep_calls[0] - 0.025) < 1e-9, (
        f"first sleep should be interval_first_sec=0.025, got {sleep_calls[0]}"
    )
    assert abs(sleep_calls[1] - 0.05) < 1e-9, (
        f"second sleep should be interval_sec=0.05, got {sleep_calls[1]}"
    )


@pytest.mark.asyncio
async def test_no_sleep_when_first_poll_finds_fill():
    """deal_details returns fill on poll #1 → no sleep at all."""
    from src.execution.live_executor import _poll_fill_price

    client = AsyncMock()
    client.get_order_deals = AsyncMock(return_value={
        "code": 0,
        "data": [{
            "orderId": "ORD6", "symbol": "ZEC_USDT", "price": 432.5, "vol": 10,
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
            client, "ZEC_USDT", order_id="ORD6", timeout_sec=1.0,
        )
    finally:
        le.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert fill_price > 0
    assert hold_vol == 10
    assert sleep_calls == [], (
        f"first-poll hit must not sleep, got {sleep_calls}"
    )


@pytest.mark.asyncio
async def test_returns_scaled_price():
    """Returned price = raw × get_binance_scale(symbol)."""
    from src.execution.live_executor import _poll_fill_price
    from src.exchanges.mexc_rest import get_binance_scale

    raw_price = 0.018695  # like DOGE_USDT
    client = AsyncMock()
    client.get_order_deals = AsyncMock(return_value={
        "code": 0,
        "data": [{
            "orderId": "ORD7", "symbol": "DOGE_USDT",
            "price": raw_price, "vol": 50,
        }],
    })

    fill_price, hold_vol = await _poll_fill_price(
        client, "DOGE_USDT", order_id="ORD7", timeout_sec=1.0,
    )

    scale = get_binance_scale("DOGE_USDT")
    expected = raw_price * scale
    assert abs(fill_price - expected) < 1e-9, (
        f"Expected scaled price {expected}, got {fill_price}"
    )
    assert hold_vol == 50


@pytest.mark.asyncio
async def test_existing_legacy_behaviour_unchanged():
    """Sanity check: positional-arg call (legacy two-arg form) still works.

    Some test code and possibly future analytics scripts call
    `_poll_fill_price(client, symbol, timeout_sec=...)`. With order_id as
    keyword-default-None, that signature must still work and behave like the
    pre-patch implementation.
    """
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

    fill_price, hold_vol = await _poll_fill_price(
        client, "ZEC_USDT", timeout_sec=1.0,
    )

    assert fill_price > 0
    assert hold_vol == 10
