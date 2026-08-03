"""min_exec_ticks — the floor on the edge we can actually transact at.

exec is the dislocation net of BOTH books' spreads:

    exec_buy_ticks  = (binance_bid - mexc_ask) / tick     # long
    exec_sell_ticks = (mexc_bid - binance_ask) / tick     # short

min_ticks reads the same-side quote gap and min_mid_gap_ticks reads the mid;
neither subtracts the cost of reaching the touch. So a real dislocation through
a wide MEXC book passes both and still has nothing left to catch — measured on
PENGU, exec<=0 fills average -1.7 to -8.1 bps with a 0-17% win rate.

These tests pin that the three gates are genuinely different quantities, not
three spellings of one.
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
class _Level:
    price: float
    qty: float = 100.0


class _FakeOB:
    def __init__(self, bid: float, ask: float):
        self._bid, self._ask = _Level(bid), _Level(ask)
        self.is_synced = True

    def best_bid(self):
        return self._bid

    def best_ask(self):
        return self._ask

    def best_bid_price(self) -> float:
        return self._bid.price

    def best_ask_price(self) -> float:
        return self._ask.price


class _FakeOBM:
    def __init__(self, books):
        self._b = books

    def all_symbols(self, ex):
        return [s for (e, s) in self._b if e == ex]

    def get(self, ex, sym):
        return self._b.get((ex, sym))


def _detector(mid_gap_ticks: float, mexc_spread_ticks: float,
              binance_spread_ticks: float = 1.0, min_ticks: int = 3,
              min_exec_ticks: float = 0.0, min_mid_gap_ticks: float = 0.0):
    """Books with an exact mid gap and independently chosen spreads."""
    m_mid = 0.006000
    b_mid = m_mid + mid_gap_ticks * TICK
    bh, mh = binance_spread_ticks * TICK / 2, mexc_spread_ticks * TICK / 2
    books = {
        ("binance", SYM): _FakeOB(b_mid - bh, b_mid + bh),
        ("mexc", SYM): _FakeOB(m_mid - mh, m_mid + mh),
    }
    cfg = StaticGapConf(enabled=True, scan_interval_sec=0.05,
                        min_gap_ticks=min_ticks, cooldown_sec=5.0)
    writer = MagicMock()
    writer.write = AsyncMock()
    det = StaticGapDetector(cfg, _FakeOBM(books), writer)
    det._pair_overrides = {SYM: PerPairDetectorOverride(
        min_gap_ticks=min_ticks, min_exec_ticks=min_exec_ticks,
        min_mid_gap_ticks=min_mid_gap_ticks)}
    det._rebuild_fast_paths()
    return det, writer


@pytest.fixture(autouse=True)
def _patch_tick():
    with patch("src.strategy.static_gap_detector.get_tick_size", return_value=TICK), \
         patch("src.exchanges.mexc_rest.get_binance_scale", return_value=1.0), \
         patch("src.strategy.static_gap_detector.to_mexc",
               side_effect=lambda s: s.replace("USDT", "_USDT")):
        yield


@pytest.mark.asyncio
async def test_identity_exec_is_mid_gap_minus_half_spreads():
    """The quantity the gate rests on, checked against the detector itself.

    mid 4, MEXC book 3 wide, Binance 1 wide:
        touch gap = 4 + (3-1)/2 = 5
        exec      = 4 - (3+1)/2 = 2
    """
    det, writer = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=3.0)
    await det._scan_once()
    assert writer.write.await_count == 1
    md = writer.write.await_args.args[0].metadata
    assert md["mid_gap_ticks"] == pytest.approx(4.0, abs=0.01)
    assert md["gap_ticks"] == pytest.approx(5.0, abs=0.01)
    assert md["exec_ticks"] == pytest.approx(2.0, abs=0.01)


@pytest.mark.asyncio
async def test_wide_book_kills_the_edge_and_the_gate_catches_it():
    """A real 4-tick dislocation through a 7-tick book: exec = 0.
    min_ticks passes it (touch gap 7) and so does a mid floor. Only exec sees it.
    """
    det, writer = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=7.0,
                            min_ticks=3, min_exec_ticks=0.5,
                            min_mid_gap_ticks=2.25)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_no_exec_edge == 1


@pytest.mark.asyncio
async def test_the_other_two_gates_would_have_let_it_through():
    """Same books, exec gate off — proving the other gates do not catch it, so
    the three are different quantities rather than three spellings of one."""
    det, writer = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=7.0,
                            min_ticks=3, min_exec_ticks=0.0,
                            min_mid_gap_ticks=2.25)
    await det._scan_once()
    assert writer.write.await_count == 1, "min_ticks + mid floor let it pass"
    assert writer.write.await_args.args[0].metadata["exec_ticks"] == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_narrow_book_with_the_same_gap_passes():
    """The same 4-tick dislocation through a 1-tick book: exec = 3, keep it."""
    det, writer = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=1.0,
                            min_ticks=3, min_exec_ticks=0.5)
    await det._scan_once()
    assert writer.write.await_count == 1
    assert det.signals_skip_no_exec_edge == 0


@pytest.mark.asyncio
async def test_gate_is_direction_agnostic():
    """A SHORT with mirrored geometry is judged on exec_sell_ticks."""
    det, writer = _detector(mid_gap_ticks=-4.0, mexc_spread_ticks=7.0,
                            min_ticks=3, min_exec_ticks=0.5)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_no_exec_edge == 1


@pytest.mark.asyncio
async def test_zero_disables_the_gate():
    """Default must not change behaviour on any pair that has not opted in."""
    det, writer = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=9.0,
                            min_ticks=3, min_exec_ticks=0.0)
    await det._scan_once()
    assert writer.write.await_count == 1
    assert det.signals_skip_no_exec_edge == 0


@pytest.mark.asyncio
async def test_threshold_sits_in_the_empty_band():
    """exec is integer-tick in practice, so a 0.5 floor must keep exec=1.

    This is why the live value is 0.5 and not 1.0: on the primary the exec=1
    level earns +1.345 bps and carries 38% of the flow.
    """
    det, writer = _detector(mid_gap_ticks=3.0, mexc_spread_ticks=3.0,
                            min_ticks=3, min_exec_ticks=0.5)
    await det._scan_once()
    md = writer.write.await_args.args[0].metadata if writer.write.await_count else None
    assert writer.write.await_count == 1, "exec=1 must survive a 0.5 floor"
    assert md["exec_ticks"] == pytest.approx(1.0, abs=0.01)
