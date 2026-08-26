"""Живий конфіг dolos замість прибитих констант.

ЩО З'ЯСУВАВ ЗНІМОК БРАУЗЕРА + РОЗБІР `fp.umd.js` (2026-08-26):
  * наш прибитий chash `973e5a66…` у поточному конфізі MEXC **ВІДСУТНІЙ** —
    він із давнішого релізу їхнього фронтенду;
  * **ЖОДНА** з 5 серверних сцен не приймає поля ордера. Усі беруть
    характеристики ПРИСТРОЮ. Наш список `symbol/side/vol/…` не відповідав
    жодній сцені НІКОЛИ;
  * і попри це пройшло 16 932 прийнятих ордери -> MEXC **не перевіряє вміст
    p0** на /order/create (узгоджується з абляцією 12.08, де ордер прийняли
    взагалі без dolos-блоку).

Тому цей модуль — не «фікс безпеки», а усунення протухання: значення
оновлюються самі, замість ручного знімка раз на реліз.

ГОЛОВНА ВЛАСТИВІСТЬ, ЯКУ ТУТ ПІНИМО: мережа НІКОЛИ не потрапляє на шлях
ордера. `CACHE.get()` синхронний; тягне окрема фонова задача.
"""
from __future__ import annotations

import pytest

import inspect
from pathlib import Path

from src.execution.webkey import dolos_config as dc

SRC = Path("src/execution/webkey/dolos_config.py").read_text()
MAIN = Path("src/main.py").read_text()


# ---- гаряча дорога лишається без мережі ----------------------------------

def test_get_is_synchronous_and_never_touches_the_network():
    """Один await тут — і кожен ордер став би заручником HTTP до MEXC."""
    src = inspect.getsource(dc.DolosConfigCache.get)
    assert "await" not in src and "async" not in src
    for bad in ("requests", "post(", "urlopen"):
        assert bad not in src


def test_fetch_is_separate_from_get():
    assert inspect.iscoroutinefunction(dc.DolosConfigCache.get) is False
    assert "def fetch_sync" in SRC


def test_fetch_never_raises():
    """Збій мережі не має валити нічого — це діагностика, не торгівля."""
    src = inspect.getsource(dc.fetch_sync)
    assert "except Exception:" in src and "return None" in src


# ---- деградація до знімка -------------------------------------------------

def test_cache_starts_on_the_snapshot_values():
    """Поки фонова задача не відпрацювала, поведінка має бути рівно такою,
    якою була б без цього модуля."""
    c = dc.DolosConfigCache()
    cfg = c.get()
    assert cfg["chash"] == dc.FALLBACK_CHASH
    assert cfg["parameters"] == dc.FALLBACK_PARAMETERS
    assert c.is_from_server is False


def test_bad_config_is_ignored_not_applied():
    """Порожня/бита відповідь не має затирати робочі значення."""
    c = dc.DolosConfigCache()
    for junk in (None, {}, {"chash": ""}, {"parameters": []},
                 {"chash": "x", "parameters": []}):
        assert c.apply(junk) is False
    assert c.get()["chash"] == dc.FALLBACK_CHASH


def test_good_config_is_applied():
    c = dc.DolosConfigCache()
    assert c.apply({"chash": "a" * 64, "parameters": ["mtoken"], "data_upload": 1})
    assert c.get()["chash"] == "a" * 64
    assert c.is_from_server is True


def test_returned_list_cannot_be_mutated_from_outside():
    """get() віддає копію — інакше випадкова правка списку в одному місці
    зіпсувала б підпис усім."""
    c = dc.DolosConfigCache()
    c.get()["parameters"].append("ЗЛАМАНО")
    assert "ЗЛАМАНО" not in c.get()["parameters"]


# ---- знання, здобуте знімком, має лишитись у коді ------------------------

def test_snapshot_values_are_recorded():
    assert dc.FALLBACK_CHASH == (
        "d6c64d28e362f314071b3f9d78ff7494d9cd7177ae0465e772d1840e9f7905d8")
    assert dc.FALLBACK_PARAMETERS == [
        "hostname", "member_id", "mhash", "mtoken", "platform_type",
        "product_type", "request_id", "sys", "sys_ver", "tencent_device_token"]


def test_legacy_values_are_kept_for_rollback():
    assert dc.LEGACY_CHASH.startswith("973e5a66")
    assert "symbol" in dc.LEGACY_PARAMETERS


def test_the_finding_that_no_scene_takes_order_fields_is_written_down():
    """Це найважливіше, що ми дізнались, і воно неочевидне: наступна сесія
    інакше знову вирішить, що p0 має містити параметри ордера."""
    assert "не приймає поля ордера" in SRC or "не відповідав жодній" in SRC


# ---- фонове оновлення -----------------------------------------------------

def test_refresh_loop_exists_and_is_wired():
    assert "async def dolos_config_refresh_loop" in MAIN
    assert 'name="dolos_config"' in MAIN


@pytest.mark.asyncio
async def test_refresh_runs_the_blocking_fetch_off_the_event_loop(monkeypatch):
    """fetch_sync робить HTTP; у циклі подій живе ДЕТЕКТОР.

    Перевіряємо ПОТІК, у якому фактично виконався забір, а не наявність рядка
    `asyncio.to_thread` у джерелі. Стара грепна версія до того ж мовчки
    зламалась, щойно блок виріс за 1800 символів — тобто «зелена» вона була
    рівно доти, доки випадково влучала у вікно зрізу.
    """
    import asyncio as _aio
    import threading
    import src.main as main_mod
    from src.execution.webkey import dolos_config as dc

    seen = {}

    def _fake(v, timeout=8.0, slot_id=None):
        seen["thread"] = threading.current_thread()
        return {"chash": "e" * 64, "parameters": ["mtoken"]}

    monkeypatch.setattr(dc, "fetch_sync", _fake)
    monkeypatch.setattr(dc, "CACHE", dc.DolosConfigCache())

    class _Store:
        async def list_all(self):
            return [type("S", (), {"visitor_id": "v", "slot_id": 1})()]

    task = _aio.create_task(
        main_mod.dolos_config_refresh_loop(_Store(), interval_sec=3600))
    for _ in range(50):
        await _aio.sleep(0.005)
        if seen:
            break
    task.cancel()
    try:
        await task
    except _aio.CancelledError:
        pass

    assert seen, "забір не викликався"
    assert seen["thread"] is not threading.main_thread(), (
        "блокуючий HTTP виконався В ЦИКЛІ ПОДІЙ — це зупиняє детектор на час "
        "запиту")


# Стійкість циклу до збою перевіряється ВИКОНАННЯМ у
# tests/test_audit_behavioural_coverage.py::test_refresh_loop_survives_a_failing_fetch
# (грепна версія тут була видалена: вона шукала `except Exception:` у зрізі
# джерела і зламалась від того, що блок підріс — тобто перевіряла довжину
# функції, а не поведінку).


# ---- «усе працює» і «задача мертва» мусять виглядати ПО-РІЗНОМУ ----------
# Перший деплой показав рівно цю пастку: забір спрацював, конфіг збігся зі
# знімком, `apply()` нічого не написав (бо змін немає) — і в логах не було
# ЖОДНОГО сліду. Відрізнити живу задачу від мертвої було неможливо.

def test_first_successful_fetch_is_logged_even_without_changes(caplog):
    import logging
    c = dc.DolosConfigCache()
    with caplog.at_level(logging.INFO):
        c.apply({"chash": dc.FALLBACK_CHASH,
                 "parameters": list(dc.FALLBACK_PARAMETERS), "data_upload": 1})
    assert any("DOLOS CFG" in r.message or "DOLOS CFG" in r.getMessage()
               for r in caplog.records), "перший успіх має лишати слід у лозі"


def test_repeated_identical_fetches_do_not_spam(caplog):
    """Раз на 6 годин — не привід писати щоразу."""
    import logging
    c = dc.DolosConfigCache()
    good = {"chash": dc.FALLBACK_CHASH,
            "parameters": list(dc.FALLBACK_PARAMETERS), "data_upload": 1}
    c.apply(good)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        c.apply(good)
        c.apply(good)
    assert not caplog.records


def test_a_changed_chash_is_loud(caplog):
    """Зміна chash = новий реліж фронтенду MEXC. Це треба бачити одразу."""
    import logging
    c = dc.DolosConfigCache()
    with caplog.at_level(logging.WARNING):
        c.apply({"chash": "b" * 64, "parameters": ["mtoken"], "data_upload": 1})
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_startup_prints_the_active_mode():
    """Оператор має бачити режим із логів, не заглядаючи в compose."""
    assert "[PATH MODE]" in MAIN
    i = MAIN.index('logger.info("[PATH MODE]')
    seg = MAIN[i:i + 700]
    assert "_PATH_MODE" in seg and "_DOLOS_ON_ORDER" in seg


@pytest.mark.parametrize("raw,explicit", [
    (None, False),        # не задано взагалі
    ("", False),          # порожньо — те саме, що не задано
    ("не число", False),  # одруківка в compose мовчки провалювалась у посів
    ("0", True),
    ("2", True),
])
def test_offset_is_explicit_detects_every_way_it_can_be_missing(
        monkeypatch, raw, explicit):
    """ВИКОНУЄМО функцію, а не грепаємо джерело.

    Попередня версія цього тесту перевіряла лише, що рядок
    "MEXC_DEVICE_OFFSET" трапляється біля логу — тобто лишалась би зеленою з
    назавжди мертвою гілкою попередження. Саме так дефект і прожив: умова була
    диз'юнкцією (`посів заданий АБО зсув заданий`), а посів заданий у compose
    ОБОХ машин, тож попередження не спрацювало б ніколи.
    """
    from src.execution.webkey import device_profile as dp
    if raw is None:
        monkeypatch.delenv("MEXC_DEVICE_OFFSET", raising=False)
    else:
        monkeypatch.setenv("MEXC_DEVICE_OFFSET", raw)
    assert dp.offset_is_explicit() is explicit


def test_seed_alone_does_not_separate_our_two_real_machines():
    """Підстава для попередження, вимірювана, а не декларативна.

    Обидва СПРАВЖНІ посіви дають той самий `sha256%6`, тож запасний шлях
    розрізняє машини лише випадково. Якщо колись профілів стане більше і
    посіви розійдуться — тест впаде і змусить перечитати попередження, а не
    лишить його брехати.
    """
    import hashlib
    from src.execution.webkey import device_profile as dp
    n = dp.profile_count()
    a = hashlib.sha256(b"primary-vultr-45.32.12.27").digest()[0] % n
    b = hashlib.sha256(b"clone1-vultr-45.76.96.241").digest()[0] % n
    assert a == b, (
        "посіви розійшлись — перечитай попередження в main.py, воно "
        "спирається на те, що вони збігаються")


@pytest.mark.asyncio
async def test_missing_webkey_is_reported_once_not_hidden_at_debug(
        monkeypatch, caplog):
    """На клоні ключів немає — забір пропускається. Але «нема ключів» і
    «задача мертва» не мусять виглядати однаково.

    ВИКОНУЄМО цикл замість грепу зрізу джерела: попередня версія шукала
    підрядки у перших 2200 символах функції і зламалась, щойно функція
    підросла. Тобто вона перевіряла ДОВЖИНУ коду, а не поведінку.
    """
    import asyncio as _aio
    import logging
    import src.main as main_mod
    from src.execution.webkey import dolos_config as dc

    called = []
    monkeypatch.setattr(dc, "fetch_sync",
                        lambda *a, **k: called.append(1))

    class _EmptyStore:
        async def list_all(self):
            return [type("S", (), {"visitor_id": None, "slot_id": 1})()]

    with caplog.at_level(logging.INFO):
        task = _aio.create_task(
            main_mod.dolos_config_refresh_loop(_EmptyStore(), interval_sec=0.01))
        await _aio.sleep(0.08)
        task.cancel()
        try:
            await task
        except _aio.CancelledError:
            pass

    assert not called, "без ключа забір не має відбуватись узагалі"
    said = [r for r in caplog.records
            if "жодного слота з вебкеєм" in r.getMessage()]
    assert said, "мовчазний пропуск — «нема ключів» і «задача мертва» злились"
    assert said[0].levelno >= logging.INFO, "на DEBUG цього не видно"
    assert len(said) == 1, (
        f"сказано {len(said)} разів за ~8 ітерацій — прапорець «один раз» "
        f"не працює, лог засмічуватиметься")
