"""signal_features має описувати ті ворота, які справді стоять на шляху.

Рекордер штампує passes_gate за одним min_ticks і комітить рядок ДО решти
воріт, тому все, що відкидають max_spread_bps / min_mid_gap_ticks /
min_exec_ticks / long_only / short_only, лежало в таблиці як "пройшло".
Перезалік вимірювався: 52.8% рядків на PEPE, 41.4% на TAO, 100% на SHIB.

Це важливо не саме по собі — цією таблицею обґрунтовуються живі зміни. Якщо
знаменник більший за реальну популяцію, зміна, що працює, виглядає інертною.

Тести пінять, що rejected_by ставиться, що passes_gate падає разом із ним, і
що кулдаун — окремий випадок: ворота він ПРОЙШОВ.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy.signal_recorder import SignalRecorder
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


def _detector(mid_gap_ticks: float, mexc_spread_ticks: float = 1.0,
              binance_spread_ticks: float = 1.0, min_ticks: int = 3,
              **ovr):
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
        min_gap_ticks=min_ticks, **ovr)}
    det._rebuild_fast_paths()

    db = MagicMock()
    db.execute = AsyncMock()
    db.executemany = AsyncMock()
    det._recorder = SignalRecorder(db, enabled=True, record_min_ticks=0.5)
    return det, writer, det._recorder


def _feat(rec):
    """Єдиний записаний кандидат."""
    bucket = rec._pending.get(SYM) or {}
    assert len(bucket) == 1, f"очікував 1 кандидата, маю {len(bucket)}"
    return next(iter(bucket.values()))["feat"]


@pytest.fixture(autouse=True)
def _patch_tick():
    with patch("src.strategy.static_gap_detector.get_tick_size", return_value=TICK), \
         patch("src.exchanges.mexc_rest.get_binance_scale", return_value=1.0), \
         patch("src.strategy.static_gap_detector.to_mexc",
               side_effect=lambda s: s.replace("USDT", "_USDT")):
        yield


@pytest.mark.asyncio
async def test_signal_that_passes_everything_is_recorded_as_passing():
    det, writer, rec = _detector(mid_gap_ticks=4.0, min_ticks=3,
                                 min_exec_ticks=0.5, min_mid_gap_ticks=2.25)
    await det._scan_once()
    assert writer.write.await_count == 1
    f = _feat(rec)
    assert f["passes_gate"] == 1
    assert f["rejected_by"] is None


@pytest.mark.asyncio
async def test_exec_gate_rejection_is_written_into_the_row():
    """Геп 4 тіки крізь книгу 7 тіків: min_ticks і мід-поріг пускають, exec=0 ні.

    Саме цей рядок раніше лягав у таблицю як passes_gate=1.
    """
    det, writer, rec = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=7.0,
                                 min_ticks=3, min_exec_ticks=0.5,
                                 min_mid_gap_ticks=2.25)
    await det._scan_once()
    assert writer.write.await_count == 0
    f = _feat(rec)
    assert f["passes_gate"] == 0
    assert f["rejected_by"] == "min_exec_ticks"
    assert det.signals_skip_no_exec_edge == 1


@pytest.mark.asyncio
async def test_mid_gap_gate_rejection_is_attributed_to_itself():
    det, writer, rec = _detector(mid_gap_ticks=2.0, min_ticks=1,
                                 min_mid_gap_ticks=2.25)
    await det._scan_once()
    assert writer.write.await_count == 0
    f = _feat(rec)
    assert f["passes_gate"] == 0
    assert f["rejected_by"] == "min_mid_gap_ticks"


@pytest.mark.asyncio
async def test_spread_gate_rejection_is_attributed_to_itself():
    det, writer, rec = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=9.0,
                                 min_ticks=3, max_spread_bps=1.0)
    await det._scan_once()
    assert writer.write.await_count == 0
    f = _feat(rec)
    assert f["passes_gate"] == 0
    assert f["rejected_by"] == "max_spread_bps"


@pytest.mark.asyncio
async def test_direction_filter_has_its_own_counter_and_reason():
    """short_only проти LONG-сигналу. Раніше це рахувалось як below_threshold,
    тобто три різні причини зливались в один лічильник."""
    det, writer, rec = _detector(mid_gap_ticks=4.0, min_ticks=3, short_only=True)
    await det._scan_once()
    assert writer.write.await_count == 0
    assert det.signals_skip_direction_filter == 1
    assert det.signals_skip_below_threshold == 0
    f = _feat(rec)
    assert f["passes_gate"] == 0
    assert f["rejected_by"] == "short_only"


@pytest.mark.asyncio
async def test_cooldown_does_not_clear_passes_gate():
    """Кулдаун — анти-дублікат, а не ворота. Сигнал ворота ПРОЙШОВ, тому
    passes_gate лишається 1, інакше дві причини зіллються в одну."""
    det, writer, rec = _detector(mid_gap_ticks=4.0, min_ticks=3)
    await det._scan_once()
    assert writer.write.await_count == 1
    rec._pending.clear()                      # перший кандидат нам не потрібен
    rec._last_record_ms.clear()
    await det._scan_once()                    # той самий напрямок → кулдаун
    assert writer.write.await_count == 1, "другий сигнал мав впертись у кулдаун"
    assert det.signals_skip_cooldown == 1
    f = _feat(rec)
    assert f["passes_gate"] == 1, "кулдаун не є гейтом переваги"
    assert f["rejected_by"] == "cooldown"


@pytest.mark.asyncio
async def test_exec_ticks_is_stored():
    """Детектор рахував exec і нікуди не зберігав — колонки не було взагалі."""
    det, writer, rec = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=3.0,
                                 min_ticks=3)
    await det._scan_once()
    # mid 4, книга MEXC 3, Binance 1 → exec = 4 - (3+1)/2 = 2
    assert _feat(rec)["exec_ticks"] == pytest.approx(2.0, abs=0.01)


@pytest.mark.asyncio
async def test_mark_rejected_ignores_a_stale_timestamp():
    """Якщо record() пропустив запис через dedup, ворота НЕ мають зіпсувати
    старіший рядок — звірка йде по точному ts."""
    det, writer, rec = _detector(mid_gap_ticks=4.0, min_ticks=3)
    await det._scan_once()
    f = _feat(rec)
    assert f["rejected_by"] is None
    rec.mark_rejected(SYM, 1, "min_exec_ticks")      # ts, якого не було
    assert f["rejected_by"] is None
    assert f["passes_gate"] == 1


@pytest.mark.asyncio
async def test_gate_counters_reach_stats_and_the_log_line():
    """Лічильники трьох найновіших воріт не було ні в stats(), ні в логах —
    тобто налаштоване значення не мало рантайм-підтвердження."""
    det, writer, rec = _detector(mid_gap_ticks=4.0, mexc_spread_ticks=7.0,
                                 min_ticks=3, min_exec_ticks=0.5)
    await det._scan_once()
    s = det.stats()
    for k in ("signals_skip_wide_spread", "signals_skip_narrow_mid_gap",
              "signals_skip_no_exec_edge", "signals_skip_direction_filter"):
        assert k in s, f"{k} не потрапляє у stats()"
    assert s["signals_skip_no_exec_edge"] == 1
    det._log_gate_skips()                     # не має падати й має бути ідемпотентним
    assert det._gate_skips_prev["no_exec_edge"] == 1
