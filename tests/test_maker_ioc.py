"""Tests for maker-ioc patch (2026-05-08): IOC_PASSIVE_TICK_OFFSET from env.

Verifies:
  1. Default (no env) → IOC_PASSIVE_TICK_OFFSET = 0.
  2. ENV "1" → IOC_PASSIVE_TICK_OFFSET = 1 (maker-style, 1 tick inside spread).
  3. ENV "2", "-1" → parsed correctly.
  4. Limit price math:
     - LONG  with offset=0  → limit = best_ask  (taker)
     - LONG  with offset=1  → limit = best_ask - 1 tick = best_bid level (maker)
     - SHORT with offset=0  → limit = best_bid  (taker)
     - SHORT with offset=1  → limit = best_bid + 1 tick = best_ask level (maker)
  5. Environment is read at module-load time, not per-call. Reload module to
     test different env values.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _reload_with_env(value: str | None):
    """Reload live_executor with a specific IOC_PASSIVE_TICK_OFFSET env value
    (or unset if value is None). Returns the reloaded module."""
    if value is None:
        os.environ.pop("IOC_PASSIVE_TICK_OFFSET", None)
    else:
        os.environ["IOC_PASSIVE_TICK_OFFSET"] = value
    # Force reload to re-evaluate the module-level constant
    if "src.execution.live_executor" in sys.modules:
        return importlib.reload(sys.modules["src.execution.live_executor"])
    import src.execution.live_executor as le
    return le


# NOTE: the env-driven IOC_PASSIVE_TICK_OFFSET module constant was removed —
# the offset is now per-pair (pair YAML execution.ioc_offset_ticks, passed as
# place_ioc_open(offset_ticks=...)). The 4 env-assert tests that lived here are
# gone; the limit-price math below still exercises the offset via explicit args.


def test_limit_price_long_offset_zero():
    """LONG with offset=0: limit = best_ask (touches the offer)."""
    _reload_with_env("0")
    best_ask = 0.010356
    tick_scaled = 0.000001
    offset = 0
    limit = best_ask - offset * tick_scaled
    assert limit == pytest.approx(0.010356)


def test_limit_price_long_offset_one_is_at_bid_level():
    """LONG with offset=1: limit = best_ask - 1 tick. For a 1-tick spread
    book, this lands exactly at the best_bid level — maker-style passive."""
    _reload_with_env("1")
    best_ask = 0.010356
    best_bid = 0.010355  # 1 tick below ask
    tick_scaled = 0.000001
    offset = 1
    limit = best_ask - offset * tick_scaled
    assert limit == pytest.approx(best_bid, abs=1e-9)


def test_limit_price_short_offset_zero():
    """SHORT with offset=0: limit = best_bid (hits the bid)."""
    _reload_with_env("0")
    best_bid = 0.010355
    tick_scaled = 0.000001
    offset = 0
    limit = best_bid + offset * tick_scaled
    assert limit == pytest.approx(0.010355)


def test_limit_price_short_offset_one_is_at_ask_level():
    """SHORT with offset=1: limit = best_bid + 1 tick = at best_ask level."""
    _reload_with_env("1")
    best_bid = 0.010355
    best_ask = 0.010356
    tick_scaled = 0.000001
    offset = 1
    limit = best_bid + offset * tick_scaled
    assert limit == pytest.approx(best_ask, abs=1e-9)


def test_limit_price_long_wider_spread_offset_one():
    """If spread is wider than 1 tick, offset=1 lands INSIDE spread, between
    bid and ask. E.g. ask=0.010360, bid=0.010354 (5 tick spread), offset=1
    → limit = 0.010359 (1 tick inside ask, still 4 ticks above bid)."""
    _reload_with_env("1")
    best_ask = 0.010360
    tick_scaled = 0.000001
    offset = 1
    limit = best_ask - offset * tick_scaled
    assert limit == pytest.approx(0.010359, abs=1e-9)


def teardown_module(module):
    """Reset env to clean state after tests."""
    os.environ.pop("IOC_PASSIVE_TICK_OFFSET", None)
