"""ПОВЕДІНКОВИЙ доказ, що shadow-ручки не можуть задушити ЖИВИЙ ордер.

ЧОМУ ЦЕЙ ФАЙЛ ІСНУЄ. Аудит 2026-09-02 показав діру в покритті, і показав її
МУТАНТОМ, а не оглядом: якщо провести `max_book_age_ms` у гілку `if
_is_live_pair:`, живий ордер перестає відправлятися (`dispatched=0`), а **вся
сюїта лишається зеленою** — 1477 passed на primary, 1451 на клоні.

Наявні тести цієї ділянки пінять ФОРМУ, не поведінку: вони шукають підрядки
через `inspect.getsource`, `SRC.rfind`, `src.count(...) == 2`. У докстрінгу
одного з них так і написано — «tests SHAPE, not behaviour».

А `tests/test_live_bypass_realism.py` виконує код, але його фікстура свідомо
повертає `None` з `best_bid()`/`best_ask()`, «so the fast-path expires cleanly
without invoking _open_position». Тобто вона НІКОЛИ не доводить живий шлях до
відправки ордера — саме тому мутант і виживав.

ЩО РОБИТЬ ЦЕЙ ФАЙЛ. Дає книгу зі СПРАВЖНІМИ цінами, тож живий шлях доходить до
`_open_position` (точка відправки), і виставляє ручки у ЗАВІДОМО ЛЕТАЛЬНІ
значення. Потім перевіряє, що ордер усе одно пішов.

**КОНТРОЛЬ ОБОВʼЯЗКОВИЙ.** Без нього тест був би вакуумним: він проходив би й
тоді, коли ручки взагалі ні на що не впливають. Тому кожен тест має пару в
shadow-режимі з ТИМИ САМИМИ значеннями, яка доводить, що ці значення справді
вбивають філ.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.ioc_executor import IOCAttemptResult
from src.execution.realism import RealismProfile
from src.strategy.shadow_engine import PairExecConfig, ShadowEngine
from src.strategy.signal import Signal

# Ручки у значеннях, які МУСЯТЬ убивати симульований філ:
#   1 мс дозволеного віку книги проти книги, якій 60 секунд;
#   1% рівня замість 100%.
LETHAL_MAX_BOOK_AGE_MS = 1
LETHAL_QUEUE_FRAC = 0.01
STALE_BOOK_AGE_MS = 60_000


def _level(price: float, size: float = 1_000_000.0):
    lv = MagicMock()
    lv.price = price
    lv.size = size
    lv.quantity = size
    return lv


def _engine(*, is_live: bool, lethal_knobs: bool, real_simulator: bool = False):
    """Двигун, який ДОХОДИТЬ до відправки — на відміну від фікстури
    `test_live_bypass_realism`, яка навмисно зупиняє шлях раніше."""
    eng = ShadowEngine.__new__(ShadowEngine)

    ob = MagicMock()
    ob.is_synced = True
    ob.mid_price = MagicMock(return_value=0.10000)
    # СПРАВЖНІ рівні — саме це доводить шлях до `_open_position`.
    ob.best_bid = MagicMock(return_value=_level(0.09999))
    ob.best_ask = MagicMock(return_value=_level(0.10001))
    # Симулятор ходить по ПРИВАТНИХ драбинах `_asks`/`_bids` (не по `asks`).
    # Спіймано контрольним тестом: із публічними іменами він бачив порожню
    # книгу і «протухав» із причини `no_asks`, тобто тест проходив би з
    # ХИБНОЇ причини.
    ob._asks = {0.10001: 1_000_000.0}
    ob._bids = {0.09999: 1_000_000.0}
    # Книга навмисно ПРОТУХЛА: якби вік перевірявся на живому шляху, ордер помер би.
    ob.last_update_ts_ms = int(time.time() * 1000) - STALE_BOOK_AGE_MS
    eng.ob_manager = MagicMock(get=MagicMock(return_value=ob))

    eng.state_manager = MagicMock(is_in_live=MagicMock(return_value=is_live))

    if real_simulator:
        from src.execution.ioc_executor import IOCExecutor
        eng.ioc_executor = IOCExecutor()
    else:
        eng.ioc_executor = MagicMock(simulate_ioc_entry=MagicMock(
            return_value=IOCAttemptResult(status="expired", target_price=0.10001,
                                          expired_reason="stub")))

    eng.realism = RealismProfile(signal_to_order_latency_ms=0,
                                 base_rejection_rate=0.0, server_error_rate=0.0)
    eng._latency_enabled = False
    eng._latency_min_ms = 0
    eng._latency_max_ms = 0
    eng._max_acceptable_drift_pct = 100.0
    eng._mexc_feed_lag_ms = 0
    eng._max_book_age_ms = LETHAL_MAX_BOOK_AGE_MS if lethal_knobs else 0
    eng._queue_frac = LETHAL_QUEUE_FRAC if lethal_knobs else 1.0

    for c in ("signals_skipped_no_book", "signals_skipped_latency_drift",
              "entries_attempted", "entries_rejected", "entries_filled",
              "entries_partial", "entries_expired"):
        setattr(eng, c, 0)

    # Точка ВІДПРАВКИ. Її виклик = «живий ордер пішов».
    eng._open_position = AsyncMock()
    return eng


def _signal():
    return Signal(symbol="PENGUUSDT", direction="long", source="static_gap",
                  confidence=0.5, binance_price=0.10000, mexc_price=0.10000)


def _cfg():
    return PairExecConfig(margin_min_usdt=5.0, margin_max_usdt=10.0,
                          leverage_min=50, leverage_max=70,
                          ioc_max_attempts=1, ioc_offset_ticks=0)


# ──────────────────────────────────────────────────────────────────────
# ГОЛОВНЕ: живий ордер відправляється попри летальні ручки
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lethal_shadow_knobs_do_not_kill_a_live_order():
    """МУТАНТ, ЩО ЦЕ ЛОВИТЬ: провести `max_book_age_ms` у гілку
    `if _is_live_pair:` у `shadow_engine._try_enter`. Без цього тесту такий
    мутант проходить УСЮ сюїту, а живий ордер при цьому не відправляється."""
    eng = _engine(is_live=True, lethal_knobs=True)

    await eng._try_enter(_signal(), None, _cfg())

    assert eng._open_position.await_count == 1, (
        "живий ордер НЕ відправлено при max_book_age_ms=%d і queue_frac=%.2f — "
        "shadow-ручка дотяглася до грошового шляху"
        % (LETHAL_MAX_BOOK_AGE_MS, LETHAL_QUEUE_FRAC))


@pytest.mark.asyncio
async def test_the_live_path_never_calls_the_simulator_at_all():
    """Структурна гарантія, на якій тримається все інше: на живій парі
    `simulate_ioc_entry` не викликається, тож ручки фізично не мають куди
    вплинути. Мутант, що прибирає гілку `if _is_live_pair:`, валить цей тест."""
    eng = _engine(is_live=True, lethal_knobs=True)

    await eng._try_enter(_signal(), None, _cfg())

    assert eng.ioc_executor.simulate_ioc_entry.call_count == 0, (
        "живий шлях покликав симулятор — ручки тепер на грошовому шляху")
    assert eng._open_position.await_count == 1


# ──────────────────────────────────────────────────────────────────────
# КОНТРОЛЬ: без нього тести вище були б вакуумними
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_control_the_same_knobs_really_do_kill_a_shadow_fill():
    """Доводить, що обрані значення НЕ безневинні.

    Якби вони нічого не робили, тести вище проходили б і на зламаному коді.
    Тут — СПРАВЖНІЙ `IOCExecutor` на тій самій протухлій книзі: філ мусить
    померти, і ордер не відправитись."""
    eng = _engine(is_live=False, lethal_knobs=True, real_simulator=True)

    await eng._try_enter(_signal(), None, _cfg())

    assert eng._open_position.await_count == 0, (
        "летальні ручки НЕ вбили shadow-філ — значення обрані невдало, і "
        "тести вище нічого не доводять")


@pytest.mark.asyncio
async def test_control_the_same_shadow_path_fills_when_the_knobs_are_off():
    """Друга половина контролю: та сама shadow-конфігурація з ВИМКНЕНИМИ
    ручками мусить налитись. Інакше попередній тест міг би проходити з
    будь-якої іншої причини (порожня книга, зламана фікстура)."""
    eng = _engine(is_live=False, lethal_knobs=False, real_simulator=True)

    await eng._try_enter(_signal(), None, _cfg())

    assert eng._open_position.await_count == 1, (
        "shadow не налився навіть із вимкненими ручками — фікстура зламана, "
        "і контроль вище нічого не доводить")


# ──────────────────────────────────────────────────────────────────────
# Сторож самої фікстури
# ──────────────────────────────────────────────────────────────────────

def test_the_fixture_mirrors_every_attribute_init_sets():
    """`ShadowEngine.__new__` пропускає `__init__`, тож усе, що там зʼявиться,
    треба дзеркалити тут. Ця фікстура вже ламалася так двічі (див. коментар у
    `test_live_bypass_realism.py`). Тест падає ГУЧНО замість того, щоб мовчки
    перестати виконувати шлях."""
    eng = _engine(is_live=True, lethal_knobs=True)
    for attr in ("_max_book_age_ms", "_queue_frac", "_mexc_feed_lag_ms",
                 "_latency_enabled", "ob_manager", "state_manager",
                 "ioc_executor", "realism"):
        assert hasattr(eng, attr), f"фікстура розсинхронізувалась: немає {attr}"
