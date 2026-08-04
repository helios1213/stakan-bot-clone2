"""max_spread_ticks — спред-кеп у тіках замість bps.

Спред MEXC завжди ціле число тіків, тож bps-кеп це насправді «≤ N тіків», де N
стрибає сходинками при русі ціни. На TAO (тік $0.01) поріг 2.0 bps дорівнює
price/50 тіків:

    < $150  →  ≤2 тіки
    $191    →  ≤3 тіки     ← сьогодні
    > $200  →  ≤4 тіки
    > $250  →  ≤5 тіків

Тобто ворота перенастроюють себе на русі ціни без жодної правки конфігу — той
самий клас, що min_mexc_lag_pct і вхідна смуга PEPE, обидва виправлені 2026-08-04.

Тести пінять пріоритет (тіки б'ють bps), включність межі й те, що дефолт 0
нічого не міняє для 19 пар, які досі на bps.
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

TICK = 0.01              # TAO
SYM = "TAOUSDT"


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


def _detector(mexc_spread_ticks: float, price: float = 191.0,
              mid_gap_ticks: float = 8.0, min_ticks: int = 5, **ovr):
    """Книга MEXC заданої ширини за заданої ціни; геп широкий, щоб не заважав."""
    m_mid = price
    b_mid = m_mid + mid_gap_ticks * TICK
    bh, mh = TICK / 2, mexc_spread_ticks * TICK / 2
    books = {
        ("binance", SYM): _FakeOB(b_mid - bh, b_mid + bh),
        ("mexc", SYM): _FakeOB(m_mid - mh, m_mid + mh),
    }
    cfg = StaticGapConf(enabled=True, scan_interval_sec=0.05,
                        min_gap_ticks=min_ticks, cooldown_sec=5.0)
    writer = MagicMock()
    writer.write = AsyncMock()
    det = StaticGapDetector(cfg, _FakeOBM(books), writer)
    det._pair_overrides = {SYM: PerPairDetectorOverride(min_gap_ticks=min_ticks, **ovr)}
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
async def test_spread_above_the_tick_cap_is_rejected():
    det, writer = _detector(mexc_spread_ticks=4.0, max_spread_ticks=3.0)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_wide_spread_t == 1


@pytest.mark.asyncio
async def test_the_tick_cap_is_inclusive():
    """Спред живе на цілій сітці, тож «3» мусить лишити рівно 3 тіки."""
    det, writer = _detector(mexc_spread_ticks=3.0, max_spread_ticks=3.0)
    await det._scan_once()
    assert writer.write.await_count == 1
    assert det.signals_skip_wide_spread_t == 0


@pytest.mark.asyncio
async def test_ticks_win_over_bps():
    """Обидва задані, тіки суворіші — має спрацювати тіковий і його лічильник."""
    det, writer = _detector(mexc_spread_ticks=4.0,
                            max_spread_ticks=3.0, max_spread_bps=99.0)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_wide_spread_t == 1
    assert det.signals_skip_wide_spread == 0


@pytest.mark.asyncio
async def test_ticks_win_even_when_looser_than_bps():
    """Пріоритет безумовний: якщо тіки задані, bps не оцінюється взагалі.

    Інакше пара, що переходить на тіки, тихо лишалась би під двома воротами.
    """
    det, writer = _detector(mexc_spread_ticks=4.0,
                            max_spread_ticks=5.0, max_spread_bps=0.01)
    await det._scan_once()
    assert writer.write.await_count == 1, "bps не мав оцінюватись"
    assert det.signals_skip_wide_spread == 0


@pytest.mark.asyncio
async def test_zero_ticks_falls_back_to_bps_unchanged():
    """19 пар досі на bps — дефолт 0 не сміє нічого змінити."""
    det, writer = _detector(mexc_spread_ticks=4.0, price=191.0,
                            max_spread_ticks=0.0, max_spread_bps=2.0)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_wide_spread == 1
    assert det.signals_skip_wide_spread_t == 0


@pytest.mark.asyncio
async def test_both_zero_is_off():
    det, writer = _detector(mexc_spread_ticks=9.0,
                            max_spread_ticks=0.0, max_spread_bps=0.0)
    await det._scan_once()
    assert writer.write.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("price,bps_admits,ticks_admits", [
    (149.0, 2, 3),     # bps-поріг тут «≤2 тіки»
    (191.0, 3, 3),     # сьогодні збігаються
    (205.0, 4, 3),     # bps роз'їхався до «≤4», тіки тримають 3
])
async def test_bps_cap_drifts_with_price_and_ticks_do_not(price, bps_admits, ticks_admits):
    """Суть ключа: за тієї самої ціни й тієї самої книги bps і тіки розходяться.

    Перевіряємо на спреді, що дорівнює тому, що ПУСКАЄ bps: тіковий поріг 3
    відкидає його рівно тоді, коли він ширший за 3, незалежно від ціни.
    """
    det_b, w_b = _detector(mexc_spread_ticks=bps_admits, price=price,
                           max_spread_bps=2.0)
    await det_b._scan_once()
    assert w_b.write.await_count == 1, "bps мав пустити свій же поріг"

    det_t, w_t = _detector(mexc_spread_ticks=bps_admits, price=price,
                           max_spread_ticks=3.0)
    await det_t._scan_once()
    assert w_t.write.await_count == (1 if bps_admits <= ticks_admits else 0)


@pytest.mark.asyncio
async def test_counter_reaches_stats():
    det, writer = _detector(mexc_spread_ticks=4.0, max_spread_ticks=3.0)
    await det._scan_once()
    assert det.stats()["signals_skip_wide_spread_t"] == 1
