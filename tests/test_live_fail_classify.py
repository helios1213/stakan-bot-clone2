# -*- coding: utf-8 -*-
"""Як бот говорить про невдале відкриття live-позиції.

Приводом став скріншот від оператора: «LIVE FAILED — Unknown error» кожні
п'ять хвилин, і в тексті — `safety_blocked: kill_active`, тобто НАША власна
зупинка. Дві вади в одному алерті: він занадто частий і він бреше про
природу події.
"""
import pytest

from src.strategy.shadow_engine import classify_live_fail

KILL = ("safety_blocked: kill_active: drawdown $24.96 from session peak "
        "$8.05 (limit $24.02)")


def cls(raw: str, sym: str = "1000PEPEUSDT") -> dict:
    # err_msg у бойовому коді приходить у нижньому регістрі
    return classify_live_fail(raw.lower(), raw, sym)


# ── власне скарга ────────────────────────────────────────────────────

def test_the_safety_kill_is_not_reported_as_an_unknown_error():
    """Запобіжник спрацював так, як ми його налаштували. Називати це
    «невідомою помилкою» — вчити оператора ігнорувати попередження."""
    r = cls(KILL)
    assert r["kind"] == "safety_kill"
    assert "Unknown" not in r["title"]
    assert r["emoji"] != "⚠️"


def test_the_safety_kill_repeats_at_most_twice_an_hour():
    assert cls(KILL)["throttle"] == 1800


def test_the_safety_kill_says_how_to_lift_it():
    """Перше питання після такого алерту — «і що тепер». Відповідь має бути
    в самому повідомленні."""
    hint = cls(KILL)["hint"]
    assert "Reset kill switch" in hint
    assert "Webkey" in hint


def test_the_safety_kill_keeps_the_numbers_that_explain_it():
    hint = cls(KILL)["hint"]
    assert "24.96" in hint and "24.02" in hint


def test_the_safety_kill_never_pauses_the_pair_by_itself():
    """Кіл зупиняє СЛОТ на 4 години. Ще й переводити пару в pause означало б
    зупинити її на всіх слотах — наслідок, якого ніхто не просив."""
    assert cls(KILL)["auto_pause"] is False


def test_an_unrecognised_error_also_drops_to_half_hourly():
    """Про це й просив оператор: «раз в 30 хв»."""
    r = cls("some brand new failure nobody has seen")
    assert r["kind"] == "unknown"
    assert r["throttle"] == 1800


# ── решта таблиці не зламалась ───────────────────────────────────────

@pytest.mark.parametrize("raw,kind,auto_pause", [
    ("api_error_6026 face verification required", "risk_control", True),
    ("insufficient balance for margin", "insufficient_balance", False),
    ("api_error_6017", "insufficient_balance", False),
    ("exception: something blew up in our own code", "internal_exception", False),
])
def test_the_known_categories_still_classify(raw, kind, auto_pause):
    r = cls(raw)
    assert r["kind"] == kind
    assert r["auto_pause"] is auto_pause


def test_only_the_risk_control_case_pauses_the_pair():
    """Авто-пауза — найважчий наслідок у таблиці. Вона має лишатись рівно
    там, де оператор мусить піти на біржу руками."""
    paused = [k for k in (
        KILL, "api_error_6026", "insufficient balance", "api_error_510",
        "connection reset by peer", "exception: boom",
        "webkey invalid", "unknown gibberish",
    ) if cls(k)["auto_pause"]]
    assert paused == ["api_error_6026"]


def test_every_category_carries_a_hint_and_a_throttle():
    """Алерт без підказки — це шум; алерт без тротлу — це спам."""
    for raw in (KILL, "api_error_6026", "insufficient balance",
                "exception: boom", "unknown gibberish"):
        r = cls(raw)
        assert r["hint"], raw
        assert r["throttle"] > 0, raw


def test_nothing_alerts_more_often_than_every_five_minutes():
    """Нижня межа для всієї таблиці — щоб новий рядок не приніс спам знову."""
    for raw in (KILL, "api_error_6026", "insufficient balance",
                "api_error_510", "connection reset", "exception: boom",
                "unknown gibberish"):
        assert cls(raw)["throttle"] >= 300, raw
