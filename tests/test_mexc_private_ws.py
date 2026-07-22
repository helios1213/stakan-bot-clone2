"""Unit tests for MexcPrivateWS fill-buffer / wait_fill logic.

These exercise the dispatch + lookup behaviour without a real socket: we feed
push.personal.order payloads (real schema captured from MEXC) straight into
_dispatch and assert wait_fill resolves correctly, including the
push-arrives-before-we-ask race and the IOC-expired (vol==0) case.
"""
import asyncio

import pytest

from src.exchanges.mexc_private_ws import MexcPrivateWS


def _order_push(order_id, state, deal_vol, deal_avg_price=0.008019, maker_fee=0.0, taker_fee=0.0):
    return {
        "channel": "push.personal.order",
        "data": {
            "orderId": order_id,
            "state": state,
            "dealVol": deal_vol,
            "dealAvgPrice": deal_avg_price,
            "makerFee": maker_fee,
            "takerFee": taker_fee,
            "symbol": "PENGU_USDT",
        },
        "ts": 1780131801923,
    }


def _ws():
    ws = MexcPrivateWS(slot_id=2, webkey="WEBxxxx")
    ws.connected = True  # bypass the real login for unit testing
    return ws


@pytest.mark.asyncio
async def test_terminal_fill_buffered_before_wait():
    """Push arrives BEFORE wait_fill is called (the common race) → returned."""
    ws = _ws()
    ws._dispatch(_order_push("OID1", state=3, deal_vol=3230, deal_avg_price=0.008019))
    fill = await ws.wait_fill("OID1", timeout_sec=0.1)
    assert fill is not None
    assert fill.deal_vol == 3230
    assert fill.deal_avg_price_raw == 0.008019
    assert fill.terminal


@pytest.mark.asyncio
async def test_wait_then_push_arrives():
    """wait_fill is awaiting; a terminal push then arrives → resolves."""
    ws = _ws()

    async def push_later():
        await asyncio.sleep(0.02)
        ws._dispatch(_order_push("OID2", state=3, deal_vol=100))

    asyncio.create_task(push_later())
    fill = await ws.wait_fill("OID2", timeout_sec=0.5)
    assert fill is not None and fill.deal_vol == 100


@pytest.mark.asyncio
async def test_expired_ioc_terminal_zero_vol():
    """state=3 with dealVol=0 = IOC expired: terminal, vol 0 (caller treats as no-fill)."""
    ws = _ws()
    ws._dispatch(_order_push("OID3", state=3, deal_vol=0))
    fill = await ws.wait_fill("OID3", timeout_sec=0.1)
    assert fill is not None and fill.deal_vol == 0 and fill.terminal


@pytest.mark.asyncio
async def test_non_terminal_not_returned_until_done():
    """state=2 (in progress) must NOT resolve; a later state=3 does."""
    ws = _ws()
    ws._dispatch(_order_push("OID4", state=2, deal_vol=50))
    fill = await ws.wait_fill("OID4", timeout_sec=0.05)
    assert fill is None  # only partial/in-progress seen → timeout

    ws._dispatch(_order_push("OID4", state=3, deal_vol=120))
    fill2 = await ws.wait_fill("OID4", timeout_sec=0.1)
    assert fill2 is not None and fill2.deal_vol == 120


@pytest.mark.asyncio
async def test_timeout_when_no_push():
    ws = _ws()
    fill = await ws.wait_fill("NOPE", timeout_sec=0.05)
    assert fill is None


@pytest.mark.asyncio
async def test_not_connected_returns_none():
    ws = MexcPrivateWS(slot_id=2, webkey="WEBxxxx")  # connected stays False
    ws._dispatch(_order_push("OID5", state=3, deal_vol=10))
    fill = await ws.wait_fill("OID5", timeout_sec=0.05)
    assert fill is None


@pytest.mark.asyncio
async def test_fee_summed_from_maker_taker():
    ws = _ws()
    ws._dispatch(_order_push("OID6", state=3, deal_vol=10, maker_fee=0.0, taker_fee=0.0))
    fill = await ws.wait_fill("OID6", timeout_sec=0.1)
    assert fill is not None and fill.fee == 0.0


@pytest.mark.asyncio
async def test_login_success_sets_connected():
    ws = MexcPrivateWS(slot_id=2, webkey="WEBxxxx")
    assert ws.connected is False
    ws._dispatch({"channel": "rs.login", "data": "success", "ts": 1})
    assert ws.connected is True


@pytest.mark.asyncio
async def test_login_failure_keeps_disconnected():
    ws = MexcPrivateWS(slot_id=2, webkey="WEBxxxx")
    ws._dispatch({"channel": "rs.login", "data": "Login failed", "ts": 1})
    assert ws.connected is False


# ---- close-fill (push.personal.position state=3 → wait_close) ----
import time as _time


def _pos_push(symbol, state, close_avg=0.00784, open_avg=0.00784, realised=0.0):
    return {
        "channel": "push.personal.position",
        "data": {
            "symbol": symbol, "state": state, "positionId": 999,
            "closeAvgPrice": close_avg, "openAvgPrice": open_avg,
            "realised": realised, "holdVol": 0 if state == 3 else 100,
        },
        "ts": 1780242901860,
    }


@pytest.mark.asyncio
async def test_close_state3_buffered_before_wait():
    ws = _ws()
    t0 = _time.monotonic()
    ws._dispatch(_pos_push("PENGU_USDT", state=3, close_avg=0.00784, open_avg=0.007838, realised=0.0087))
    c = await ws.wait_close("PENGU_USDT", after_ts=t0, timeout_sec=0.1)
    assert c is not None
    assert c.close_avg_price_raw == 0.00784
    assert c.open_avg_price_raw == 0.007838
    assert c.realised == 0.0087


@pytest.mark.asyncio
async def test_close_state_open_ignored():
    ws = _ws()
    t0 = _time.monotonic()
    ws._dispatch(_pos_push("PENGU_USDT", state=1))  # open, not a close
    c = await ws.wait_close("PENGU_USDT", after_ts=t0, timeout_sec=0.05)
    assert c is None


@pytest.mark.asyncio
async def test_close_stale_rejected_by_after_ts():
    ws = _ws()
    ws._dispatch(_pos_push("PENGU_USDT", state=3))  # arrives "before" our close
    after = _time.monotonic() + 0.001  # our close submitted AFTER the stale push
    c = await ws.wait_close("PENGU_USDT", after_ts=after, timeout_sec=0.05)
    assert c is None  # stale close must not match → caller falls back to REST


@pytest.mark.asyncio
async def test_close_wait_then_push():
    ws = _ws()
    t0 = _time.monotonic()

    async def push_later():
        await asyncio.sleep(0.02)
        ws._dispatch(_pos_push("PENGU_USDT", state=3, close_avg=0.5))

    asyncio.create_task(push_later())
    c = await ws.wait_close("PENGU_USDT", after_ts=t0, timeout_sec=0.5)
    assert c is not None and c.close_avg_price_raw == 0.5


@pytest.mark.asyncio
async def test_close_not_connected_returns_none():
    ws = MexcPrivateWS(slot_id=2, webkey="WEBxxxx")  # connected stays False
    ws._dispatch(_pos_push("PENGU_USDT", state=3))
    c = await ws.wait_close("PENGU_USDT", after_ts=0.0, timeout_sec=0.05)
    assert c is None


@pytest.mark.asyncio
async def test_close_timeout_no_push():
    ws = _ws()
    c = await ws.wait_close("NOPE_USDT", after_ts=_time.monotonic(), timeout_sec=0.05)
    assert c is None
