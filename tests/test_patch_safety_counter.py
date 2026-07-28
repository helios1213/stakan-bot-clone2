"""Test for F-013: mark_position_closed_externally must decrement the
LiveSafetyController's open_live_positions counter.

Without the fix, reconciliation that marks a stale engine position closed
leaves the safety counter elevated. Future can_open_live(symbol) returns
False with "max_concurrent_per_symbol reached", silently bricking the
slot from trading that symbol until process restart.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.live_safety import LiveSafetyController
from src.strategy.shadow_engine import ShadowEngine
from src.strategy.shadow_position import ShadowPosition


@pytest.mark.asyncio
async def test_mark_position_closed_externally_decrements_safety_counter():
    """After mark_position_closed_externally on a live position, the
    LiveSafetyController.state.open_live_positions[symbol] must be
    decremented to 0 (i.e. the key removed)."""
    # Build the safety controller and pre-load it with an open position
    safety = LiveSafetyController(
        max_concurrent_per_symbol=1,
        max_concurrent_total=1,
        max_margin_per_trade_usdt=100.0,
    )
    safety.record_open("SUIUSDT")
    assert safety.state.open_live_positions == {"SUIUSDT": 1}
    # Sanity: can_open_live now refuses (max_concurrent reached)
    allowed, reason = safety.can_open_live("SUIUSDT", margin_usdt=10.0)
    assert allowed is False
    assert "max_concurrent" in reason

    # Build a minimal live_pool that returns this safety controller for slot 1
    live_pool = MagicMock()
    live_pool.get_safety = MagicMock(return_value=safety)

    # Build a minimal ShadowEngine; bypass __init__
    eng = ShadowEngine.__new__(ShadowEngine)
    eng.db = None
    eng.live_db = None
    eng.live_pool = live_pool
    eng._open_positions = {}
    eng._cooldown_until = {}
    eng._pair_configs = {}
    eng.positions_closed = 0
    eng.ob_manager = MagicMock()
    eng.ob_manager.get = MagicMock(return_value=None)  # no orderbook → fallback to entry_price
    eng._persist_trade = AsyncMock()
    eng.alerts = None

    # Build a live position the engine "owns" in slot 1
    pos = ShadowPosition(
        symbol="SUIUSDT",
        direction="long",
        detector_source="static_gap",
        confidence=0.5,
        leverage=50,
        margin_usdt=10.0,
        notional_usdt=500.0,
        qty=100.0,
        entry_price=2.0,
    )
    pos.mode = "live"
    pos.account_label = "slot1"

    # Add to engine's open positions so cleanup at end of mark_externally works
    eng._open_positions["SUIUSDT"] = [pos]

    # ACT: external close
    await eng.mark_position_closed_externally(
        pos, reason="reconciliation_external_close",
    )

    # ASSERT: safety counter went back to zero (key removed by record_close)
    assert safety.state.open_live_positions == {}, (
        f"safety counter not decremented: {safety.state.open_live_positions}"
    )
    # AND the slot can open again
    allowed_after, _ = safety.can_open_live("SUIUSDT", margin_usdt=10.0)
    assert allowed_after is True


@pytest.mark.asyncio
async def test_mark_position_closed_externally_skips_safety_for_shadow_pos():
    """Shadow positions never touched the safety counter when opened, so
    don't try to decrement it on external close. The slot lookup path
    should be skipped entirely for mode='shadow'."""
    safety = LiveSafetyController()
    live_pool = MagicMock()
    live_pool.get_safety = MagicMock(return_value=safety)

    eng = ShadowEngine.__new__(ShadowEngine)
    eng.db = None
    eng.live_db = None
    eng.live_pool = live_pool
    eng._open_positions = {}
    eng.positions_closed = 0
    eng.ob_manager = MagicMock()
    eng.ob_manager.get = MagicMock(return_value=None)
    eng._persist_trade = AsyncMock()
    eng.alerts = None

    pos = ShadowPosition(
        symbol="SUIUSDT",
        direction="long",
        detector_source="static_gap",
        confidence=0.5,
        leverage=50,
        margin_usdt=10.0,
        notional_usdt=500.0,
        qty=100.0,
        entry_price=2.0,
    )
    pos.mode = "shadow"   # ← key difference
    pos.account_label = None

    eng._open_positions["SUIUSDT"] = [pos]

    await eng.mark_position_closed_externally(
        pos, reason="reconciliation_external_close",
    )

    # get_safety should NEVER have been called for a shadow position
    live_pool.get_safety.assert_not_called()
