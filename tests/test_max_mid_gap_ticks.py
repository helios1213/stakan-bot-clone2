"""max_mid_gap_ticks — тіковий потолок мід-гепу.

Тіковий мінімум був, потолка не було, тому пару, обмежену зверху в bps
(PEPE: max_mexc_lag_pct 0.02), не можна було перевести в тіки не втративши
верхню межу. А втрачати її не можна: bps-потолок ріже широкі когорти лише
поки ціна низька, і при 0.0030 когорта 6.0t заходить назад сама собою.

Виміряно на PEPE попередньої епохи (єдиної, де ці когорти торгувались):
6.0t −$108.88 WR 32.2%, 7.0t −$57.38 WR 25.0%, 8.0t −$5.82 WR 31.7%.

Тести пінять межу як ВКЛЮЧНУ (когорти лежать точно на пів-тіках, тож
поріг 5.5 мусить лишити 5.5) і незалежність від мінімуму.
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

TICK = 0.0000001          # 1000PEPE: contract 1e-10 × scale 1000
SYM = "1000PEPEUSDT"


@dataclass
class _Level:
    price: float
    size: float = 100.0


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


def _detector(mid_gap_ticks: float, min_ticks: int = 1, **ovr):
    """Книги з ТОЧНИМ мід-гепом; спреди по 1 тіку з обох боків."""
    m_mid = 0.0029000
    b_mid = m_mid + mid_gap_ticks * TICK
    h = TICK / 2
    books = {
        ("binance", SYM): _FakeOB(b_mid - h, b_mid + h),
        ("mexc", SYM): _FakeOB(m_mid - h, m_mid + h),
    }
    cfg = StaticGapConf(enabled=True, scan_interval_sec=0.05,
                        min_gap_ticks=min_ticks, cooldown_sec=5.0)
    writer = MagicMock()
    writer.write = AsyncMock()
    det = StaticGapDetector(cfg, _FakeOBM(books), writer)
    det._pair_overrides = {SYM: PerPairDetectorOverride(
        min_gap_ticks=min_ticks, **ovr)}
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
async def test_gap_above_the_ceiling_is_rejected():
    det, writer = _detector(mid_gap_ticks=6.0, max_mid_gap_ticks=5.5)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_wide_mid_gap == 1


@pytest.mark.asyncio
async def test_the_ceiling_is_inclusive():
    """Когорти лежать точно на пів-тіках, тому поріг 5.5 МУСИТЬ лишити 5.5:
    інакше правка «поставив 5.5» тихо ріже те, що мала зберегти."""
    det, writer = _detector(mid_gap_ticks=5.5, max_mid_gap_ticks=5.5)
    await det._scan_once()
    assert writer.write.await_count == 1
    assert det.signals_skip_wide_mid_gap == 0


@pytest.mark.asyncio
async def test_zero_disables_the_ceiling():
    """Дефолт не має міняти поведінку на жодній парі, що не підписалась."""
    det, writer = _detector(mid_gap_ticks=12.0, max_mid_gap_ticks=0.0)
    await det._scan_once()
    assert writer.write.await_count == 1
    assert det.signals_skip_wide_mid_gap == 0


@pytest.mark.asyncio
async def test_ceiling_works_without_a_floor():
    """Потолок незалежний від min_mid_gap_ticks — інакше пара, якій потрібен
    лише верх, мусила б вигадувати фіктивний низ."""
    det, writer = _detector(mid_gap_ticks=9.0, min_mid_gap_ticks=0.0,
                            max_mid_gap_ticks=5.5)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_wide_mid_gap == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("gap,passes,skip_lo,skip_hi", [
    (4.0, False, 1, 0),     # нижче підлоги — ріже мінімум
    (4.5, True,  0, 0),     # рівно підлога — лишається
    (5.0, True,  0, 0),     # прибуткова когорта PEPE (+$845 за епоху)
    (5.5, True,  0, 0),     # рівно потолок — лишається
    (6.0, False, 0, 1),     # вище потолка — ріже максимум
])
async def test_the_pepe_band_keeps_exactly_the_cohorts_it_should(
        gap, passes, skip_lo, skip_hi):
    """Смуга [4.5, 5.5] у тіках — саме те, що ставиться на PEPE.

    Заміняє bps-смугу [0.013%, 0.02%], яка в тіках пливе: на 0.0036 це було
    [4.68t, 7.19t], на 0.00295 стало [3.83t, 5.90t] — півтора тіка зсуву без
    жодної правки конфігу.
    """
    det, writer = _detector(mid_gap_ticks=gap, min_mid_gap_ticks=4.5,
                            max_mid_gap_ticks=5.5)
    await det._scan_once()
    assert writer.write.await_count == (1 if passes else 0)
    assert det.signals_skip_narrow_mid_gap == skip_lo
    assert det.signals_skip_wide_mid_gap == skip_hi


@pytest.mark.asyncio
async def test_ceiling_is_direction_agnostic():
    """Мід-геп береться за модулем, тож SHORT судиться так само."""
    det, writer = _detector(mid_gap_ticks=-6.0, max_mid_gap_ticks=5.5)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_wide_mid_gap == 1


@pytest.mark.asyncio
async def test_counter_reaches_stats():
    det, writer = _detector(mid_gap_ticks=6.0, max_mid_gap_ticks=5.5)
    await det._scan_once()
    assert det.stats()["signals_skip_wide_mid_gap"] == 1
