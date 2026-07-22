"""Tests for simple_trail exit strategy (Stage 19).

Covers:
  1. config_loader — parsing exit_strategy section + defaults
  2. _check_simple_trail_exit — direction normalisation, adverse stop,
     trail rule, min_hold blocking, missing data tolerance
  3. dispatch — _check_exit runs time_limit + stop_loss + simple_trail
"""
from __future__ import annotations

import time

import pytest
import yaml

from src.config_loader import (
    ConfigLoader,
    ExitStrategyConfig,
    _parse_exit_strategy,
)
from src.strategy.shadow_engine import ShadowEngine
from src.strategy.shadow_position import ShadowPosition


# ──────────────────────────────────────────────────────────────────────
# Config parsing
# ──────────────────────────────────────────────────────────────────────

def test_parse_exit_strategy_defaults():
    es = _parse_exit_strategy({}, ExitStrategyConfig())
    assert es.stop_adverse_ticks == 2
    assert es.trail_distance_ticks == 1
    assert es.min_hold_ms == 300


def test_parse_exit_strategy_valid_simple_trail():
    es = _parse_exit_strategy(
        {"stop_adverse_ticks": 3,
         "trail_distance_ticks": 2, "min_hold_ms": 500},
        ExitStrategyConfig(),
    )
    assert es.stop_adverse_ticks == 3
    assert es.trail_distance_ticks == 2
    assert es.min_hold_ms == 500


def test_pair_loads_simple_trail_from_yaml(tmp_path):
    (tmp_path / "pairs").mkdir()
    pair_path = tmp_path / "pairs" / "TAOUSDT.yaml"
    pair_path.write_text(yaml.safe_dump({
        "detector": {"min_ticks": 5},
        "exit_strategy": {
            "stop_adverse_ticks": 2,
            "trail_distance_ticks": 1,
            "min_hold_ms": 300,
        },
    }))
    loader = ConfigLoader(tmp_path)
    loader.load()
    pc = loader.get("TAOUSDT")
    assert pc.exit_strategy.stop_adverse_ticks == 2


def test_pair_without_exit_strategy_uses_defaults(tmp_path):
    (tmp_path / "pairs").mkdir()
    (tmp_path / "pairs" / "PENGUUSDT.yaml").write_text(yaml.safe_dump({
        "detector": {"min_ticks": 3},
    }))
    loader = ConfigLoader(tmp_path)
    loader.load()
    pc = loader.get("PENGUUSDT")
    assert pc.exit_strategy == ExitStrategyConfig()


def test_global_exit_strategy_inherited_by_pairs(tmp_path):
    """If global.yaml sets exit_strategy, pairs without overrides inherit."""
    (tmp_path / "pairs").mkdir()
    (tmp_path / "global.yaml").write_text(yaml.safe_dump({
        "exit_strategy": {"stop_adverse_ticks": 3},
    }))
    (tmp_path / "pairs" / "X.yaml").write_text(yaml.safe_dump({
        "detector": {"min_ticks": 5},
    }))
    loader = ConfigLoader(tmp_path)
    loader.load()
    pc = loader.get("X")
    assert pc.exit_strategy.stop_adverse_ticks == 3


# ──────────────────────────────────────────────────────────────────────
# _check_simple_trail_exit — direct unit tests
# ──────────────────────────────────────────────────────────────────────

def _mk_engine_with_loader(loader: ConfigLoader) -> ShadowEngine:
    """Build a minimal ShadowEngine with just enough wiring to call exit checks."""
    eng = ShadowEngine.__new__(ShadowEngine)
    eng._config_loader = loader
    return eng


def _mk_pos(symbol="TAOUSDT", direction="long",
            entry_price=324.0, current_price=324.0,
            peak=0.0, elapsed_sec=1.0) -> ShadowPosition:
    """Minimal ShadowPosition with the fields the exit check reads."""
    p = ShadowPosition(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        leverage=50,
        margin_usdt=5.0,
        notional_usdt=250.0,
        detector_source="static_gap",
        confidence=0.8,
    )
    p.current_price = current_price
    p.peak_price_favorable = peak
    p.opened_at_ms = int(time.time() * 1000) - int(elapsed_sec * 1000)
    p.current_roi_pct = 0.0
    return p


@pytest.fixture
def loader_simple_trail(tmp_path):
    (tmp_path / "pairs").mkdir()
    (tmp_path / "pairs" / "TAOUSDT.yaml").write_text(yaml.safe_dump({
        "exit_strategy": {
            "mode": "simple_trail",
            "stop_adverse_ticks": 2,
            "trail_distance_ticks": 1,
            "min_hold_ms": 300,
        },
    }))
    loader = ConfigLoader(tmp_path)
    loader.load()
    return loader


def test_simple_trail_no_exit_at_breakeven(loader_simple_trail):
    eng = _mk_engine_with_loader(loader_simple_trail)
    # entry=324.00, current=324.00, no peak — pure breakeven
    pos = _mk_pos(entry_price=324.00, current_price=324.00, peak=0.0, elapsed_sec=1.0)
    assert eng._check_simple_trail_exit(pos) is None


def test_simple_trail_adverse_long_fires(loader_simple_trail):
    """Long: current = entry - 2 ticks → adverse trigger."""
    eng = _mk_engine_with_loader(loader_simple_trail)
    # tick=$0.01 for TAO, 2 ticks adverse for long = 324.00 - 0.02 = 323.98
    pos = _mk_pos(entry_price=324.00, current_price=323.98, peak=0.0, elapsed_sec=1.0)
    assert eng._check_simple_trail_exit(pos) == "simple_adverse"


def test_simple_trail_adverse_short_fires(loader_simple_trail):
    """Short: current = entry + 2 ticks → adverse trigger."""
    eng = _mk_engine_with_loader(loader_simple_trail)
    pos = _mk_pos(direction="short", entry_price=324.00,
                  current_price=324.02, peak=0.0, elapsed_sec=1.0)
    assert eng._check_simple_trail_exit(pos) == "simple_adverse"


def test_simple_trail_1tick_adverse_no_exit(loader_simple_trail):
    """Long: -1 tick adverse, threshold is 2 → no exit."""
    eng = _mk_engine_with_loader(loader_simple_trail)
    pos = _mk_pos(entry_price=324.00, current_price=323.99, peak=0.0, elapsed_sec=1.0)
    assert eng._check_simple_trail_exit(pos) is None


def test_simple_trail_min_hold_blocks_early_exit(loader_simple_trail):
    """Even 5 ticks adverse, but only 100ms elapsed (< min_hold 300ms) → no exit."""
    eng = _mk_engine_with_loader(loader_simple_trail)
    pos = _mk_pos(entry_price=324.00, current_price=323.95, peak=0.0, elapsed_sec=0.1)
    assert eng._check_simple_trail_exit(pos) is None


def test_simple_trail_trail_fires_after_peak_pullback(loader_simple_trail):
    """Long: peak 324.05 (5 ticks up), current 324.04 (4 ticks up) → 1 tick pullback → exit."""
    eng = _mk_engine_with_loader(loader_simple_trail)
    pos = _mk_pos(entry_price=324.00, current_price=324.04, peak=324.05, elapsed_sec=2.0)
    assert eng._check_simple_trail_exit(pos) == "simple_trail"


def test_simple_trail_no_trail_at_new_peak(loader_simple_trail):
    """current is the peak (pullback=0) → no trail exit."""
    eng = _mk_engine_with_loader(loader_simple_trail)
    pos = _mk_pos(entry_price=324.00, current_price=324.05, peak=324.05, elapsed_sec=2.0)
    assert eng._check_simple_trail_exit(pos) is None


def test_simple_trail_trail_doesnt_fire_below_entry(loader_simple_trail):
    """If we never went profitable, peak=entry; pullback rule needs peak>entry."""
    eng = _mk_engine_with_loader(loader_simple_trail)
    # Peak = entry (never improved), current = 1 tick below — should NOT fire trail.
    # (Adverse rule is 2 ticks, also doesn't fire.)
    pos = _mk_pos(entry_price=324.00, current_price=323.99, peak=324.00, elapsed_sec=1.0)
    assert eng._check_simple_trail_exit(pos) is None


def test_simple_trail_short_trail_fires(loader_simple_trail):
    """Short: peak (favorable=down) 323.95, current 323.96 → 1 tick retrace → exit."""
    eng = _mk_engine_with_loader(loader_simple_trail)
    pos = _mk_pos(direction="short", entry_price=324.00,
                  current_price=323.96, peak=323.95, elapsed_sec=2.0)
    assert eng._check_simple_trail_exit(pos) == "simple_trail"


def test_simple_trail_no_loader_returns_none():
    """Engine without config_loader → simple_trail check returns None silently."""
    eng = ShadowEngine.__new__(ShadowEngine)
    eng._config_loader = None
    pos = _mk_pos()
    assert eng._check_simple_trail_exit(pos) is None
