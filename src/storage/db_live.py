"""
LiveDatabase — окрема SQLite БД для live trading даних.

Повністю ізольована від shadow DB. Створюється і керується незалежно.
Schema: live_trades, live_state.

Використання:
    live_db = LiveDatabase("/app/data/stakan-live.db")
    await live_db.connect()
    await init_live_db(live_db)
    await live_db.execute("INSERT INTO live_trades ...", params)
"""
from __future__ import annotations

import logging
import time
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


# ============================================================
# Schema — mirrors shadow_trades but isolated
# ============================================================

LIVE_SCHEMA_STATEMENTS = [
    # Live trades
    """CREATE TABLE IF NOT EXISTS live_trades (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id           INTEGER,
        symbol              TEXT NOT NULL,
        direction           TEXT NOT NULL,
        leverage            INTEGER NOT NULL,
        margin_usdt         REAL NOT NULL,
        notional_usdt       REAL NOT NULL,
        entry_price         REAL NOT NULL,
        entry_slippage_pct  REAL NOT NULL,
        opened_at           INTEGER NOT NULL,
        exit_price          REAL,
        exit_slippage_pct   REAL,
        closed_at           INTEGER,
        exit_reason         TEXT,
        pnl_usdt            REAL,
        roi_pct             REAL,
        duration_sec        INTEGER,
        duration_ms         INTEGER,
        mfe_pct             REAL,
        mae_pct             REAL,
        peak_roi_pct        REAL,
        trough_roi_pct      REAL,
        entry_target_price  REAL,
        entry_filled_pct    REAL,
        entry_status        TEXT,
        entry_attempted_at  INTEGER,
        detector_source     TEXT,
        confidence          REAL,
        binance_price_at_entry  REAL,
        mexc_price_at_entry     REAL,
        mexc_lag_at_entry_pct   REAL,
        entry_fees_usdt     REAL,
        exit_fees_usdt      REAL,
        net_pnl_usdt        REAL,
        time_to_max_favorable_sec INTEGER,
        mode                TEXT,
        account_label       TEXT,
        mexc_order_id_open  TEXT,
        mexc_order_id_close TEXT,
        real_entry_latency_ms INTEGER,
        real_close_latency_ms INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_live_symbol_time ON live_trades(symbol, opened_at)",
    "CREATE INDEX IF NOT EXISTS idx_live_closed ON live_trades(closed_at)",
    "CREATE INDEX IF NOT EXISTS idx_live_account ON live_trades(account_label)",

    # State for recovery
    """CREATE TABLE IF NOT EXISTS live_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )""",

    # Per-slot cumulative-PnL reset markers. The slot view sums net_pnl_usdt
    # for trades with closed_at >= reset_at; "Reset PnL" sets reset_at = now.
    # No row → reset_at treated as 0 (count everything since the beginning).
    """CREATE TABLE IF NOT EXISTS slot_pnl_reset (
        slot_id  INTEGER PRIMARY KEY,
        reset_at INTEGER NOT NULL
    )""",

    # Failed/expired live-open attempts. live_trades only stores opens that
    # actually FILLED, so the DB was blind to the true fill rate — misses
    # (ioc_expired_no_fill, rejects, 510s) vanished into logs. With this table,
    # fill_rate = filled / (filled + misses) is queryable per symbol/window:
    #   filled = SELECT COUNT(*) FROM live_trades       WHERE symbol=? AND opened_at>?
    #   missed = SELECT COUNT(*) FROM live_open_misses  WHERE symbol=? AND ts>?
    """CREATE TABLE IF NOT EXISTS live_open_misses (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ts             INTEGER NOT NULL,
        symbol         TEXT NOT NULL,
        direction      TEXT NOT NULL,
        slot_id        INTEGER,
        reason         TEXT NOT NULL,
        confidence     REAL,
        ioc_offset_ticks INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_live_misses_sym_ts ON live_open_misses(symbol, ts)",

    # Schema version
    """CREATE TABLE IF NOT EXISTS live_schema_version (
        version INTEGER PRIMARY KEY,
        applied_at INTEGER NOT NULL
    )""",
]

LIVE_SCHEMA_VERSION = 1


class LiveDatabase:
    """Independent SQLite connection for live trading data."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = aiosqlite.Row

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("LiveDatabase not connected. Call connect() first.")
        return self._conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def executemany(self, sql: str, params_list: list[tuple[Any, ...]]) -> None:
        await self.conn.executemany(sql, params_list)
        await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())


async def _add_columns_idempotent_live(
    live_db: LiveDatabase,
    table: str,
    columns: list[tuple[str, str]],
) -> None:
    """Add columns to live DB table if they don't already exist. Mirror of
    _add_columns_idempotent in db.py — duplicated here to keep db_live.py
    standalone (LiveDatabase wraps a separate aiosqlite connection)."""
    cursor = await live_db.conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cursor.fetchall()}

    for col_name, col_type in columns:
        if col_name in existing:
            continue
        try:
            await live_db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
            logger.info("Added column %s.%s (%s)", table, col_name, col_type)
        except Exception as e:
            logger.warning("Failed to add %s.%s: %s", table, col_name, e)


async def _drop_column_idempotent_live(
    live_db: LiveDatabase,
    table: str,
    col: str,
) -> None:
    """Drop a column from a live DB table if present (SQLite ≥3.35)."""
    cursor = await live_db.conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cursor.fetchall()}
    if col not in existing:
        return
    try:
        await live_db.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        logger.info("Dropped column %s.%s", table, col)
    except Exception as e:
        logger.warning("Failed to drop %s.%s: %s", table, col, e)


# Latency-logging columns added after initial schema.
# Idempotent — safe to run on every startup, only adds missing columns. Required
# for DBs created before patch (CREATE TABLE IF NOT EXISTS does not retrofit).
LIVE_TRADES_LATENCY_COLUMNS = [
    ("mexc_order_id_open",    "TEXT"),
    ("mexc_order_id_close",   "TEXT"),
    ("real_entry_latency_ms", "INTEGER"),
    ("real_close_latency_ms", "INTEGER"),
    # latency breakdown:
    # signal_to_pickup_ms  : signal.created_at → engine picked it up
    # submit_ms            : engine start → POST done to MEXC
    # response_ms          : POST done → MEXC response received
    # fill_poll_ms         : response → fill confirmed via poll (IOC only)
    # real_entry_latency_ms above = signal_to_pickup + submit + response + fill_poll
    ("latency_signal_to_pickup_ms", "INTEGER"),
    ("latency_submit_ms",           "INTEGER"),
    ("latency_response_ms",         "INTEGER"),
    ("latency_fill_poll_ms",        "INTEGER"),
    # Close side analogous
    ("latency_close_submit_ms",     "INTEGER"),
    ("latency_close_response_ms",   "INTEGER"),
    # peak_ticks snapshots at fixed milestones (warmup hypothesis).
    # Used to validate "proof of life" rule: peak_ticks >= 1 at t=1500ms reliably
    # separates winners from phase_1_gap_collapse losers?
    # replaces phase_1 with warmup-exit.
    ("peak_ticks_at_500ms",  "REAL"),
    ("peak_ticks_at_1000ms", "REAL"),
    ("peak_ticks_at_1500ms", "REAL"),
    ("peak_ticks_at_2000ms", "REAL"),
    # Instantaneous adverse excursion at 1000ms (ticks, positive =
    # against us) — what nevergreen_cut tests, unlike terminal mae_pct.
    ("adverse_ticks_at_1000ms", "REAL"),
]


async def init_live_db(live_db: LiveDatabase) -> None:
    """Create schema if not exists. Idempotent."""
    for stmt in LIVE_SCHEMA_STATEMENTS:
        try:
            await live_db.execute(stmt)
        except Exception:
            logger.exception("Failed to execute live schema stmt: %s", stmt[:80])

    # Idempotent column migration for DBs created before latency-logging patch.
    await _add_columns_idempotent_live(live_db, "live_trades", LIVE_TRADES_LATENCY_COLUMNS)
    # 2026-07-20: per-signal join key (signal.created_at_ms) — no FK, links trade→signal_features.
    await _add_columns_idempotent_live(live_db, "live_trades", [("signal_uid", "INTEGER")])
    # 2026-06: misses log moved bps → tick-exact offset.
    await _add_columns_idempotent_live(live_db, "live_open_misses", [("ioc_offset_ticks", "INTEGER")])
    await _drop_column_idempotent_live(live_db, "live_open_misses", "ioc_offset_bps")

    row = await live_db.fetchone(
        "SELECT version FROM live_schema_version ORDER BY version DESC LIMIT 1"
    )
    if not row or row["version"] < LIVE_SCHEMA_VERSION:
        await live_db.execute(
            "INSERT OR REPLACE INTO live_schema_version (version, applied_at) VALUES (?, ?)",
            (LIVE_SCHEMA_VERSION, int(time.time())),
        )

    logger.info("Live database initialized at %s (schema v%d)",
                live_db.db_path, LIVE_SCHEMA_VERSION)
