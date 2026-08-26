"""Попереджувальний сторож комісії: спинити слот ДО платного філу.

Наявний fee-guard реактивний — спрацьовує вже НА філі з комісією (2026-08-25
це коштувало $0.407004 на PEPE). Цей сторож питає
`/account/tiered_fee_rate/v2` напряму і халтить раніше.

НАЙВАЖЛИВІШЕ ТУТ — НЕ ВИЯВЛЕННЯ, А БЕЗПЕЧНА ДЕГРАДАЦІЯ. Сторож, що халтить
торгівлю через таймаут або одиничний глюк біржі, шкідливіший за свою користь:
він зупиняє заробіток на рівному місці. Тому більшість тестів нижче про те,
коли він МУСИТЬ мовчати.
"""
from __future__ import annotations

import pytest

from src.execution import fee_watchdog as fw


class _Client:
    """Стаб `_request`. `resp` — або словник, або виняток."""

    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def _request(self, method, path, **kw):
        self.calls.append(path)
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


class _Executor:
    def __init__(self):
        self.tripped = []

    async def _trip_fee_guard(self, symbol, fee_usdt):
        self.tripped.append((symbol, fee_usdt))


def _resp(maker, taker, balance=213.68):
    return {"data": {"symbol": "PEPE_USDT", "realMakerFee": maker,
                     "realTakerFee": taker, "originalMakerFee": maker,
                     "originalTakerFee": taker, "walletBalance": balance}}


@pytest.fixture()
def wd():
    return fw.FeeWatchdog()


# ---- МОВЧИТЬ, коли має мовчати (головне) ---------------------------------

@pytest.mark.asyncio
async def test_zero_fees_do_not_halt(wd):
    ex = _Executor()
    r = await wd.check_slot(1, _Client(_resp(0, 0)), "PEPE_USDT", ex)
    assert r["ok"] and not r["halted"] and ex.tripped == []


@pytest.mark.asyncio
async def test_network_error_never_halts(wd):
    """Таймаут — це НЕ комісія. Інакше блимання мережі зупиняло б торгівлю."""
    ex = _Executor()
    for _ in range(10):
        r = await wd.check_slot(1, _Client(TimeoutError("бум")), "PEPE_USDT", ex)
        assert not r["halted"]
    assert ex.tripped == []


@pytest.mark.asyncio
async def test_repeated_errors_do_not_accumulate_strikes(wd):
    """Серія таймаутів не має НАКОПИЧУВАТИ підтвердження: інакше сторож
    халтив би, не отримавши жодного реального читання."""
    ex = _Executor()
    for _ in range(5):
        await wd.check_slot(1, _Client(TimeoutError()), "PEPE_USDT", ex)
    assert wd._strikes.get(1, 0) == 0
    assert ex.tripped == []


@pytest.mark.asyncio
async def test_missing_fields_are_not_treated_as_a_fee(wd):
    """`None` від рейт-ліміту — не ненульова ставка. Це вже записано
    в CLAUDE.md: «a rate-limited None is NOT a non-zero fee»."""
    ex = _Executor()
    for resp in ({"data": {}}, {}, {"data": {"realMakerFee": None,
                                             "realTakerFee": None}}):
        r = await wd.check_slot(1, _Client(resp), "PEPE_USDT", ex)
        assert not r["halted"] and not r["ok"]
    assert ex.tripped == []


@pytest.mark.asyncio
async def test_one_nonzero_reading_is_not_enough(wd):
    """Одиничний глюк біржі не має зупиняти торгівлю."""
    ex = _Executor()
    r = await wd.check_slot(1, _Client(_resp(0.0001, 0.0004)), "PEPE_USDT", ex)
    assert r["strikes"] == 1 and not r["halted"] and ex.tripped == []


@pytest.mark.asyncio
async def test_a_zero_reading_resets_the_counter(wd):
    """Ненульове -> нульове означає, що то був глюк. Лічильник має обнулитись,
    інакше два випадкові глюки за добу склались би в халт."""
    ex = _Executor()
    await wd.check_slot(1, _Client(_resp(0.0001, 0.0004)), "PEPE_USDT", ex)
    await wd.check_slot(1, _Client(_resp(0, 0)), "PEPE_USDT", ex)
    assert wd._strikes[1] == 0
    await wd.check_slot(1, _Client(_resp(0.0001, 0.0004)), "PEPE_USDT", ex)
    assert ex.tripped == []


# ---- ХАЛТИТЬ, коли справді треба ----------------------------------------

@pytest.mark.asyncio
async def test_two_consecutive_nonzero_readings_halt(wd):
    ex = _Executor()
    await wd.check_slot(1, _Client(_resp(0.0001, 0.0004)), "PEPE_USDT", ex)
    r = await wd.check_slot(1, _Client(_resp(0.0001, 0.0004)), "PEPE_USDT", ex)
    assert r["halted"] and len(ex.tripped) == 1


@pytest.mark.asyncio
async def test_nonzero_taker_alone_halts(wd):
    """Нульового мейкера ЗАМАЛО: наші IOC перетинають спред, тобто платять
    тейкера. Саме тому BTC (taker 0.0002) не годиться як пара для перевірки."""
    ex = _Executor()
    for _ in range(fw.CONFIRMATIONS):
        await wd.check_slot(1, _Client(_resp(0, 0.0002)), "PEPE_USDT", ex)
    assert ex.tripped


@pytest.mark.asyncio
async def test_halt_reports_zero_fee_because_no_fill_happened(wd):
    """`fee_usdt=0.0` — не помилка, а суть: платного філу НЕ БУЛО."""
    ex = _Executor()
    for _ in range(fw.CONFIRMATIONS):
        await wd.check_slot(1, _Client(_resp(0.0001, 0.0004)), "PEPE_USDT", ex)
    assert ex.tripped[0] == ("PEPE_USDT", 0.0)


@pytest.mark.asyncio
async def test_it_reuses_the_existing_guard_not_its_own_halt():
    """Власний шлях халту розійшовся б із реактивним guard (той ще й вимикає
    слот у БД, переводить пару в shadow і шле алерт)."""
    import inspect
    src = inspect.getsource(fw.FeeWatchdog.check_slot)
    assert "_trip_fee_guard" in src
    assert "set_live_enabled" not in src, "халт має йти ЧЕРЕЗ executor"


@pytest.mark.asyncio
async def test_a_failing_halt_does_not_crash_the_loop(wd):
    class Boom:
        async def _trip_fee_guard(self, *a):
            raise RuntimeError("біда")
    for _ in range(fw.CONFIRMATIONS):
        r = await wd.check_slot(1, _Client(_resp(0.0001, 0.0004)), "PEPE_USDT", Boom())
    assert r["halted"] is False


@pytest.mark.asyncio
async def test_observe_only_mode_never_halts(wd):
    """executor=None -> лише подивитись. Для діагностики й тестів."""
    for _ in range(fw.CONFIRMATIONS + 2):
        r = await wd.check_slot(1, _Client(_resp(0.0001, 0.0004)), "PEPE_USDT", None)
    assert not r["halted"]


# ---- слоти незалежні -----------------------------------------------------

@pytest.mark.asyncio
async def test_strikes_are_per_slot(wd):
    """Ставка per-account: ненульова на слоті 1 не має халтити слот 2."""
    e1, e2 = _Executor(), _Executor()
    await wd.check_slot(1, _Client(_resp(0.0001, 0.0004)), "PEPE_USDT", e1)
    await wd.check_slot(2, _Client(_resp(0, 0)), "PEPE_USDT", e2)
    await wd.check_slot(1, _Client(_resp(0.0001, 0.0004)), "PEPE_USDT", e1)
    assert e1.tripped and not e2.tripped


# ---- правильні поля і правильна пара -------------------------------------

@pytest.mark.asyncio
async def test_it_asks_v2_and_the_given_symbol(wd):
    c = _Client(_resp(0, 0))
    await wd.check_slot(1, c, "SOXL_USDT", None)
    assert c.calls == ["/account/tiered_fee_rate/v2?symbol=SOXL_USDT"]


@pytest.mark.asyncio
async def test_it_reads_real_fees_not_original(wd):
    """`original*` — базова сітка; `real*` враховують знижки. Дивитись треба
    на real, інакше акаунт зі знижкою халтився б даремно."""
    ex = _Executor()
    resp = {"data": {"originalMakerFee": 0.0001, "originalTakerFee": 0.0004,
                     "realMakerFee": 0, "realTakerFee": 0, "walletBalance": 1}}
    for _ in range(fw.CONFIRMATIONS + 1):
        r = await wd.check_slot(1, _Client(resp), "PEPE_USDT", ex)
    assert not r["halted"] and ex.tripped == []


@pytest.mark.asyncio
async def test_balance_is_surfaced(wd):
    """`walletBalance` у логу — єдиний спосіб побачити, що питаєш ТОЙ акаунт.
    2026-08-26 слот за годину змінив три акаунти, і без цього поля я тричі
    зробив хибний висновок про комісію."""
    r = await wd.check_slot(1, _Client(_resp(0, 0, balance=213.68)), "PEPE_USDT", None)
    assert r["balance"] == 213.68


# ---- цикл ----------------------------------------------------------------

def test_loop_is_wired_after_live_pool_exists():
    """Задача читає live_pool; запуск ДО його створення впав би на старті
    бота — саме це й сталось у першій версії."""
    from pathlib import Path
    src = Path("src/main.py").read_text().splitlines()
    made = min(i for i, l in enumerate(src) if "live_pool = LiveExecutorPool" in l)
    used = min(i for i, l in enumerate(src)
               if "fee_watchdog_loop(webkey_store" in l and "async def" not in l)
    assert made < used


def test_loop_survives_failures():
    from pathlib import Path
    src = Path("src/main.py").read_text()
    i = src.index("async def fee_watchdog_loop")
    seg = src[i:i + 2200]
    assert "except Exception:" in seg and "logger.exception" in seg


def test_loop_can_be_disabled():
    assert hasattr(fw, "ENABLED")
    from pathlib import Path
    assert "FEE_WATCHDOG" in Path("src/execution/fee_watchdog.py").read_text()


def test_poll_interval_is_not_one_second():
    """Браузерний скрипт опитував раз на секунду — це 86 400 запитів на добу
    і зайвий привід для рейт-ліміту. Ставка між тіками не міняється."""
    assert fw.POLL_SEC >= 10
