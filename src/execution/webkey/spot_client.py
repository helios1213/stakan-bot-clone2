"""MEXC SPOT orders over the web ("webkey") path.

Established facts this builds on — do not re-derive:
  * `sign_web` is body-generic: md5(nonce + json_body + md5(webkey+nonce)[7:]).
    It signs a spot body unchanged, exactly like a futures one.
  * Spot does NOT enforce the dolos block (ablation 2026-08-19: a plain body +
    web-sign was accepted, success code 200). So the spot client is minimal.
  * Auth is the webkey in the `u_id` / `uc_token` cookies. No API key involved.
  * Spot success code is **200**, futures is **0** — `_ok()` accepts both.

Order anatomy:
    POST https://www.mexc.com/api/platform/spot/order/place
    headers: x-mxc-sign + x-mxc-nonce, platform: WEB, cookies u_id/uc_token
    body: {currencyId, marketCurrencyId, tradeType BUY|SELL, price, quantity,
           orderType LIMIT_ORDER|MARKET_ORDER, orderSource WEB}

`currencyId` is a hash, not a ticker — resolved by `spot_currency.py`, which
also carries the price/quantity decimals for the pair, so precision and ids come
from ONE source instead of two.

SAFETY (house rule): `dry_run` defaults to **True**. A dry-run client builds and
signs nothing-bound requests, logs exactly what it WOULD send, and returns a
synthetic accepted response. Going live takes an explicit `dry_run=False`.
Every order is wrapped: a failure is logged and returned as a rejection, never
raised into the caller's loop.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from .signing import sign_web
from .spot_currency import SpotCurrency, SpotCurrencyResolver

logger = logging.getLogger(__name__)

WEB_ORIGIN = "https://www.mexc.com"
SPOT_ORDER_URL = f"{WEB_ORIGIN}/api/platform/spot/order/place"
BALANCES_URL = f"{WEB_ORIGIN}/api/gateway/spot/finance/asset/currency/balances"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

BUY, SELL = "BUY", "SELL"
LIMIT, MARKET = "LIMIT_ORDER", "MARKET_ORDER"

# Spot returns 200 on success; futures returns 0. Accept both so this can share
# helpers with the futures path.
_OK_CODES = {"0", "200"}


def is_ok(resp: dict | None) -> bool:
    return bool(resp) and str(resp.get("code")) in _OK_CODES


@dataclass
class OrderResult:
    ok: bool
    dry_run: bool
    ticker: str
    side: str
    price: str
    quantity: str
    response: dict
    latency_ms: float = 0.0

    @property
    def error(self) -> str:
        if self.ok:
            return ""
        return str(self.response.get("msg")
                   or self.response.get("message")
                   or self.response.get("code")
                   or "unknown")


def fmt_decimals(value: float, decimals: int | None, default: int = 6) -> str:
    """Format to a fixed number of decimals, then trim trailing zeros.

    MEXC rejects a quantity with more precision than the pair allows, so the
    decimals come from the pair's own `qs`/`ps` rather than a guess.
    """
    d = default if decimals is None else int(decimals)
    s = f"{value:.{d}f}".rstrip("0").rstrip(".")
    return s or "0"


class SpotWebClient:
    """Places spot orders with the webkey. DRY-RUN BY DEFAULT.

    Usage:
        client = SpotWebClient(webkey)                   # dry-run
        client = SpotWebClient(webkey, dry_run=False)    # live — explicit

        res = await client.buy("PENGU", usdt=2.0, price=0.0123)
        if not res.ok:
            log.warning("skipped: %s", res.error)
    """

    def __init__(self, webkey: str, *, dry_run: bool = True,
                 resolver: SpotCurrencyResolver | None = None,
                 timeout: float = 20.0, quote: str = "USDT") -> None:
        if not webkey:
            raise ValueError("empty webkey")
        self._webkey = webkey
        self.dry_run = bool(dry_run)
        self.quote = quote.upper()
        self._timeout = timeout
        self._resolver = resolver or SpotCurrencyResolver()
        self._session = None
        if self.dry_run:
            logger.info("SpotWebClient: DRY-RUN — orders are logged, not sent")
        else:
            logger.warning("SpotWebClient: LIVE — orders WILL be placed")

    # ---- plumbing -------------------------------------------------------

    def _ensure_session(self):
        """Lazily build the curl_cffi session and seed the webkey cookies.

        Chrome impersonation matters: Akamai fronts www.mexc.com and rejects a
        stock TLS fingerprint.
        """
        if self._session is not None:
            return self._session
        from curl_cffi import requests as curl_requests
        s = curl_requests.AsyncSession(impersonate="chrome")
        s.cookies.set("u_id", self._webkey, domain=".mexc.com")
        s.cookies.set("uc_token", self._webkey, domain=".mexc.com")
        self._session = s
        return s

    def _headers(self, sign: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "accept": "*/*",
            "content-type": "application/json",
            "language": "en-US",
            "origin": WEB_ORIGIN,
            "platform": "WEB",
            "referer": f"{WEB_ORIGIN}/exchange/MX_{self.quote}",
            "user-agent": _UA,
        }
        if sign:
            h.update(sign)
        return h

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            finally:
                self._session = None

    # ---- reads ----------------------------------------------------------

    async def balances(self, currency_ids: list[str]) -> dict[str, dict]:
        """Balances for the given currencyIds. Cookie-auth only, no signing.

        Returns {TICKER: {"available": float, "currency_id": str}}. Note this
        also self-maps ticker -> currencyId for HELD coins; unheld coins simply
        do not appear (that limitation is why spot_currency.py exists).
        """
        s = self._ensure_session()
        url = f"{BALANCES_URL}?coinId={','.join(currency_ids)}"
        r = await s.get(url, headers=self._headers(), timeout=self._timeout)
        if r.status_code != 200:
            logger.warning("spot balances HTTP %s", r.status_code)
            return {}
        out: dict[str, dict] = {}
        for row in (r.json().get("data") or []):
            cur = row.get("currency")
            if not cur:
                continue
            out[cur] = {
                "available": float(row.get("available") or 0),
                "currency_id": row.get("vcoinId"),
            }
        return out

    async def currency(self, ticker: str) -> SpotCurrency:
        return await self._resolver.resolve(ticker, self.quote)

    # ---- orders ---------------------------------------------------------

    async def place_order(self, currency_id: str, market_currency_id: str,
                          side: str, price: str, quantity: str,
                          order_type: str = LIMIT, *, ticker: str = "?") -> OrderResult:
        """Low-level order. Wrapped: never raises, returns a rejection instead."""
        body = {
            "currencyId": currency_id,
            "marketCurrencyId": market_currency_id,
            "tradeType": side,
            "price": str(price),
            "quantity": str(quantity),
            "orderType": order_type,
            "orderSource": "WEB",
        }

        if self.dry_run:
            logger.info("[DRY spot] %s %s qty=%s @ %s (nothing sent)",
                        side, ticker, quantity, price)
            return OrderResult(ok=True, dry_run=True, ticker=ticker, side=side,
                               price=str(price), quantity=str(quantity),
                               response={"code": 200, "data": {"dryRun": True}})

        body_json = json.dumps(body, separators=(",", ":"))
        headers = self._headers(sign_web(body, self._webkey))
        t0 = time.perf_counter()
        try:
            s = self._ensure_session()
            r = await s.post(SPOT_ORDER_URL, data=body_json,
                             headers=headers, timeout=self._timeout)
            ms = (time.perf_counter() - t0) * 1000
            try:
                payload = r.json()
            except Exception:
                payload = {"code": r.status_code, "msg": r.text[:200]}
        except Exception as e:
            # House rule: one failed order logs and skips, never cascades.
            logger.warning("[spot] %s %s FAILED: %s — skipped", side, ticker, e)
            return OrderResult(ok=False, dry_run=False, ticker=ticker, side=side,
                               price=str(price), quantity=str(quantity),
                               response={"code": "EXC", "msg": f"{type(e).__name__}: {e}"})

        ok = is_ok(payload)
        log = logger.info if ok else logger.warning
        log("[spot] %s %s qty=%s @ %s -> code=%s %.0fms",
            side, ticker, quantity, price, payload.get("code"), ms)
        return OrderResult(ok=ok, dry_run=False, ticker=ticker, side=side,
                           price=str(price), quantity=str(quantity),
                           response=payload, latency_ms=ms)

    async def _sized_order(self, ticker: str, side: str, price: float,
                           quantity: float) -> OrderResult:
        cur = await self.currency(ticker)
        return await self.place_order(
            currency_id=cur.currency_id,
            market_currency_id=cur.market_currency_id,
            side=side,
            price=fmt_decimals(price, cur.price_scale),
            quantity=fmt_decimals(quantity, cur.qty_scale, default=2),
            ticker=ticker,
        )

    async def buy(self, ticker: str, *, usdt: float, price: float) -> OrderResult:
        """Buy ~`usdt` worth of `ticker` at `price` (quantity derived)."""
        if usdt <= 0 or price <= 0:
            raise ValueError(f"buy {ticker}: usdt and price must be > 0")
        return await self._sized_order(ticker, BUY, price, usdt / price)

    async def sell(self, ticker: str, *, quantity: float, price: float) -> OrderResult:
        if quantity <= 0 or price <= 0:
            raise ValueError(f"sell {ticker}: quantity and price must be > 0")
        return await self._sized_order(ticker, SELL, price, quantity)
