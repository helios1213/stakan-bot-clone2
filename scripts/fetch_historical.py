"""
Historical candle fetcher — pull 7+ days of 1m candles for analysis.

Sources:
  - MEXC futures: https://contract.mexc.com/api/v1/contract/kline/{symbol}
  - Binance futures: https://fapi.binance.com/fapi/v1/klines

Storage: `historical_candles` table.

Usage:
    python scripts/fetch_historical.py --pairs ZECUSDT,TAOUSDT --days 7
    python scripts/fetch_historical.py --all --days 7

Run once (or daily via cron) to keep data fresh.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Default 6 pairs we care about (top volume on MEXC)
DEFAULT_PAIRS = [
    "ZECUSDT",   # Highest volume, our king
    "TAOUSDT",   # Stable overnight
    "1000PEPEUSDT",  # High volume meme
    "ENAUSDT",   # Re-evaluate
    "PENGUUSDT", # Re-evaluate
    "BCHUSDT",   # Slow but reliable
]

# MEXC uses "ZEC_USDT", Binance uses "ZECUSDT"
def to_mexc_symbol(symbol: str) -> str:
    """ZECUSDT → ZEC_USDT; 1000PEPEUSDT → PEPE_USDT (MEXC has no 1000 prefix)."""
    if symbol.startswith("1000") and symbol.endswith("USDT"):
        return f"{symbol[4:-4]}_USDT"
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}_USDT"
    return symbol


SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_candles (
    exchange    TEXT NOT NULL,        -- 'mexc' or 'binance'
    symbol      TEXT NOT NULL,        -- internal: ZECUSDT, 1000PEPEUSDT
    interval    TEXT NOT NULL DEFAULT '1m',
    open_time   INTEGER NOT NULL,     -- unix ms
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,        -- base asset volume
    quote_volume REAL,                -- quote asset volume (USDT)
    trades      INTEGER,               -- number of trades
    PRIMARY KEY (exchange, symbol, interval, open_time)
);
CREATE INDEX IF NOT EXISTS idx_hcandles_symbol_time
    ON historical_candles(symbol, open_time);
"""


async def fetch_mexc_candles(
    client: httpx.AsyncClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """
    Fetch MEXC futures 1m candles in the given range.

    MEXC API returns max 2000 candles per call. We page through.
    """
    mexc_symbol = to_mexc_symbol(symbol)
    out: list[dict] = []
    cur_start = start_ms

    while cur_start < end_ms:
        # Calculate batch end (max 2000 candles = 2000 minutes ≈ 33h)
        batch_end_ms = min(end_ms, cur_start + 2000 * 60_000)
        url = "https://contract.mexc.com/api/v1/contract/kline/" + mexc_symbol
        params = {
            "interval": "Min1",
            "start": cur_start // 1000,  # MEXC uses seconds
            "end": batch_end_ms // 1000,
        }
        try:
            r = await client.get(url, params=params, timeout=15.0)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("MEXC %s fetch error: %s", symbol, e)
            break

        if not data.get("success"):
            logger.warning("MEXC %s API non-success: %s", symbol, data.get("message"))
            break

        # MEXC returns dict of arrays: time, open, close, high, low, vol, amount
        d = data.get("data", {})
        if not d or not d.get("time"):
            break

        n = len(d["time"])
        for i in range(n):
            out.append({
                "exchange": "mexc",
                "symbol": symbol,
                "open_time": d["time"][i] * 1000,  # to ms
                "open": float(d["open"][i]),
                "high": float(d["high"][i]),
                "low": float(d["low"][i]),
                "close": float(d["close"][i]),
                "volume": float(d["vol"][i]),
                "quote_volume": float(d["amount"][i]),
                "trades": None,
            })

        # Advance past last candle
        last_time = d["time"][-1] * 1000
        if last_time <= cur_start:
            break  # no progress, stop
        cur_start = last_time + 60_000  # next minute

        # Rate limit gentle
        await asyncio.sleep(0.2)

    return out


async def fetch_binance_candles(
    client: httpx.AsyncClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    """
    Fetch Binance futures 1m candles. Binance returns max 1500 per call.
    """
    out: list[dict] = []
    cur_start = start_ms

    while cur_start < end_ms:
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": cur_start,
            "endTime": end_ms,
            "limit": 1500,
        }
        try:
            r = await client.get(url, params=params, timeout=15.0)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("Binance %s fetch error: %s", symbol, e)
            break

        if not data:
            break

        for arr in data:
            # [openTime, open, high, low, close, volume, closeTime, quoteVolume,
            #  trades, takerBuyBase, takerBuyQuote, ignore]
            out.append({
                "exchange": "binance",
                "symbol": symbol,
                "open_time": int(arr[0]),
                "open": float(arr[1]),
                "high": float(arr[2]),
                "low": float(arr[3]),
                "close": float(arr[4]),
                "volume": float(arr[5]),
                "quote_volume": float(arr[7]),
                "trades": int(arr[8]),
            })

        last_time = int(data[-1][0])
        if last_time <= cur_start:
            break
        cur_start = last_time + 60_000

        await asyncio.sleep(0.1)  # Binance is more lenient

    return out


def insert_candles(db_path: str, candles: list[dict]) -> int:
    """Bulk insert candles into DB. Returns count inserted."""
    if not candles:
        return 0
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript(SCHEMA)
    rows = [
        (c["exchange"], c["symbol"], "1m", c["open_time"],
         c["open"], c["high"], c["low"], c["close"],
         c["volume"], c["quote_volume"], c["trades"])
        for c in candles
    ]
    cur.executemany(
        """INSERT OR IGNORE INTO historical_candles
           (exchange, symbol, interval, open_time, open, high, low, close,
            volume, quote_volume, trades)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    inserted = cur.rowcount
    conn.commit()
    conn.close()
    return inserted


async def fetch_pair(
    db_path: str,
    symbol: str,
    days: int,
) -> dict:
    """Fetch candles for one pair from both MEXC and Binance."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000

    async with httpx.AsyncClient() as client:
        # Fetch in parallel from both exchanges
        mexc_task = fetch_mexc_candles(client, symbol, start_ms, end_ms)
        binance_task = fetch_binance_candles(client, symbol, start_ms, end_ms)
        mexc_candles, binance_candles = await asyncio.gather(mexc_task, binance_task)

    n_mexc = insert_candles(db_path, mexc_candles)
    n_binance = insert_candles(db_path, binance_candles)

    return {
        "symbol": symbol,
        "mexc": n_mexc,
        "binance": n_binance,
        "total": n_mexc + n_binance,
    }


async def main_async(args) -> None:
    pairs = args.pairs.split(",") if args.pairs else DEFAULT_PAIRS
    pairs = [p.strip().upper() for p in pairs if p.strip()]

    print(f"Fetching {args.days} days of 1m candles for {len(pairs)} pairs...")
    print(f"DB: {args.db}")
    print()

    total_mexc = 0
    total_binance = 0
    for sym in pairs:
        print(f"  {sym} ... ", end="", flush=True)
        try:
            result = await fetch_pair(args.db, sym, args.days)
            print(f"MEXC={result['mexc']:>5}  Binance={result['binance']:>5}")
            total_mexc += result["mexc"]
            total_binance += result["binance"]
        except Exception as e:
            print(f"ERROR: {e}")

    print()
    print(f"TOTAL: MEXC={total_mexc}, Binance={total_binance} candles inserted")


def main():
    parser = argparse.ArgumentParser(description="Fetch historical 1m candles")
    parser.add_argument("--pairs", default="", help="Comma-separated symbols (default: 6 standard)")
    parser.add_argument("--all", action="store_true", help="Use all 6 default pairs (same as no --pairs)")
    parser.add_argument("--days", type=int, default=7, help="Days of history to fetch")
    parser.add_argument("--db", default="/app/data/stakan.db", help="DB path")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
