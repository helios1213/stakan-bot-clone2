"""
MEXC Futures REST client (public endpoints, read-only).

Provides:
  - Orderbook snapshots for depth analysis (get_depth)
  - Symbol alias + price-scale helpers (to_mexc/to_binance/get_binance_scale)

MEXC symbol format: BTC_USDT (with underscore).
Binance format:     BTCUSDT  (no separator).
We convert between them via to_mexc()/to_binance().

All endpoints here are public (no authentication needed).
For private endpoints (orders, account) we'll need signed requests
later — for shadow phase that's not required.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import aiohttp
import orjson
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)



# Symbol aliases between Binance and MEXC.
# Some tokens trade on Binance as "1000X" (a contract == 1000 units of X),
# while MEXC uses native units. We need both an alias (for symbol lookup)
# AND a price scale factor (so prices from MEXC can be normalised to Binance units).
#
# Example: PEPE
#   Binance:  symbol=1000PEPEUSDT, price ~ $0.00001234 per 1000 PEPE
#   MEXC:     symbol=PEPE_USDT,    price ~ $0.00000001234 per 1 PEPE
#   Ratio:    Binance / MEXC = 1000  →  binance_scale=1000
#
# After scaling, MEXC prices match Binance scale, so lead-lag detector,
# trade-flow, wall detector all work without modification.
BINANCE_TO_MEXC_ALIASES: dict[str, str] = {
    "PUMPUSDT": "PUMPFUN_USDT",
    "1000PEPEUSDT": "PEPE_USDT",
    "1000SHIBUSDT": "SHIB_USDT",
    "10000SATSUSDT": "SATS_USDT",
    "1000FLOKIUSDT": "FLOKI_USDT",
    "1000BONKUSDT": "BONK_USDT",
    "1000RATSUSDT": "RATS_USDT",
    "1000XECUSDT": "XEC_USDT",
    # Стокові перпи: рідний символ MEXC із суфіксом STOCK
    "SKHYNIXUSDT": "SKHYNIXSTOCK_USDT",
    "SPCXUSDT": "SPCXSTOCK_USDT",
    "MUUSDT": "MUSTOCK_USDT",
    "SNDKUSDT": "SNDKSTOCK_USDT",
    # SOXL мапиться правильно дефолтом (SOXL_USDT) — аліас не потрібен
}
MEXC_TO_BINANCE_ALIASES: dict[str, str] = {v: k for k, v in BINANCE_TO_MEXC_ALIASES.items()}

# Scale factor: multiply MEXC raw price by this to get Binance-equivalent price.
# Keys are MEXC symbols (with underscore). Default = 1.0 (no scaling).
SYMBOL_SCALE_TO_BINANCE: dict[str, float] = {
    "PEPE_USDT": 1000.0,
    "SHIB_USDT": 1000.0,
    "SATS_USDT": 10000.0,
    "FLOKI_USDT": 1000.0,
    "BONK_USDT": 1000.0,
    "RATS_USDT": 1000.0,
    "XEC_USDT": 1000.0,
}


@lru_cache(maxsize=512)
def to_mexc(symbol: str) -> str:
    """BTCUSDT -> BTC_USDT. If already has underscore, return as-is.

    Memoized: pure function of a small fixed symbol set. The alias dicts
    and quote-suffix list are module constants (never mutated at runtime),
    so caching is safe and removes the suffix-scan + f-string alloc from
    every call. maxsize bounds it well above the universe size.
    """
    if "_" in symbol:
        return symbol
    if symbol in BINANCE_TO_MEXC_ALIASES:
        return BINANCE_TO_MEXC_ALIASES[symbol]
    # Try common quote suffixes
    for quote in ("USDT", "USDC", "USD"):
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            return f"{base}_{quote}"
    return symbol  # fallback


@lru_cache(maxsize=512)
def to_binance(mexc_symbol: str) -> str:
    """BTC_USDT -> BTCUSDT. Honors aliases.

    Memoized: pure function of a fixed symbol set. Called per WS depth
    message on the hot path; caching removes the .replace() string
    allocation from the common (non-aliased) case.
    """
    if mexc_symbol in MEXC_TO_BINANCE_ALIASES:
        return MEXC_TO_BINANCE_ALIASES[mexc_symbol]
    return mexc_symbol.replace("_", "")


def get_binance_scale(mexc_symbol: str) -> float:
    """
    Get the scale factor to convert a MEXC raw price to Binance-equivalent price.
    Returns 1.0 for unknown / non-aliased symbols.

    Usage: binance_price = mexc_price * get_binance_scale(mexc_symbol)
    """
    return SYMBOL_SCALE_TO_BINANCE.get(mexc_symbol, 1.0)


class MexcRestClient:
    """Async REST client for MEXC Futures public endpoints."""

    BASE_URL = "https://contract.mexc.com"

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> MexcRestClient:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Use 'async with' or pass a session.")
        return self._session

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.BASE_URL}{path}"
        async with self.session.get(url, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"MEXC {path} HTTP {resp.status}: {text[:200]}")
            data = await resp.json(loads=orjson.loads)
        if not data.get("success"):
            raise RuntimeError(f"MEXC {path} returned success=false: {data}")
        return data

    # ---- public endpoints ----
    async def get_depth(self, symbol: str, limit: int = 100) -> dict:
        """
        Orderbook snapshot.
        symbol: MEXC format (BTC_USDT).
        Returns {bids: [[price, vol, count], ...], asks: [...], version: int}
        """
        data = await self._get(f"/api/v1/contract/depth/{symbol}", params={"limit": limit})
        return data.get("data", {})
