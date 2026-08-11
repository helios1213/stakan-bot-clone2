# -*- coding: utf-8 -*-
"""Сесія запобіжника — календарна доба 00:00-00:00, і рестарт її не стирає.

Дві вимоги оператора:
  1. доба рахується з 00:00 до 00:00 (за годинником бота, не UTC);
  2. якщо від півночі не було зростання і день одразу пішов у -$25 —
     теж зупинка, а не тільки просадка від додатного піку.
"""
import datetime
import os

import pytest

from src.execution.live_safety import LiveSafetyController


def ctl(**kw):
    kw.setdefault("max_drawdown_usdt", 25.0)
    return LiveSafetyController(**kw)


# ── 1. доба = локальна північ ────────────────────────────────────────

def test_the_session_starts_at_local_midnight_not_utc():
    """Бот живе в Києві; на UTC-межі «доба» починалась о 03:00 за
    годинником оператора, і денний звіт ніколи не сходився з тим, що
    показував запобіжник."""
    c = ctl()
    start = datetime.datetime.fromtimestamp(c.session_start_ts(), c._tz)
    assert (start.hour, start.minute, start.second) == (0, 0, 0)


def test_the_session_boundary_is_exactly_one_day_ahead():
    c = ctl()
    span = c._daily_reset_at_ts - c.session_start_ts()
    # 23/24/25 годин — доба переходу на літній/зимовий час теж валідна
    assert span in (23 * 3600, 24 * 3600, 25 * 3600), span


def test_the_session_start_is_in_the_past_and_the_reset_in_the_future():
    import time
    c = ctl()
    now = time.time()
    assert c.session_start_ts() <= now < c._daily_reset_at_ts


def test_an_unknown_timezone_falls_back_instead_of_crashing(monkeypatch):
    """Запобіжник не має падати через зіпсовану змінну оточення."""
    monkeypatch.setenv("TZ", "Not/AZone")
    c = ctl()
    assert c.session_start_ts() > 0


# ── 2. падіння від нуля теж зупиняє ──────────────────────────────────

def test_a_day_that_only_ever_loses_still_gets_killed():
    """Головна вимога: пік дня = 0, зростання не було, -$25 → стоп."""
    c = ctl()
    for _ in range(25):
        c.record_close("X", -1.0, notional_usdt=5000.0)   # межа = стеля $25
        if c.is_killed():
            break
    assert c.is_killed()
    assert c.state.peak_pnl == 0.0, "пік не мав ставати відʼємним"
    assert c.state.today_pnl <= -25.0


def test_the_limit_before_any_close_is_the_ceiling_not_zero():
    """Інакше перший же день без історії розміру лишався б без кіла."""
    assert ctl().drawdown_limit() == 25.0


def test_the_flat_limit_is_25_at_any_size():
    """Плоско: $25 незалежно від нотіоналу (%-правила більше немає)."""
    c = ctl()
    c.record_close("X", 0.0, notional_usdt=1000.0)
    assert c.drawdown_limit() == pytest.approx(25.0)
    c.record_close("X", -25.0, notional_usdt=1000.0)
    assert c.is_killed()


# ── 3. рестарт більше не стирає сесію ────────────────────────────────

def test_a_restart_no_longer_erases_the_session_peak():
    """Було: контролер створювався заново, пік $73.88 зникав, і просадка
    рахувалась із нуля — тобто рестарт знімав запобіжник."""
    fresh = ctl()
    fresh.hydrate_session([(80.0, 4000.0), (-6.12, 4000.0)])
    assert fresh.state.today_pnl == pytest.approx(73.88)
    assert fresh.state.peak_pnl == pytest.approx(80.0)
    assert not fresh.is_killed()


def test_hydration_reproduces_exactly_what_the_live_run_would_hold():
    """Відновлення має давати той самий стан, що й послідовність
    record_close — інакше після рестарту межа поїде."""
    live = ctl()
    closes = [(3.0, 4000.0), (-1.0, 3000.0), (7.5, 5000.0), (-2.25, 4500.0)]
    for pnl, n in closes:
        live.record_close("X", pnl, notional_usdt=n)

    restored = ctl()
    restored.hydrate_session(closes)

    for f in ("today_pnl", "peak_pnl", "today_trades",
              "avg_notional_usdt", "consecutive_losses"):
        assert getattr(restored.state, f) == pytest.approx(
            getattr(live.state, f)), f
    assert restored.drawdown_limit() == pytest.approx(live.drawdown_limit())


def test_a_breach_that_was_live_before_the_restart_is_re_engaged():
    """Найважливіше: якщо на момент підняття просадка вже пробита, кіл має
    стояти ОДРАЗУ, а не чекати наступного закриття."""
    c = ctl()
    c.hydrate_session([(30.0, 4000.0), (-26.0, 4000.0)])
    assert c.is_killed()
    assert "відновлено після рестарту" in c.state.kill_reason


def test_hydration_is_idempotent_so_a_rebuild_cannot_double_the_day():
    """rebuild_from_store викликається періодично — другий прохід не має
    додати день ще раз."""
    c = ctl()
    assert c.hydrate_session([(5.0, 4000.0)]) is True
    assert c.hydrate_session([(5.0, 4000.0)]) is False
    assert c.state.today_pnl == pytest.approx(5.0)
    assert c.state.today_trades == 1


def test_hydration_of_an_empty_day_changes_nothing():
    c = ctl()
    assert c.hydrate_session([]) is False
    assert c.state.today_trades == 0
    assert not c.is_killed()


def test_hydration_survives_null_pnl_rows():
    """У базі трапляються рядки з NULL — вони не мають валити підняття слота."""
    c = ctl()
    c.hydrate_session([(None, 4000.0), (2.0, None), (-1.0, 4000.0)])
    assert c.state.today_pnl == pytest.approx(1.0)
    assert c.state.today_trades == 3
