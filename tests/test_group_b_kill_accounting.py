"""Group B: a real loss must reach the $25 peak-drawdown kill (record_close).

#5/#6: an orphan close whose MEXC history had not settled (result.exit_price==0)
       previously skipped BOTH the live_trades row AND record_close — the real
       loss escaped the kill. Now we re-poll history and book the real value.
#4:    an externally-closed / liquidated position previously booked a ~$0 mid
       estimate; now it books the exchange-real close price.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.safety.reconciliation as recon
from src.safety.reconciliation import reconcile_once


def _executor(get_open_positions, close_result=None):
    client = MagicMock()
    client.get_open_positions = get_open_positions
    ex = MagicMock()
    ex.slot_id = 2
    ex.client_pool = MagicMock()
    ex.client_pool.get = AsyncMock(return_value=client)
    if close_result is not None:
        ex.market_close_position = AsyncMock(return_value=close_result)
    return ex


def _pool_for(executor, slot_id=2, safety=None):
    p = MagicMock()
    p._executors = {slot_id: executor}
    p.active_executors = MagicMock(return_value={slot_id: executor})
    p.webkey_store = MagicMock()
    p.webkey_store.list_enabled_complete = AsyncMock(
        return_value=[SimpleNamespace(slot_id=slot_id)])
    p.get_or_create_executor = MagicMock(return_value=executor)
    p.get_executor = MagicMock(return_value=executor)
    p.slot_has_key = AsyncMock(return_value=True)
    p.get_safety = MagicMock(return_value=safety)
    return p


@pytest.mark.asyncio
async def test_5_6_unsettled_orphan_close_still_feeds_the_kill(monkeypatch):
    """market_close succeeded but exit_price==0 (history lag). The real loss
    (-$30) must be back-filled and passed to record_close."""
    orphan = {"symbol": "1000PEPE_USDT", "positionType": 1, "holdVol": 3000,
              "leverage": 48, "openAvgPrice": 0.0027, "im": 90.0,
              "createTime": int((time.time() - 3600) * 1000)}
    executor = _executor(
        AsyncMock(return_value={"code": 0, "data": [orphan]}),
        close_result=SimpleNamespace(success=True, exit_price=0.0,
                                     realized_pnl_usdt=0.0,
                                     entry_price_confirmed=0.0, error_msg=None))
    safety = MagicMock()
    pool = _pool_for(executor, safety=safety)

    # history settles on the reconcile re-poll: close=0.00265, realised=-$30
    monkeypatch.setattr(recon, "_real_realized_from_history",
                        AsyncMock(return_value=(0.00265, -30.0)), raising=False)

    engine = SimpleNamespace(_open_positions={}, live_pool=pool,
                             live_db=MagicMock(execute=AsyncMock()))
    await reconcile_once(engine, pool, alerts=None)

    safety.record_close.assert_called_once()
    # pnl is the 2nd positional arg of record_close(symbol, pnl, notional_usdt=)
    assert safety.record_close.call_args.args[1] == -30.0


@pytest.mark.asyncio
async def test_4_external_close_books_the_real_exit_not_mid(monkeypatch):
    """A vanished (externally-closed/liquidated) position must be marked closed
    with the exchange-real close price, not a mid ~$0 estimate."""
    pos = SimpleNamespace(mode="live", is_open=True, is_closing=False,
                          symbol="1000PEPEUSDT", elapsed_sec=120.0,
                          opened_at_ms=int((time.time() - 3600) * 1000),
                          account_label="slot2")
    executor = _executor(AsyncMock(return_value={"code": 0, "data": []}))  # position gone
    pool = _pool_for(executor)

    monkeypatch.setattr(recon, "_real_realized_from_history",
                        AsyncMock(return_value=(0.00250, -25.0)), raising=False)

    marked = AsyncMock()
    engine = SimpleNamespace(_open_positions={"1000PEPEUSDT": [pos]},
                             live_pool=pool, live_db=None,
                             mark_position_closed_externally=marked)
    await reconcile_once(engine, pool, alerts=None)

    marked.assert_awaited_once()
    assert marked.await_args.kwargs.get("exit_price_hint") == 0.00250
