"""Ключ, якого ця збірка не знає, мусить кричати — а не мовчати.

Спіймано на PEPE 2026-08-03 22:07–22:11. У ямл поїхали нові
`min/max_mid_gap_ticks` і занулились старі `min/max_mexc_lag_pct`, а код із
підтримкою нових ключів ще не був задеплоєний. Стара збірка підхопила файл по
mtime, послухалась нулів (вимкнула гейт) і мовчки проігнорувала невідомі їй
ключі. Чотири хвилини пара торгувала БЕЗ гейту по гепу; у даних це видно як
угода на 6.0t при щойно поставленому потолку 5.5.

Парсери читають через raw.get(...), тому зайвий ключ не дає ні помилки, ні
сліду. Тести пінять, що слід тепер є — і що завантаження при цьому НЕ падає:
ямл із ключем із майбутньої версії мусить пережити відкат образу.
"""
from __future__ import annotations

import logging
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import ConfigLoader

_GLOBAL = """
detector:
  scan_interval_sec: 0.05
  cooldown_sec: 3.0
  min_ticks: 4
exit_strategy:
  stop_adverse_bps: 10
  trail_distance_bps: 3
  breakeven_trigger_bps: 5
"""


def _mk(tmp_path: Path, pair_body: str) -> ConfigLoader:
    (tmp_path / "global.yaml").write_text(_GLOBAL)
    pairs = tmp_path / "pairs"
    pairs.mkdir()
    (pairs / "TESTUSDT.yaml").write_text(textwrap.dedent(pair_body))
    ldr = ConfigLoader(str(tmp_path))
    ldr.load()
    return ldr


def test_unknown_key_is_reported_with_file_and_section(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        ldr = _mk(tmp_path, """
            detector:
              min_ticks: 5
              min_mid_gap_ticks_TYPO: 4.5
            """)
    msgs = [r.getMessage() for r in caplog.records]
    hit = [m for m in msgs if "min_mid_gap_ticks_TYPO" in m]
    assert hit, f"невідомий ключ не згадано в жодному попередженні: {msgs}"
    assert "detector" in hit[0], "секція не названа"
    assert "TESTUSDT" in hit[0], "файл не названий"
    # і при цьому конфіг завантажився
    assert ldr.get("TESTUSDT").detector.min_ticks == 5


def test_unknown_key_does_not_break_loading(tmp_path):
    """Відкат образу не має класти бота на ямлі з ключем нової версії."""
    ldr = _mk(tmp_path, """
        detector:
          min_ticks: 7
          a_key_from_the_future: 123
        execution:
          ioc_offset_ticks: 3
          another_future_key: "abc"
        """)
    c = ldr.get("TESTUSDT")
    assert c.detector.min_ticks == 7
    assert c.execution.ioc_offset_ticks == 3


def test_clean_config_says_nothing(tmp_path, caplog):
    """Жодного шуму на нормальному файлі, інакше попередження перестануть читати."""
    with caplog.at_level(logging.WARNING):
        _mk(tmp_path, """
            detector:
              min_ticks: 5
              min_mid_gap_ticks: 4.5
              max_mid_gap_ticks: 5.5
            execution:
              ioc_offset_ticks: 2
              stop_loss_ticks: 8
            """)
    noisy = [m for m in (r.getMessage() for r in caplog.records)
             if "[CONFIG]" in m]
    assert not noisy, f"попередження на чистому конфігу: {noisy}"


def test_every_section_is_checked(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        _mk(tmp_path, """
            detector:
              bogus_detector_key: 1
            exit_strategy:
              stop_adverse_bps: 10
              bogus_exit_key: 2
            execution:
              bogus_execution_key: 3
            """)
    blob = " ".join(r.getMessage() for r in caplog.records)
    for k in ("bogus_detector_key", "bogus_exit_key", "bogus_execution_key"):
        assert k in blob, f"{k} не спіймано"
