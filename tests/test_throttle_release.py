"""Заміна вебкея має знімати засувку відкриттів одразу.

Симптом (2026-08-04): «кинуло блок на слот, я вже замінив вебкей, а він всеодно є».

Дві причини, обидві реальні:
  1. delete() чистив open_throttle_until, а set_webkey() — НІ. Тобто видалення
     ключа засувку знімало, а ЗАМІНА (типовий сценарій) лишала її в базі.
  2. Зняття, що є в гарячому шляху live-open, ЛІНИВЕ: настає лише коли по парі
     цього слота приходить сигнал. Слот без пари, пара в shadow, тиха година —
     і засувка висить, хоча ключ уже інший.

Правило, яке НЕ можна ламати: не знімати, поки ключа нема. Ліміт на АКАУНТІ, а
не на слоті, тож зняття на половині «видалив/вставив» перепробувало б обмежений
акаунт і заробило свіжі 6 годин (виміряно 2026-07-29 12:17:38 → 12:17:39).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy.shadow_engine import ShadowEngine

MODE_SEC = 21600.0


def _slot(sid, wk, ot):
    return SimpleNamespace(slot_id=sid, webkey_refreshed_at=wk, open_throttle_until=ot)


def _engine(slots):
    """ShadowEngine без __init__ — нам потрібен лише стан засувки та live_pool."""
    e = ShadowEngine.__new__(ShadowEngine)
    e._open_rl_mode_until = {}
    e._open_rl_code = {}
    e._open_rl_persisted = {}
    e._slot_cooldown_until = {}
    e._open_rl_wk_seen = {}
    e._env_float = lambda name, default: default
    store = MagicMock()
    store.list_all = AsyncMock(return_value=slots)
    store.set_open_throttle_until = AsyncMock()
    e.live_pool = SimpleNamespace(webkey_store=store)
    return e, store


@pytest.mark.asyncio
async def test_new_key_drops_an_armed_latch_without_waiting_for_a_signal():
    """Суть фікса: зняття настає з періодичного циклу, а не з гарячого шляху."""
    now = int(time.time())
    e, store = _engine([_slot(1, wk=now - 60, ot=now + 4 * 3600)])
    e._open_rl_mode_until[1] = time.monotonic() + 4 * 3600      # засувка в памʼяті
    await e._release_throttle_for_new_keys()
    assert 1 not in e._open_rl_mode_until, "памʼять процесу не очищена"
    store.set_open_throttle_until.assert_awaited_once_with(1, None)


@pytest.mark.asyncio
async def test_stale_memory_is_dropped_when_the_db_deadline_is_already_gone():
    """set_webkey тепер чистить БД одразу — памʼять процесу мусить наздогнати."""
    now = int(time.time())
    e, store = _engine([_slot(1, wk=now - 10, ot=None)])
    e._open_rl_mode_until[1] = time.monotonic() + 3600
    e._open_rl_wk_seen[1] = now - 10                            # штамп ми вже бачили
    await e._release_throttle_for_new_keys()
    assert 1 not in e._open_rl_mode_until
    store.set_open_throttle_until.assert_not_awaited()          # у БД уже чисто


@pytest.mark.asyncio
async def test_latch_earned_by_the_key_still_in_place_is_kept():
    """Ключ СТАРІШИЙ за момент, коли засувку заробили — вона наша, тримаємо.

    Це стан слота 2 на 2026-08-04: ключ від 08-03 16:13, засувка заробити о 19:44.
    """
    now = int(time.time())
    e, store = _engine([_slot(2, wk=now - 30 * 3600, ot=now + 4 * 3600)])
    armed = time.monotonic() + 4 * 3600
    e._open_rl_mode_until[2] = armed
    e._open_rl_wk_seen[2] = now - 30 * 3600
    await e._release_throttle_for_new_keys()
    assert e._open_rl_mode_until[2] == armed, "чужу засувку зняли"
    store.set_open_throttle_until.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_key_never_releases():
    """Видалення ключа НЕ знімає ліміт MEXC — він на акаунті. Зняття на половині
    «видалив/вставив» перепробувало б обмежений акаунт і заробило свіжі 6 годин."""
    now = int(time.time())
    e, store = _engine([_slot(1, wk=None, ot=now + 4 * 3600)])
    armed = time.monotonic() + 4 * 3600
    e._open_rl_mode_until[1] = armed
    await e._release_throttle_for_new_keys()
    assert e._open_rl_mode_until[1] == armed
    store.set_open_throttle_until.assert_not_awaited()


@pytest.mark.asyncio
async def test_key_newer_than_the_latch_releases_after_a_restart():
    """Після рестарту seen-мапа порожня, тож зняття мусить триматись на
    stateless-ознаці: ключ новіший за момент, коли засувку заробили."""
    now = int(time.time())
    ot = now + 3600                       # заробили о (ot - 6год)
    e, store = _engine([_slot(1, wk=now - 60, ot=ot)])
    e._open_rl_mode_until[1] = time.monotonic() + 3600
    await e._release_throttle_for_new_keys()      # _open_rl_wk_seen порожня
    assert 1 not in e._open_rl_mode_until
    store.set_open_throttle_until.assert_awaited_once_with(1, None)


@pytest.mark.asyncio
async def test_quiet_slot_is_left_alone_and_does_not_spam_the_db():
    """Ані засувки в памʼяті, ані в БД — жодних записів."""
    now = int(time.time())
    e, store = _engine([_slot(1, wk=now - 5000, ot=None)])
    await e._release_throttle_for_new_keys()
    store.set_open_throttle_until.assert_not_awaited()
    assert e._open_rl_wk_seen[1] == now - 5000


@pytest.mark.asyncio
async def test_release_clears_every_piece_of_slot_state():
    now = int(time.time())
    e, store = _engine([_slot(1, wk=now - 60, ot=now + 4 * 3600)])
    e._open_rl_mode_until[1] = time.monotonic() + 4 * 3600
    e._open_rl_code[1] = "10014"
    e._open_rl_persisted[1] = now + 4 * 3600
    e._slot_cooldown_until[1] = time.monotonic() + 600
    await e._release_throttle_for_new_keys()
    for d, name in ((e._open_rl_mode_until, "mode_until"), (e._open_rl_code, "code"),
                    (e._open_rl_persisted, "persisted"),
                    (e._slot_cooldown_until, "cooldown")):
        assert 1 not in d, f"{name} лишився"


@pytest.mark.asyncio
async def test_a_dead_store_does_not_take_the_engine_down():
    e, store = _engine([])
    store.list_all = AsyncMock(side_effect=RuntimeError("БД впала"))
    await e._release_throttle_for_new_keys()      # не має кинути
