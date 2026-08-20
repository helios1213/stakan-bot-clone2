"""Tests for the spot web order client.

Nothing here touches the network or places an order. What these pin:

  1. DRY-RUN IS THE DEFAULT. A client built without arguments sends nothing.
     This is the house rule that keeps an order-placing tool safe by construction.
  2. Precision comes from the pair's own ps/qs, so MEXC does not reject the
     quantity for having more decimals than the pair allows.
  3. A transport failure is CONTAINED — logged and returned as a rejection,
     never raised into the caller's loop (one bad order must not cascade).
  4. Spot success is code 200, futures is 0 — both count as ok.
"""
from __future__ import annotations

import pytest

from src.execution.webkey.spot_client import (
    BUY,
    SELL,
    OrderResult,
    SpotWebClient,
    fmt_decimals,
    is_ok,
)
from src.execution.webkey.spot_currency import SpotCurrency, SpotCurrencyResolver

WEBKEY = "WEB" + "0" * 64

PENGU = SpotCurrency(
    ticker="PENGU", quote="USDT",
    currency_id="1c2cf4f4531b404d9bb5622f3881f32c",
    market_currency_id="128f589271cb4951b03e71e6323eb7be",
    price_scale=6, qty_scale=2, full_name="Pudgy Penguins",
    order_types=("LIMIT_ORDER", "MARKET_ORDER"),
)


class _StubResolver(SpotCurrencyResolver):
    """Resolver that answers from memory — no page fetch."""

    def __init__(self, cur: SpotCurrency = PENGU):
        super().__init__()
        self._stub = cur

    async def resolve(self, base, quote="USDT", force=False):
        return self._stub


def _client(**kw) -> SpotWebClient:
    kw.setdefault("resolver", _StubResolver())
    return SpotWebClient(WEBKEY, **kw)


def test_dry_run_is_the_default():
    """The safety property: you must ASK for live."""
    assert _client().dry_run is True
    assert _client(dry_run=False).dry_run is False


def test_empty_webkey_rejected():
    with pytest.raises(ValueError):
        SpotWebClient("")


@pytest.mark.parametrize("code,expected", [
    (200, True), ("200", True),     # spot success
    (0, True), ("0", True),         # futures success
    (602, False), (None, False),    # signature failure / nothing
])
def test_ok_codes(code, expected):
    assert is_ok({"code": code}) is expected


def test_fmt_decimals_respects_pair_precision():
    assert fmt_decimals(0.123456789, 6) == "0.123457"
    assert fmt_decimals(12.5, 2) == "12.5"
    assert fmt_decimals(10.0, 2) == "10"
    assert fmt_decimals(0.0, 2) == "0"
    assert fmt_decimals(1.23, None, default=3) == "1.23"


@pytest.mark.asyncio
async def test_dry_run_sends_nothing_and_reports_ok(monkeypatch):
    def boom(self):
        raise AssertionError("dry-run must never open a session")

    monkeypatch.setattr(SpotWebClient, "_ensure_session", boom)

    res = await _client().buy("PENGU", usdt=2.0, price=0.0123)
    assert isinstance(res, OrderResult)
    assert res.ok and res.dry_run
    assert res.side == BUY


@pytest.mark.asyncio
async def test_quantity_and_price_use_pair_scales():
    """usdt/price = 162.601... base; PENGU allows 2 decimals -> 162.6"""
    sent = {}

    class _Rec(SpotWebClient):
        async def place_order(self, currency_id, market_currency_id, side,
                              price, quantity, order_type="LIMIT_ORDER", *, ticker="?"):
            sent.update(currency_id=currency_id, market=market_currency_id,
                        side=side, price=price, quantity=quantity, ticker=ticker)
            return OrderResult(True, True, ticker, side, price, quantity, {"code": 200})

    c = _Rec(WEBKEY, resolver=_StubResolver())
    await c.buy("PENGU", usdt=2.0, price=0.0123)

    assert sent["currency_id"] == PENGU.currency_id
    assert sent["market"] == PENGU.market_currency_id
    assert sent["quantity"] == "162.6"     # qs=2
    assert sent["price"] == "0.0123"       # ps=6, trailing zeros trimmed


@pytest.mark.asyncio
async def test_transport_failure_is_contained(monkeypatch):
    """A dead connection must come back as a rejection, not an exception."""
    class _Boom:
        async def post(self, *a, **k):
            raise ConnectionError("connection reset")

    monkeypatch.setattr(SpotWebClient, "_ensure_session", lambda self: _Boom())

    res = await _client(dry_run=False).sell("PENGU", quantity=5.0, price=0.02)
    assert res.ok is False
    assert "ConnectionError" in res.error
    assert res.side == SELL


@pytest.mark.asyncio
async def test_live_order_is_signed_and_shaped(monkeypatch):
    """The live path must send the exact documented body + web-sign headers."""
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"code": 200, "data": {"orderId": "42"}}

    class _Sess:
        async def post(self, url, data=None, headers=None, timeout=None):
            captured.update(url=url, body=data, headers=headers)
            return _Resp()

    monkeypatch.setattr(SpotWebClient, "_ensure_session", lambda self: _Sess())

    res = await _client(dry_run=False).buy("PENGU", usdt=2.0, price=0.0123)
    assert res.ok and not res.dry_run

    assert captured["url"].endswith("/api/platform/spot/order/place")
    body = captured["body"]
    for field in ('"currencyId"', '"marketCurrencyId"', '"tradeType"',
                  '"orderType":"LIMIT_ORDER"', '"orderSource":"WEB"'):
        assert field in body, f"missing {field} in {body}"
    # dolos is NOT enforced on spot — the body must stay minimal
    for absent in ("p0", "k0", "chash", "mtoken", "mhash"):
        assert f'"{absent}"' not in body

    h = captured["headers"]
    assert "x-mxc-sign" in h and "x-mxc-nonce" in h
    assert h["platform"] == "WEB"


@pytest.mark.asyncio
async def test_bad_sizes_rejected_before_any_io():
    c = _client(dry_run=False)
    with pytest.raises(ValueError):
        await c.buy("PENGU", usdt=0, price=1.0)
    with pytest.raises(ValueError):
        await c.sell("PENGU", quantity=1.0, price=0)
