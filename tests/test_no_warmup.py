"""
Regression test for the warmup removal (probe 2026-08-12).

Guarantees:
  1. warmup() issues NO network GET (the old cold GET /futures/{symbol} is gone).
  2. The session is seeded with app-auth cookies (u_id / uc_token) at creation.
  3. A hot-path style call reuses the session without any GET.

The fake session raises on any .get(), so if warmup or _ensure_session ever
reintroduces a network warmup, this test fails loudly.
"""
from __future__ import annotations

import pytest

from src.execution.webkey import client as client_mod


class _FakeCookies:
    def __init__(self) -> None:
        self._d: dict[tuple[str, str | None], str] = {}

    def set(self, k, v, domain=None):
        self._d[(k, domain)] = v

    def __iter__(self):
        return iter(self._d)


class _FakeSession:
    """Stand-in for curl_cffi AsyncSession that forbids network GETs."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cookies = _FakeCookies()
        self.get_calls = 0
        self.post_calls = 0

    async def get(self, *args, **kwargs):
        self.get_calls += 1
        raise AssertionError("hot path must not issue a network GET (warmup removed)")

    async def post(self, *args, **kwargs):
        self.post_calls += 1
        return None

    async def close(self):
        return None


def _make_client(monkeypatch) -> client_mod.MexcWebClient:
    monkeypatch.setattr(client_mod.curl_requests, "AsyncSession", _FakeSession)
    return client_mod.MexcWebClient(webkey="WEB" + "0" * 64, visitor_id="v" * 20)


@pytest.mark.asyncio
async def test_warmup_issues_no_network_get(monkeypatch):
    c = _make_client(monkeypatch)
    await c.warmup()                 # must not raise -> proves no .get()
    await c.warmup(force=True)        # force path must also stay network-free
    session = await c._ensure_session()
    assert session.get_calls == 0


@pytest.mark.asyncio
async def test_session_seeds_app_auth_cookies(monkeypatch):
    c = _make_client(monkeypatch)
    session = await c._ensure_session()
    cookie_keys = {name for (name, _domain) in session.cookies}
    assert "u_id" in cookie_keys
    assert "uc_token" in cookie_keys


@pytest.mark.asyncio
async def test_session_is_reused_across_calls(monkeypatch):
    c = _make_client(monkeypatch)
    s1 = await c._ensure_session()
    await c.warmup()
    s2 = await c._ensure_session()
    assert s1 is s2                   # one session, kept warm, no rebuild
    assert s1.get_calls == 0
