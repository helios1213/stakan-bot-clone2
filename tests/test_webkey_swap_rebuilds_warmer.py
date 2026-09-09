# -*- coding: utf-8 -*-
"""Заміна вебкея на льоту МУСИТЬ перестворити warmer (виміряний баг 2026-09-09).

ЩО БУЛО. `SlotWarmer` захоплює ключ у момент створення — `_account_key`,
`SpotWebClient(webkey)`, `FeeGate(client)` — і цикл більше його не
перестворює (`if sid in warmers: continue`). Пул клієнтів інвалідується
правильно, але warmer тримає ПОСИЛАННЯ на старий обʼєкт, тож це не рятує.

ВИМІРЯНО на primary слот 1: ключ помер 09-07 22:25 (`code=401`), оператор
замінив його 22:32 і ще раз 09-08 12:01 — після чого слот стояв ДВІ ДОБИ.
Симптоми: `ставку не прочитано у 23 із 23` ~37 разів на годину, баланси не
читаються, спотова половина визнається нежиттєздатною, її файл стану
замерзає на позавчорашній даті. Бот при цьому `healthy`.

КНОПКА НЕ РЯТУВАЛА, і це не здогад: за ті дві доби в лозі немає ані `ON`,
ані `OFF` для слота 1. Цикл опитує раз на POLL_SEC — якщо OFF і ON встигають
між поллами, проміжного стану він не бачить.
"""
import asyncio

import pytest

from src.execution import soft_start_runner as ssr
from src.execution.soft_start_runner import SlotWarmer as RealSlotWarmer
from src.execution.soft_start_runner import _account_fingerprint

KEY_A = "WEB" + "a" * 64
KEY_B = "WEB" + "b" * 64


class _Slot:
    def __init__(self, slot_id, webkey, soft_start_enabled=True):
        self.slot_id = slot_id
        self.webkey = webkey
        self.soft_start_enabled = soft_start_enabled
        self.live_enabled = False


class _Store:
    def __init__(self, slots):
        self.slots = slots

    async def list_all(self):
        return self.slots

    async def set_soft_start(self, slot_id, enabled):
        for s in self.slots:
            if s.slot_id == slot_id:
                s.soft_start_enabled = enabled


class _Pool:
    async def get(self, slot_id):
        return object()


class _W:
    """Мінімальний warmer, що ДЗЕРКАЛИТЬ реальний у тому, що читає цикл."""
    made: list["_W"] = []

    def __init__(self, slot_id, webkey, client, universe, *, dry_run,
                 futures_allowed=True, **kw):
        self.slot_id = slot_id
        self.webkey = webkey
        self._account_key = _account_fingerprint(webkey)
        self.draining = False
        self.futures_allowed = futures_allowed
        self.futures = None
        self.reporter = None
        self.started = 0
        self.ticks = 0
        self.stop_calls = 0
        _W.made.append(self)

    def set_futures_allowed(self, allowed):
        self.futures_allowed = allowed

    async def start(self):
        self.started += 1

    async def tick(self):
        self.ticks += 1

    async def stop(self):
        self.stop_calls += 1
        return True

    def finished(self):
        return False


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    _W.made = []
    monkeypatch.setattr(ssr, "SlotWarmer", _W)
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)


async def _run(store, passes=2, on_sleep=None):
    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if on_sleep is not None:
            on_sleep(calls["n"])
        if calls["n"] >= passes:
            raise asyncio.CancelledError

    orig = ssr.asyncio.sleep
    ssr.asyncio.sleep = fake_sleep
    try:
        with pytest.raises(asyncio.CancelledError):
            await ssr.soft_start_loop(store, _Pool(), lambda: ["HYPEUSDT"])
    finally:
        ssr.asyncio.sleep = orig


# ------------------------------------------------------------------ ПРОВОДКА

@pytest.mark.asyncio
async def test_a_swapped_webkey_rebuilds_the_warmer():
    """ГОЛОВНИЙ ТЕСТ. Прибери перевірку з циклу — і він упаде.

    Саме цього бракувало: слот стояв дві доби зі старим ключем.
    """
    slot = _Slot(1, KEY_A)
    store = _Store([slot])

    def swap(n):
        if n == 1:
            slot.webkey = KEY_B          # оператор перелогінився

    await _run(store, passes=2, on_sleep=swap)

    assert len(_W.made) == 2, f"warmer не перестворено: створено {len(_W.made)}"
    assert _W.made[0]._account_key == _account_fingerprint(KEY_A)
    assert _W.made[1]._account_key == _account_fingerprint(KEY_B)
    assert _W.made[1].started == 1, "новий warmer не пройшов start()"


@pytest.mark.asyncio
async def test_the_same_key_does_NOT_churn_the_warmer():
    """Зворотний бік: без зміни ключа warmer має жити далі.

    Інакше фікс перестворював би рушії щохвилини — а це скидало б стан
    прогріву й лічильники на кожному поллі.
    """
    store = _Store([_Slot(1, KEY_A)])
    await _run(store, passes=4)
    assert len(_W.made) == 1, f"warmer перестворювався даремно: {len(_W.made)}"
    assert _W.made[0].ticks == 4


@pytest.mark.asyncio
async def test_the_swap_does_NOT_call_stop_on_the_old_warmer():
    """stop() ходив би на біржу СТАРИМ ключем і впав би.

    А невдалий stop лишає warmer у `draining` назавжди — рівно та пастка,
    через яку кнопка й не допомагала. Тому старий warmer просто викидається.
    """
    slot = _Slot(1, KEY_A)
    store = _Store([slot])

    def swap(n):
        if n == 1:
            slot.webkey = KEY_B

    await _run(store, passes=2, on_sleep=swap)
    assert _W.made[0].stop_calls == 0, "старий warmer не можна зупиняти мертвим ключем"


@pytest.mark.asyncio
async def test_an_open_position_is_shouted_about_but_still_rebuilt(caplog):
    """Позиція переживає перелогін (та сама біржа), але оператор має знати.

    Якщо ключ виявиться від ІНШОГО акаунта — позиція лишиться сиротою, і це
    той випадок, коли треба глянути на біржу власними очима.
    """
    slot = _Slot(1, KEY_A)
    store = _Store([slot])

    def swap(n):
        if n == 1:
            _W.made[0].futures = type("F", (), {
                "state": type("S", (), {"position": {"symbol": "SOXLUSDT"}})()})()
            slot.webkey = KEY_B

    with caplog.at_level("WARNING", logger="src.execution.soft_start_runner"):
        await _run(store, passes=2, on_sleep=swap)

    assert len(_W.made) == 2, "warmer мав перестворитись і з відкритою позицією"
    assert "SOXLUSDT" in caplog.text, "про позицію не крикнули"
    assert any(r.levelname == "CRITICAL" for r in caplog.records), \
        "відкрита позиція при заміні ключа має бути CRITICAL, а не INFO"


# --------------------------------------------------- сторож проти дрейфу

def test_the_real_warmer_stamps_the_same_fingerprint():
    """Формула ОДНА на обидва вживання.

    Якби `__init__` рахував відбиток інакше, ніж цикл, перевірка стала б
    no-op, який виглядає робочим — найгірший із можливих результатів.
    """
    assert _account_fingerprint(KEY_A) != _account_fingerprint(KEY_B)
    assert len(_account_fingerprint(KEY_A)) == 16
    # і саме воно лягає в _account_key справжнього warmer
    import inspect
    src = inspect.getsource(RealSlotWarmer.__init__)
    assert "_account_key = _account_fingerprint(webkey)" in src
