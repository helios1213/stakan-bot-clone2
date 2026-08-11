# -*- coding: utf-8 -*-
"""Плоский сукупний денний кіл + вимкнення %-просадки.

Для тесту SOXL оператор попросив: «вимкни стоп у %, нехай буде просто загальний
в -25». Тобто peak-drawdown вимкнено, лишається один плоский поріг на сукупний
денний PnL, розмір-незалежний.
"""
import pytest

from src.execution.live_safety import LiveSafetyController


def test_flat_daily_kill_fires_on_cumulative_loss_regardless_of_size():
    """Головне: кіл на сукупному −$25, і на $100 нотіоналі, і на $5000 — байдуже."""
    for notional in (100.0, 5000.0):
        c = LiveSafetyController(max_drawdown_usdt=0.0,
                                 drawdown_pct_of_notional=0.0,
                                 daily_loss_kill_usdt=-25.0)
        # тонкими збитками до −$25
        for _ in range(300):
            c.record_close("SOXLUSDT", -0.10, notional_usdt=notional)
            if c.is_killed():
                break
        assert c.is_killed()
        assert c.state.today_pnl <= -25.0
        assert "денний" in c.state.kill_reason


def test_peak_drawdown_is_off_when_ceiling_is_zero():
    """Просадка від піку НЕ вбиває, коли стеля 0 — навіть велика віддача від піку."""
    c = LiveSafetyController(max_drawdown_usdt=0.0,
                             drawdown_pct_of_notional=0.0,
                             daily_loss_kill_usdt=-25.0)
    c.record_close("SOXLUSDT", +20.0, notional_usdt=125.0)   # пік +20
    c.record_close("SOXLUSDT", -18.0, notional_usdt=125.0)   # віддав 18 від піку
    assert not c.is_killed(), "peak-drawdown спрацював, хоча має бути вимкнений"
    assert c.state.today_pnl == pytest.approx(2.0)


def test_a_green_day_that_never_hits_minus_25_is_not_killed():
    """Саме те, що дратувало раніше: зелений день не має спинятись."""
    c = LiveSafetyController(max_drawdown_usdt=0.0,
                             drawdown_pct_of_notional=0.0,
                             daily_loss_kill_usdt=-25.0)
    c.record_close("SOXLUSDT", +50.0, notional_usdt=125.0)
    c.record_close("SOXLUSDT", -24.0, notional_usdt=125.0)   # день усе ще +26
    assert not c.is_killed()


def test_daily_kill_off_by_default_keeps_old_peak_drawdown_behaviour():
    """Регресія: без daily kill (=0) і зі стелею — стара %-просадка як була."""
    c = LiveSafetyController(max_drawdown_usdt=25.0,
                             drawdown_pct_of_notional=0.01)
    assert c.daily_loss_kill_usdt == 0.0
    c.record_close("X", +30.0, notional_usdt=5000.0)   # пік +30, межа min(25,50)=25
    c.record_close("X", -26.0, notional_usdt=5000.0)   # просадка 26 >= 25
    assert c.is_killed()
    assert "from session peak" in c.state.kill_reason


def test_summary_reports_the_daily_kill_not_a_misleading_zero():
    """Алерт reset читає drawdown_limit_usdt — при вимкненій %-просадці він має
    показувати денний поріг, а не «межа $0»."""
    c = LiveSafetyController(max_drawdown_usdt=0.0,
                             drawdown_pct_of_notional=0.0,
                             daily_loss_kill_usdt=-25.0)
    s = c.state_summary()
    assert s["drawdown_limit_usdt"] == 25.0
    assert "денний" in s["drawdown_limit_basis"]
    assert s["daily_loss_kill_usdt"] == -25.0


def test_both_kills_can_coexist():
    """Можна лишити %-просадку І додати денний поріг — б'є той, що перший."""
    c = LiveSafetyController(max_drawdown_usdt=25.0,
                             drawdown_pct_of_notional=0.01,
                             daily_loss_kill_usdt=-25.0)
    s = c.state_summary()
    # стеля активна до першого закриття
    assert s["drawdown_limit_usdt"] == 25.0
    assert "денний" in s["drawdown_limit_basis"]


def test_no_kill_at_all_is_reported_honestly():
    c = LiveSafetyController(max_drawdown_usdt=0.0,
                             drawdown_pct_of_notional=0.0,
                             daily_loss_kill_usdt=0.0)
    assert c.state_summary()["drawdown_limit_usdt"] == 0.0
    assert "вимкнено" in c.state_summary()["drawdown_limit_basis"]
    # і жодне закриття не вмикає кіл
    c.record_close("X", -1000.0, notional_usdt=125.0)
    assert not c.is_killed()
