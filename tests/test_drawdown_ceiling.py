"""Межа просадки: min(стеля, max(підлога, pct × нотіонал)).

Два дефекти, виправлені 2026-08-04:

1. min() проти стелі НЕ БУЛО — env діяв лише поки avg_notional <= 0, тобто до
   першого закриття в процесі. Банер обіцяв $20, реально стояло $116/$37/$79,
   і підняття маржі в slot_pair_sizing тихо піднімало поріг зупинки разом із нею.

2. Множник 2.5% ніколи не звірявся з реальністю. По 22 слото-днях просадка як
   частка нотіоналу має медіану 0.46%, p90 0.99%, МАКСИМУМ 1.05% — тобто поріг
   стояв у 2.4x вище за все спостережуване і не в'язав ніколи. Тепер 1.0%.

Тести пінять усі три межі й те, що виграє тісніша.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.execution.live_safety import LiveSafetyController


def _ctl(max_dd=150.0, pct=0.01, **kw):
    return LiveSafetyController(max_drawdown_usdt=max_dd,
                                drawdown_pct_of_notional=pct, **kw)


def test_pct_gives_the_measured_number_on_the_live_pepe_slot():
    """1.0% від $4650 = $46.50 — робочий поріг, стеля $150 не заважає."""
    c = _ctl()
    c.record_close("1000PEPEUSDT", 0.10, notional_usdt=4650.0)
    assert c.drawdown_limit() == pytest.approx(46.5)


def test_pct_on_the_pengu_slot():
    """1.0% від $1480 = $14.80."""
    c = _ctl()
    c.record_close("PENGUUSDT", 0.05, notional_usdt=1480.0)
    assert c.drawdown_limit() == pytest.approx(14.8)


def test_ceiling_binds_only_on_a_runaway_size():
    """Стеля — не робочий поріг: вона вмикається аж від ~$15k нотіоналу."""
    c = _ctl()
    c.record_close("X", 0.0, notional_usdt=14000.0)
    assert c.drawdown_limit() == pytest.approx(140.0), "ще не стеля"
    for _ in range(80):
        c.record_close("X", 0.0, notional_usdt=40000.0)
    assert c.drawdown_limit() == pytest.approx(150.0), "стеля мусить в'язати"


def test_floor_applies_to_a_small_position():
    """1.0% від $400 = $4, підлога $5 мусить виграти."""
    c = _ctl()
    c.record_close("HYPEUSDT", 0.01, notional_usdt=400.0)
    assert c.drawdown_limit() == pytest.approx(5.0)


def test_micro_position_does_not_produce_a_limit_of_pennies():
    c = _ctl()
    c.record_close("ONDOUSDT", 0.01, notional_usdt=50.0)   # 1.0% = $0.50
    assert c.drawdown_limit() == pytest.approx(5.0)


def test_before_the_first_close_the_ceiling_is_all_we_have():
    """Розмір ще невідомий — діє сама стеля."""
    c = _ctl()
    assert c.state.avg_notional_usdt <= 0
    assert c.drawdown_limit() == pytest.approx(150.0)


def test_raising_size_can_no_longer_raise_the_stop():
    """Суть фікса: маржа вгору більше не тягне поріг зупинки за собою.

    Раніше slot_pair_sizing 95-100 підняв межу з $20 до $116 без жодної
    правки біля файлу безпеки.
    """
    c = _ctl(max_dd=60.0)
    c.record_close("X", 0.0, notional_usdt=500.0)
    for _ in range(80):                     # EMA 0.9/0.1 доводить до великого
        c.record_close("X", 0.0, notional_usdt=50000.0)
    big = c.drawdown_limit()
    assert c.state.avg_notional_usdt > 20000, "нотіонал мав вирости"
    assert big <= 60.0 + 1e-9, f"стеля протекла: {big}"


def test_env_value_is_reported_as_the_effective_limit():
    c = _ctl()
    c.record_close("1000PEPEUSDT", 0.10, notional_usdt=4650.0)
    s = c.state_summary()
    assert s["drawdown_limit_usdt"] == pytest.approx(46.5)
    assert "1.00%" in s["drawdown_limit_basis"]
    assert s["avg_notional_usdt"] == pytest.approx(4650.0)


def test_changing_the_regime_is_an_env_edit_not_a_code_edit():
    """Множник приходить з LIVE_DRAWDOWN_PCT_OF_NOTIONAL."""
    c = _ctl(pct=0.025)                     # старий режим
    c.record_close("1000PEPEUSDT", 0.10, notional_usdt=4650.0)
    assert c.drawdown_limit() == pytest.approx(116.25)
