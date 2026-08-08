# -*- coding: utf-8 -*-
"""/get_config мусить показувати ВЕСЬ конфіг пари.

Попередня версія обіцяла в коментарі «every field ... Nothing curated out», а
насправді мовчки ховала 13 ключів — і серед них усі ворота входу, якими ми
тюнимо. Тому головний тест тут не про верстку, а про повноту: жодне поле
жодної з трьох секцій не має зникнути з екрана, включно з тими, які додадуть
пізніше.
"""
import dataclasses

import pytest

from src.config_loader import (DetectorConfig, ExecutionConfig,
                               ExitStrategyConfig, PairConfig)
from src.telegram_bot.bot import _fmt_pair_config_full


def _pc(**kw) -> PairConfig:
    return PairConfig(
        symbol="TESTUSDT",
        detector=DetectorConfig(**kw.get("d", {})),
        exit_strategy=ExitStrategyConfig(**kw.get("e", {})),
        execution=ExecutionConfig(**kw.get("c", {})),
    )


def test_every_config_field_is_on_screen():
    """Сторож повноти: новий ключ у конфізі не може зникнути з команди."""
    pc = _pc()
    txt = _fmt_pair_config_full("TESTUSDT", pc, live=False)
    missing = [
        f.name
        for obj in (pc.detector, pc.exit_strategy, pc.execution)
        for f in dataclasses.fields(obj)
        if f.name not in txt
    ]
    assert not missing, f"поля зникли з /get_config: {missing}"


def test_the_entry_gates_are_shown_with_their_values():
    """Саме через ці ворота ми тюнимо — вони мусять бути видні з числами."""
    pc = _pc(d={"min_mid_gap_ticks": 4.5, "max_mid_gap_ticks": 5.5,
               "min_exec_ticks": 0.5, "max_spread_ticks": 3.0},
             c={"min_mexc_lag_pct": 0.02})
    txt = _fmt_pair_config_full("TESTUSDT", pc, live=True)
    for key, val in (("min_mid_gap_ticks", "4.5"), ("max_mid_gap_ticks", "5.5"),
                     ("min_exec_ticks", "0.5"), ("max_spread_ticks", "3"),
                     ("min_mexc_lag_pct", "0.02")):
        line = next(ln for ln in txt.splitlines() if key in ln)
        assert val in line, f"{key}: у рядку немає значення {val} → {line!r}"


def test_a_zero_gate_reads_as_disabled_not_as_a_threshold():
    """0 у воротах — це «вимкнено», а не «поріг нуль». Плутанина тут уже
    коштувала нам воріт, які думали, що діють."""
    txt = _fmt_pair_config_full("TESTUSDT", _pc(d={"min_exec_ticks": 0.0}),
                                live=False)
    line = next(ln for ln in txt.splitlines() if "min_exec_ticks" in ln)
    assert "вимкнено" in line, line


def test_a_zero_exit_bps_shows_the_tick_fallback_it_falls_back_to():
    """А ось для виходів 0 НЕ означає «вимкнено» — воно падає на тіки."""
    pc = _pc(e={"trail_distance_bps": 0.0, "trail_distance_ticks": 3})
    txt = _fmt_pair_config_full("TESTUSDT", pc, live=False)
    line = next(ln for ln in txt.splitlines() if "trail_distance_bps" in ln)
    assert "вимкнено" not in line, line
    assert "trail_distance_ticks=3" in line, line


def test_the_slot_override_is_shown_because_the_yaml_size_is_not_what_trades():
    """PENGU 07.08: ямл показував $35-40, слот торгував $95-100, і команда про
    це мовчала. Подвоєння експозиції має бути видно тут."""
    pc = _pc(c={"margin_min_usdt": 35, "margin_max_usdt": 40,
                "leverage_min": 45, "leverage_max": 50})
    txt = _fmt_pair_config_full(
        "TESTUSDT", pc, live=True,
        overrides=[(2, {"margin_min_usdt": 95.0, "margin_max_usdt": 100.0,
                        "leverage_min": 45, "leverage_max": 50})])
    assert "ПЕРЕКРИВАЄ" in txt
    assert "$95-100" in txt
    assert "$4,275-5,000" in txt, "не показано нотіонал, яким реально торгують"


def test_a_slot_that_matches_the_yaml_is_not_flagged_as_a_conflict():
    pc = _pc(c={"margin_min_usdt": 95, "margin_max_usdt": 100,
                "leverage_min": 45, "leverage_max": 50})
    txt = _fmt_pair_config_full(
        "TESTUSDT", pc, live=True,
        overrides=[(2, {"margin_min_usdt": 95.0, "margin_max_usdt": 100.0,
                        "leverage_min": 45, "leverage_max": 50})])
    assert "ПЕРЕКРИВАЄ" not in txt
    assert "слот 2 = ямл" in txt


def test_an_idle_slot_is_marked_so_its_size_is_not_read_as_live():
    pc = _pc(c={"margin_min_usdt": 35, "margin_max_usdt": 40})
    txt = _fmt_pair_config_full(
        "TESTUSDT", pc, live=True,
        overrides=[(1, {"margin_min_usdt": 65.0, "margin_max_usdt": 70.0,
                        "leverage_min": 45, "leverage_max": 50,
                        "_active": False})])
    assert "не торгує" in txt


def test_bps_thresholds_are_translated_into_ticks_when_a_price_is_known():
    """Поріг у bps на фіксованому тіку повзе з ціною — переклад у тіки і є
    та цифра, за якою насправді треба стежити."""
    pc = _pc(d={"max_spread_bps": 1.3})
    txt = _fmt_pair_config_full("TESTUSDT", pc, live=True,
                                tick=1e-7, price=0.00286765)
    line = next(ln for ln in txt.splitlines() if "max_spread_bps" in ln)
    assert "3.7т" in line, line   # 1.3 bps ÷ 0.349 bps/тік
    assert "0.35 bps" in txt      # підпис: скільки коштує тік


def test_no_price_means_no_invented_tick_numbers():
    txt = _fmt_pair_config_full("TESTUSDT", _pc(d={"max_spread_bps": 1.3}),
                                live=True)
    assert "≈" not in txt, "переклад у тіки без ціни — це вигадка"
    assert "1 тік" not in txt


def test_the_message_fits_in_one_telegram_send():
    """4096 — жорсткий ліміт; конфіг пари має вміщатись без розрізання."""
    txt = _fmt_pair_config_full(
        "TESTUSDT", _pc(), live=True, epoch_ts=1785794813,
        overrides=[(1, {"margin_min_usdt": 65.0, "margin_max_usdt": 70.0,
                        "leverage_min": 45, "leverage_max": 50}),
                   (2, {"margin_min_usdt": 95.0, "margin_max_usdt": 100.0,
                        "leverage_min": 45, "leverage_max": 50})],
        tick=1e-7, price=0.00286765)
    assert len(txt) < 4096, len(txt)


def test_the_mode_shown_is_the_runtime_state():
    assert "LIVE" in _fmt_pair_config_full("X", _pc(), live=True)
    assert "SHADOW" in _fmt_pair_config_full("X", _pc(), live=False)
