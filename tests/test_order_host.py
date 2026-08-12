"""
Regression test for the order-host switch (A/B measured 2026-08-12).

Guarantees:
  1. Requests are SENT to API_URL (contract.mexc.com by default) — the host that
     measured ~50ms faster and more stable on /order/create.
  2. origin/referer headers still point at BASE_URL (futures.mexc.com) — the web
     origin. That split is exactly the configuration that measured code=0; do not
     "tidy" it by pointing both at one host without re-measuring.
  3. MEXC_API_HOST env override works, so the host can be reverted without a code
     change.
"""
from __future__ import annotations

import importlib

import pytest

from src.execution.webkey import client as client_mod


class _FakeCookies:
    def set(self, *a, **k): pass


class _FakeResponse:
    status_code = 200
    def json(self): return {"code": 0, "data": {}}


class _FakeSession:
    def __init__(self, **kw):
        self.cookies = _FakeCookies()
        self.last_url = None
        self.last_headers = None

    async def post(self, url, **kwargs):
        self.last_url = url
        self.last_headers = kwargs.get("headers")
        return _FakeResponse()

    async def get(self, url, **kwargs):
        self.last_url = url
        self.last_headers = kwargs.get("headers")
        return _FakeResponse()

    async def close(self): pass


def _client(monkeypatch):
    monkeypatch.setattr(client_mod.curl_requests, "AsyncSession", _FakeSession)
    return client_mod.MexcWebClient(webkey="WEB" + "0" * 64, visitor_id="v" * 20)


@pytest.mark.asyncio
async def test_order_is_sent_to_contract_host(monkeypatch):
    c = _client(monkeypatch)
    await c.submit_order(symbol="ZEC_USDT", side=1, vol=1, leverage=1,
                         order_type="3", price="1")
    session = await c._ensure_session()
    assert session.last_url.startswith("https://contract.mexc.com"), session.last_url
    assert "/api/v1/private/order/create" in session.last_url


@pytest.mark.asyncio
async def test_origin_and_referer_stay_on_web_origin(monkeypatch):
    c = _client(monkeypatch)
    await c.submit_order(symbol="ZEC_USDT", side=1, vol=1, leverage=1,
                         order_type="3", price="1")
    session = await c._ensure_session()
    h = session.last_headers
    assert h["origin"] == "https://futures.mexc.com"
    assert h["referer"].startswith("https://futures.mexc.com")


@pytest.mark.asyncio
async def test_reads_also_use_contract_host(monkeypatch):
    c = _client(monkeypatch)
    await c.get_open_positions()
    session = await c._ensure_session()
    assert session.last_url.startswith("https://contract.mexc.com"), session.last_url


def test_env_override_reverts_host(monkeypatch):
    monkeypatch.setenv("MEXC_API_HOST", "https://futures.mexc.com")
    reloaded = importlib.reload(client_mod)
    try:
        assert reloaded.MexcWebClient.API_URL == "https://futures.mexc.com"
    finally:
        monkeypatch.delenv("MEXC_API_HOST", raising=False)
        importlib.reload(client_mod)
