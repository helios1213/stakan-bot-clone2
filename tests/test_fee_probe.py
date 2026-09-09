# -*- coding: utf-8 -*-
"""Проба після ПРЕВЕНТИВНОГО халту: перевірити тариф реальним ордером.

НАВІЩО. Превентивний сторож халтить слот за ТАРИФОМ (`tiered_fee_rate/v2`),
без жодного філу. Це читання вже двічі провалило власну перевірку
ідентичності: 2026-09-04 усі чотири відповіді прийшли з `walletBalance=0`,
а за 69 секунд до халту на тому ж акаунті пройшло 57 WS-філів із НУЛЬОВОЮ
комісією. Тобто «тариф каже платно» і «з нас беруть гроші» — різні речі, і
відрізнити їх може лише реальний філ.

ЧОМУ ПРОСТО СКИНУТИ ГАРД НЕ ВИСТАЧАЛО: сторож опитує тариф раз на 10с і
халтить після 2 підтверджень, тобто повертав слот у халт за ~20 секунд. Живий
ордер за цей час не встигав — він чекає на сигнал детектора, а не йде негайно.

ЩО ПІНИТЬСЯ:
  * проба вмикається ЛИШЕ після превентивного халту (після реактивного факт
    списання вже доведений — перевіряти нічого);
  * поки вона діє, сторож МОВЧИТЬ (проводка: інакше він халтне знову);
  * перший філ виносить вирок в обидва боки;
  * вікно обмежене — проба, що лишилась мовчки, це знятий запобіжник.
"""
import asyncio

import pytest

from src.execution import fee_watchdog as fw
from src.execution.live_executor import (FEE_PROBE_WINDOW_SEC, LiveExecutor)


def _ex(slot_id=1):
    """Виконавець без __init__ — рівно ті поля, що читає механізм проби."""
    e = LiveExecutor.__new__(LiveExecutor)
    e.slot_id = slot_id
    e._halted = False
    e._halt_was_preventive = False
    e._fee_probe_until = 0.0
    # Дзеркалить `__init__`: `_trip_fee_guard` питає `account_block_fresh()`,
    # щоб назвати справжню причину халту. Без цих полів фіча падала б із
    # AttributeError — а `getattr`-обхід зробив би її мовчки інертною.
    e.account_block = None
    e.account_block_msg = None
    e.account_block_at_ts = 0.0
    e.alerts = None
    e.webkey_store = None
    return e


# ------------------------------------------------- джерело халту

@pytest.mark.asyncio
async def test_a_preventive_halt_is_marked_as_such():
    e = _ex()
    await e._trip_fee_guard("PEPE_USDT", 0.0, preventive=True)
    assert e._halted is True
    assert e._halt_was_preventive is True


@pytest.mark.asyncio
async def test_a_reactive_halt_is_NOT_marked_preventive():
    e = _ex()
    await e._trip_fee_guard("PEPE_USDT", 0.074010)
    assert e._halted is True
    assert e._halt_was_preventive is False


# ------------------------------------------------- озброєння проби

@pytest.mark.asyncio
async def test_reset_after_a_PREVENTIVE_halt_arms_the_probe():
    e = _ex()
    await e._trip_fee_guard("PEPE_USDT", 0.0, preventive=True)
    assert e.reset_fee_guard() is True
    assert e._halted is False
    assert e.fee_probe_active() is True


@pytest.mark.asyncio
async def test_reset_after_a_REACTIVE_halt_does_NOT_arm_it():
    """Комісію вже бачили на філі — доводити нічого, пробі тут не місце."""
    e = _ex()
    await e._trip_fee_guard("PEPE_USDT", 0.074010)
    assert e.reset_fee_guard() is True
    assert e._halted is False
    assert e.fee_probe_active() is False


@pytest.mark.asyncio
async def test_the_probe_window_expires():
    """Проба, що лишилась увімкненою мовчки, — це знятий запобіжник."""
    e = _ex()
    await e._trip_fee_guard("PEPE_USDT", 0.0, preventive=True)
    e.reset_fee_guard()
    t0 = e._fee_probe_until
    assert e.fee_probe_active(now=t0 - 1) is True
    assert e.fee_probe_active(now=t0 + 1) is False
    assert e._fee_probe_until == 0.0, "протерміноване вікно має гаснути"
    assert FEE_PROBE_WINDOW_SEC == 1800


@pytest.mark.asyncio
async def test_a_new_halt_during_a_probe_kills_it():
    e = _ex()
    await e._trip_fee_guard("PEPE_USDT", 0.0, preventive=True)
    e.reset_fee_guard()
    assert e.fee_probe_active() is True
    await e._trip_fee_guard("PEPE_USDT", 0.0500)      # реальна комісія
    assert e.fee_probe_active() is False
    assert e._halted is True


# ------------------------------------------------- ПРОВОДКА: сторож мовчить

@pytest.mark.asyncio
async def test_the_watchdog_STAYS_SILENT_while_the_probe_runs():
    """ГОЛОВНИЙ ТЕСТ ПРОВОДКИ.

    Прибери перевірку проби зі сторожа — і він знову халтне за ~20с, тобто
    жоден живий ордер не встигне статись і вимірювати буде нічого.
    """
    class _Client:
        calls = 0

        async def _request(self, *a, **kw):
            _Client.calls += 1
            return {"data": {"realMakerFee": 0.0001, "realTakerFee": 0.0004,
                             "walletBalance": 0}}

    class _Probing:
        def __init__(self):
            self.tripped = []

        def fee_probe_active(self):
            return True

        async def _trip_fee_guard(self, symbol, fee, *, preventive=False):
            self.tripped.append(symbol)

    wd = fw.FeeWatchdog()
    ex = _Probing()
    for _ in range(fw.CONFIRMATIONS + 2):
        out = await wd.check_slot(1, _Client(), "PEPE_USDT", ex)
    assert ex.tripped == [], "сторож халтнув попри пробу"
    assert out.get("probe") is True
    assert _Client.calls == 0, "сторож не мав навіть питати тариф під час проби"


@pytest.mark.asyncio
async def test_without_a_probe_the_watchdog_still_halts():
    """Зворотний бік: проба не має зламати сторожа в звичайному режимі."""
    class _Client:
        async def _request(self, *a, **kw):
            return {"data": {"realMakerFee": 0.0001, "realTakerFee": 0.0004,
                             "walletBalance": 213.68}}

    class _Idle:
        def __init__(self):
            self.tripped = []
            self.preventive = None

        def fee_probe_active(self):
            return False

        async def _trip_fee_guard(self, symbol, fee, *, preventive=False):
            self.tripped.append(symbol)
            self.preventive = preventive

    wd = fw.FeeWatchdog()
    ex = _Idle()
    for _ in range(fw.CONFIRMATIONS):
        await wd.check_slot(1, _Client(), "PEPE_USDT", ex)
    assert ex.tripped == ["PEPE_USDT"]
    assert ex.preventive is True, "сторож мусить позначати свій халт превентивним"


# ------------------------------------------------- вирок філу

def test_a_zero_fee_fill_resolves_the_probe():
    e = _ex()
    e._fee_probe_until = 1e18                 # проба діє
    e._resolve_fee_probe("PEPE_USDT", 0.0)
    assert e.fee_probe_active() is False, "нульовий філ мав зняти пробу"


def test_resolving_without_a_probe_is_a_no_op():
    e = _ex()
    e._resolve_fee_probe("PEPE_USDT", 0.0)
    assert e._fee_probe_until == 0.0


def test_the_fill_path_actually_calls_the_resolver():
    """Сторож проводки: формула може бути ідеальною і невживаною.

    Тут інспектується джерело свідомо — реальний шлях філу лежить усередині
    `place_ioc_open` за мережею, WS-пулом і півсотнею гілок, і виконати його
    в тесті дорожче, ніж він вартий. Але без цієї перевірки `_resolve_fee_probe`
    міг би просто ніколи не викликатись.
    """
    import inspect
    src = inspect.getsource(LiveExecutor)
    assert "self._resolve_fee_probe(symbol, _fill_fee)" in src, \
        "шлях ВІДКРИТТЯ не резолвить пробу"
    assert "self._resolve_fee_probe(symbol, cf.fee)" in src, \
        "шлях ЗАКРИТТЯ не резолвить пробу"
