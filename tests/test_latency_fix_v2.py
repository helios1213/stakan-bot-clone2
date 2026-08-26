"""Tests for latency-fix-v2 patch: get_order_deals + impersonate alignment."""
import pytest
from unittest.mock import AsyncMock


# ---- get_order_deals tests ----

@pytest.mark.asyncio
async def test_get_order_deals_calls_correct_endpoint():
    """get_order_deals should call GET /order/deal_details/{order_id}."""
    from src.execution.webkey.client import MexcWebClient

    client = MexcWebClient(
        webkey="test_key",
        visitor_id="test_visitor",
    )
    # Mock _request to capture what endpoint is called
    client._request = AsyncMock(return_value={"code": 0, "data": []})

    await client.get_order_deals("12345")

    client._request.assert_called_once_with(
        "GET", "/order/deal_details/12345",
    )


@pytest.mark.asyncio
async def test_get_order_deals_returns_response():
    """get_order_deals should return the raw response dict."""
    from src.execution.webkey.client import MexcWebClient

    client = MexcWebClient(
        webkey="test_key",
        visitor_id="test_visitor",
    )
    expected = {
        "code": 0,
        "data": [
            {"price": "432.50", "vol": 100, "feeCurrency": "USDT"},
            {"price": "432.51", "vol": 50, "feeCurrency": "USDT"},
        ],
    }
    client._request = AsyncMock(return_value=expected)

    result = await client.get_order_deals("67890")
    assert result == expected
    assert result["code"] == 0
    assert len(result["data"]) == 2


@pytest.mark.asyncio
async def test_get_order_deals_no_auth_signing():
    """get_order_deals is a read — should NOT need dolos or web signing."""
    from src.execution.webkey.client import MexcWebClient

    client = MexcWebClient(
        webkey="test_key",
        visitor_id="test_visitor",
    )
    client._request = AsyncMock(return_value={"code": 0, "data": []})

    await client.get_order_deals("99999")

    # _request should be called WITHOUT needs_dolos / needs_web_sign
    args, kwargs = client._request.call_args
    assert "needs_dolos" not in kwargs or kwargs.get("needs_dolos") is False
    assert "needs_web_sign" not in kwargs or kwargs.get("needs_web_sign") is False


# ---- impersonate alignment tests ----

def test_impersonate_is_aligned_with_the_ua_headers():
    """Версія більше НЕ прибита до 136: вона береться з _CHROME_VER, і та сама
    змінна живить UA і sec-ch-ua. Пінимо саме УЗГОДЖЕНІСТЬ, а не число —
    інакше кожне підняття версії ламало б тест на рівному місці.
    (Дефолт піднято 136 -> 146 після знімка живого браузера 2026-08-26, де
    реальний Chrome виявився 147; 146 — найновіша ціль, яку знає curl_cffi.)"""
    import re
    from src.execution.webkey import client as m

    # Кожен слот має власний профіль, тож звіряємо клієнта САМ ІЗ СОБОЮ:
    # TLS-ціль, UA і sec-ch-ua мусять називати одну версію.
    for slot in (None, 1, 2):
        c = m.MexcWebClient(webkey="test_key", visitor_id="test_visitor",
                            slot_id=slot)
        v = re.fullmatch(r"chrome(\d+)[a-z]?", c.impersonate).group(1)
        assert f"Chrome/{v}.0.0.0" in c.user_agent
        assert f'"Google Chrome";v="{v}"' in c.sec_ch_ua
        assert f'"Chromium";v="{v}"' in c.sec_ch_ua


def test_ua_matches_impersonate_version():
    """UA і sec-ch-ua мають називати ТУ САМУ версію, що й TLS-ціль.
    Число не фіксуємо — фіксуємо збіг: розсинхрон «TLS каже одне, заголовок
    інше» і є тим, що фінгерпринт-системи бачать найлегше."""
    from src.execution.webkey import client as m

    v = m._CHROME_VER
    assert f"Chrome/{v}." in m._DEFAULT_UA, m._DEFAULT_UA
    assert f'"Google Chrome";v="{v}"' in m._DEFAULT_SEC_CH_UA, m._DEFAULT_SEC_CH_UA
    assert f'"Chromium";v="{v}"' in m._DEFAULT_SEC_CH_UA, m._DEFAULT_SEC_CH_UA
    assert m._CHROME_IMPERSONATE == f"chrome{v}"


def test_ua_and_sec_ch_ua_same_major_version():
    """UA major version and sec-ch-ua version must match to avoid Akamai mismatch."""
    import re
    from src.execution.webkey.client import _DEFAULT_UA, _DEFAULT_SEC_CH_UA

    # Extract Chrome/NNN from UA
    ua_match = re.search(r"Chrome/(\d+)", _DEFAULT_UA)
    assert ua_match, f"Could not find Chrome/NNN in UA: {_DEFAULT_UA}"
    ua_version = ua_match.group(1)

    # Extract version from sec-ch-ua
    sec_match = re.search(r'"Google Chrome";v="(\d+)"', _DEFAULT_SEC_CH_UA)
    assert sec_match, f"Could not find version in sec-ch-ua: {_DEFAULT_SEC_CH_UA}"
    sec_version = sec_match.group(1)

    assert ua_version == sec_version, (
        f"UA says Chrome/{ua_version} but sec-ch-ua says v={sec_version}"
    )


def test_impersonate_matches_the_clients_own_ua():
    """TLS-ціль і UA мають називати ТУ САМУ версію.

    Звіряємо з UA САМОГО КЛІЄНТА, а не з модульним _DEFAULT_UA: з 2026-08-26
    кожен слот має власний профіль пристрою, тож модульний дефолт більше не
    описує конкретного клієнта. Раніше тут допускався розбіг до 2 версій —
    тепер вимагаємо точний збіг, бо профіль будує обидва поля з одного числа.
    """
    import re
    from src.execution.webkey.client import MexcWebClient

    for slot in (None, 1, 2, 3):
        c = MexcWebClient(webkey="k", visitor_id="v" * 20, slot_id=slot)
        imp = int(re.search(r"(\d+)", c.impersonate).group(1))
        ua = int(re.search(r"Chrome/(\d+)", c.user_agent).group(1))
        assert imp == ua, f"slot={slot}: TLS={c.impersonate} проти UA Chrome/{ua}"
