"""
Live raw data collector — runs in background, captures real-time data
that historical APIs don't provide:

  - Orderbook snapshots (top 5 levels) every 5 seconds per pair

This is for DEEP per-pair analysis: spread distribution, order flow,
whale walls detection, micro-structure regimes.

Storage: `live_orderbook_snapshots`.
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Pairs we deeply care about — collector only writes for these.
COLLECTED_PAIRS = {
    "ZECUSDT",
    "TAOUSDT",
    "1000PEPEUSDT",
    "ENAUSDT",
    "PENGUUSDT",
    "BCHUSDT",
}

def extract_top5(book_snapshot, side: str = "bids") -> list[tuple[float, float]]:
    """Extract top-5 (price, qty) pairs from an OrderBook."""
    if side == "bids":
        levels = book_snapshot.top_bids(5)
    elif side == "asks":
        levels = book_snapshot.top_asks(5)
    else:
        return [(None, None)] * 5
    out = [(float(lvl.price), float(lvl.size)) for lvl in levels[:5]]
    while len(out) < 5:
        out.append((None, None))
    return out


async def snapshot_orderbook(
    db, exchange: str, symbol: str, ob_manager
) -> bool:
    """Take one snapshot of the orderbook and persist."""
    try:
        book = ob_manager.get(exchange, symbol)
    except Exception:
        return False
    if book is None:
        return False

    bids = extract_top5(book, "bids")
    asks = extract_top5(book, "asks")

    # Quick metrics
    bid1 = bids[0][0] if bids[0][0] is not None else None
    ask1 = asks[0][0] if asks[0][0] is not None else None
    if bid1 is None or ask1 is None or bid1 <= 0 or ask1 <= 0:
        return False
    mid = (bid1 + ask1) / 2
    spread_pct = (ask1 - bid1) / mid * 100 if mid > 0 else None
    bid_depth = sum(b[1] or 0 for b in bids)
    ask_depth = sum(a[1] or 0 for a in asks)

    ts_ms = int(time.time() * 1000)

    args = [
        exchange, symbol, ts_ms,
        bids[0][0], bids[0][1], bids[1][0], bids[1][1],
        bids[2][0], bids[2][1], bids[3][0], bids[3][1],
        bids[4][0], bids[4][1],
        asks[0][0], asks[0][1], asks[1][0], asks[1][1],
        asks[2][0], asks[2][1], asks[3][0], asks[3][1],
        asks[4][0], asks[4][1],
        mid, spread_pct, bid_depth, ask_depth,
    ]
    try:
        await db.execute(
            """
            INSERT INTO live_orderbook_snapshots (
                exchange, symbol, ts_ms,
                bid1_price, bid1_qty, bid2_price, bid2_qty,
                bid3_price, bid3_qty, bid4_price, bid4_qty,
                bid5_price, bid5_qty,
                ask1_price, ask1_qty, ask2_price, ask2_qty,
                ask3_price, ask3_qty, ask4_price, ask4_qty,
                ask5_price, ask5_qty,
                mid_price, spread_pct, bid_depth_5, ask_depth_5
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?)
            """,
            args,
        )
        return True
    except Exception as e:
        logger.debug("orderbook snapshot insert failed: %s", e)
        return False


async def raw_data_collector_loop(
    db,
    ob_manager,
    interval_sec: float = 5.0,
    pairs: set[str] | None = None,
) -> None:
    """
    Periodically snapshot orderbooks for the 6 deep-analyzed pairs.

    Light overhead: 6 pairs × 2 exchanges × 1 row / 5s = 17k rows/day.
    At ~250 bytes per row = ~4 MB/day for orderbook data.
    """
    pairs = pairs or COLLECTED_PAIRS

    # Ensure schema (Database wrapper has no executescript, run statements one by one)
    schema_statements = [
        """CREATE TABLE IF NOT EXISTS live_orderbook_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange    TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            ts_ms       INTEGER NOT NULL,
            bid1_price  REAL, bid1_qty REAL,
            bid2_price  REAL, bid2_qty REAL,
            bid3_price  REAL, bid3_qty REAL,
            bid4_price  REAL, bid4_qty REAL,
            bid5_price  REAL, bid5_qty REAL,
            ask1_price  REAL, ask1_qty REAL,
            ask2_price  REAL, ask2_qty REAL,
            ask3_price  REAL, ask3_qty REAL,
            ask4_price  REAL, ask4_qty REAL,
            ask5_price  REAL, ask5_qty REAL,
            mid_price   REAL,
            spread_pct  REAL,
            bid_depth_5 REAL,
            ask_depth_5 REAL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_lob_symbol_ts ON live_orderbook_snapshots(symbol, ts_ms)",
        "CREATE INDEX IF NOT EXISTS idx_lob_exch_symbol_ts ON live_orderbook_snapshots(exchange, symbol, ts_ms)",
    ]
    for stmt in schema_statements:
        try:
            await db.execute(stmt)
        except Exception:
            logger.exception("Failed to execute raw_data schema stmt")

    logger.info(
        "Raw data collector started — %d pairs, interval=%.1fs",
        len(pairs), interval_sec,
    )

    iteration = 0
    while True:
        try:
            iteration += 1
            saved_total = 0
            for symbol in pairs:
                for exchange in ("mexc", "binance"):
                    ok = await snapshot_orderbook(db, exchange, symbol, ob_manager)
                    if ok:
                        saved_total += 1

            # Periodic stats
            if iteration % 60 == 0:  # every ~5 minutes
                logger.info(
                    "[raw_data] %d snapshots in last batch (iter=%d)",
                    saved_total, iteration,
                )

        except Exception:
            logger.exception("raw_data_collector_loop iteration failed")

        await asyncio.sleep(interval_sec)
