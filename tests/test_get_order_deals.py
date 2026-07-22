"""
Tests for get_order_deals endpoint (fast-poll patch, May 2026).

Verifies:
  1. URL path is /api/platform/futures/api/v1/private/order/deal_details/{order_id}
  2. Method is GET (not POST)
  3. No dolos/web signing required for this read-only endpoint (consistent with
     other GET endpoints like get_open_positions).
  4. Response is passed through unchanged.
"""
import pytest
from unittest.mock import patch

from src.execution.webkey.client import MexcWebClient


@pytest.fixture
def client():
    return MexcWebClient(
        webkey="WEBdeadbeef",
        visitor_id="v1isit0rId",
    )


@pytest.mark.asyncio
async def test_get_order_deals_calls_correct_url(client):
    """Endpoint should be GET /order/deal_details/{order_id}."""
    captured = {}

    async def fake_request(method, endpoint, body=None, query_params="",
                           needs_dolos=False, needs_web_sign=False):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["body"] = body
        captured["needs_dolos"] = needs_dolos
        captured["needs_web_sign"] = needs_web_sign
        return {"code": 0, "data": []}

    with patch.object(client, "_request", side_effect=fake_request):
        await client.get_order_deals("1234567890")

    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/order/deal_details/1234567890"
    assert captured["body"] is None
    # Read endpoint — no signing needed (matches get_open_positions pattern)
    assert captured["needs_dolos"] is False
    assert captured["needs_web_sign"] is False


@pytest.mark.asyncio
async def test_get_order_deals_returns_payload_unchanged(client):
    """Response from _request should be returned as-is."""
    expected = {
        "success": True,
        "code": 0,
        "data": [
            {
                "id": 999,
                "orderId": "1234567890",
                "symbol": "ZEC_USDT",
                "price": 432.5,
                "vol": 10,
                "fee": 0.017,
                "taker": True,
            }
        ],
    }
    with patch.object(client, "_request", return_value=expected):
        result = await client.get_order_deals("1234567890")

    assert result == expected
