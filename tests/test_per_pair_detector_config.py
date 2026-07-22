"""Tests for Stage 14 — per-pair detector config overrides.

Verifies 2-level cascade:
  1. Per-pair override (when set in pair_configs)
  2. Global StaticGapConf default

(May 2026 full-purge: CVD + imbalance fields removed; only min_gap_ticks
and cooldown_sec remain as per-pair overrides.)
"""
from unittest.mock import MagicMock

import pytest

from src.strategy.static_gap_detector import (
    PerPairDetectorOverride,
    StaticGapConf,
    StaticGapDetector,
)


@pytest.fixture
def base_cfg():
    """Global config with distinct values for testing fallback."""
    return StaticGapConf(
        enabled=True,
        min_gap_ticks=3,
        cooldown_sec=3.0,
    )


@pytest.fixture
def detector(base_cfg):
    ob = MagicMock()
    sw = MagicMock()
    return StaticGapDetector(base_cfg, ob, sw, db=None)


# ──────────────────────────────────────────────────────────────────────
# Fallback to global (no overrides set)
# ──────────────────────────────────────────────────────────────────────

def test_eff_min_gap_falls_back_to_global(detector):
    assert detector._eff_min_gap_ticks("PENGUUSDT") == 3


def test_eff_cooldown_falls_back_to_global(detector):
    assert detector._eff_cooldown_sec("PENGUUSDT") == 3.0


# ──────────────────────────────────────────────────────────────────────
# Per-pair overrides take priority
# ──────────────────────────────────────────────────────────────────────

def test_per_pair_min_gap_overrides_global(detector):
    detector._pair_overrides["PENGUUSDT"] = PerPairDetectorOverride(min_gap_ticks=5)
    assert detector._eff_min_gap_ticks("PENGUUSDT") == 5
    # Other pair still uses global
    assert detector._eff_min_gap_ticks("TAOUSDT") == 3


def test_per_pair_cooldown_overrides_global(detector):
    detector._pair_overrides["PENGUUSDT"] = PerPairDetectorOverride(cooldown_sec=10.0)
    assert detector._eff_cooldown_sec("PENGUUSDT") == 10.0
    assert detector._eff_cooldown_sec("TAOUSDT") == 3.0


def test_per_pair_partial_overrides_only_set_fields(detector):
    """Setting min_gap_ticks alone must not affect cooldown."""
    detector._pair_overrides["PENGUUSDT"] = PerPairDetectorOverride(min_gap_ticks=5)
    # Overridden
    assert detector._eff_min_gap_ticks("PENGUUSDT") == 5
    # Not overridden — still global
    assert detector._eff_cooldown_sec("PENGUUSDT") == 3.0


def test_multiple_pairs_independent_overrides(detector):
    detector._pair_overrides["PENGUUSDT"] = PerPairDetectorOverride(min_gap_ticks=4)
    detector._pair_overrides["TAOUSDT"] = PerPairDetectorOverride(min_gap_ticks=6, cooldown_sec=8.0)

    assert detector._eff_min_gap_ticks("PENGUUSDT") == 4
    assert detector._eff_min_gap_ticks("TAOUSDT") == 6
    assert detector._eff_min_gap_ticks("ZECUSDT") == 3  # global

    assert detector._eff_cooldown_sec("PENGUUSDT") == 3.0  # global
    assert detector._eff_cooldown_sec("TAOUSDT") == 8.0


# ──────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────

def test_unknown_symbol_uses_global(detector):
    assert detector._eff_min_gap_ticks("NEVER_HEARD_OF_THIS_PAIR") == 3
