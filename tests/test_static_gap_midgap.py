"""Tests for static_gap_detector midgap patch (2026-05-08).

Verifies:
  1. Mid-vs-mid math is correct (gap direction matches expected signal direction).
  2. Below threshold → no signal.
  3. Above threshold → signal fires once, cooldown blocks duplicate, gap-close
     allows re-emit on direction flip.
  4. Math is symmetric (positive gap → LONG, negative → SHORT).
  5. require_both_sides field is silently ignored (deprecated noop).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy.static_gap_detector import StaticGapConf, StaticGapDetector


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class _BookEntry:
    price: float
    qty: float = 100.0


class _FakeOB:
    """Minimal OrderBook stand-in with controllable best_bid/best_ask."""
    def __init__(self, bid: float, ask: float, synced: bool = True):
        self._bid = _BookEntry(bid)
        self._ask = _BookEntry(ask)
        self.is_synced = synced

    def best_bid(self):
        return self._bid

    def best_ask(self):
        return self._ask

    # Alloc-free accessors used by the detector hot path. Return 0.0 to
    # signal "empty book" — matches the production OrderBook contract.
    def best_bid_price(self) -> float:
        return self._bid.price if self._bid is not None else 0.0

    def best_ask_price(self) -> float:
        return self._ask.price if self._ask is not None else 0.0


class _FakeOBManager:
    def __init__(self, books: dict[tuple[str, str], _FakeOB]):
        self._books = books

    def all_symbols(self, exchange: str) -> list[str]:
        return [sym for (ex, sym) in self._books if ex == exchange]

    def get(self, exchange: str, symbol: str):
        return self._books.get((exchange, symbol))


def _make_detector(
    binance_mid: float,
    mexc_mid: float,
    spread: float = 0.000001,
    min_gap_ticks: int = 3,
    cooldown_sec: float = 5.0,
):
    """Build a detector with fake books having the requested mids and a
    symmetric spread of `spread`. Defaults match PENGU (tick=1e-6)."""
    sym = "PENGUUSDT"
    half = spread / 2
    books = {
        ("binance", sym): _FakeOB(binance_mid - half, binance_mid + half),
        ("mexc",    sym): _FakeOB(mexc_mid    - half, mexc_mid    + half),
    }
    cfg = StaticGapConf(
        enabled=True,
        scan_interval_sec=0.05,
        min_gap_ticks=min_gap_ticks,
        cooldown_sec=cooldown_sec,
    )
    writer = MagicMock()
    writer.write = AsyncMock()
    det = StaticGapDetector(cfg, _FakeOBManager(books), writer)
    return det, writer, sym


# Patch get_tick_size + get_binance_scale to known values (PENGU-like).
# Note: get_binance_scale is imported inside _scan_once() via the FQ name
# `src.exchanges.mexc_rest`, so we patch it at that source module.
@pytest.fixture(autouse=True)
def _patch_tick_and_scale():
    with patch(
        "src.strategy.static_gap_detector.get_tick_size",
        return_value=0.000001,
    ), patch(
        "src.exchanges.mexc_rest.get_binance_scale",
        return_value=1.0,
    ), patch(
        "src.strategy.static_gap_detector.to_mexc",
        side_effect=lambda s: s.replace("USDT", "_USDT"),
    ):
        yield


# ──────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_long_signal_when_binance_higher():
    """Binance mid 0.010359, MEXC mid 0.010356 → gap = +3 ticks → LONG.
    This matches the user's screenshot scenario but with binance > mexc."""
    det, writer, sym = _make_detector(binance_mid=0.010359, mexc_mid=0.010356)
    await det._scan_once()

    assert writer.write.await_count == 1, "Expected exactly one signal"
    sig = writer.write.await_args.args[0]
    assert sig.symbol == sym
    assert sig.direction == "long"
    assert sig.metadata["gap_ticks"] == pytest.approx(3.0, abs=0.01)


@pytest.mark.asyncio
async def test_short_signal_when_binance_lower():
    """Binance mid 0.010356, MEXC mid 0.010359 → gap = -3 ticks → SHORT.
    This matches the user's screenshot exactly (Image 1 b=356, Image 2 m=359)."""
    det, writer, sym = _make_detector(binance_mid=0.010356, mexc_mid=0.010359)
    await det._scan_once()

    assert writer.write.await_count == 1
    sig = writer.write.await_args.args[0]
    assert sig.direction == "short"
    # New same-side-lag formula: gap_ticks is always POSITIVE (magnitude of lag).
    # SHORT fires when (mexc_ask - binance_ask) >= 3 ticks → gap_ticks = +3, not -3.
    assert sig.metadata["gap_ticks"] == pytest.approx(3.0, abs=0.01)


@pytest.mark.asyncio
async def test_below_threshold_no_signal():
    """Gap of only 2 ticks (below default 3) → no signal."""
    det, writer, _ = _make_detector(binance_mid=0.010358, mexc_mid=0.010356)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_below_threshold == 1


@pytest.mark.asyncio
async def test_exactly_at_threshold_emits():
    """Gap of exactly 3 ticks → signal fires (>= comparison)."""
    det, writer, _ = _make_detector(
        binance_mid=0.010359, mexc_mid=0.010356, min_gap_ticks=3,
    )
    await det._scan_once()
    assert writer.write.await_count == 1


@pytest.mark.asyncio
async def test_cooldown_blocks_duplicate_same_direction():
    """Two scans in same direction within cooldown → only one signal."""
    det, writer, _ = _make_detector(binance_mid=0.010359, mexc_mid=0.010356)
    await det._scan_once()
    await det._scan_once()
    assert writer.write.await_count == 1
    assert det.signals_skip_cooldown == 1


@pytest.mark.asyncio
async def test_gap_closes_then_reopens_direction_flip_emits_immediately():
    """LONG signal → gap closes → SHORT gap opens → SHORT fires (different
    direction overrides cooldown gating)."""
    sym = "PENGUUSDT"
    spread = 0.000001
    half = spread / 2

    # Mutable book holders — we'll swap mids between scans
    bin_ob = _FakeOB(0.010359 - half, 0.010359 + half)
    mex_ob = _FakeOB(0.010356 - half, 0.010356 + half)
    books = {("binance", sym): bin_ob, ("mexc", sym): mex_ob}

    cfg = StaticGapConf(enabled=True, min_gap_ticks=3, cooldown_sec=10.0)
    writer = MagicMock(); writer.write = AsyncMock()
    det = StaticGapDetector(cfg, _FakeOBManager(books), writer)

    # Scan 1: LONG (b=0.010359, m=0.010356)
    await det._scan_once()
    assert writer.write.await_count == 1
    assert writer.write.await_args.args[0].direction == "long"

    # Gap closes (mids equal)
    bin_ob._bid = _BookEntry(0.010356 - half)
    bin_ob._ask = _BookEntry(0.010356 + half)
    await det._scan_once()
    assert writer.write.await_count == 1  # no new signal

    # SHORT gap opens (b=0.010356, m=0.010359)
    mex_ob._bid = _BookEntry(0.010359 - half)
    mex_ob._ask = _BookEntry(0.010359 + half)
    await det._scan_once()
    assert writer.write.await_count == 2
    assert writer.write.await_args.args[0].direction == "short"


@pytest.mark.asyncio
async def test_unsynced_book_skipped():
    sym = "PENGUUSDT"
    spread = 0.000001
    half = spread / 2
    books = {
        ("binance", sym): _FakeOB(0.010359 - half, 0.010359 + half, synced=False),
        ("mexc",    sym): _FakeOB(0.010356 - half, 0.010356 + half),
    }
    cfg = StaticGapConf(enabled=True, min_gap_ticks=3)
    writer = MagicMock(); writer.write = AsyncMock()
    det = StaticGapDetector(cfg, _FakeOBManager(books), writer)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_no_ob == 1


# REMOVED in stage-4 refactor (2026-05-09):
#   test_require_both_sides_field_is_noop
# The require_both_sides field was removed from StaticGapConf entirely.
# Setting it would now raise TypeError, so the test is no longer applicable.


@pytest.mark.asyncio
async def test_confidence_scales_with_gap():
    """3 ticks → 0.5, 6 ticks → 1.0, 12 ticks → 1.0 (capped)."""
    cases = [
        (0.010359, 0.010356, 0.5),    # 3 ticks
        (0.010362, 0.010356, 1.0),    # 6 ticks
        (0.010368, 0.010356, 1.0),    # 12 ticks (capped)
    ]
    for b_mid, m_mid, expected_conf in cases:
        det, writer, _ = _make_detector(binance_mid=b_mid, mexc_mid=m_mid)
        await det._scan_once()
        sig = writer.write.await_args.args[0]
        assert sig.confidence == pytest.approx(expected_conf, abs=0.01), (
            f"b={b_mid} m={m_mid} expected conf={expected_conf}, got {sig.confidence}"
        )
