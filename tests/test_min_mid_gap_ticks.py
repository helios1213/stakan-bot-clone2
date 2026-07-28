"""min_mid_gap_ticks — the mid-gap floor, measured in ticks.

min_ticks reads the SAME-SIDE quote gap, and that quantity decomposes exactly:

    gap_ticks = mid_gap_ticks + (mexc_spread - binance_spread) / 2

so a wide MEXC book carries a signal over the tick gate even when the real
dislocation is a tick smaller. These tests pin the identity, then pin that the
new gate cuts on the mid rather than on the touch.

Ticks and not bps because the mid sits on a half-tick grid: on TAO the 4.0-tick
cohort loses $29.80 at a 38% win rate while every wider one earns, and a bps
threshold would cut a different cohort as the price drifts.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy.static_gap_detector import (
    PerPairDetectorOverride,
    StaticGapConf,
    StaticGapDetector,
)

TICK = 0.000001
SYM = "PENGUUSDT"


@dataclass
class _BookEntry:
    price: float
    qty: float = 100.0


class _FakeOB:
    def __init__(self, bid: float, ask: float):
        self._bid = _BookEntry(bid)
        self._ask = _BookEntry(ask)
        self.is_synced = True

    def best_bid(self):
        return self._bid

    def best_ask(self):
        return self._ask

    def best_bid_price(self) -> float:
        return self._bid.price

    def best_ask_price(self) -> float:
        return self._ask.price


class _FakeOBManager:
    def __init__(self, books):
        self._books = books

    def all_symbols(self, exchange: str):
        return [s for (ex, s) in self._books if ex == exchange]

    def get(self, exchange: str, symbol: str):
        return self._books.get((exchange, symbol))


def _detector(mid_gap_ticks: float, mexc_spread_ticks: float,
              binance_spread_ticks: float = 1.0,
              min_ticks: int = 5, min_mid_gap_ticks: float = 0.0):
    """Books with an exact mid gap and independently chosen spreads."""
    m_mid = 0.010000
    b_mid = m_mid + mid_gap_ticks * TICK
    bh = binance_spread_ticks * TICK / 2
    mh = mexc_spread_ticks * TICK / 2
    books = {
        ("binance", SYM): _FakeOB(b_mid - bh, b_mid + bh),
        ("mexc", SYM): _FakeOB(m_mid - mh, m_mid + mh),
    }
    cfg = StaticGapConf(enabled=True, scan_interval_sec=0.05,
                        min_gap_ticks=min_ticks, cooldown_sec=5.0)
    writer = MagicMock()
    writer.write = AsyncMock()
    det = StaticGapDetector(cfg, _FakeOBManager(books), writer)
    det._pair_overrides = {SYM: PerPairDetectorOverride(
        min_gap_ticks=min_ticks, min_mid_gap_ticks=min_mid_gap_ticks)}
    det._rebuild_fast_paths()
    return det, writer


@pytest.fixture(autouse=True)
def _patch_tick_and_scale():
    with patch("src.strategy.static_gap_detector.get_tick_size", return_value=TICK), \
         patch("src.exchanges.mexc_rest.get_binance_scale", return_value=1.0), \
         patch("src.strategy.static_gap_detector.to_mexc",
               side_effect=lambda s: s.replace("USDT", "_USDT")):
        yield


@pytest.mark.asyncio
async def test_identity_tick_gap_is_mid_gap_plus_half_excess_spread():
    """The decomposition the whole gate rests on, checked against the detector."""
    det, writer = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=3.0)
    await det._scan_once()

    assert writer.write.await_count == 1
    md = writer.write.await_args.args[0].metadata
    # mid 4.0 + (3 - 1)/2 = 5.0 on the touch
    assert md["mid_gap_ticks"] == pytest.approx(4.0, abs=0.01)
    assert md["gap_ticks"] == pytest.approx(5.0, abs=0.01)


@pytest.mark.asyncio
async def test_wide_book_carries_a_narrow_dislocation_over_min_ticks():
    """min_ticks=5 admits a 4-tick mid gap when the MEXC book is 3 ticks wide.
    This is the leak the new gate exists to close, so it must be real first."""
    det, writer = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=3.0, min_ticks=5)
    await det._scan_once()
    assert writer.write.await_count == 1, "tick gate should pass it"


@pytest.mark.asyncio
async def test_gate_rejects_the_narrow_mid_gap():
    det, writer = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=3.0,
                            min_ticks=5, min_mid_gap_ticks=4.25)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_narrow_mid_gap == 1


@pytest.mark.asyncio
async def test_gate_passes_the_wider_mid_gap():
    """4.5 ticks of real dislocation clears a 4.25 floor — the cohort we keep."""
    det, writer = _detector(mid_gap_ticks=4.5, mexc_spread_ticks=3.0,
                            min_ticks=5, min_mid_gap_ticks=4.25)
    await det._scan_once()
    assert writer.write.await_count == 1
    assert det.signals_skip_narrow_mid_gap == 0


@pytest.mark.asyncio
async def test_zero_disables_the_gate():
    """Default must not change behaviour anywhere.

    A 1-tick dislocation on a 9-tick book: 1 + (9-1)/2 = 5 ticks on the touch,
    so min_ticks=5 admits it and only the new gate could ever refuse it.
    """
    det, writer = _detector(mid_gap_ticks=1.0, mexc_spread_ticks=9.0,
                            min_ticks=5, min_mid_gap_ticks=0.0)
    await det._scan_once()
    assert writer.write.await_count == 1
    assert det.signals_skip_narrow_mid_gap == 0


@pytest.mark.asyncio
async def test_gate_is_direction_agnostic():
    """A SHORT with the same geometry is cut the same way (mid gap is signed)."""
    det, writer = _detector(mid_gap_ticks=-4.0, mexc_spread_ticks=3.0,
                            min_ticks=5, min_mid_gap_ticks=4.25)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_narrow_mid_gap == 1


@pytest.mark.asyncio
async def test_floor_at_the_boundary_does_not_cut_its_own_cohort():
    """A threshold set ON a cohort must keep it — the gap_eps guard.

    This is why the live value is 4.25 and not 4.5: a floor written exactly at a
    populated half-tick level would be decided by float noise.
    """
    det, writer = _detector(mid_gap_ticks=4.5, mexc_spread_ticks=3.0,
                            min_ticks=5, min_mid_gap_ticks=4.5)
    await det._scan_once()
    assert writer.write.await_count == 1


@pytest.mark.asyncio
async def test_narrow_book_signal_is_untouched():
    """On a 1-tick MEXC book the touch gap IS the mid gap, so nothing changes."""
    det, writer = _detector(mid_gap_ticks=5.0, mexc_spread_ticks=1.0,
                            min_ticks=5, min_mid_gap_ticks=4.25)
    await det._scan_once()
    assert writer.write.await_count == 1
    md = writer.write.await_args.args[0].metadata
    assert md["gap_ticks"] == pytest.approx(md["mid_gap_ticks"], abs=0.01)
