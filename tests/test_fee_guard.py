"""Tests for the zero-fee guard in LiveExecutor.

The strategy only has edge at 0% MEXC maker fee. If any fill comes back with a
non-zero fee, live trading on that slot must halt automatically.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.live_executor import (
    FEE_GUARD_EPSILON_USDT,
    LiveExecutor,
    _poll_fill_price,
)


def _executor(slot_id=2):
    store = MagicMock()
    store.set_live_enabled = AsyncMock()
    alerts = MagicMock()
    alerts.send = AsyncMock()
    ex = LiveExecutor(
        client_pool=MagicMock(), slot_id=slot_id,
        webkey_store=store, alerts=alerts,
    )
    return ex, store, alerts


@pytest.mark.asyncio
async def test_trip_disables_slot_and_alerts():
    ex, store, alerts = _executor(slot_id=2)
    assert ex._halted is False

    await ex._trip_fee_guard("TAO_USDT", 0.017)

    assert ex._halted is True
    store.set_live_enabled.assert_awaited_once_with(2, False)   # durable halt
    alerts.send.assert_awaited_once()                           # operator alerted


@pytest.mark.asyncio
async def test_trip_is_idempotent():
    ex, store, alerts = _executor()
    await ex._trip_fee_guard("TAO_USDT", 0.017)
    await ex._trip_fee_guard("TAO_USDT", 0.99)  # second fee event
    # Slot disabled / alerted exactly once, not twice.
    store.set_live_enabled.assert_awaited_once()
    alerts.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_place_ioc_open_refuses_when_halted():
    ex, _, _ = _executor(slot_id=1)
    ex._halted = True
    res = await ex.place_ioc_open(
        "TAO_USDT", "long", notional_usdt=1000.0, leverage=50, mexc_ob=MagicMock(),
    )
    assert res.success is False
    assert "fee_guard_halted" in (res.error_msg or "")


@pytest.mark.asyncio
async def test_poll_fill_price_surfaces_fee():
    """Non-zero MEXC fee in deal_details is summed into fee_out."""
    client = MagicMock()
    client.get_order_deals = AsyncMock(return_value={
        "code": 0,
        "data": [
            {"price": 275.0, "vol": 6, "fee": 0.010},
            {"price": 275.1, "vol": 4, "fee": 0.007},
        ],
    })
    fee_box: list[float] = []
    price, vol = await _poll_fill_price(
        client, "TAO_USDT", order_id="abc", timeout_sec=1.0, fee_out=fee_box,
    )
    assert vol == 10
    assert price > 0
    assert fee_box and abs(fee_box[0] - 0.017) < 1e-9
    assert fee_box[0] > FEE_GUARD_EPSILON_USDT


@pytest.mark.asyncio
async def test_poll_fill_price_zero_fee_does_not_trip_threshold():
    client = MagicMock()
    client.get_order_deals = AsyncMock(return_value={
        "code": 0,
        "data": [{"price": 275.0, "vol": 10, "fee": 0.0}],
    })
    fee_box: list[float] = []
    _price, vol = await _poll_fill_price(
        client, "TAO_USDT", order_id="abc", timeout_sec=1.0, fee_out=fee_box,
    )
    assert vol == 10
    assert fee_box == [0.0]
    assert fee_box[0] <= FEE_GUARD_EPSILON_USDT
