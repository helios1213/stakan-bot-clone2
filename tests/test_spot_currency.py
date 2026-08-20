"""Offline tests for the spot ticker -> currencyId resolver.

Everything here runs without network: the HTML fixtures are real fragments
captured from the pair page on 2026-08-20.

What these pin:
  1. The brace-matching parser survives the nested `aol` array and escaped
     strings — a flat regex truncated the blob here.
  2. A pair that isn't listed FAILS instead of silently returning some other
     coin's hash (MEXC redirects unknown pairs to a default pair).
  3. Known ids are reproduced exactly (USDT / MX were known by other means).
  4. The cache serves repeats and does not re-fetch.
"""
from __future__ import annotations

import pytest

from src.execution.webkey.spot_currency import (
    SEED,
    SpotCurrency,
    SpotCurrencyError,
    SpotCurrencyResolver,
    info_to_currency,
    parse_info_blob,
)

# Real fragment: note the nested `aol` array and the surrounding page noise.
PENGU_HTML = (
    '<script>window.__NUXT__={"stuff":true,"trace":"sentry-sampled=false",'
    '"info":{"id":"e10fa367872e456da211b5b05b96ff29",'
    '"mcd":"128f589271cb4951b03e71e6323eb7be",'
    '"cd":"1c2cf4f4531b404d9bb5622f3881f32c",'
    '"vn":"PENGU","mn":"USDT","fn":"Pudgy Penguins","ps":6,"qs":2,"bos":"sell",'
    '"aol":[{"ot":1,"otn":"LIMIT_ORDER","ots":1},{"ot":5,"otn":"MARKET_ORDER","ots":1}]},'
    '"after":{"unrelated":1}}</script>'
)

MX_HTML = (
    '<div>x</div><script>a={"info":{"id":"7fb2a8ab8a5e4eb699ac34ee340489f8",'
    '"mcd":"128f589271cb4951b03e71e6323eb7be",'
    '"cd":"8c1e92655f9a4064808ce03ec1a48d38",'
    '"vn":"MX","mn":"USDT","fn":"MX Token","ps":4,"qs":2,"bos":"none",'
    '"aol":[{"ot":1,"otn":"LIMIT_ORDER","ots":1}]}}</script>'
)


def test_parses_nested_blob():
    info = parse_info_blob(PENGU_HTML)
    assert info is not None
    assert info["cd"] == "1c2cf4f4531b404d9bb5622f3881f32c"
    assert info["mcd"] == SEED["USDT"]
    # the nested array must survive intact — this is what a flat regex broke
    assert [o["otn"] for o in info["aol"]] == ["LIMIT_ORDER", "MARKET_ORDER"]


def test_parser_handles_escaped_quotes_before_blob():
    html = r'{"note":"he said \"info\":{fake}","info":{"cd":"a","mcd":"b","vn":"X","mn":"USDT"}}'
    info = parse_info_blob(html)
    assert info is not None and info["cd"] == "a"


def test_no_blob_returns_none():
    assert parse_info_blob("<html>bot check, nothing here</html>") is None


def test_known_ids_reproduced():
    """MX/USDT were known via the balances endpoint long before this parser."""
    cur = info_to_currency(parse_info_blob(MX_HTML), "MX", "USDT")
    assert cur.currency_id == SEED["MX"]
    assert cur.market_currency_id == SEED["USDT"]
    assert (cur.price_scale, cur.qty_scale) == (4, 2)
    assert cur.supports_limit and not cur.supports_market


def test_wrong_pair_is_an_error_not_a_wrong_hash():
    """MEXC redirects unlisted pairs to a default pair. Returning THAT coin's
    hash into an order body would buy the wrong asset — must raise instead."""
    info = parse_info_blob(MX_HTML)          # page says MX_USDT
    with pytest.raises(SpotCurrencyError, match="not listed"):
        info_to_currency(info, "NOSUCHCOIN", "USDT")


def test_missing_ids_is_an_error():
    info = parse_info_blob('{"info":{"vn":"X","mn":"USDT","ps":2}}')
    with pytest.raises(SpotCurrencyError, match="no cd/mcd"):
        info_to_currency(info, "X", "USDT")


@pytest.mark.asyncio
async def test_resolver_caches_and_does_not_refetch(monkeypatch):
    calls = []

    async def fake_get(self, url):
        calls.append(url)
        return PENGU_HTML

    monkeypatch.setattr(SpotCurrencyResolver, "_get", fake_get)
    r = SpotCurrencyResolver()

    a = await r.resolve("PENGU")
    b = await r.resolve("pengu")             # case-insensitive, same cache key
    assert a == b
    assert isinstance(a, SpotCurrency)
    assert a.currency_id == "1c2cf4f4531b404d9bb5622f3881f32c"
    assert len(calls) == 1, "second resolve must come from cache"

    await r.resolve("PENGU", force=True)
    assert len(calls) == 2, "force=True must re-fetch"


@pytest.mark.asyncio
async def test_resolve_many_skips_failures(monkeypatch):
    """One delisted ticker must not abort warming the rest of the universe."""
    async def fake_get(self, url):
        if "PENGU" in url:
            return PENGU_HTML
        return "<html>nothing</html>"

    monkeypatch.setattr(SpotCurrencyResolver, "_get", fake_get)
    r = SpotCurrencyResolver()
    out = await r.resolve_many(["PENGU", "DEADCOIN"])
    assert set(out) == {"PENGU"}
