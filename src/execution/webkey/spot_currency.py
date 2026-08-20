"""Resolve a spot ticker -> MEXC `currencyId` (and precision), with no auth.

Why this module exists
----------------------
The spot web order body addresses coins by `currencyId` — an opaque 32-hex hash,
not a ticker:

    {"currencyId": "<base hash>", "marketCurrencyId": "<quote hash>", ...}

Until now the only known way to learn a coin's hash was the balances endpoint,
which returns `vcoinId` **only for coins the account already HOLDS**. That made
"warm a new token" start with "go buy 1-2 USDT of it first" — the blocker on the
spot soft-start.

Source (found 2026-08-20)
-------------------------
The public pair page embeds the mapping directly in its HTML:

    "info":{"id":"...","mcd":"<quote currencyId>","cd":"<base currencyId>",
            "vn":"PENGU","mn":"USDT","fn":"Pudgy Penguins","ps":6,"qs":2,
            "bos":"sell","aol":[{"ot":1,"otn":"LIMIT_ORDER","ots":1},...]}

    cd  -> currencyId        (base)
    mcd -> marketCurrencyId  (quote)
    ps  -> price scale (decimals)      qs -> quantity scale (decimals)
    aol -> allowed order types (LIMIT_ORDER / MARKET_ORDER)

Verified: reproduces the two ids we already knew by other means —
USDT 128f589271cb4951b03e71e6323eb7be, MX 8c1e92655f9a4064808ce03ec1a48d38.

Dead ends — don't re-derive (checked 2026-08-20)
------------------------------------------------
* `spot/market/tickers?openPriceMode=2` -> 404.
* `spot/market/symbols` -> 200, 3.9 MB, all 2105 pairs WITH priceScale/quantityScale,
  but every `id` is null and the body contains zero 32-hex hashes. Useful for
  precision, useless for ids.
* currencyId is NOT a hash of the ticker (md5/sha256 of every obvious variant tried).

Transport
---------
MEXC fronts this with Akamai: plain `curl` gets 414 bytes of nothing (TLS
fingerprint), while `curl_cffi` with impersonate="chrome" — the same transport
`client.py` already uses — gets the full page. stdlib `urllib` with a browser UA
also works and is the fallback when curl_cffi is unavailable.

Read-only: issues public GETs. Signs nothing, sends no orders, needs no webkey.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PAGE_URL = "https://www.mexc.com/exchange/{base}_{quote}"
_INFO_RE = re.compile(r'"info"\s*:\s*\{')

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# currencyIds never change, so the cache can be long-lived. 24h is a compromise
# between "never re-fetch" and picking up a delisting/relist.
DEFAULT_TTL_SEC = 24 * 3600

# Known-good ids, kept as a seed so a cold cache still resolves the usual quote
# and lets tests assert against real values.
SEED: dict[str, str] = {
    "USDT": "128f589271cb4951b03e71e6323eb7be",
    "MX": "8c1e92655f9a4064808ce03ec1a48d38",
}


class SpotCurrencyError(RuntimeError):
    """Raised when a ticker cannot be resolved (not listed, or page shape changed)."""


@dataclass(frozen=True)
class SpotCurrency:
    """Everything the spot order body needs for one pair."""
    ticker: str
    quote: str
    currency_id: str          # -> body["currencyId"]
    market_currency_id: str   # -> body["marketCurrencyId"]
    price_scale: int | None
    qty_scale: int | None
    full_name: str = ""
    order_types: tuple[str, ...] = ()

    @property
    def supports_limit(self) -> bool:
        return "LIMIT_ORDER" in self.order_types

    @property
    def supports_market(self) -> bool:
        return "MARKET_ORDER" in self.order_types


def parse_info_blob(html: str) -> dict | None:
    """Extract the embedded ``"info":{...}`` object from a pair page.

    Brace-matching rather than a regex for the whole object: the blob contains
    nested objects (``aol``) and strings with escapes, which a flat regex would
    truncate. Returns None when the page carries no info blob (unlisted pair,
    bot-check interstitial, or a page-shape change).
    """
    m = _INFO_RE.search(html)
    if not m:
        return None
    start = m.end() - 1                      # position of the '{'
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def info_to_currency(info: dict, want_base: str, want_quote: str) -> SpotCurrency:
    """Validate an info blob and shape it into a SpotCurrency."""
    vn = (info.get("vn") or "").upper()
    mn = (info.get("mn") or "").upper()
    cd = info.get("cd")
    mcd = info.get("mcd")

    if not cd or not mcd:
        raise SpotCurrencyError(
            f"{want_base}_{want_quote}: info blob has no cd/mcd (page shape changed?)")

    # MEXC redirects unknown pairs to a default pair, so a mismatch here means
    # "not listed" — NOT "here are your ids". Fail loudly rather than return
    # some other coin's hash into an order body.
    if vn != want_base.upper() or mn != want_quote.upper():
        raise SpotCurrencyError(
            f"{want_base}_{want_quote}: page returned {vn}_{mn} — pair not listed?")

    order_types = tuple(
        o.get("otn") for o in (info.get("aol") or [])
        if isinstance(o, dict) and o.get("otn")
    )
    return SpotCurrency(
        ticker=vn,
        quote=mn,
        currency_id=cd,
        market_currency_id=mcd,
        price_scale=info.get("ps"),
        qty_scale=info.get("qs"),
        full_name=info.get("fn") or "",
        order_types=order_types,
    )


def _fetch_urllib(url: str, timeout: float) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers={
        "accept": "*/*", "user-agent": _UA, "language": "en-US",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


class SpotCurrencyResolver:
    """Caching ticker -> SpotCurrency resolver.

    Usage:
        resolver = SpotCurrencyResolver()
        cur = await resolver.resolve("PENGU")
        body["currencyId"] = cur.currency_id
        body["marketCurrencyId"] = cur.market_currency_id

    Concurrency: one in-flight fetch per (base, quote). Callers that ask for the
    same pair while a fetch is running await that fetch instead of starting
    another — a warm-up loop resolving its universe must not hammer the page.
    """

    def __init__(self, ttl_sec: float = DEFAULT_TTL_SEC, timeout: float = 20.0) -> None:
        self._ttl = ttl_sec
        self._timeout = timeout
        self._cache: dict[tuple[str, str], tuple[float, SpotCurrency]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def cached(self, base: str, quote: str = "USDT") -> SpotCurrency | None:
        key = (base.upper(), quote.upper())
        hit = self._cache.get(key)
        if not hit:
            return None
        ts, cur = hit
        if time.monotonic() - ts > self._ttl:
            self._cache.pop(key, None)
            return None
        return cur

    async def resolve(self, base: str, quote: str = "USDT",
                      force: bool = False) -> SpotCurrency:
        key = (base.upper(), quote.upper())
        if not force:
            hit = self.cached(*key)
            if hit is not None:
                return hit

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if not force:                     # another waiter may have filled it
                hit = self.cached(*key)
                if hit is not None:
                    return hit
            cur = await self._fetch(*key)
            self._cache[key] = (time.monotonic(), cur)
            return cur

    async def resolve_many(self, bases, quote: str = "USDT") -> dict[str, SpotCurrency]:
        """Resolve a universe. Failures are logged and skipped, never raised —
        one delisted ticker must not abort warming the rest."""
        out: dict[str, SpotCurrency] = {}
        for b in bases:
            try:
                out[b.upper()] = await self.resolve(b, quote)
            except Exception as e:
                logger.warning("spot currencyId: %s_%s unresolved: %s", b, quote, e)
        return out

    async def _fetch(self, base: str, quote: str) -> SpotCurrency:
        url = PAGE_URL.format(base=base, quote=quote)
        html = await self._get(url)
        info = parse_info_blob(html)
        if info is None:
            raise SpotCurrencyError(
                f"{base}_{quote}: no info blob in {len(html)} bytes "
                f"(bot-check page, or page shape changed)")
        return info_to_currency(info, base, quote)

    async def _get(self, url: str) -> str:
        """curl_cffi (Chrome TLS) first — plain curl/requests get blocked by
        Akamai. stdlib urllib is the fallback and does work with a browser UA."""
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            return await asyncio.to_thread(_fetch_urllib, url, self._timeout)

        async with curl_requests.AsyncSession(impersonate="chrome") as s:
            r = await s.get(url, headers={"accept": "*/*", "language": "en-US"},
                            timeout=self._timeout)
            if r.status_code != 200:
                raise SpotCurrencyError(f"{url} -> HTTP {r.status_code}")
            return r.text
