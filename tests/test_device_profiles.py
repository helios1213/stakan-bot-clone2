"""Свій СТАБІЛЬНИЙ профіль пристрою на слот.

ЩО БУЛО ЗЛАМАНО. Усі слоти обох ботів (до чотирьох акаунтів) ходили з
ОДНАКОВИМ профілем: той самий UA, ОС, TLS-відбиток і `sys` у dolos-p0.
Відрізнявся лише visitor_id — тобто чотири акаунти виглядали одним пристроєм.

ЧОМУ НЕ РАНДОМ. У реального браузера відбиток СТАБІЛЬНИЙ: людина не міняє ОС
щодня. Профіль, що стрибає при кожному старті, — сам по собі яскравіший маркер
автоматизації, ніж однаковість. Тому профіль = f(visitor_id): детермінований,
переживає рестарт, міняється лише разом із ротацією вебкея.

ГОЛОВНЕ, ЩО ПІНИМО: УЗГОДЖЕНІСТЬ усередині профілю. TLS-ціль, User-Agent,
sec-ch-ua, sec-ch-ua-platform і sys/sys_ver у p0 мусять називати ОДНЕ й те саме.
Розсинхрон усередині гірший за будь-яку однаковість між слотами.
"""
from __future__ import annotations

import re

import pytest

from src.execution.webkey import device_profile as dp
from src.execution.webkey.client import MexcWebClient

V1 = "Y0B3VbBfdYpoEEF4eZ8t"
V2 = "aB9xKq2mNp7RtLw4Zv6E"


def _client(visitor):
    return MexcWebClient("WEB" + "0" * 64, visitor)


# ---- стабільність (а не рандом) -------------------------------------------

def test_profile_is_stable_for_the_same_visitor():
    """Рестарт не має міняти пристрій."""
    assert dp.for_visitor(V1) == dp.for_visitor(V1)
    assert _client(V1).impersonate == _client(V1).impersonate


def test_nothing_random_is_used():
    """random/uuid тут = профіль стрибає між рестартами."""
    import inspect
    src = inspect.getsource(dp)
    for bad in ("random", "uuid", "time.time"):
        assert bad not in src, f"{bad} робить профіль нестабільним"


def test_empty_visitor_is_deterministic_not_random():
    assert dp.for_visitor(None) == dp.for_visitor("")


# ---- різні слоти — різні пристрої ----------------------------------------

def test_different_visitors_can_get_different_profiles():
    seen = {dp.for_visitor(f"visitor{i:03d}").impersonate for i in range(40)}
    assert len(seen) > 1, "усі слоти знову виглядають одним пристроєм"


def test_the_pool_has_room_for_four_accounts():
    assert dp.profile_count() >= 4


# ---- УЗГОДЖЕНІСТЬ усередині профілю (найважливіше) -----------------------

@pytest.mark.parametrize("p", dp._PROFILES, ids=lambda p: p.impersonate + p.sys)
def test_tls_target_matches_the_user_agent(p):
    ver = re.fullmatch(r"chrome(\d+)[a-z]?", p.impersonate).group(1)
    assert f"Chrome/{ver}.0.0.0" in p.user_agent


@pytest.mark.parametrize("p", dp._PROFILES, ids=lambda p: p.impersonate + p.sys)
def test_sec_ch_ua_matches_the_user_agent(p):
    ver = re.fullmatch(r"chrome(\d+)[a-z]?", p.impersonate).group(1)
    assert f'"Google Chrome";v="{ver}"' in p.sec_ch_ua
    assert f'"Chromium";v="{ver}"' in p.sec_ch_ua
    assert '"Not.A/Brand";v="8"' in p.sec_ch_ua, "GREASE-версія як у знімку"


@pytest.mark.parametrize("p", dp._PROFILES, ids=lambda p: p.impersonate + p.sys)
def test_os_agrees_across_ua_platform_and_dolos(p):
    """UA каже macOS, а p0 каже Windows — це гірше, ніж нічого не міняти."""
    if p.sys == "Mac OS":
        assert "Macintosh" in p.user_agent and p.sec_ch_ua_platform == '"macOS"'
        assert p.sys_ver == "10.15.7" and "10_15_7" in p.user_agent
    else:
        assert "Windows" in p.user_agent and p.sec_ch_ua_platform == '"Windows"'
        assert p.sys_ver == "10.0" and "Windows NT 10.0" in p.user_agent


@pytest.mark.parametrize("p", dp._PROFILES, ids=lambda p: p.impersonate + p.sys)
def test_every_target_exists_in_curl_cffi(p):
    """Неіснуюча ціль валить сесію на старті, тобто вбиває слот повністю."""
    import typing
    from curl_cffi.requests.impersonate import BrowserTypeLiteral
    assert p.impersonate in set(typing.get_args(BrowserTypeLiteral))


def test_no_stale_browser_versions():
    """Старий браузер — теж аномалія. Тримаємо свіжі."""
    for p in dp._PROFILES:
        ver = int(re.fullmatch(r"chrome(\d+)[a-z]?", p.impersonate).group(1))
        assert ver >= 136, f"{p.impersonate} задавнений"


# ---- профіль справді доходить до запиту ----------------------------------

def test_client_uses_the_profile_end_to_end():
    c = _client(V2)
    p = dp.for_visitor(V2)
    assert c.impersonate == p.impersonate
    assert c.user_agent == p.user_agent
    h = c._common_headers(symbol="SOXL_USDT")
    assert h["user-agent"] == p.user_agent
    assert h["sec-ch-ua"] == p.sec_ch_ua
    assert h["sec-ch-ua-platform"] == p.sec_ch_ua_platform


def test_dolos_payload_agrees_with_the_headers():
    """sys у p0 має збігатись із тим, що в UA — інакше ми самі себе видаємо."""
    for v in (V1, V2):
        c = _client(v)
        d = c._device_payload()
        assert d["sys"] == dp.for_visitor(v).sys
        assert d["sys_ver"] == dp.for_visitor(v).sys_ver
        if d["sys"] == "Mac OS":
            assert "Macintosh" in c.user_agent
        else:
            assert "Windows" in c.user_agent


def test_explicit_arguments_still_win():
    """Проби й тести задають UA свідомо — профіль не має їх перебивати."""
    c = MexcWebClient("WEB" + "0" * 64, V1, impersonate="chrome131",
                      user_agent="custom-agent")
    assert c.impersonate == "chrome131" and c.user_agent == "custom-agent"


def test_profiles_can_be_disabled(monkeypatch):
    """Відкат до єдиного спільного профілю — однією змінною."""
    import importlib
    from src.execution.webkey import client as m
    monkeypatch.setenv("MEXC_DEVICE_PROFILE", "0")
    m2 = importlib.reload(m)
    try:
        a = m2.MexcWebClient("WEB" + "0" * 64, V1)
        b = m2.MexcWebClient("WEB" + "0" * 64, V2)
        assert a.impersonate == b.impersonate == m2._CHROME_IMPERSONATE
    finally:
        monkeypatch.delenv("MEXC_DEVICE_PROFILE", raising=False)
        importlib.reload(m)


# ---- прив'язка до СЛОТА, а не до вебкея ----------------------------------
# Знайдено оператором: при виборі hash(visitor_id) % N (а) два незалежні слоти
# легко брали ОДИН профіль — на чотирьох акаунтах це сталось одразу; і (б)
# ротація вебкея міняла visitor_id, тобто перелогін давав слоту НОВИЙ пристрій,
# чого в реальності не буває — людина перезаходить на тому самому компʼютері.

def test_rotating_the_webkey_does_not_change_the_device():
    """Головне тут. Перелогін на біржі не має перетворювати слот на інший ПК."""
    a = MexcWebClient("WEB" + "0" * 64, "СТАРИЙ_visitor_id", slot_id=1)
    b = MexcWebClient("WEB" + "1" * 64, "НОВИЙ_visitor_id", slot_id=1)
    assert a.impersonate == b.impersonate
    assert a.user_agent == b.user_agent
    assert a._device_payload()["sys"] == b._device_payload()["sys"]


def test_slots_on_one_machine_never_collide():
    """Зсув за номером слота -> різні профілі за побудовою, поки слотів <= N."""
    seen = [dp.for_slot(i, seed="машина-А") for i in range(dp.profile_count())]
    assert len({p.impersonate + p.sys for p in seen}) == dp.profile_count()


def test_two_machines_get_different_devices_for_the_same_slot():
    """Обидві коробки звуться `vultr`, тож без явного зсуву слот 1 на primary
    збігся б зі слотом 1 на клоні.

    ПЕРША ВЕРСІЯ ЦЬОГО ТЕСТУ БУЛА МАРНОЮ: вона брала вигадані посіви, які
    випадково розійшлись, і пропустила те, що СПРАВЖНІ посіви primary/clone
    дають ОДНАКОВИЙ `sha256(посів)[0] % 6`. Тому нижче — реальні значення
    з docker-compose обох ботів."""
    a = dp.for_slot(1, offset=0)   # primary, MEXC_DEVICE_OFFSET=0
    b = dp.for_slot(1, offset=2)   # клон,    MEXC_DEVICE_OFFSET=2
    assert (a.impersonate, a.sys) != (b.impersonate, b.sys)


def test_all_four_production_accounts_are_distinct_devices():
    """Головний тест цієї фічі: 2 боти × 2 слоти = чотири РІЗНІ пристрої.
    Саме це й ламалось — оператор помітив, що профілі збіглись."""
    got = [(dp.for_slot(s, offset=o).impersonate, dp.for_slot(s, offset=o).sys)
           for o in (0, 2) for s in (1, 2)]
    assert len(set(got)) == 4, f"пристрої повторюються: {got}"


# monkeypatch, а не пряма мутація `os.environ`. Три тести нижче колись
# БЕЗУМОВНО робили `os.environ.pop(...)` у `finally` — тобто прогін у робочому
# контейнері лишав процес БЕЗ `MEXC_DEVICE_SEED` і `MEXC_DEVICE_OFFSET`, хоч
# вони там задані. Наступний тест у тому ж прогоні бачив уже інший світ.
# monkeypatch відновлює ПОПЕРЕДНЄ значення, а не видаляє його.

def test_offset_beats_the_seed(monkeypatch):
    """Хеш посіву — везіння; явний зсув — гарантія. Зсув має перемагати."""
    monkeypatch.setenv("MEXC_DEVICE_SEED", "будь-що")
    monkeypatch.setenv("MEXC_DEVICE_OFFSET", "3")
    assert dp.machine_offset() == 3


def test_broken_offset_does_not_crash(monkeypatch):
    monkeypatch.setenv("MEXC_DEVICE_OFFSET", "не-число")
    assert 0 <= dp.machine_offset() < dp.profile_count()


def test_broken_offset_is_not_silently_explicit(monkeypatch):
    """Одруживка в compose провалюється у запасний шлях — і має бути видно.

    `offset_is_explicit()` каже False саме тому, що попередження в main.py
    мусить спрацювати: інакше оператор думає, що захист від збігу профілів
    стоїть, а він мовчки зник.
    """
    monkeypatch.setenv("MEXC_DEVICE_OFFSET", "не-число")
    assert dp.offset_is_explicit() is False


def test_missing_seed_is_detectable(monkeypatch):
    """Мовчазний дефолт означав би однакові пристрої на двох ботах."""
    monkeypatch.delenv("MEXC_DEVICE_SEED", raising=False)
    assert dp.seed_is_explicit() is False


def test_seed_comes_from_env(monkeypatch):
    monkeypatch.setenv("MEXC_DEVICE_SEED", "моя-машина")
    assert dp.machine_seed() == "моя-машина"
    assert dp.seed_is_explicit() is True


def test_profile_is_stable_across_restarts_for_a_slot():
    assert dp.for_slot(2, seed="s") == dp.for_slot(2, seed="s")


# ---------------------------------------------------------------------------
# MEXC_CHROME_VER мусить ПЕРЕБИВАТИ профіль (аудит 2026-08-26)
# ---------------------------------------------------------------------------

def test_chrome_version_override_keeps_the_profile_os():
    """Примусова версія не має міняти ОС — інакше ми створюємо розсинхрон.

    Важіль був МЕРТВИЙ: при увімкнених профілях (дефолт) усі чотири поля
    бралися з профілю, бо умова `impersonate == _CHROME_IMPERSONATE`
    лишалась істинною і з env, і без нього.
    """
    base = dp.for_slot(1, offset=1)          # якийсь конкретний профіль
    forced = base.with_chrome_version("133a")
    assert forced.impersonate == "chrome133a"
    assert "Chrome/133a" in forced.user_agent
    assert '"Google Chrome";v="133a"' in forced.sec_ch_ua
    # ОС і все, що з неї випливає, лишається профільним:
    assert forced.sec_ch_ua_platform == base.sec_ch_ua_platform
    assert (forced.sys, forced.sys_ver) == (base.sys, base.sys_ver)


def test_chrome_version_override_stays_coherent_across_all_sources():
    """Та сама вимога, що й до звичайного профілю: TLS ↔ UA ↔ sec-ch-ua."""
    import re
    forced = dp.for_slot(0, offset=0).with_chrome_version(142)
    v = forced.impersonate.removeprefix("chrome")
    assert re.search(rf"Chrome/{v}\.", forced.user_agent)
    assert f'"Google Chrome";v="{v}"' in forced.sec_ch_ua
    assert f'"Chromium";v="{v}"' in forced.sec_ch_ua


def test_spot_client_takes_the_same_device_as_the_slot():
    """Спот і фʼючерси ходять під ОДНИМ акаунтом з ОДНІЄЇ IP.

    До 2026-08-26 спот хардкодив `Chrome/151` при `impersonate="chrome"`
    (цілі 151 у curl_cffi НЕМАЄ) і не слав `sec-ch-ua` взагалі.
    """
    from src.execution.webkey.spot_client import SpotWebClient
    c = SpotWebClient("dummy-key", slot_id=2)
    prof = dp.for_slot(2)
    assert c._profile.user_agent == prof.user_agent
    h = c._headers()
    assert h["user-agent"] == prof.user_agent
    assert h["sec-ch-ua"] == prof.sec_ch_ua
    assert h["sec-ch-ua-platform"] == prof.sec_ch_ua_platform
    assert "151" not in h["user-agent"], "хардкод Chrome/151 повернувся"


def test_ws_handshake_is_not_a_python_client():
    """`User-Agent: Python/3.11 websockets/13.1` вʼяже акаунт із неброузерним
    клієнтом напряму — і на ПРИВАТНОМУ каналі, який шле вебкей."""
    from src.exchanges.mexc_private_ws import _ws_headers
    h = _ws_headers(1)
    assert h and "Python" not in h["User-Agent"]
    assert h["User-Agent"] == dp.for_slot(1).user_agent


def test_trochilus_uid_is_per_slot(monkeypatch):
    """uid різний НА КОЖЕН АКАУНТ, тобто на слот. Одна глобальна змінна
    відправляла б чужий uid із сусіднього акаунта."""
    from src.execution.webkey import client as cl
    monkeypatch.setenv("MEXC_TROCHILUS_UID", "111")
    monkeypatch.setenv("MEXC_TROCHILUS_UID_SLOT2", "222")
    assert cl._trochilus_uid(2) == "222"
    assert cl._trochilus_uid(1) == "111", "запасна глобальна перестала діяти"
    monkeypatch.delenv("MEXC_TROCHILUS_UID")
    assert cl._trochilus_uid(1) == "", "порожньо = заголовок не шлеться зовсім"


def test_no_hardcoded_browser_version_anywhere_in_src():
    """ЗАПОБІЖНИК ВІД ПОВЕРНЕННЯ. Хардкод версії браузера — це розсинхрон, що
    чекає нагоди: TLS іде з curl_cffi, UA з константи, і вони розходяться
    мовчки. `Chrome/151` як TLS-ціль НЕ ІСНУЄ взагалі, а UA з ним ішов у
    чотирьох файлах, включно з тими, що ходять із вебкеєм.

    Шукаємо `Chrome/<число>` у РЯДКОВИХ ЛІТЕРАЛАХ, не в коментарях: пояснення
    в коментарі — це історія, а не поведінка.
    """
    import ast
    import pathlib

    bad = []
    root = pathlib.Path(__file__).resolve().parent.parent / "src"
    pat = re.compile(r"Chrome/\d")
    for f in root.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        # Докстрінги — це ПОЯСНЕННЯ (зокрема історія цього самого дефекту),
        # а не значення, що йде на дріт. Збираємо їх, щоб не ловити себе ж.
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docs.add(id(body[0].value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docs and pat.search(node.value)):
                bad.append(f"{f.relative_to(root)}:{node.lineno}")
    # device_profile сам будує UA — там літерал є за визначенням.
    bad = [b for b in bad if not b.startswith("execution/webkey/device_profile.py")]
    assert not bad, (
        "версію браузера прибито в рядку — бери її з device_profile: "
        + ", ".join(bad))
