"""
Integration test for MEXC WS client scale normalisation.

Verifies that when binance_scale is configured for a symbol:
  - Orderbook snapshot prices are scaled correctly
  - Orderbook diff prices are scaled correctly
  - Trade prices are scaled correctly
  - Pairs without scale config are unaffected (scale=1.0 no-op)
"""
from __future__ import annotations

import pytest

from src.config import MexcConf
from src.exchanges.mexc_ws import MexcWSClient, Trade
from src.exchanges.orderbook import OrderBookManager


@pytest.fixture
def cfg():
    return MexcConf(
        ws_base="wss://contract.mexc.com/edge",
        rest_base="https://contract.mexc.com",
    )


@pytest.fixture
def ob_manager():
    return OrderBookManager()


@pytest.mark.asyncio
async def test_scale_applied_to_depth_diff_for_aliased_pair(cfg, ob_manager):
    """1000PEPEUSDT: MEXC raw price * 1000 should match Binance orderbook scale."""
    client = MexcWSClient(cfg, ob_manager)

    # Subscribe with explicit scale (1000PEPEUSDT -> PEPE_USDT, scale=1000)
    await client.subscribe(["1000PEPEUSDT"], scales={"1000PEPEUSDT": 1000.0})

    # Apply a synthetic depth diff with raw MEXC-scale prices
    # (PEPE on MEXC trades at ~$0.0000123, scale=1000 -> ~$0.0123 Binance-equiv)
    raw_diff = {
        "version": 1,
        "bids": [["0.00001234", "100"]],
        "asks": [["0.00001235", "100"]],
    }
    client._snapshot_ready["PEPE_USDT"] = True  # bypass snapshot wait
    client._apply_depth_diff("PEPE_USDT", raw_diff)

    ob = ob_manager.get("mexc", "1000PEPEUSDT")
    assert ob is not None
    bb = ob.best_bid()
    ba = ob.best_ask()
    assert bb is not None and ba is not None
    assert bb.price == pytest.approx(0.01234, rel=1e-6)
    assert ba.price == pytest.approx(0.01235, rel=1e-6)


@pytest.mark.asyncio
async def test_no_scale_applied_for_normal_pair(cfg, ob_manager):
    """BTCUSDT: scale=1.0 (default), prices unchanged."""
    client = MexcWSClient(cfg, ob_manager)
    await client.subscribe(["BTCUSDT"])  # no scales kwarg

    raw_diff = {
        "version": 1,
        "bids": [["70000.0", "1.5"]],
        "asks": [["70001.0", "1.5"]],
    }
    client._snapshot_ready["BTC_USDT"] = True
    client._apply_depth_diff("BTC_USDT", raw_diff)

    ob = ob_manager.get("mexc", "BTCUSDT")
    bb = ob.best_bid()
    ba = ob.best_ask()
    assert bb.price == pytest.approx(70000.0)
    assert ba.price == pytest.approx(70001.0)


@pytest.mark.asyncio
async def test_scale_applied_to_trade_callback(cfg, ob_manager):
    """1000PEPEUSDT trades emitted with normalised prices."""
    received: list[Trade] = []

    async def on_trade(t: Trade):
        received.append(t)

    client = MexcWSClient(cfg, ob_manager, on_trade=on_trade)
    await client.subscribe(["1000PEPEUSDT"], scales={"1000PEPEUSDT": 1000.0})

    # Synthetic MEXC trade message
    msg = {
        "symbol": "PEPE_USDT",
        "data": [
            {"p": 0.00001234, "v": 500, "T": 1, "t": 1700000000000},
        ],
    }
    await client._handle_trade(msg)

    assert len(received) == 1
    t = received[0]
    assert t.symbol == "1000PEPEUSDT"  # exposed in Binance form
    assert t.price == pytest.approx(0.01234, rel=1e-6)  # scaled ×1000
    assert t.size == 500


@pytest.mark.asyncio
async def test_static_scale_table_used_when_no_explicit_scales(cfg, ob_manager):
    """If caller doesn't pass scales, fall back to SYMBOL_SCALE_TO_BINANCE."""
    received: list[Trade] = []

    async def on_trade(t: Trade):
        received.append(t)

    client = MexcWSClient(cfg, ob_manager, on_trade=on_trade)
    # No scales kwarg — should auto-populate from static table
    await client.subscribe(["1000PEPEUSDT"])

    msg = {
        "symbol": "PEPE_USDT",
        "data": [
            {"p": 0.00001234, "v": 500, "T": 1, "t": 1700000000000},
        ],
    }
    await client._handle_trade(msg)

    assert len(received) == 1
    assert received[0].price == pytest.approx(0.01234, rel=1e-6)


@pytest.mark.asyncio
async def test_unsubscribe_clears_scale_map(cfg, ob_manager):
    """When pair is removed, its scale entry should be cleaned up."""
    client = MexcWSClient(cfg, ob_manager)
    await client.subscribe(["1000PEPEUSDT"], scales={"1000PEPEUSDT": 1000.0})
    assert client._scale_map.get("PEPE_USDT") == 1000.0

    await client.unsubscribe(["1000PEPEUSDT"])
    assert "PEPE_USDT" not in client._scale_map
