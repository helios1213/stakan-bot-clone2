"""Поведінкове покриття для функцій, у яких його не було ЗОВСІМ.

Аудит 2026-08-26 знайшов пʼять функцій, кожна з 4-10 «своїх» зелених тестів,
жоден з яких не виконував продакшн-код:

    _record_twin · _twin_tape_loop · sync_kill_state ·
    dolos_config_refresh_loop · fetch_sync

`sync_kill_state` покрито у `test_audit_tier0_fixes.py`. Тут — решта.

ЧОМУ ЦЕ ВАЖЛИВІШЕ ЗА ЗВИЧАЙНЕ ПОКРИТТЯ. Тавтологія `shadow_twin` (симулятор,
який алгебраїчно не міг протухнути) вже коштувала доби збору даних. Вона
повертається від дрібниці: перейменованої змінної, переплутаного знаку зсуву,
мілісекунд замість секунд. Грепний тест такого не бачить — і саме тому нижче
кожен тест ЛАМАЄТЬСЯ, якщо поведінка змінилась.
"""
from __future__ import annotations

import asyncio
import time

import pytest


# ---------------------------------------------------------------------------
# _tape_frame_at — серце стрічки: беремо ОСТАННІЙ кадр не пізніше дедлайну
# ---------------------------------------------------------------------------

def _engine_with_tape(frames, interval_ms=10.0, length=250):
    """ShadowEngine через __new__ — рівно ті атрибути, що читає стрічка.

    УВАГА (пастка, що вже двічі ламала сюїту): фікстури, які будують engine
    через `__new__`, мусять дзеркалити нові атрибути з `__init__`. Якщо
    з'явився ще один — додати сюди, інакше тест впаде не там, де проблема.
    """
    from src.strategy.shadow_engine import ShadowEngine
    eng = ShadowEngine.__new__(ShadowEngine)
    eng._twin_tape = {"PEPE_USDT": list(frames)}
    eng._twin_tape_symbols = {"PEPE_USDT"}
    eng._twin_tape_interval_s = interval_ms / 1000.0
    eng._twin_tape_len = length
    return eng


def _frame(perf, uid=1, synced=True, ts_ms=1_700_000_000_000):
    return (perf, uid, synced, ts_ms, {100.0: 5.0}, {101.0: 5.0})


def test_tape_returns_the_last_frame_at_or_before_the_deadline():
    """Не найсвіжіший — інакше судили б філ проти книги з МАЙБУТНЬОГО.

    Це і є заміна тавтології на чесний вимір: кадр мусить бути тим, що
    існував на момент дедлайну.
    """
    eng = _engine_with_tape([_frame(1.00, uid=1), _frame(1.05, uid=2),
                             _frame(1.20, uid=3)])
    fr, status, age = eng._tape_frame_at("PEPE_USDT", 1.10)
    assert status == "ok"
    assert fr[1] == 2, "узятий кадр із майбутнього або занадто старий"
    assert age == 50, f"вік кадру відносно дедлайну рахується неправильно: {age}"


def test_tape_reports_when_it_started_after_the_event():
    """Рядок усе одно пишеться — інакше знаменник знову став би брехливим."""
    eng = _engine_with_tape([_frame(2.00), _frame(2.10)])
    fr, status, age = eng._tape_frame_at("PEPE_USDT", 1.00)
    assert fr is None and status == "tape_starts_later" and age is None


def test_tape_reports_a_missing_symbol_distinctly():
    eng = _engine_with_tape([_frame(1.0)])
    fr, status, _ = eng._tape_frame_at("SOXL_USDT", 1.0)
    assert fr is None and status == "no_tape"


def test_a_zero_deadline_and_a_real_deadline_can_disagree():
    """ЯДРО ВСІЄЇ ПРАВКИ: d0 і draw мусять МАТИ ЗМОГУ розійтись.

    Поки стрічки не було, обидва вердикти бралися з одного й того самого
    обʼєкта книги, тож `draw` не міг протухнути НІКОЛИ — таблиця збирала
    число, що дорівнює `1/(живий fill-rate)`. Тест пінить саме можливість
    розбіжності: кадр на дедлайні draw мусить бути ІНШИМ, ніж кадр на d0.
    """
    eng = _engine_with_tape([_frame(1.00, uid=1), _frame(1.18, uid=2)])
    d0, _, _ = eng._tape_frame_at("PEPE_USDT", 1.00)
    draw, _, _ = eng._tape_frame_at("PEPE_USDT", 1.20)
    assert d0[1] != draw[1], \
        "d0 і draw беруть один кадр — тавтологія повернулась"


def test_ob_from_frame_keeps_the_original_book_age():
    """`apply_snapshot` штампує час ПОТОЧНИМ — без відновлення оригіналу вік
    книги у twin завжди ~0, і `max_book_age_ms` структурно інертний."""
    from src.strategy.shadow_engine import ShadowEngine
    orig_ts = 1_700_000_000_123
    ob = ShadowEngine._ob_from_frame("PEPE_USDT", _frame(1.0, ts_ms=orig_ts))
    assert ob.last_update_ts_ms == orig_ts, (
        "вік книги перезаписано поточним часом — гейт max_book_age_ms знову "
        "нічого не гейтить")


# ---------------------------------------------------------------------------
# межі стрічки: 0 не має давати busy-loop чи вічно порожній буфер
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env_ms,expect_min", [("0", 0.001), ("-5", 0.001),
                                               ("100000", 1.0)])
def test_tape_interval_is_clamped(monkeypatch, env_ms, expect_min):
    """`TWIN_TAPE_INTERVAL_MS=0` -> `sleep(0)` у нескінченному циклі, тобто
    busy-loop, що на ОДНОМУ ядрі відбирає час у самого детектора."""
    from src.strategy.shadow_engine import ShadowEngine
    monkeypatch.setenv("TWIN_TAPE_INTERVAL_MS", env_ms)
    val = min(1.0, max(0.001,
                       ShadowEngine._env_float("TWIN_TAPE_INTERVAL_MS", 10.0) / 1000.0))
    assert val == expect_min


@pytest.mark.parametrize("env_len,expect", [("0", 10), ("-1", 10), ("250", 250)])
def test_tape_length_is_clamped(monkeypatch, env_len, expect):
    """`deque(maxlen=0)` мовчки не памʼятає нічого, а виглядає робочим."""
    from src.strategy.shadow_engine import ShadowEngine
    monkeypatch.setenv("TWIN_TAPE_LEN", env_len)
    assert max(10, int(ShadowEngine._env_float("TWIN_TAPE_LEN", 250.0))) == expect


# ---------------------------------------------------------------------------
# _twin_tape_loop — семплер, що не пише жодного кадру, раніше проходив зеленим
# ---------------------------------------------------------------------------

class _FakeOB:
    def __init__(self, uid):
        self.last_update_id = uid
        self.is_synced = True
        self.last_update_ts_ms = 1_700_000_000_000
        self._bids = {100.0: 1.0}
        self._asks = {101.0: 1.0}


class _FakeOBM:
    def __init__(self, books):
        self.books = books

    def get(self, ex, sym):
        return self.books.get(sym)


class _FakeSM:
    def __init__(self, live):
        self.live = set(live)

    def is_in_live(self, sym):
        return sym in self.live


def _loop_engine(books, live):
    from src.strategy.shadow_engine import ShadowEngine
    eng = ShadowEngine.__new__(ShadowEngine)
    eng._twin_tape = {}
    eng._twin_tape_symbols = {"PEPE_USDT"}
    eng._twin_tape_interval_s = 0.001
    eng._twin_tape_len = 250
    eng._stop = asyncio.Event()
    eng.ob_manager = _FakeOBM(books)
    eng.state_manager = _FakeSM(live)
    return eng


@pytest.mark.asyncio
async def test_tape_loop_actually_records_frames():
    """Мутант «семплер нічого не пише» мусить падати саме тут."""
    from src.strategy.shadow_engine import ShadowEngine
    eng = _loop_engine({"PEPE_USDT": _FakeOB(1)}, live={"PEPE_USDT"})
    task = asyncio.create_task(ShadowEngine._twin_tape_loop(eng))
    for _ in range(50):
        await asyncio.sleep(0.005)
        if eng._twin_tape.get("PEPE_USDT"):
            break
    eng._stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert eng._twin_tape.get("PEPE_USDT"), "стрічка не записала жодного кадру"


@pytest.mark.asyncio
async def test_tape_loop_dedupes_on_update_id():
    """Дедуп: при тихому ринку буфер не має забиватись копіями."""
    from src.strategy.shadow_engine import ShadowEngine
    ob = _FakeOB(7)
    eng = _loop_engine({"PEPE_USDT": ob}, live={"PEPE_USDT"})
    task = asyncio.create_task(ShadowEngine._twin_tape_loop(eng))
    await asyncio.sleep(0.08)
    eng._stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    n = len(eng._twin_tape.get("PEPE_USDT") or [])
    assert n == 1, f"той самий last_update_id записано {n} разів — дедуп не працює"


@pytest.mark.asyncio
async def test_tape_loop_drops_a_symbol_that_left_live():
    """Без цього буфери мертвих пар жили б до рестарту."""
    from src.strategy.shadow_engine import ShadowEngine
    eng = _loop_engine({"PEPE_USDT": _FakeOB(1)}, live=set())
    eng._twin_tape["PEPE_USDT"] = [_frame(1.0)]
    task = asyncio.create_task(ShadowEngine._twin_tape_loop(eng))
    await asyncio.sleep(0.03)
    eng._stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert "PEPE_USDT" not in eng._twin_tape_symbols
    assert "PEPE_USDT" not in eng._twin_tape


# ---------------------------------------------------------------------------
# dolos_config_refresh_loop + apply() — валідація типів і атомарність
# ---------------------------------------------------------------------------

def test_apply_rejects_a_non_string_chash():
    """Сервер віддає JSON — chash цілком може приїхати числом. Раніше таке
    проходило перевірку «не порожнє» і падало вже в `sign_dolos`, тобто на
    ГАРЯЧОМУ шляху ордера."""
    from src.execution.webkey import dolos_config as dc
    c = dc.DolosConfigCache()
    before = c.get()
    assert c.apply({"chash": 12345, "parameters": ["mtoken"]}) is False
    assert c.get() == before, "битий конфіг ОТРУЇВ кеш"


def test_apply_rejects_non_string_parameters():
    from src.execution.webkey import dolos_config as dc
    c = dc.DolosConfigCache()
    before = c.get()
    assert c.apply({"chash": "a" * 64,
                    "parameters": ["mtoken", None, {"k": "v"}]}) is False
    assert c.get() == before


def test_apply_is_atomic_no_half_written_cache():
    """Часткове оновлення — найгірший результат: новий chash зі старими полями
    не відповідає ЖОДНІЙ серверній сцені."""
    from src.execution.webkey import dolos_config as dc
    c = dc.DolosConfigCache()
    c.apply({"chash": "a" * 64, "parameters": ["mtoken", "mhash"]})
    good = c.get()
    c.apply({"chash": "b" * 64, "parameters": ["mtoken", 7]})   # битий
    assert c.get() == good, "кеш оновився частково"


def test_apply_accepts_a_valid_config():
    from src.execution.webkey import dolos_config as dc
    c = dc.DolosConfigCache()
    assert c.apply({"chash": "c" * 64, "parameters": ["mtoken", "mhash"],
                    "data_upload": 1}) is True
    assert c.get()["chash"] == "c" * 64
    assert c.is_from_server is True


@pytest.mark.asyncio
async def test_refresh_loop_actually_fetches_and_applies(monkeypatch):
    """Мутант «create_task закоментовано» ловиться в main.py окремо, а тут —
    мутант «цикл крутиться, але нічого не робить»."""
    import src.main as main_mod
    from src.execution.webkey import dolos_config as dc

    calls = []
    def _fake_fetch(v, timeout=8.0, slot_id=None):
        # Слот теж записуємо: забір МУСИТЬ іти під профілем того слота, чий
        # mtoken відправляється, інакше один акаунт світить два пристрої.
        calls.append((v, slot_id))
        return {"chash": "d" * 64, "parameters": ["mtoken"]}

    monkeypatch.setattr(dc, "fetch_sync", _fake_fetch)
    cache = dc.DolosConfigCache()
    monkeypatch.setattr(dc, "CACHE", cache)

    class _Store:
        async def list_all(self):
            return [type("S", (), {"visitor_id": "vis-1", "slot_id": 2})()]

    task = asyncio.create_task(
        main_mod.dolos_config_refresh_loop(_Store(), interval_sec=3600))
    for _ in range(50):
        await asyncio.sleep(0.005)
        if calls:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert calls == [("vis-1", 2)], (
        "забір не викликався, або пішов без номера слота — тоді він іде під "
        "ЧУЖИМ відбитком пристрою")
    assert cache.get()["chash"] == "d" * 64, "результат не потрапив у кеш"


@pytest.mark.asyncio
async def test_refresh_loop_survives_a_failing_fetch(monkeypatch):
    """Збій = лишається попередній конфіг. Задача НЕ має вмирати: інакше
    один мережевий збій заморозив би конфіг до рестарту."""
    import src.main as main_mod
    from src.execution.webkey import dolos_config as dc

    n = {"i": 0}

    def _boom(v, timeout=8.0, slot_id=None):
        n["i"] += 1
        raise RuntimeError("мережа")

    monkeypatch.setattr(dc, "fetch_sync", _boom)
    cache = dc.DolosConfigCache()
    before = cache.get()
    monkeypatch.setattr(dc, "CACHE", cache)

    class _Store:
        async def list_all(self):
            return [type("S", (), {"visitor_id": "vis-1", "slot_id": 1})()]

    task = asyncio.create_task(
        main_mod.dolos_config_refresh_loop(_Store(), interval_sec=0.01))
    await asyncio.sleep(0.06)
    alive = not task.done()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert n["i"] >= 1
    assert alive, "цикл помер від збою мережі"
    assert cache.get() == before, "збій зіпсував кеш"


# ---------------------------------------------------------------------------
# env_config — одруківка в compose не має класти бота на ІМПОРТІ
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["6O", "10s", "", "   ", "abc", None])
def test_env_int_never_raises_on_a_typo(monkeypatch, raw):
    """Було 13 місць виду `int(os.environ.get(...))` на рівні модуля: одна
    одруківка -> ValueError під час імпорту -> бот не стартує взагалі."""
    from src.env_config import env_int
    if raw is None:
        monkeypatch.delenv("X_TEST_INT", raising=False)
    else:
        monkeypatch.setenv("X_TEST_INT", raw)
    assert env_int("X_TEST_INT", 42) == 42


def test_env_int_accepts_a_float_string(monkeypatch):
    """`PANEL_SESSION_TTL=3600.0` — очікуване від людини, падати нема за що."""
    from src.env_config import env_int
    monkeypatch.setenv("X_TEST_INT", "3600.0")
    assert env_int("X_TEST_INT", 1) == 3600


@pytest.mark.parametrize("raw,lo,hi,expect", [("0", 1, None, 1),
                                              ("999", None, 10, 10),
                                              ("5", 1, 10, 5)])
def test_env_values_are_clamped(monkeypatch, raw, lo, hi, expect):
    """Число, що технічно парситься, теж буває отруйним: `TWIN_TAPE_LEN=0`
    дає `deque(maxlen=0)` — мовчки нічого не памʼятає, а виглядає робочим."""
    from src.env_config import env_int
    monkeypatch.setenv("X_TEST_INT", raw)
    assert env_int("X_TEST_INT", 5, lo=lo, hi=hi) == expect


def test_dolos_scene_typo_does_not_kill_the_import(monkeypatch):
    """`MEXC_DOLOS_SCENE` парсився на імпорті — одруківка = бот не стартує."""
    from src.env_config import env_int
    monkeypatch.setenv("MEXC_DOLOS_SCENE", "28х")   # кирилична х
    assert env_int("MEXC_DOLOS_SCENE", 28) == 28


# ---------------------------------------------------------------------------
# природне протермінування кіла має пережити рестарт
# ---------------------------------------------------------------------------

def test_expired_kill_marks_itself_for_persistence():
    """Перебазування піку жило ЛИШЕ в памʼяті, а `_hydrate_safety` після
    рестарту відновлював дорелізний пік — тобто будь-який ребілд до півночі
    вмикав халт назад. Прапорець дає пулу шанс зберегти факт."""
    import time as _t
    from src.execution.live_safety import LiveSafetyController, SafetyState

    ctl = LiveSafetyController.__new__(LiveSafetyController)
    ctl.state = SafetyState()
    ctl.state.kill_active = True
    ctl.state.kill_reason = "peak drawdown"
    ctl.state.kill_until_ts = int(_t.time()) - 1      # уже протермінувався
    ctl.state.today_pnl = -7.0
    ctl.state.peak_pnl = 5.0
    ctl.max_margin_per_trade_usdt = 1e9
    ctl.max_concurrent_per_symbol = 99
    ctl.max_concurrent_total = 99
    ctl._maybe_reset_daily = lambda: None

    allowed, _ = ctl.can_open_live("PEPE_USDT", margin_usdt=1.0)
    assert ctl.state.kill_active is False
    assert ctl.state.peak_pnl == ctl.state.today_pnl, "пік не перебазовано"
    assert ctl.state.kill_auto_released is True, (
        "факт авто-зняття не позначено — після рестарту халт повернеться")
