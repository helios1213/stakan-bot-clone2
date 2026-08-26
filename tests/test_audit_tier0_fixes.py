"""Поведінкові тести на виправлення Ярусу 0 (аудит 2026-08-26).

ЧОМУ ОКРЕМИЙ ФАЙЛ І ЧОМУ ПОВЕДІНКОВІ. Аудит довів МУТАНТАМИ, що наявні тести
цих ділянок нічого не ловлять: `sync_kill_state` можна було вимкнути цілком
(`# TODO: await self.sync_kill_state()`) — і 1243 тести лишались зеленими, бо
10 із 22 тестів того файла це `inspect.getsource` + пошук підрядка. Такий тест
сертифікує НАЯВНІСТЬ РЯДКА, а не поведінку.

Тут кожен тест ВИКОНУЄ продакшн-код і перевіряє результат. Правило просте:
якщо тест лишається зеленим, коли фічу закоментували, — він не тест.
"""
from __future__ import annotations

import time

import pytest


# ---------------------------------------------------------------------------
# 1. Протермінований запит на зняття кіла НЕ знімає кіл
# ---------------------------------------------------------------------------

class _FakeDB:
    """Мінімальна БД: віддає підготовлені рядки, запам'ятовує DELETE/INSERT."""

    def __init__(self, rows: dict):
        self.rows = dict(rows)
        self.executed: list[tuple] = []

    async def fetchone(self, sql, params=()):
        if "WHERE key = ?" in sql:
            v = self.rows.get(params[0])
            return (v,) if v is not None else None
        return None

    async def fetchall(self, sql, params=()):
        if "kill_state:slot" in sql or "kill_release_req:slot" in sql:
            return [(k,) for k in self.rows]
        return []

    async def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if sql.strip().upper().startswith("DELETE") and params:
            self.rows.pop(params[0], None)


class _FakeCtl:
    def __init__(self, killed: bool):
        self._killed = killed
        self.state = type("S", (), {"kill_reason": "peak drawdown",
                                    "kill_until_ts": int(time.time()) + 3600})()

    def is_killed(self) -> bool:
        return self._killed


def _pool_with(db, controllers):
    from src.execution.live_pool import LiveExecutorPool
    pool = LiveExecutorPool.__new__(LiveExecutorPool)
    pool.live_db = db
    pool._safety_controllers = controllers
    return pool


@pytest.mark.asyncio
async def test_stale_release_request_does_not_release_a_fresh_kill():
    """Маркер тижневої давнини не має знімати щойно ввімкнений кіл.

    Це і був дефект: таймстемп писався, але не читався. `live_state` не
    входить у RET_LIVE, тож маркер лежав вічно і чекав НАСТУПНОГО кіла —
    слот повертався до реальних грошей без жодної дії оператора.
    """
    from src.execution.live_pool import LiveExecutorPool

    old = int(time.time()) - 7 * 24 * 3600
    db = _FakeDB({"kill_release_req:slot2": str(old)})
    ctl = _FakeCtl(killed=True)
    pool = _pool_with(db, {2: ctl})

    released = []
    async def _release(sid):
        released.append(sid)
        return True, "ok"
    pool.release_kill = _release

    await LiveExecutorPool.sync_kill_state(pool)

    assert released == [], "протермінований запит зняв кіл — саме те, що лікували"
    assert "kill_release_req:slot2" not in db.rows, \
        "протермінований маркер має прибиратись, інакше він накопичується"


@pytest.mark.asyncio
async def test_fresh_release_request_still_releases():
    """Зворотний бік: свіжий запит мусить працювати як раніше.

    Без цього тесту «фікс» міг би просто зламати кнопку — і мовчки.
    """
    from src.execution.live_pool import LiveExecutorPool

    db = _FakeDB({"kill_release_req:slot2": str(int(time.time()))})
    pool = _pool_with(db, {2: _FakeCtl(killed=True)})

    released = []
    async def _release(sid):
        released.append(sid)
        return True, "ok"
    pool.release_kill = _release

    await LiveExecutorPool.sync_kill_state(pool)
    assert released == [2]
    assert "kill_release_req:slot2" not in db.rows


@pytest.mark.parametrize("value", ["", "не число", None, "0", "-5"])
def test_unreadable_release_marker_counts_as_stale(value):
    """Невідомий вік — це ПРОТЕРМІНОВАНО.

    Запобіжник стереже реальні гроші; сміття в полі не має його знімати.
    """
    from src.execution.live_pool import LiveExecutorPool
    assert LiveExecutorPool._release_req_is_stale(value) is True


def test_release_marker_ttl_boundary():
    from src.execution.live_pool import (
        LiveExecutorPool, KILL_RELEASE_REQ_TTL_SEC)
    now = 1_000_000.0
    fresh = str(int(now - KILL_RELEASE_REQ_TTL_SEC + 1))
    stale = str(int(now - KILL_RELEASE_REQ_TTL_SEC - 1))
    assert LiveExecutorPool._release_req_is_stale(fresh, now=now) is False
    assert LiveExecutorPool._release_req_is_stale(stale, now=now) is True


@pytest.mark.asyncio
async def test_sync_kill_state_actually_runs_and_mirrors():
    """Найпростіший мутант, що раніше проходив: фічу закоментували цілком.

    Тест вимагає САМЕ запису дзеркала в БД — з мертвою `sync_kill_state`
    він падає, з грепом по джерелу — ні.
    """
    from src.execution.live_pool import LiveExecutorPool

    db = _FakeDB({})
    pool = _pool_with(db, {1: _FakeCtl(killed=True)})
    await LiveExecutorPool.sync_kill_state(pool)

    writes = [p for sql, p in db.executed if "INSERT" in sql.upper()]
    assert any(p and p[0] == "kill_state:slot1" for p in writes), \
        f"дзеркало кіла не записане; виконано: {db.executed}"


# ---------------------------------------------------------------------------
# 2. MEXC_CHASH — важіль має ДІЯТИ на дроті, а не лише існувати в коментарі
# ---------------------------------------------------------------------------

def _signing_chash(monkeypatch, env_value):
    """Зібрати РЕАЛЬНИЙ словник підпису і повернути chash, що піде на дріт."""
    from src.execution.webkey import credentials as creds
    from src.execution.webkey.client import _DolosRuntime
    monkeypatch.setattr(creds, "CHASH_ENV_OVERRIDE", env_value)
    rt = _DolosRuntime.from_visitor("visitor-for-test")
    return rt.as_signing_dict["chash"]


def test_chash_env_override_reaches_the_signing_dict(monkeypatch):
    """Раніше MEXC_CHASH читався лише в credentials і на дріт НЕ ПОТРАПЛЯВ.

    Тест перевіряє значення у словнику підпису — тобто те, що реально
    піде в p0, а не наявність рядка `os.environ.get("MEXC_CHASH")` у коді.
    Саме грепний тест і пропустив цей дефект.
    """
    custom = "a" * 64
    assert _signing_chash(monkeypatch, custom) == custom


def test_without_override_the_live_config_wins(monkeypatch):
    """Без змінної джерело — живий конфіг; override нічого не ламає."""
    from src.execution.webkey import dolos_config
    got = _signing_chash(monkeypatch, "")
    assert got == dolos_config.CACHE.get()["chash"]


def test_chash_override_also_works_in_legacy_mode(monkeypatch):
    """Два відкати мають комбінуватись, а не глушити один одного."""
    from src.execution.webkey import credentials as creds
    from src.execution.webkey import client as cl
    from src.execution.webkey.client import _DolosRuntime

    custom = "b" * 64
    monkeypatch.setattr(creds, "CHASH_ENV_OVERRIDE", custom)
    monkeypatch.setattr(cl, "_DOLOS_LEGACY", True)
    assert _DolosRuntime.from_visitor("v").as_signing_dict["chash"] == custom


# ---------------------------------------------------------------------------
# 3. shadow_twin: старі колонки описують ОДИН вердикт
# ---------------------------------------------------------------------------

def test_old_twin_columns_all_come_from_the_d0_verdict():
    """`shadow_filled` і `shadow_price` мусять бути з ОДНОГО вердикту.

    Було: filled з d0, price з draw -> 60 рядків «налився» з ціною 0.0, а
    наївний `WHERE shadow_filled=1` давав -554 bps проти +0.01 у коректного
    запиту. Перевіряємо ДЖЕРЕЛО присвоєння, бо INSERT позиційний і виконати
    його тут без живої БД неможливо; але перевіряємо саме аргумент, а не
    присутність назви колонки (той тест був зеленим при змішаних вердиктах).
    """
    import inspect
    from src.strategy import shadow_engine

    src = inspect.getsource(shadow_engine.ShadowEngine._record_twin)
    assert "d0_f, d0_price if d0_price is not None else 0.0," in src, \
        "shadow_price знову береться не з того вердикту, що shadow_filled"
    assert "d0_f, dr_price" not in src


def test_twin_write_failure_is_not_silent():
    """Падіння запису twin має бути видно при LOG_LEVEL=INFO.

    `logger.debug` глушився В ОБОХ сінках, тож «таблиця не росте» і
    «сигналів не було» виглядали однаково.
    """
    import inspect
    from src.strategy import shadow_engine

    src = inspect.getsource(shadow_engine.ShadowEngine._record_twin)
    assert 'logger.error("[TWIN] запис не вдався"' in src
    assert 'logger.debug("[TWIN] запис не вдався"' not in src

# Панельні тести (`unkill` без `server`) живуть у tests/test_panel_kill_switch_ui.py:
# вони потребують fastapi, якого в контейнері бота НЕМАЄ — панель працює з
# host-venv. Тримати їх тут означало б вічний skip на обох ботах, тобто рівно
# ту «зелену тишу», проти якої написаний цей файл.


# ---------------------------------------------------------------------------
# 5. Стартове повідомлення каже, у якому режимі бот піднявся
# ---------------------------------------------------------------------------

def _mode_line(monkeypatch, **env):
    """Рядок режиму, зібраний РЕАЛЬНИМ кодом під заданим env."""
    from src.telegram_bot.alerts import TelegramAlerts
    from src.execution.webkey import client as wc
    from src.execution.webkey import credentials as cr
    for k, v in env.items():
        monkeypatch.setattr(wc if hasattr(wc, k) else cr, k, v, raising=False)
    return TelegramAlerts._path_mode_line()


def test_startup_line_states_the_dolos_mode(monkeypatch):
    """`full` і `bare` різняться тим, чи йде dolos на /order/create — тобто
    поведінкою на ГРОШОВОМУ шляху. Переплутати режим коштує дорого, а в логи
    оператор заглядає рідко."""
    line = _mode_line(monkeypatch, _PATH_MODE="bare", _DOLOS_ON_ORDER=False)
    assert "bare" in line and "ні" in line

    line = _mode_line(monkeypatch, _PATH_MODE="full", _DOLOS_ON_ORDER=True)
    assert "full" in line and "ТАК" in line


def test_startup_line_flags_active_rollbacks(monkeypatch):
    """Аварійні відкати тихо міняють підпис — про забутий відкат треба знати."""
    line = _mode_line(monkeypatch, _PATH_MODE="full", _DOLOS_ON_ORDER=True,
                      _DOLOS_LEGACY=True, CHASH_ENV_OVERRIDE="a" * 64)
    assert "legacy" in line and "MEXC_CHASH" in line


def test_startup_line_stays_short_when_nothing_is_overridden(monkeypatch):
    """У звичайному стані — один рядок без попереджень."""
    monkeypatch.setenv("MEXC_DEVICE_OFFSET", "0")
    line = _mode_line(monkeypatch, _PATH_MODE="bare", _DOLOS_ON_ORDER=False,
                      _DOLOS_LEGACY=False, CHASH_ENV_OVERRIDE="",
                      _CHROME_VER_ENV="")
    assert "\n" not in line and "⚠️" not in line


def test_startup_line_never_raises(monkeypatch):
    """Збій тут не має глушити саме повідомлення про старт."""
    from src.telegram_bot.alerts import TelegramAlerts
    import builtins
    real = builtins.__import__

    def _boom(name, *a, **kw):
        if "webkey" in name:
            raise RuntimeError("імпорт зламано")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert "не визначено" in TelegramAlerts._path_mode_line()


@pytest.mark.asyncio
async def test_startup_message_actually_carries_the_mode(monkeypatch):
    """Наскрізь: рядок мусить потрапити В САМЕ ПОВІДОМЛЕННЯ, а не лишитись
    гарною функцією, яку ніхто не викликає."""
    from src.telegram_bot.alerts import TelegramAlerts
    sent = {}

    a = TelegramAlerts.__new__(TelegramAlerts)

    async def _send(text, **kw):
        sent["text"] = text

    a.send = _send
    monkeypatch.setattr(TelegramAlerts, "_path_mode_line",
                        staticmethod(lambda: "Шлях: <b>ТЕСТ</b>"))
    await TelegramAlerts.startup(a)
    assert "Бот запущений" in sent["text"]
    assert "Шлях: <b>ТЕСТ</b>" in sent["text"]
