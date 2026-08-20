"""Ask MEXC what THIS account actually pays on a pair, before trading it.

Why this exists
---------------
Soft-start may only open futures positions on pairs that are genuinely 0% for
this account. Before this module the bot had no way to know that up front:

  * `fee_guard` (live_executor) finds out AFTER a fill has been charged — too
    late for a rule that says "don't open such pairs at all";
  * `realism.NON_ZERO_FEE_PAIRS` is an empty simulation stub, not exchange data;
  * the PUBLIC `contract/detail` is wrong for this account — it lists 592
    non-zero pairs, and `TAO_USDT` / `XMR_USDT` show 0.0001 there while this
    account actually pays **0** (the promo is only visible privately).

Source (found 2026-08-20):

    GET /account/tiered_fee_rate?symbol=<CONTRACT>     [web-signed, webkey]
    -> {"makerFee": 0, "takerFee": 0, "makerFeeDiscount": 1,
        "feeRateMode": "NORMAL", ...}

Design decisions that matter:

  * **fail-closed.** Unknown fee -> `is_zero_maker()` returns False. A pair whose
    rate we could not read is never traded by soft-start. A rate-limited miss
    must never be mistaken for "0%".
  * **retries.** A transient `None` is not evidence of a fee — during the audit
    6 of 7 "unknown" pairs turned out to be 0% once retried.
  * **`to_mexc()` for symbols.** `1000PEPEUSDT -> PEPE_USDT`,
    `MUUSDT -> MUSTOCK_USDT`. Rolling your own "insert an underscore" rule
    mislabels those as unknown and silently drops 6 tradeable pairs.
  * fee tiers change rarely; the cache TTL is long, but `refresh()` exists for
    a deliberate re-check before a live run.

READ-ONLY: issues signed GETs. Never places or cancels anything.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

FEE_ENDPOINT = "/account/tiered_fee_rate?symbol={sym}"
DEFAULT_TTL_SEC = 3600.0          # fee tiers move rarely; an hour is plenty
DEFAULT_RETRIES = 3
DEFAULT_PACING_SEC = 0.25         # be polite: this is a live account


@dataclass(frozen=True)
class PairFee:
    symbol: str            # MEXC contract symbol, e.g. PEPE_USDT
    maker: float
    taker: float
    mode: str = ""

    @property
    def zero_maker(self) -> bool:
        return self.maker == 0.0

    @property
    def zero_both(self) -> bool:
        return self.maker == 0.0 and self.taker == 0.0


def to_contract_symbol(symbol: str) -> str:
    """Bot symbol -> MEXC contract symbol, via the bot's own alias table."""
    from src.exchanges.mexc_rest import to_mexc
    return to_mexc(symbol)


class FeeGate:
    """Caching, fail-closed lookup of this account's per-pair fee.

        gate = FeeGate(client)
        if await gate.is_zero_maker("HYPEUSDT"):
            ...                       # safe to open
    """

    def __init__(self, client, *, ttl_sec: float = DEFAULT_TTL_SEC,
                 retries: int = DEFAULT_RETRIES,
                 pacing_sec: float = DEFAULT_PACING_SEC) -> None:
        self._client = client
        self._ttl = ttl_sec
        self._retries = max(1, retries)
        self._pacing = pacing_sec
        self._cache: dict[str, tuple[float, PairFee]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def cached(self, symbol: str) -> PairFee | None:
        cs = to_contract_symbol(symbol)
        hit = self._cache.get(cs)
        if not hit:
            return None
        ts, fee = hit
        if time.monotonic() - ts > self._ttl:
            self._cache.pop(cs, None)
            return None
        return fee

    async def fee(self, symbol: str, *, force: bool = False) -> PairFee | None:
        """This account's fee for `symbol`, or None if it could not be read."""
        cs = to_contract_symbol(symbol)
        if not force:
            hit = self.cached(symbol)
            if hit is not None:
                return hit

        lock = self._locks.setdefault(cs, asyncio.Lock())
        async with lock:
            if not force:
                hit = self.cached(symbol)
                if hit is not None:
                    return hit
            fee = await self._fetch(cs)
            if fee is not None:
                self._cache[cs] = (time.monotonic(), fee)
            return fee

    async def _fetch(self, contract_symbol: str) -> PairFee | None:
        for attempt in range(self._retries):
            try:
                r = await self._client._request(
                    "GET", FEE_ENDPOINT.format(sym=contract_symbol),
                    needs_web_sign=True)
                data = (r or {}).get("data") or {}
                maker = data.get("makerFee")
                taker = data.get("takerFee")
                if maker is not None:
                    return PairFee(symbol=contract_symbol,
                                   maker=float(maker),
                                   taker=float(taker if taker is not None else 0.0),
                                   mode=str(data.get("feeRateMode") or ""))
            except Exception as e:
                logger.debug("fee lookup %s attempt %d: %s",
                             contract_symbol, attempt + 1, e)
            await asyncio.sleep(self._pacing * (attempt + 1))
        logger.warning("fee unknown for %s after %d tries — treating as NOT zero-fee",
                       contract_symbol, self._retries)
        return None

    async def is_zero_maker(self, symbol: str, *, force: bool = False) -> bool:
        """True only when the exchange positively says maker == 0.

        Fail-closed: an unreadable rate returns False, so soft-start skips the
        pair rather than opening it on an assumption.
        """
        fee = await self.fee(symbol, force=force)
        return bool(fee and fee.zero_maker)

    async def zero_fee_universe(self, symbols) -> list[str]:
        """Filter `symbols` down to the ones this account trades at 0% maker.

        Returns BOT symbols (as passed in), so callers can feed the result
        straight back into the rest of the bot.
        """
        out: list[str] = []
        for i, s in enumerate(symbols):
            # Pace BETWEEN pairs, not just between retries. Hammering 24 symbols
            # back to back gets rate-limited, and because this gate is
            # fail-closed that shows up as perfectly good 0% pairs being
            # dropped — measured: ZEC_USDT excluded with no pacing, included
            # with it. Skip the wait for anything already cached.
            if i and self.cached(s) is None:
                await asyncio.sleep(self._pacing)
            try:
                if await self.is_zero_maker(s):
                    out.append(s)
                else:
                    logger.info("fee gate: skipping %s — not 0%% maker for this account", s)
            except Exception as e:
                logger.warning("fee gate: %s errored (%s) — skipped", s, e)
        return out

    def refresh(self) -> None:
        """Drop the cache so the next lookup re-reads from the exchange."""
        self._cache.clear()
