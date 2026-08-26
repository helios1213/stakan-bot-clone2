"""dolos на /order/create — ПОВЕРНУТО 2026-08-25 (рішення оператора).

Історія в двох реченнях. Абляція 2026-08-12 довела, що MEXC приймає ордер і
БЕЗ dolos-блоку, і його прибрали заради швидкості. 25.08 акаунт втратив 0%
промо, оператор вирішив повернути браузерний шлях.

Гарантії, які тут пінимо:
  1. За замовчуванням /order/create ШЛЕ dolos (`needs_dolos=True`), web-sign
     лишається обовʼязковим у будь-якому разі (без нього code=602).
  2. `MEXC_DOLOS_ON_ORDER=0` вимикає назад БЕЗ зміни коду.
  3. Збій збірання dolos НЕ вбиває ордер — він іде плоским тілом (біржа таке
     приймає, див. абляцію) і гучно логується. Раніше ця гілка на order/create
     не виконувалась узагалі, тож виняток у ній ніхто не ловив, а тепер це
     гарячий шлях із реальними грошима.
  4. `close_all_positions` як слав dolos, так і слє — не чіпали.

ВАРТІСТЬ, виміряна а не оцінена: sign_dolos p50=1.708мс проти HTTP RTT
p50=152мс, тобто ~1% шляху.

ЧЕСНО: доказів, що dolos-drop спричинив втрату промо, немає, і є три виміри
проти (клон із dolos-drop від 13.08 має 24 303 угоди без жодної комісії;
перше спрацювання fee-guard на primary — 17.08, ДО приїзду dolos-drop; тариф
читається з /account/tiered_fee_rate як властивість АКАУНТА). Повернуто як
рішення оператора при мізерній ціні, а не як доведений фікс.

_request стабиться, щоб ловити kwargs без мережі.
"""
from __future__ import annotations

import pytest

from src.execution.webkey import client as client_mod


class _FakeCookies:
    def set(self, *a, **k): pass


class _FakeSession:
    def __init__(self, **kw): self.cookies = _FakeCookies()
    async def close(self): pass


def _client(monkeypatch):
    monkeypatch.setattr(client_mod.curl_requests, "AsyncSession", _FakeSession)
    return client_mod.MexcWebClient(webkey="WEB" + "0" * 64, visitor_id="v" * 20)


@pytest.mark.asyncio
async def test_order_create_sends_dolos_and_web_sign(monkeypatch):
    c = _client(monkeypatch)
    seen = {}

    async def fake_request(method, endpoint, **kwargs):
        seen["method"] = method
        seen["endpoint"] = endpoint
        seen.update(kwargs)
        return {"code": 0, "data": {"orderId": "1"}}

    monkeypatch.setattr(c, "_request", fake_request)
    await c.submit_order(symbol="ZEC_USDT", side=1, vol=1, leverage=1,
                         order_type="3", price="1")

    assert seen["endpoint"] == "/order/create"
    assert seen["needs_dolos"] is True, "order/create має слати dolos (дефолт з 2026-08-25)"
    assert seen["needs_web_sign"] is True, "order/create must keep web-sign"


@pytest.mark.asyncio
async def test_close_all_still_uses_dolos(monkeypatch):
    c = _client(monkeypatch)
    seen = {}

    async def fake_request(method, endpoint, **kwargs):
        seen["endpoint"] = endpoint
        seen.update(kwargs)
        return {"code": 0}

    monkeypatch.setattr(c, "_request", fake_request)
    await c.close_all_positions("ZEC_USDT")

    assert seen["endpoint"] == "/position/close_all"
    assert seen["needs_dolos"] is True, "close_all was not ablated — dolos must stay"
    assert seen["needs_web_sign"] is True


# ---- прапорець вимкнення без зміни коду ----------------------------------

def test_flag_defaults_to_on(monkeypatch):
    import importlib
    monkeypatch.delenv("MEXC_DOLOS_ON_ORDER", raising=False)
    m = importlib.reload(client_mod)
    try:
        assert m._DOLOS_ON_ORDER is True
    finally:
        importlib.reload(client_mod)


@pytest.mark.parametrize("val,expect", [
    ("0", False), ("false", False), ("no", False), ("off", False),
    ("1", True), ("true", True), ("yes", True),
    # ПОРОЖНЄ значення = «не задано» -> вирішує MEXC_PATH_MODE (дефолт full).
    # Раніше порожнє означало «вимкнено», і це було пасткою: людина, що
    # виставила змінну без значення, мовчки лишалась без dolos.
    ("", True),
])
def test_flag_env_override(monkeypatch, val, expect):
    """Відкат має бути однією змінною в compose, як з MEXC_API_HOST."""
    import importlib
    monkeypatch.setenv("MEXC_DOLOS_ON_ORDER", val)
    m = importlib.reload(client_mod)
    try:
        assert m._DOLOS_ON_ORDER is expect
    finally:
        monkeypatch.delenv("MEXC_DOLOS_ON_ORDER", raising=False)
        importlib.reload(client_mod)


# ---- збій dolos не має вбивати ордер -------------------------------------

def test_dolos_failure_degrades_instead_of_killing_the_order():
    """Найважливіше тут. dolos — це ПІДПИС, а не торгове рішення: біржа
    приймає і плоске тіло. Виняток тут означав би втрачений ордер на живих
    грошах, причому на шляху, який до 25.08 не виконувався взагалі."""
    import inspect
    src = inspect.getsource(client_mod.MexcWebClient._request)
    i = src.index("if needs_dolos and body is not None:")
    seg = src[i:i + 2200]   # блок підріс разом із _device_payload()
    assert "try:" in seg and "except Exception:" in seg
    assert "full_body = body" in seg, "деградація має повертати ПЛОСКЕ тіло"
    assert "logger.error" in seg, "мовчазна деградація зробила б проблему невидимою"


def test_empty_visitor_id_is_caught_explicitly():
    import inspect
    src = inspect.getsource(client_mod.MexcWebClient._request)
    assert 'getattr(self.dolos, "visitor_id", None)' in src


def test_order_create_reads_the_flag_not_a_literal():
    import inspect
    src = inspect.getsource(client_mod.MexcWebClient.submit_order)
    assert "needs_dolos=_DOLOS_ON_ORDER" in src, (
        "жорсткий літерал тут зробив би прапорець декоративним")


# ---- узгодженість відбитка ------------------------------------------------
# Найлегше, що бачить фінгерпринт-система, — не «поганий» відбиток, а
# РОЗСИНХРОН: TLS каже одну версію Chrome, а заголовок іншу. Зараз усі три
# джерела беруться з однієї змінної; ці тести стежать, щоб їх не рознесло.

def test_tls_target_and_headers_report_the_same_chrome():
    import importlib
    m = importlib.reload(client_mod)
    try:
        v = m._CHROME_VER
        assert m._CHROME_IMPERSONATE == f"chrome{v}"
        assert f"Chrome/{v}.0.0.0" in m._DEFAULT_UA
        assert f'"Google Chrome";v="{v}"' in m._DEFAULT_SEC_CH_UA
        assert f'"Chromium";v="{v}"' in m._DEFAULT_SEC_CH_UA
    finally:
        importlib.reload(client_mod)


def test_version_moves_everywhere_at_once(monkeypatch):
    """Зсув однієї частини створив би той розсинхрон, якого зараз немає."""
    import importlib
    monkeypatch.setenv("MEXC_CHROME_VER", "146")
    m = importlib.reload(client_mod)
    try:
        assert m._CHROME_IMPERSONATE == "chrome146"
        assert "Chrome/146.0.0.0" in m._DEFAULT_UA
        assert 'v="146"' in m._DEFAULT_SEC_CH_UA
    finally:
        monkeypatch.delenv("MEXC_CHROME_VER", raising=False)
        importlib.reload(client_mod)


def test_no_hardcoded_chrome_version_left():
    from pathlib import Path
    src = Path("src/execution/webkey/client.py").read_text()
    body = src[src.index("_CHROME_VER = "):]
    assert "chrome136" not in body and "Chrome/136" not in body, (
        "літерал версії повертає розсинхрон чорним ходом")


def test_impersonate_target_is_one_curl_cffi_knows():
    import importlib, typing
    m = importlib.reload(client_mod)
    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral
        known = set(typing.get_args(BrowserTypeLiteral))
        assert m._CHROME_IMPERSONATE in known, (
            f"{m._CHROME_IMPERSONATE} немає в curl_cffi — сесія впаде на старті")
    finally:
        importlib.reload(client_mod)


# ---- звірка з ЖИВИМ знімком браузера (2026-08-26) -------------------------
# Оператор зняв реальний запит /order/create зі свого браузера. Нижче — те, що
# розійшлось, і тести, щоб воно не розійшлось знову. Це вже не здогади про
# «кращий відбиток», а дослівне порівняння.

def test_referer_follows_the_traded_symbol():
    """Було: referer ЗАВЖДИ вказував на ZEC_USDT, хоч ордер ішов на SOXL.
    У браузері referer = сторінка ТІЄЇ САМОЇ пари. Найдешевша для виявлення
    розбіжність з усіх, що ми мали."""
    import inspect
    src = inspect.getsource(client_mod.MexcWebClient._common_headers)
    assert '_ref_sym' in src and '{_ref_sym}' in src
    assert '{_WARMUP_SYMBOL}?type=linear_swap' not in src, "referer знову прибитий до ZEC"


def test_symbol_is_taken_from_the_request_body():
    import inspect
    src = inspect.getsource(client_mod.MexcWebClient._request)
    assert 'body.get("symbol")' in src
    assert 'symbol=_sym' in src


def test_referer_defaults_safely_when_there_is_no_symbol():
    """GET-и без тіла не мають ламатись через відсутній символ."""
    import inspect
    src = inspect.getsource(client_mod.MexcWebClient._common_headers)
    assert 'symbol or _WARMUP_SYMBOL' in src


def test_no_literal_zero_trochilus_uid(monkeypatch):
    """Слали `trochilus-uid: 0` — найдешевший маркер «це не браузер».
    Тепер: не задано в env -> заголовка немає взагалі."""
    import importlib
    monkeypatch.delenv("MEXC_TROCHILUS_UID", raising=False)
    m = importlib.reload(client_mod)
    try:
        assert m._TROCHILUS_UID == ""
        src = __import__("inspect").getsource(m.MexcWebClient._common_headers)
        assert '"trochilus-uid": "0"' not in src
        assert 'if _TROCHILUS_UID:' in src
    finally:
        importlib.reload(client_mod)


def test_trochilus_uid_is_sent_when_configured(monkeypatch):
    """Значення РІЗНЕ на двох ботах (це id акаунта), тому env, а не константа."""
    import importlib
    monkeypatch.setenv("MEXC_TROCHILUS_UID", "12345678")
    m = importlib.reload(client_mod)
    try:
        assert m._TROCHILUS_UID == "12345678"
    finally:
        monkeypatch.delenv("MEXC_TROCHILUS_UID", raising=False)
        importlib.reload(client_mod)


def test_sec_ch_ua_matches_the_browser_format():
    """Знімок: `"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"`.
    У нас був інший ПОРЯДОК брендів і застаріла GREASE-версія v="99"."""
    import importlib
    m = importlib.reload(client_mod)
    try:
        v = m._CHROME_VER
        assert m._DEFAULT_SEC_CH_UA == (
            f'"Google Chrome";v="{v}", "Not.A/Brand";v="8", "Chromium";v="{v}"')
        assert 'v="99"' not in m._DEFAULT_SEC_CH_UA
    finally:
        importlib.reload(client_mod)


def test_chash_is_overridable_without_a_code_change():
    """Перемикання має бути ОДНІЄЮ змінною: помилка в chash ламає КОЖЕН ордер,
    тож відкат не має вимагати правки коду й перезбірки.

    БЕЗ importlib.reload(credentials) СВІДОМО. Перезавантаження цього модуля
    ламає ІДЕНТИЧНІСТЬ класів винятків для тестів, які їх уже імпортували, і
    `pytest.raises` перестає впізнавати цілком правильний виняток. Пастка
    описана в CLAUDE.md, і я на неї тут наступив: 7 падінь у
    test_webkey_credentials.py, причому ЛИШЕ коли той файл іде ПІСЛЯ цього.
    Перевіряємо ДЖЕРЕЛО, а не перезавантажений модуль."""
    from pathlib import Path
    src = Path("src/execution/webkey/credentials.py").read_text()
    assert 'os.environ.get("MEXC_CHASH", "")' in src
    assert "_BOOTSTRAP_CHASH_DEFAULT" in src


def test_chash_default_is_the_value_from_the_live_browser():
    """Знімок 2026-08-26: браузер шле d6c64d28…; перемкнуто рішенням оператора."""
    from src.execution.webkey.credentials import BOOTSTRAP_CHASH
    assert BOOTSTRAP_CHASH == (
        "d6c64d28e362f314071b3f9d78ff7494d9cd7177ae0465e772d1840e9f7905d8")


def test_previous_chash_is_kept_for_rollback():
    """Відкат має бути можливим без археології в git."""
    from pathlib import Path
    src = Path("src/execution/webkey/credentials.py").read_text()
    assert "973e5a66902be9ff97f3e916b71d4535c47b8a30c5f4122a7683d6ef701f30dd" in src


# ---- повна емуляція: p0 містить поля СЦЕНИ, а не поля ордера -------------

def test_p0_carries_every_field_the_server_scene_asks_for():
    """Раніше ми клали в p0 параметри ордера, яких не просить ЖОДНА сцена."""
    from src.execution.webkey import dolos_config as dc
    c = client_mod.MexcWebClient("WEB" + "0" * 64, "Y0B3VbBfdYpoEEF4eZ8t")
    payload = {"symbol": "SOXL_USDT", "side": 1,
               "mtoken": c.dolos.visitor_id, "mhash": c.dolos.mhash,
               **c._device_payload()}
    missing = [p for p in dc.FALLBACK_PARAMETERS if p not in payload]
    assert not missing, f"сцена просить, а ми не кладемо: {missing}"


def test_device_fields_do_not_contradict_our_own_headers():
    """p0, що каже Windows, поруч із UA, що каже macOS, — гірше за відсутність
    p0 взагалі. Пінимо УЗГОДЖЕНІСТЬ, а не конкретну ОС: із появою профілів на
    слот вона різна в різних слотів, і жорстке "Mac OS" тут зламалось би на
    рівному місці."""
    for slot in (None, 1, 2, 3):
        c = client_mod.MexcWebClient("WEB" + "0" * 64, "v" * 20, slot_id=slot)
        d = c._device_payload()
        if d["sys"] == "Mac OS":
            assert "Macintosh" in c.user_agent and d["sys_ver"] == "10.15.7"
            assert "10_15_7" in c.user_agent
        else:
            assert d["sys"] == "Windows" and "Windows NT" in c.user_agent
            assert d["sys_ver"] == "10.0"


def test_request_id_matches_the_browser_format():
    """Знімок кукі: `x_fingerprint_requestId=1787685214220.at2lp1`."""
    import re
    c = client_mod.MexcWebClient("WEB" + "0" * 64, "v" * 20)
    rid = c._device_payload()["request_id"]
    assert re.fullmatch(r"\d{13}\.[a-z0-9]{6}", rid), rid


def test_request_id_is_fresh_each_time():
    c = client_mod.MexcWebClient("WEB" + "0" * 64, "v" * 20)
    assert c._device_payload()["request_id"] != c._device_payload()["request_id"]


def test_tencent_token_is_empty_not_invented():
    """Його видає SDK Tencent, якого в нас немає. Порожнє поле пояснюється,
    вигаданий токен — ні."""
    c = client_mod.MexcWebClient("WEB" + "0" * 64, "v" * 20)
    assert c._device_payload()["tencent_device_token"] == ""


def test_hostname_matches_the_origin_we_send():
    c = client_mod.MexcWebClient("WEB" + "0" * 64, "v" * 20)
    assert c._device_payload()["hostname"] in c.BASE_URL


def test_signing_dict_uses_the_live_cache():
    from src.execution.webkey import dolos_config as dc
    c = client_mod.MexcWebClient("WEB" + "0" * 64, "v" * 20)
    sd = c.dolos.as_signing_dict
    assert sd["chash"] == dc.CACHE.get()["chash"]
    assert sd["parameters"] == dc.CACHE.get()["parameters"]


def test_legacy_mode_restores_the_old_scheme(monkeypatch):
    """Відкат до доведено робочої пари «старий chash + поля ордера»."""
    import importlib
    monkeypatch.setenv("MEXC_DOLOS_LEGACY", "1")
    m = importlib.reload(client_mod)
    try:
        c = m.MexcWebClient("WEB" + "0" * 64, "v" * 20)
        sd = c.dolos.as_signing_dict
        assert sd["chash"].startswith("973e5a66")
        assert "symbol" in sd["parameters"]
    finally:
        monkeypatch.delenv("MEXC_DOLOS_LEGACY", raising=False)
        importlib.reload(client_mod)


# ---- два режими шляху одним перемикачем ----------------------------------
# Замість пʼяти окремих змінних: full = «браузерний» (dolos + повна емуляція),
# bare = найкоротший доведений шлях (без dolos на /order/create, -3.9мс).
# TLS-профіль, заголовки, referer і новий chash лишаються В ОБОХ: вони нічого
# не коштують за часом, а close_all_positions шле dolos у будь-якому разі.

def _reload(monkeypatch, **env):
    import importlib
    for k in ("MEXC_PATH_MODE", "MEXC_DOLOS_ON_ORDER"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(client_mod)


def test_default_mode_is_full(monkeypatch):
    m = _reload(monkeypatch)
    try:
        assert m._PATH_MODE == "full" and m._DOLOS_ON_ORDER is True
    finally:
        _reload(monkeypatch)


def test_bare_mode_drops_dolos_from_the_order(monkeypatch):
    m = _reload(monkeypatch, MEXC_PATH_MODE="bare")
    try:
        assert m._DOLOS_ON_ORDER is False
    finally:
        _reload(monkeypatch)


def test_bare_mode_keeps_everything_that_is_free(monkeypatch):
    """Голий режим — про ШВИДКІСТЬ, не про відкат емуляції. Профіль пристрою,
    referer за парою і новий chash коштують 0мс, тож лишаються."""
    from src.execution.webkey.credentials import BOOTSTRAP_CHASH
    m = _reload(monkeypatch, MEXC_PATH_MODE="bare")
    try:
        c = m.MexcWebClient("WEB" + "0" * 64, "v" * 20, slot_id=1)
        assert c.impersonate.startswith("chrome")
        assert "SOXL_USDT" in c._common_headers(symbol="SOXL_USDT")["referer"]
        assert BOOTSTRAP_CHASH.startswith("d6c64d28")
    finally:
        _reload(monkeypatch)


def test_explicit_flag_overrides_the_mode(monkeypatch):
    """Щоб можна було зібрати проміжну комбінацію, не чіпаючи код."""
    m = _reload(monkeypatch, MEXC_PATH_MODE="bare", MEXC_DOLOS_ON_ORDER="1")
    try:
        assert m._DOLOS_ON_ORDER is True
    finally:
        _reload(monkeypatch)


def test_unknown_mode_falls_back_to_full(monkeypatch):
    """Друкарська помилка в env не має мовчки вимикати емуляцію."""
    m = _reload(monkeypatch, MEXC_PATH_MODE="ляля")
    try:
        assert m._PATH_MODE == "full"
    finally:
        _reload(monkeypatch)


def test_close_all_sends_dolos_in_both_modes(monkeypatch):
    """Саме там новий chash і працює, незалежно від режиму."""
    import inspect
    for mode in ("full", "bare"):
        m = _reload(monkeypatch, MEXC_PATH_MODE=mode)
        src = inspect.getsource(m.MexcWebClient.close_all_positions)
        assert "needs_dolos=True" in src, mode
    _reload(monkeypatch)
