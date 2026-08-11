# -*- coding: utf-8 -*-
"""Єдиний кіл: плоский $25 від піку сесії. %-правило й денний кіл видалені.

Оператор 2026-08-11: «просто стоп -$25 і тільки він, від піку сесії, а стоп у %
видалити взагалі». Цей файл замінює test_drawdown_ceiling / test_notional_basis
/ test_flat_daily_kill, які перевіряли старі механізми.
"""
import pytest

from src.execution.live_safety import LiveSafetyController


def ctl(ceiling=25.0):
    return LiveSafetyController(max_drawdown_usdt=ceiling)


def test_the_only_kill_is_peak_drawdown_flat_25():
    c = ctl()
    c.record_close("X", +40.0, notional_usdt=3000.0)      # пік +40
    c.record_close("X", -24.0, notional_usdt=3000.0)      # 24 від піку — живий
    assert not c.is_killed()
    c.record_close("X", -2.0, notional_usdt=3000.0)       # 26 від піку — стоп
    assert c.is_killed()
    assert "from session peak" in c.state.kill_reason


def test_limit_is_25_regardless_of_notional():
    for n in (94.0, 1000.0, 3000.0, 15000.0):
        c = ctl()
        c.record_close("X", 0.0, notional_usdt=n)
        assert c.drawdown_limit() == 25.0, f"нотіонал {n} → межа має бути $25"


def test_no_percentage_attributes_remain():
    """%-правило видалене з коду — контролер не має цих полів."""
    c = ctl()
    assert not hasattr(c, "drawdown_pct_of_notional")
    assert not hasattr(c, "daily_loss_kill_usdt")


def test_a_green_day_stops_only_after_giving_back_25_from_peak():
    """Peak-drawdown за природою спиняє й на зеленому — але лише коли віддано
    $25 від максимуму. Це усвідомлений вибір оператора."""
    c = ctl()
    c.record_close("X", +100.0, notional_usdt=3000.0)     # пік +100
    c.record_close("X", -24.0, notional_usdt=3000.0)      # день +76, віддав 24
    assert not c.is_killed()
    c.record_close("X", -2.0, notional_usdt=3000.0)       # віддав 26 — стоп (день ще +74)
    assert c.is_killed()


def test_a_pure_loss_day_stops_at_25_from_zero_peak():
    """Пік стартує з 0; якщо росту не було, просадка = сам збиток."""
    c = ctl()
    for _ in range(30):
        c.record_close("X", -1.0, notional_usdt=3000.0)
        if c.is_killed():
            break
    assert c.is_killed()
    assert c.state.peak_pnl == 0.0
    assert c.state.today_pnl <= -25.0


def test_summary_reports_the_flat_stop_honestly():
    s = ctl().state_summary()
    assert s["drawdown_limit_usdt"] == 25.0
    assert "від піку" in s["drawdown_limit_basis"]
    assert "daily_loss_kill_usdt" not in s


def test_zero_ceiling_means_no_kill():
    c = ctl(ceiling=0.0)
    s = c.state_summary()
    assert s["drawdown_limit_usdt"] == 0.0
    assert "вимкнено" in s["drawdown_limit_basis"]
    c.state.peak_pnl = 1000.0
    c.record_close("X", -1000.0, notional_usdt=3000.0)
    assert not c.is_killed()
