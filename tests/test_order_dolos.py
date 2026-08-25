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
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
    ("1", True), ("true", True), ("yes", True),
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
    seg = src[i:i + 1400]
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
