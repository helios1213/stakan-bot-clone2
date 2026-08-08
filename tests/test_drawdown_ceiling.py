"""Просадка: одне число і жодних конкурентів — pct × нотіонал.

Три дефекти, виправлені 2026-08-04:

1. Множник 2.5% ніколи не звірявся з реальністю. По 22 слото-днях просадка як
   частка нотіоналу має медіану 0.46%, p90 0.99%, МАКСИМУМ 1.05% — тобто поріг
   стояв у 2.4x вище за все спостережуване і не спрацював ЖОДНОГО разу за всю
   глибину логів. На слоті PEPE це давало межу $116 при позиції $4650.

2. min() проти env-стелі не існувало: LIVE_MAX_DRAWDOWN діяв лише поки
   avg_notional <= 0, тобто до першого закриття. Банер обіцяв $20, реально
   стояло $116/$37/$79, і підняття маржі тихо піднімало поріг зупинки.

3. Три числа на одну межу (стеля, множник, підлога) означали, що ніхто не міг
   сказати, яке з них зараз в'яже. Стелю й підлогу видалено.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.execution.live_safety import LiveSafetyController


def _ctl(pct=0.01, ceiling=25.0):
    return LiveSafetyController(max_drawdown_usdt=ceiling,
                                drawdown_pct_of_notional=pct)


def test_the_live_pepe_slot_gets_the_measured_number():
    """1.0% від $4650 = $46.50 (було $116 при множнику 2.5%)."""
    c = _ctl(ceiling=999.0)                     # стелю відсунуто, міряємо відсоток
    c.record_close("1000PEPEUSDT", 0.10, notional_usdt=4650.0)
    assert c.drawdown_limit() == pytest.approx(46.5)


def test_the_live_pengu_slot_scales_down_with_it():
    """Той самий відсоток на меншій позиції: 1.0% від $1480 = $14.80."""
    c = _ctl()
    c.record_close("PENGUUSDT", 0.05, notional_usdt=1480.0)
    assert c.drawdown_limit() == pytest.approx(14.8)


def test_the_ceiling_caps_any_size():
    """Суть повернення стелі 2026-08-08: розмір більше не тягне стоп за собою.

    Маржу PENGU підняли вдвічі — поріг поїхав з ~$22 до $45.63 сам по собі.
    Тепер хоч 40к нотіоналу, поріг лишається $25.
    """
    c = _ctl()
    for _ in range(80):
        c.record_close("X", 0.0, notional_usdt=40000.0)
    assert c.drawdown_limit() == pytest.approx(25.0)


def test_the_percentage_still_rules_below_the_ceiling():
    """Нижче ~$2500 нотіоналу в'яже відсоток, а не стеля — дрібна пара не
    отримує непропорційно широкого стопу."""
    c = _ctl()
    c.record_close("X", 0.0, notional_usdt=1500.0)
    assert c.drawdown_limit() == pytest.approx(15.0)


def test_no_floor():
    """Підлоги немає й далі — межа строго пропорційна знизу.

    ⚠️ Наслідок: HYPE (нотіонал ~$94) отримує межу $0.94. Пара в shadow;
    перед вмиканням у лайв поріг треба переглянути.
    """
    c = _ctl()
    c.record_close("HYPEUSDT", 0.01, notional_usdt=94.0)
    assert c.drawdown_limit() == pytest.approx(0.94)
    assert not hasattr(c, "min_drawdown_usdt"), "підлога не має існувати"


def test_before_the_first_close_the_ceiling_protects():
    """Розмір ще невідомий — діє стеля. Раніше тут було 0 (кіла немає взагалі);
    зі стелею захищати з першої хвилини дешевше, ніж не захищати."""
    c = _ctl()
    assert c.state.avg_notional_usdt <= 0
    assert c.drawdown_limit() == pytest.approx(25.0)


def test_the_first_close_arms_it_immediately():
    """record_close оновлює avg_notional ПЕРЕД перевіркою, тож дірки немає."""
    c = _ctl()
    c.state.peak_pnl = 100.0
    c.record_close("X", -100.0, notional_usdt=2000.0)   # 200 від піку > $20
    assert c.state.kill_active is True


def test_state_summary_reports_the_effective_limit():
    c = _ctl()
    c.record_close("1000PEPEUSDT", 0.10, notional_usdt=4650.0)
    s = c.state_summary()
    assert s["drawdown_limit_usdt"] == pytest.approx(25.0)
    assert "стеля" in s["drawdown_limit_basis"]
    assert s["avg_notional_usdt"] == pytest.approx(4650.0)


def test_changing_the_regime_is_an_env_edit_not_a_code_edit():
    """Єдиний важіль — LIVE_DRAWDOWN_PCT_OF_NOTIONAL."""
    c = _ctl(pct=0.025, ceiling=999.0)          # старий режим, стеля відсунута
    c.record_close("1000PEPEUSDT", 0.10, notional_usdt=4650.0)
    assert c.drawdown_limit() == pytest.approx(116.25)
