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

def test_impersonate_default_is_chrome136():
    """Default impersonate should be chrome136 (aligned with UA headers)."""
    from src.execution.webkey.client import MexcWebClient

    client = MexcWebClient(
        webkey="test_key",
        visitor_id="test_visitor",
    )
    assert client.impersonate == "chrome136"


def test_ua_matches_impersonate_version():
    """User-Agent and sec-ch-ua should reference Chrome 136, matching impersonate."""
    from src.execution.webkey.client import _DEFAULT_UA, _DEFAULT_SEC_CH_UA

    assert "Chrome/136" in _DEFAULT_UA, f"UA should contain Chrome/136, got: {_DEFAULT_UA}"
    assert '"136"' in _DEFAULT_SEC_CH_UA, (
        f"sec-ch-ua should contain '136', got: {_DEFAULT_SEC_CH_UA}"
    )


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


def test_impersonate_close_to_ua_version():
    """impersonate version should be within 2 of UA Chrome version."""
    import re
    from src.execution.webkey.client import MexcWebClient, _DEFAULT_UA

    client = MexcWebClient(webkey="k", visitor_id="v")
    imp_match = re.search(r"(\d+)", client.impersonate)
    ua_match = re.search(r"Chrome/(\d+)", _DEFAULT_UA)

    assert imp_match and ua_match
    imp_ver = int(imp_match.group(1))
    ua_ver = int(ua_match.group(1))
    assert abs(imp_ver - ua_ver) <= 2, (
        f"impersonate={client.impersonate} (v{imp_ver}) too far from UA Chrome/{ua_ver}"
    )
