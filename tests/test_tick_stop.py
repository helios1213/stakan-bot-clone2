"""Tests for tick-stop patch (2026-05-08).

Verifies:
  1. PairExecConfig has stop_loss_ticks field with default 0.
  2. When stop_loss_ticks > 0, ROI-based SL is bypassed and tick-based wins.
  3. Tick math is correct for both LONG and SHORT.
  4. When stop_loss_ticks = 0, ROI-based SL works as before (regression).
  5. SL grace period applies to BOTH tick and ROI modes.
  6. Preset no longer overrides stop_loss_roi_pct.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy.shadow_engine import PairExecConfig, ShadowEngine


def _make_engine() -> ShadowEngine:
    """Bypass __init__ for unit tests of pure methods."""
    eng = ShadowEngine.__new__(ShadowEngine)
    eng.ob_manager = MagicMock()
    eng.funding_guard = MagicMock()
    eng.funding_guard.is_too_close = MagicMock(return_value=False)
    return eng


def _make_pos(**kwargs):
    """Build a minimal ShadowPosition-like object for exit checks.
    elapsed_sec is a property computed from opened_at_ms — backdate it
    to simulate desired elapsed time."""
    import time
    from src.strategy.shadow_position import ShadowPosition

    elapsed = kwargs.pop("elapsed_sec", 5.0)
    # opened_at_ms = now - elapsed*1000 → elapsed_sec property returns ~elapsed
    opened_at_ms = int(time.time() * 1000 - elapsed * 1000)

    pos = ShadowPosition(
        symbol=kwargs.get("symbol", "PENGUUSDT"),
        direction=kwargs.get("direction", "long"),
        entry_price=kwargs.get("entry_price", 0.010500),
        leverage=kwargs.get("leverage", 65),
        margin_usdt=kwargs.get("margin_usdt", 20.0),
        notional_usdt=kwargs.get("notional_usdt", 1300.0),
        opened_at_ms=opened_at_ms,
        signal_id=kwargs.get("signal_id", 1),
        detector_source=kwargs.get("detector_source", "static_gap"),
        confidence=kwargs.get("confidence", 0.5),
    )
    pos.current_price = kwargs.get("current_price", 0.010500)
    if pos.direction == "long":
        price_pct = (pos.current_price - pos.entry_price) / pos.entry_price * 100
    else:
        price_pct = (pos.entry_price - pos.current_price) / pos.entry_price * 100
    pos.current_roi_pct = price_pct * pos.leverage
    return pos


# ──────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────

def test_default_stop_loss_ticks_is_five():
    """After May 2026 refactor, default stop_loss_ticks=5 (was 0=disabled).
    ROI-based SL was removed."""
    cfg = PairExecConfig()
    assert cfg.stop_loss_ticks == 5


# Patch get_tick_size and get_binance_scale globally
@pytest.fixture(autouse=True)
def _patch_tick():
    with patch(
        "src.execution.live_executor.get_tick_size",
        return_value=0.000001,
    ), patch(
        "src.exchanges.mexc_rest.get_binance_scale",
        return_value=1.0,
    ), patch(
        "src.exchanges.mexc_rest.to_mexc",
        side_effect=lambda s: s.replace("USDT", "_USDT"),
    ):
        yield


def test_tick_sl_long_triggers_at_2_ticks_against():
    """LONG entry @ 0.010500. After price drops to 0.010498 (-2 ticks),
    SL must fire when stop_loss_ticks=2."""
    eng = _make_engine()
    pos = _make_pos(
        direction="long",
        entry_price=0.010500,
        current_price=0.010498,  # -2 ticks
    )
    cfg = PairExecConfig(stop_loss_ticks=2, max_hold_sec=600)
    result = eng._check_exit(pos, cfg)
    assert result == "stop_loss"


def test_tick_sl_long_no_trigger_at_1_tick():
    """LONG with stop_loss_ticks=2: -1 tick adverse must NOT trigger."""
    eng = _make_engine()
    pos = _make_pos(
        direction="long",
        entry_price=0.010500,
        current_price=0.010499,  # -1 tick
    )
    cfg = PairExecConfig(stop_loss_ticks=2, max_hold_sec=600)
    result = eng._check_exit(pos, cfg)
    assert result is None


def test_tick_sl_short_triggers_at_2_ticks_against():
    """SHORT entry @ 0.010500. After price rises to 0.010502 (+2 ticks
    against short), SL must fire."""
    eng = _make_engine()
    pos = _make_pos(
        direction="short",
        entry_price=0.010500,
        current_price=0.010502,  # +2 ticks against short
    )
    cfg = PairExecConfig(stop_loss_ticks=2, max_hold_sec=600)
    result = eng._check_exit(pos, cfg)
    assert result == "stop_loss"


def test_tick_sl_in_favor_no_trigger():
    """LONG that's IN PROFIT (price went UP) must not trigger SL."""
    eng = _make_engine()
    pos = _make_pos(
        direction="long",
        entry_price=0.010500,
        current_price=0.010505,  # +5 ticks IN profit
    )
    cfg = PairExecConfig(stop_loss_ticks=2, max_hold_sec=600)
    result = eng._check_exit(pos, cfg)
    assert result is None


def test_tick_sl_does_not_fire_when_ticks_zero():
    """After May 2026 refactor: stop_loss_ticks=0 effectively DISABLES SL.
    No ROI fallback — bot relies purely on phase/simple_trail exits + time_limit.
    """
    eng = _make_engine()
    pos = _make_pos(
        direction="long",
        entry_price=0.010500,
        current_price=0.010490,  # large adverse, but stop_loss_ticks=0
        leverage=65,
    )
    cfg = PairExecConfig(stop_loss_ticks=0, max_hold_sec=600)
    result = eng._check_exit(pos, cfg)
    assert result != "stop_loss", f"With stop_loss_ticks=0, SL must NOT fire, got {result}"


def test_sl_grace_blocks_tick_sl():
    """sl_grace_sec blocks tick-based SL during first N seconds."""
    eng = _make_engine()
    pos = _make_pos(
        direction="long",
        entry_price=0.010500,
        current_price=0.010498,  # -2 ticks
        elapsed_sec=0.5,
    )
    cfg = PairExecConfig(stop_loss_ticks=2, sl_grace_sec=2.0, max_hold_sec=600)
    assert eng._check_exit(pos, cfg) != "stop_loss"


# NOTE: tests test_preset_no_longer_forces_sl, test_preset_apply_preserves_db_sl_ticks,
# test_preset_apply_preserves_db_sl_roi were removed in stage-2 refactor (2026-05-09).
# They tested _STRATEGY_PRESETS / _apply_detector_strategy system which has been
# fully removed. Their guarantees (preset doesn't clobber DB values) are now
# trivially true because there is no preset to clobber anything.
