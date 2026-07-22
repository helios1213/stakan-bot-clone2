"""Tests for Stage 17a — peak_ticks snapshot collection.

Background:
  After Stage 16 deploy, SQL on live trades showed phase_1_gap_collapse
  cuts 40% of trades with MFE~0, while winners achieve MFE >1 tick in
  1-3 seconds. Hypothesis: a "proof of life" warmup rule
  (peak_ticks >= 1 at t=1500ms) reliably separates winners from losers.

  To validate WITHOUT deploying a behavioural change, this patch adds
  passive snapshot collection: every ShadowPosition records peak_ticks
  at fixed milestones (500/1000/1500/2000 ms). After 2-3 hours of live
  data, SQL can answer:
    "Of N winners, how many would have been killed by warmup(peak<1@1500ms)?
     Of M phase_1 losers, how many would have been killed?"

  Only if the ratio is heavily skewed (kills most losers, few winners)
  does Stage 17b ship the actual warmup rule.

Tests verify:
  1. record_peak_snapshot writes only on first-cross of each milestone.
  2. Idempotent: subsequent calls for same milestone don't overwrite.
  3. Milestones not yet reached stay None.
  4. Snapshot fields are properly initialized on a fresh ShadowPosition.
"""
from __future__ import annotations


from src.strategy.shadow_position import ShadowPosition


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _new_pos() -> ShadowPosition:
    """A minimal open ShadowPosition for snapshot testing."""
    return ShadowPosition(
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


# ──────────────────────────────────────────────────────────────────────
# 1. Initial state — all snapshots None
# ──────────────────────────────────────────────────────────────────────

def test_fresh_position_has_no_snapshots():
    pos = _new_pos()
    assert pos.peak_ticks_at_500ms is None
    assert pos.peak_ticks_at_1000ms is None
    assert pos.peak_ticks_at_1500ms is None
    assert pos.peak_ticks_at_2000ms is None


# ──────────────────────────────────────────────────────────────────────
# 2. First-cross writes only the relevant milestone(s)
# ──────────────────────────────────────────────────────────────────────

def test_snapshot_at_500ms_writes_only_500ms():
    pos = _new_pos()
    pos.record_peak_snapshot(elapsed_ms=600, peak_ticks=2.5)
    assert pos.peak_ticks_at_500ms == 2.5
    assert pos.peak_ticks_at_1000ms is None
    assert pos.peak_ticks_at_1500ms is None
    assert pos.peak_ticks_at_2000ms is None


def test_snapshot_at_1000ms_writes_500_and_1000_if_first_call():
    """First call after entry that's already past 1000ms records BOTH
    milestones (because watch loop ticks at 100ms, so first call may
    arrive at 600/700ms; if a trade ticked past 1000ms before the
    snapshot was attempted, both milestones get the same value).
    """
    pos = _new_pos()
    pos.record_peak_snapshot(elapsed_ms=1100, peak_ticks=1.0)
    assert pos.peak_ticks_at_500ms == 1.0
    assert pos.peak_ticks_at_1000ms == 1.0
    assert pos.peak_ticks_at_1500ms is None
    assert pos.peak_ticks_at_2000ms is None


def test_snapshot_past_all_milestones_writes_all():
    pos = _new_pos()
    pos.record_peak_snapshot(elapsed_ms=2500, peak_ticks=4.0)
    assert pos.peak_ticks_at_500ms == 4.0
    assert pos.peak_ticks_at_1000ms == 4.0
    assert pos.peak_ticks_at_1500ms == 4.0
    assert pos.peak_ticks_at_2000ms == 4.0


# ──────────────────────────────────────────────────────────────────────
# 3. Idempotency — subsequent calls do NOT overwrite
# ──────────────────────────────────────────────────────────────────────

def test_subsequent_call_does_not_overwrite_500ms():
    """Watch loop ticks every 100ms; once a milestone is captured it
    must stay frozen (first-cross semantics)."""
    pos = _new_pos()
    pos.record_peak_snapshot(elapsed_ms=600, peak_ticks=2.0)
    assert pos.peak_ticks_at_500ms == 2.0
    pos.record_peak_snapshot(elapsed_ms=700, peak_ticks=3.0)
    assert pos.peak_ticks_at_500ms == 2.0  # unchanged


def test_sequence_of_calls_freezes_each_milestone():
    """Realistic scenario: watch loop fires snapshot every tick, each
    milestone gets captured on first cross only."""
    pos = _new_pos()
    pos.record_peak_snapshot(elapsed_ms=600, peak_ticks=0.5)   # captures 500ms
    pos.record_peak_snapshot(elapsed_ms=700, peak_ticks=0.8)   # 500ms unchanged
    pos.record_peak_snapshot(elapsed_ms=1100, peak_ticks=1.2)  # captures 1000ms
    pos.record_peak_snapshot(elapsed_ms=1300, peak_ticks=1.5)  # nothing new
    pos.record_peak_snapshot(elapsed_ms=1600, peak_ticks=2.0)  # captures 1500ms
    pos.record_peak_snapshot(elapsed_ms=2100, peak_ticks=3.0)  # captures 2000ms

    assert pos.peak_ticks_at_500ms == 0.5
    assert pos.peak_ticks_at_1000ms == 1.2
    assert pos.peak_ticks_at_1500ms == 2.0
    assert pos.peak_ticks_at_2000ms == 3.0


# ──────────────────────────────────────────────────────────────────────
# 4. Negative peak_ticks (position never in profit)
# ──────────────────────────────────────────────────────────────────────

def test_negative_peak_ticks_recorded():
    """phase_1 losers archetype: peak_ticks ≈ 0 or even slightly negative
    at every milestone (peak_price_favorable never improved from entry)."""
    pos = _new_pos()
    pos.record_peak_snapshot(elapsed_ms=600, peak_ticks=0.0)
    pos.record_peak_snapshot(elapsed_ms=1100, peak_ticks=0.0)
    pos.record_peak_snapshot(elapsed_ms=1600, peak_ticks=0.2)  # tiny wick
    assert pos.peak_ticks_at_500ms == 0.0
    assert pos.peak_ticks_at_1000ms == 0.0
    assert pos.peak_ticks_at_1500ms == 0.2


# ──────────────────────────────────────────────────────────────────────
# 5. Sub-500ms call records nothing
# ──────────────────────────────────────────────────────────────────────

def test_call_before_first_milestone_records_nothing():
    pos = _new_pos()
    pos.record_peak_snapshot(elapsed_ms=300, peak_ticks=99.0)
    assert pos.peak_ticks_at_500ms is None
    assert pos.peak_ticks_at_1000ms is None
    assert pos.peak_ticks_at_1500ms is None
    assert pos.peak_ticks_at_2000ms is None


def test_call_at_exactly_500ms_records_500ms():
    pos = _new_pos()
    pos.record_peak_snapshot(elapsed_ms=500, peak_ticks=1.5)
    assert pos.peak_ticks_at_500ms == 1.5
    assert pos.peak_ticks_at_1000ms is None


# ──────────────────────────────────────────────────────────────────────
# 6. Trade closed early — late milestones stay None
# ──────────────────────────────────────────────────────────────────────

def test_trade_closed_at_800ms_leaves_late_milestones_null():
    """Realistic phase_0 scenario: trade exits at 800ms with -3 ticks.
    peak_ticks_at_500ms is captured; later milestones never reached."""
    pos = _new_pos()
    pos.record_peak_snapshot(elapsed_ms=600, peak_ticks=-0.5)
    pos.record_peak_snapshot(elapsed_ms=700, peak_ticks=-0.8)
    # Trade closes at 800ms — no more snapshot calls.
    assert pos.peak_ticks_at_500ms == -0.5
    assert pos.peak_ticks_at_1000ms is None
    assert pos.peak_ticks_at_1500ms is None
    assert pos.peak_ticks_at_2000ms is None
