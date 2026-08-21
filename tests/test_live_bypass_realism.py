"""Tests for Stage 15 — bypass realism gates for live pairs.

Stage 13 fixed only the signal_to_order_latency_ms sleep for live pairs.
Stage 15 closes the sibling gaps in _try_enter:

  1. Patch C latency sleep + drift filter   (was: applied to live too)
  2. should_reject_order random skip         (was: applied to live too)
  3. simulate_server_error 2s sleep + skip   (was: applied to live too)
  4. Cooldown timing now uses live PnL       (was: used shadow PnL)

Each gate is a SHADOW-ONLY simulation knob designed to match real
latency/error rates in backtests. Applying them to live trades layered
artificial delays and skips on top of the real latency LiveExecutor was
already incurring.

These tests use ShadowEngine.__new__ to construct a minimal engine
without invoking __init__, then exercise _try_enter and _close_position
with mocked dependencies.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.ioc_executor import IOCAttemptResult
from src.execution.realism import RealismProfile
from src.strategy.shadow_engine import PairExecConfig, ShadowEngine
from src.strategy.shadow_position import ShadowPosition
from src.strategy.signal import Signal


# ──────────────────────────────────────────────────────────────────────
# Engine fixture builder
# ──────────────────────────────────────────────────────────────────────

def _make_engine(*, is_live: bool, realism: RealismProfile | None = None) -> ShadowEngine:
    """Build a minimal ShadowEngine for _try_enter testing.

    is_live: what state_manager.is_in_live returns for any symbol.
    realism: profile to install. Defaults to one that triggers ALL realism
             gates (high rates) so absence-of-effect for live is observable.
    """
    eng = ShadowEngine.__new__(ShadowEngine)

    # MEXC orderbook — synced, with a stable mid_price.
    # latency-fix May 2026: live fast-path now reads best_bid/best_ask
    # directly (skipping simulate_ioc_entry). Return None from both so
    # the fast-path expires cleanly without invoking _open_position
    # (which has many more dependencies these tests don't wire up).
    mexc_ob = MagicMock()
    mexc_ob.is_synced = True
    mexc_ob.mid_price = MagicMock(return_value=0.10000)
    mexc_ob.best_bid = MagicMock(return_value=None)
    mexc_ob.best_ask = MagicMock(return_value=None)
    ob_manager = MagicMock()
    ob_manager.get = MagicMock(return_value=mexc_ob)
    eng.ob_manager = ob_manager

    # State manager: control live vs shadow per test.
    state_manager = MagicMock()
    state_manager.is_in_live = MagicMock(return_value=is_live)
    eng.state_manager = state_manager

    # IOC executor: return "expired" so _try_enter terminates cleanly
    # without invoking _open_position (which has many more dependencies).
    ioc = MagicMock()
    ioc.simulate_ioc_entry = MagicMock(return_value=IOCAttemptResult(
        status="expired", target_price=0.10000, expired_reason="test_terminator",
    ))
    eng.ioc_executor = ioc

    # Realism: defaults aggressive so a "leak" would show.
    if realism is None:
        realism = RealismProfile(
            signal_to_order_latency_ms=350,
            base_rejection_rate=1.0,         # deterministic skip if applied
            server_error_rate=1.0,           # deterministic skip if applied
            server_timeout_extra_ms=2000,
        )
    eng.realism = realism

    # Patch C state (mirrors __init__ logic with entry_latency_ms=150)
    eng._latency_enabled = True
    eng._latency_min_ms = 100
    eng._latency_max_ms = 250
    eng._max_acceptable_drift_pct = 0.05
    # T1.1/T1.3 knobs (2026-08-21). Both default to 0 = off, which is what these
    # bypass tests want: they assert that SHADOW-only gates do not run for live
    # pairs, and an active feed-lag wait would add a second shadow-only gate to
    # reason about. __new__ skips __init__, so anything added there must be
    # mirrored here — this fixture has now broken that way twice.
    eng._mexc_feed_lag_ms = 0
    eng._max_book_age_ms = 0
    eng._queue_frac = 1.0          # T1.2; 1.0 = вимкнено

    # Counters touched by _try_enter
    eng.signals_skipped_no_book = 0
    eng.signals_skipped_latency_drift = 0
    eng.entries_attempted = 0
    eng.entries_rejected = 0
    eng.entries_filled = 0
    eng.entries_partial = 0
    eng.entries_expired = 0

    return eng


def _make_signal() -> Signal:
    return Signal(
        symbol="PENGUUSDT",
        direction="long",
        source="static_gap",
        confidence=0.5,
        binance_price=0.10000,
        mexc_price=0.10000,
    )


def _make_cfg() -> PairExecConfig:
    return PairExecConfig(
        margin_min_usdt=5.0,
        margin_max_usdt=10.0,
        leverage_min=50,
        leverage_max=70,
        ioc_max_attempts=1,
        ioc_offset_ticks=0,
    )


# ──────────────────────────────────────────────────────────────────────
# Sleep-tracking helper
# ──────────────────────────────────────────────────────────────────────

class SleepTracker:
    """Replaces asyncio.sleep with a no-op recorder."""
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, duration):
        self.calls.append(duration)
        # Don't actually sleep — tests should be fast.

    def patch(self, monkeypatch):
        monkeypatch.setattr(
            "src.strategy.shadow_engine.asyncio.sleep", self
        )


# ──────────────────────────────────────────────────────────────────────
# 1. Patch C latency sleep — must be skipped for live
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_c_latency_skipped_for_live(monkeypatch):
    """Live pair: Patch C 100-250ms sleep must NOT fire."""
    eng = _make_engine(is_live=True)
    tracker = SleepTracker()
    tracker.patch(monkeypatch)

    await eng._try_enter(_make_signal(), signal_id=None, cfg=_make_cfg())

    # Patch C sleeps are in 0.05-0.30 range. Stage 13 sleep (0.35) is also
    # in this region — both must be absent for live.
    artificial_sleeps = [d for d in tracker.calls if 0.05 <= d <= 0.5]
    assert artificial_sleeps == [], (
        f"Live pair received artificial realism sleeps: {artificial_sleeps}"
    )


@pytest.mark.asyncio
async def test_patch_c_latency_still_runs_for_shadow(monkeypatch):
    """Shadow pair: Patch C still fires (so backtests stay calibrated)."""
    # Use a realism profile that doesn't randomly skip — we want to reach
    # Patch C's sleep specifically.
    realism = RealismProfile(
        signal_to_order_latency_ms=0,   # disable Stage 13 sleep so we isolate Patch C
        base_rejection_rate=0.0,
        server_error_rate=0.0,
    )
    eng = _make_engine(is_live=False, realism=realism)
    tracker = SleepTracker()
    tracker.patch(monkeypatch)

    await eng._try_enter(_make_signal(), signal_id=None, cfg=_make_cfg())

    # Patch C sleep should have fired (100-250ms range).
    patch_c_sleeps = [d for d in tracker.calls if 0.10 <= d <= 0.25]
    assert len(patch_c_sleeps) >= 1, (
        f"Shadow pair did NOT receive Patch C sleep: {tracker.calls}"
    )


# ──────────────────────────────────────────────────────────────────────
# 2. should_reject_order — must NOT random-skip live signals
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_should_reject_skipped_for_live(monkeypatch):
    """Live pair: base_rejection_rate=1.0 must NOT skip the entry."""
    # base_rejection_rate=1.0 means 100% skip if applied. Confirm it's NOT.
    realism = RealismProfile(
        signal_to_order_latency_ms=0,
        base_rejection_rate=1.0,
        server_error_rate=0.0,
    )
    eng = _make_engine(is_live=True, realism=realism)
    # Patch C will sleep harmlessly if enabled; disable to keep test clean.
    eng._latency_enabled = False
    tracker = SleepTracker()
    tracker.patch(monkeypatch)

    await eng._try_enter(_make_signal(), signal_id=None, cfg=_make_cfg())

    # If should_reject_order fired, entries_rejected would be 1 AND
    # entries_attempted would NOT increment. Verify entries_attempted DID
    # increment → reject was skipped for live.
    #
    # latency-fix May 2026: live no longer calls simulate_ioc_entry (it now
    # builds an IOCAttemptResult stub directly from best_bid/best_ask). The
    # invariant moves up one level: did we reach the IOC attempt loop at all?
    # entries_attempted is incremented BEFORE the loop in both paths.
    assert eng.entries_attempted == 1, (
        "Live entry was random-rejected — should_reject_order applied to live"
    )
    assert eng.entries_rejected == 0


@pytest.mark.asyncio
async def test_should_reject_still_active_for_shadow(monkeypatch):
    """Shadow pair: base_rejection_rate=1.0 DOES skip the entry."""
    realism = RealismProfile(
        signal_to_order_latency_ms=0,
        base_rejection_rate=1.0,
        server_error_rate=0.0,
    )
    eng = _make_engine(is_live=False, realism=realism)
    eng._latency_enabled = False
    tracker = SleepTracker()
    tracker.patch(monkeypatch)

    await eng._try_enter(_make_signal(), signal_id=None, cfg=_make_cfg())

    assert eng.entries_rejected == 1
    assert not eng.ioc_executor.simulate_ioc_entry.called


# ──────────────────────────────────────────────────────────────────────
# 3. simulate_server_error — must NOT 2s-sleep + skip live signals
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_server_error_skipped_for_live(monkeypatch):
    """Live pair: server_error_rate=1.0 must NOT fire 2-second sleep + skip."""
    realism = RealismProfile(
        signal_to_order_latency_ms=0,
        base_rejection_rate=0.0,
        server_error_rate=1.0,
        server_timeout_extra_ms=2000,
    )
    eng = _make_engine(is_live=True, realism=realism)
    eng._latency_enabled = False
    tracker = SleepTracker()
    tracker.patch(monkeypatch)

    await eng._try_enter(_make_signal(), signal_id=None, cfg=_make_cfg())

    # 2-second sleep would land in 1.0–3.0 range (with jitter).
    server_error_sleeps = [d for d in tracker.calls if 1.0 <= d <= 3.0]
    assert server_error_sleeps == [], (
        f"Live pair received simulate_server_error sleep: {server_error_sleeps}"
    )
    # latency-fix May 2026: live skips simulate_ioc_entry; check
    # entries_attempted instead (incremented before the IOC loop in both paths).
    assert eng.entries_attempted == 1
    assert eng.entries_rejected == 0


@pytest.mark.asyncio
async def test_server_error_still_active_for_shadow(monkeypatch):
    """Shadow pair: server_error_rate=1.0 DOES fire 2s sleep + skip."""
    realism = RealismProfile(
        signal_to_order_latency_ms=0,
        base_rejection_rate=0.0,
        server_error_rate=1.0,
        server_timeout_extra_ms=2000,
    )
    eng = _make_engine(is_live=False, realism=realism)
    eng._latency_enabled = False
    tracker = SleepTracker()
    tracker.patch(monkeypatch)

    await eng._try_enter(_make_signal(), signal_id=None, cfg=_make_cfg())

    server_error_sleeps = [d for d in tracker.calls if 1.0 <= d <= 3.0]
    assert len(server_error_sleeps) == 1, (
        f"Shadow pair did NOT receive server_error sleep: {tracker.calls}"
    )
    assert eng.entries_rejected == 1
    assert not eng.ioc_executor.simulate_ioc_entry.called


# ──────────────────────────────────────────────────────────────────────
# 4. Stage 13 sleep — should still be skipped for live (regression test)
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stage13_latency_still_skipped_for_live(monkeypatch):
    """Regression: Stage 13's signal_to_order_latency_ms guard intact."""
    realism = RealismProfile(
        signal_to_order_latency_ms=350,
        base_rejection_rate=0.0,
        server_error_rate=0.0,
    )
    eng = _make_engine(is_live=True, realism=realism)
    eng._latency_enabled = False
    tracker = SleepTracker()
    tracker.patch(monkeypatch)

    await eng._try_enter(_make_signal(), signal_id=None, cfg=_make_cfg())

    stage13_sleeps = [d for d in tracker.calls if d == 0.35]
    assert stage13_sleeps == [], (
        f"Stage 13 latency sleep applied to live: {tracker.calls}"
    )


# ──────────────────────────────────────────────────────────────────────
# 5. Cooldown timing — uses live PnL after override (not shadow PnL)
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_uses_live_pnl_after_override(monkeypatch):
    """A live LOSS that shadow thought was a win → uses cooldown_after_loss_sec.

    Setup: live trade with shadow pos.net_pnl_usdt = +0.50 (shadow simulator
    thought it was a win), but live_realized_pnl_usdt = -0.30 (real loss).
    After override, cooldown must use cooldown_after_loss_sec (30) not
    cooldown_after_win_sec (5).
    """
    eng = ShadowEngine.__new__(ShadowEngine)
    eng.live_db = None
    eng.live_pool = None
    eng.alerts = None
    eng._open_positions = {"PENGUUSDT": []}
    eng._cooldown_until = {}
    eng.positions_closed = 0
    eng.funding_cost_paid = 0.0
    eng.realism = RealismProfile()  # all zeros — no funding, no fees, no realism

    # ob_manager: return a synced book with best_bid/best_ask but skip
    # market exit branch by returning None mid for safe fallback.
    mexc_ob = MagicMock()
    mexc_ob.is_synced = False  # forces fallback to pos.current_price
    ob_manager = MagicMock()
    ob_manager.get = MagicMock(return_value=mexc_ob)
    eng.ob_manager = ob_manager

    # market_executor not used because mexc_ob.is_synced=False
    eng.market_executor = MagicMock()

    # Pair config with distinct cooldown values to detect which branch fires.
    cfg = PairExecConfig(
        cooldown_after_win_sec=5,
        cooldown_after_loss_sec=30,
    )
    eng._pair_configs = {"PENGUUSDT": cfg}

    # Mock _persist_trade so we don't hit DB.
    eng._persist_trade = AsyncMock()

    # Build live position with mismatched shadow vs live PnL.
    pos = ShadowPosition(
        symbol="PENGUUSDT",
        direction="long",
        detector_source="static_gap",
        confidence=0.5,
        leverage=50,
        margin_usdt=5.0,
        notional_usdt=250.0,
        qty=2500.0,
        entry_target_price=0.10000,
        entry_price=0.10000,
    )
    pos.mode = "live"
    pos.update_price(0.10010)  # +10 ticks favorable so shadow thinks win
    pos.account_label = "slot1"
    pos.live_exit_price_real = 0.09997      # actually went 3 ticks against us
    pos.live_realized_pnl_usdt = -0.30      # real loss

    # is_open is a read-only property (True iff exit_reason is None); freshly
    # constructed ShadowPosition has exit_reason=None, so it's already open.

    import time as _t
    t_before = int(_t.time())
    await eng._close_position(pos, reason="test_close")
    t_after = int(_t.time())

    # After override, net_pnl_usdt = -0.30 → loss branch fires → cooldown 30s.
    # keyed per account: pos.account_label == "slot1"
    cooldown_until = eng._cooldown_until[(1, "PENGUUSDT")]
    cooldown_duration = cooldown_until - t_before

    # Tolerate 1s for clock drift.
    assert 29 <= cooldown_duration <= 31, (
        f"Cooldown duration {cooldown_duration}s does not match "
        f"cooldown_after_loss_sec=30. pos.net_pnl_usdt={pos.net_pnl_usdt}"
    )
    # And confirm the live override actually replaced shadow PnL.
    assert pos.net_pnl_usdt == -0.30


@pytest.mark.asyncio
async def test_cooldown_uses_win_sec_when_truly_won(monkeypatch):
    """Sanity: a live WIN uses cooldown_after_win_sec."""
    eng = ShadowEngine.__new__(ShadowEngine)
    eng.live_db = None
    eng.live_pool = None
    eng.alerts = None
    eng._open_positions = {"PENGUUSDT": []}
    eng._cooldown_until = {}
    eng.positions_closed = 0
    eng.funding_cost_paid = 0.0
    eng.realism = RealismProfile()

    mexc_ob = MagicMock()
    mexc_ob.is_synced = False
    ob_manager = MagicMock()
    ob_manager.get = MagicMock(return_value=mexc_ob)
    eng.ob_manager = ob_manager
    eng.market_executor = MagicMock()

    cfg = PairExecConfig(
        cooldown_after_win_sec=5,
        cooldown_after_loss_sec=30,
    )
    eng._pair_configs = {"PENGUUSDT": cfg}
    eng._persist_trade = AsyncMock()

    pos = ShadowPosition(
        symbol="PENGUUSDT",
        direction="long",
        detector_source="static_gap",
        confidence=0.5,
        leverage=50,
        margin_usdt=5.0,
        notional_usdt=250.0,
        qty=2500.0,
        entry_target_price=0.10000,
        entry_price=0.10000,
    )
    pos.mode = "live"
    pos.update_price(0.10010)
    pos.account_label = "slot1"
    pos.live_exit_price_real = 0.10005
    pos.live_realized_pnl_usdt = +0.40

    import time as _t
    t_before = int(_t.time())
    await eng._close_position(pos, reason="test_close")

    # keyed per account: pos.account_label == "slot1"
    cooldown_duration = eng._cooldown_until[(1, "PENGUUSDT")] - t_before
    assert 4 <= cooldown_duration <= 6, (
        f"Win cooldown {cooldown_duration}s does not match "
        f"cooldown_after_win_sec=5"
    )
    assert pos.net_pnl_usdt == +0.40
