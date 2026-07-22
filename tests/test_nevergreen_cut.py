"""Never-green deep-dip cut (Rule 1b in _check_simple_trail_exit).

Data foundation: never-green trades win ~1.4% even at -4t, vs 64% for
ever-green at the same depth. Dip-recover winners are shallow (median -2t)
and mostly green by ~1.5s, so they MUST survive this cut. These tests pin:
  - never-green + deep + late  → cut
  - ever-green at same depth    → NOT cut (protects dip-recover winners)
  - never-green but shallow     → NOT cut
  - never-green deep but early  → NOT cut (before nevergreen_cut_ms)
  - disabled (cut_ms=0)         → NOT cut (dormant default)
"""
import time
from types import SimpleNamespace

import src.strategy.shadow_engine as se
from src.config_loader import ExitStrategyConfig
from src.strategy.shadow_position import ShadowPosition

# stop_adverse high so Rule 1 (simple_adverse) never preempts Rule 1b;
# trail/breakeven/stall/DOA disabled so ONLY adverse + nevergreen can fire.
NG = ExitStrategyConfig(
    stop_adverse_ticks=100, stop_adverse_bps=0.0,
    trail_distance_ticks=100, trail_distance_bps=0.0,
    breakeven_trigger_ticks=0.0, breakeven_trigger_bps=0.0,
    stall_timeout_ms=0, dead_on_arrival_timeout_ms=0,
    min_hold_ms=300,
    nevergreen_cut_ms=1200, nevergreen_adverse_ticks=4.0, nevergreen_peak_ticks=1.0,
)
DISABLED = ExitStrategyConfig(
    stop_adverse_ticks=100, trail_distance_ticks=100, min_hold_ms=300,
    nevergreen_cut_ms=0,  # dormant
)
_SELF = SimpleNamespace(_config_loader=None)  # method only touches self in cold-start


def _pos(direction, entry, current, peak_fav, elapsed_ms, es=NG):
    p = ShadowPosition(symbol="1000PEPEUSDT", direction=direction,
                       detector_source="test", confidence=1.0)
    p.tick_scaled = 1.0          # 1 tick == 1.0 price unit (clean math)
    p.entry_price = entry
    p.current_price = current
    p.peak_price_favorable = peak_fav
    p.opened_at_ms = int(time.time() * 1000) - elapsed_ms
    p.exit_strategy_cached = es
    return p


def _check(pos):
    return se.ShadowEngine._check_simple_trail_exit(_SELF, pos)


def test_nevergreen_deep_late_long_is_cut():
    # never green (peak=0), -4t adverse, 2000ms elapsed
    assert _check(_pos("long", 100.0, 96.0, 0.0, 2000)) == "nevergreen_cut"


def test_nevergreen_deep_late_short_is_cut():
    # short: entry 100, current 104 → -4t adverse; never green
    assert _check(_pos("short", 100.0, 104.0, 0.0, 2000)) == "nevergreen_cut"


def test_ever_green_same_depth_survives():
    # peak was +3t (ever green) at the SAME -4t depth → dip-recover winner, keep
    assert _check(_pos("long", 100.0, 96.0, 103.0, 2000)) != "nevergreen_cut"


def test_nevergreen_shallow_survives():
    # never green but only -2t (< 4t) → below threshold, keep
    assert _check(_pos("long", 100.0, 98.0, 0.0, 2000)) != "nevergreen_cut"


def test_nevergreen_deep_but_early_survives():
    # never green, -4t, but only 500ms (< 1200ms) → too early, keep
    assert _check(_pos("long", 100.0, 96.0, 0.0, 500)) != "nevergreen_cut"


def test_disabled_does_not_cut():
    # cut_ms=0 → rule dormant even on a never-green deep late dip
    assert _check(_pos("long", 100.0, 96.0, 0.0, 2000, es=DISABLED)) != "nevergreen_cut"


def test_boundary_exactly_at_threshold_is_cut():
    # exactly -4t and exactly 1200ms → inclusive boundary fires
    assert _check(_pos("long", 100.0, 96.0, 0.0, 1200)) == "nevergreen_cut"
