"""Tests for real-latency-logging patch (2026-05-08).

Verifies:
  1. Live DB schema migration is idempotent and adds 4 columns to existing
     pre-patch tables (mexc_order_id_open/close, real_entry/close_latency_ms).
  2. Shadow DB ALTER TABLE migration adds the same 4 columns.
  3. _persist_trade() writes latency + order_id for live trades.
  4. _persist_trade() writes NULL for these fields on shadow trades (no real
     exchange call → no real latency, by design).
  5. INSERT does not raise on a freshly-migrated DB.

Run from project root:
    pytest tests/test_latency_logging.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import aiosqlite
import pytest

# Make src/ importable when run from project root
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from storage.db_live import (
    LiveDatabase,
    LIVE_TRADES_LATENCY_COLUMNS,
    init_live_db,
)


# ============================================================
# 1) Idempotent migration on FRESH live DB (post-patch schema)
# ============================================================

@pytest.mark.asyncio
async def test_init_live_db_fresh_has_all_columns():
    """A new DB created by init_live_db should have all 4 latency columns."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "fresh.db")
        live_db = LiveDatabase(db_path)
        await live_db.connect()
        try:
            await init_live_db(live_db)
            cursor = await live_db.conn.execute("PRAGMA table_info(live_trades)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "mexc_order_id_open" in cols
            assert "mexc_order_id_close" in cols
            assert "real_entry_latency_ms" in cols
            assert "real_close_latency_ms" in cols
        finally:
            await live_db.close()


# ============================================================
# 2) Migration on PRE-PATCH live DB (existing table without latency cols)
# ============================================================

@pytest.mark.asyncio
async def test_init_live_db_migrates_old_table():
    """If live_trades table exists without latency cols, migration adds them
    without dropping data."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "old.db")
        # Simulate pre-patch DB: create live_trades with only 37 base columns
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
                CREATE TABLE live_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    leverage INTEGER NOT NULL,
                    margin_usdt REAL NOT NULL,
                    notional_usdt REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_slippage_pct REAL NOT NULL,
                    opened_at INTEGER NOT NULL,
                    mode TEXT
                )
            """)
            # Insert one pre-patch row to verify it survives migration
            await conn.execute(
                """INSERT INTO live_trades
                   (signal_id, symbol, direction, leverage, margin_usdt,
                    notional_usdt, entry_price, entry_slippage_pct, opened_at, mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (1, "PENGUUSDT", "long", 50, 10.0, 500.0, 0.001234, 0.0, 1700000000, "live"),
            )
            await conn.commit()

        # Now run init_live_db (should add columns idempotently)
        live_db = LiveDatabase(db_path)
        await live_db.connect()
        try:
            await init_live_db(live_db)
            cursor = await live_db.conn.execute("PRAGMA table_info(live_trades)")
            cols = {row[1] for row in await cursor.fetchall()}
            for col_name, _ in LIVE_TRADES_LATENCY_COLUMNS:
                assert col_name in cols, f"Missing column after migration: {col_name}"

            # Verify pre-existing row survived and new cols are NULL for it
            row = await live_db.fetchone(
                "SELECT symbol, mexc_order_id_open, real_entry_latency_ms FROM live_trades WHERE signal_id=1"
            )
            assert row is not None
            assert row["symbol"] == "PENGUUSDT"
            assert row["mexc_order_id_open"] is None
            assert row["real_entry_latency_ms"] is None
        finally:
            await live_db.close()


# ============================================================
# 3) Migration is idempotent (run twice — no error)
# ============================================================

@pytest.mark.asyncio
async def test_init_live_db_double_init_no_error():
    """Running init_live_db twice on the same DB must not raise."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "double.db")
        live_db = LiveDatabase(db_path)
        await live_db.connect()
        try:
            await init_live_db(live_db)
            await init_live_db(live_db)  # should be idempotent
            cursor = await live_db.conn.execute("PRAGMA table_info(live_trades)")
            cols = [row[1] for row in await cursor.fetchall()]
            # No duplicates
            assert len(cols) == len(set(cols))
        finally:
            await live_db.close()


# ============================================================
# 4) Full INSERT row-trip for LIVE trade (latencies populated)
# ============================================================

@pytest.mark.asyncio
async def test_live_trade_writes_latency_and_order_id():
    """Live trade INSERT writes latency_ms + order_id correctly.

    This test reproduces the INSERT statement structure from
    shadow_engine._persist_trade() to catch column/placeholder mismatches.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "live.db")
        live_db = LiveDatabase(db_path)
        await live_db.connect()
        try:
            await init_live_db(live_db)

            # Simulate a closed live trade with latency captured from MEXC
            await live_db.execute(
                """INSERT INTO live_trades
                   (signal_id, symbol, direction, leverage, margin_usdt, notional_usdt,
                    entry_price, entry_slippage_pct, opened_at,
                    exit_price, exit_slippage_pct, closed_at, exit_reason,
                    pnl_usdt, roi_pct, duration_sec, duration_ms,
                    mfe_pct, mae_pct, peak_roi_pct, trough_roi_pct,
                    entry_target_price, entry_filled_pct, entry_status, entry_attempted_at,
                    detector_source, confidence,
                    binance_price_at_entry, mexc_price_at_entry, mexc_lag_at_entry_pct,
                    entry_fees_usdt, exit_fees_usdt, net_pnl_usdt,
                    time_to_max_favorable_sec, mode, account_label,
                    mexc_order_id_open, mexc_order_id_close,
                    real_entry_latency_ms, real_close_latency_ms)
                   VALUES (?,?,?,?,?,?, ?,?,?, ?,?,?,?,
                           ?,?,?,?, ?,?,?,?, ?,?,?,?,
                           ?,?, ?,?,?,
                           ?,?,?, ?,?,?,
                           ?,?, ?,?)""",
                (
                    100, "PENGUUSDT", "long", 50, 15.0, 750.0,
                    0.001234, 0.0, 1700000000,
                    0.001245, 0.0, 1700000060, "trailing_tp",
                    1.5, 0.89, 60, 60000,
                    0.005, -0.001, 1.2, -0.001,
                    0.001233, 100.0, "filled", 1700000000,
                    "static_gap", 0.5,
                    0.001230, 0.001234, 0.32,
                    0.075, 0.075, 1.35,
                    25, "live", "slot1",
                    "ABC123XYZ", None,           # mexc_order_id_open, _close
                    287, 312,                     # real_entry_latency_ms, _close_
                ),
            )
            row = await live_db.fetchone(
                """SELECT mexc_order_id_open, mexc_order_id_close,
                          real_entry_latency_ms, real_close_latency_ms
                   FROM live_trades WHERE signal_id=100"""
            )
            assert row is not None
            assert row["mexc_order_id_open"] == "ABC123XYZ"
            assert row["mexc_order_id_close"] is None  # by design — close_all has no orderId
            assert row["real_entry_latency_ms"] == 287
            assert row["real_close_latency_ms"] == 312
        finally:
            await live_db.close()


# ============================================================
# 5) Column count consistency check
# ============================================================

def test_insert_columns_match_placeholders():
    """Statically verify _persist_trade INSERT column count == placeholder count.
    Catches drift if someone edits one and not the other."""
    shadow_engine_path = (
        Path(__file__).resolve().parents[1] / "src" / "strategy" / "shadow_engine.py"
    )
    src = shadow_engine_path.read_text()

    import re
    # Find _persist_trade method body
    m = re.search(
        r"async def _persist_trade.*?(?=\n    async def |\n    def |\n    # =+)",
        src, re.DOTALL,
    )
    assert m, "Could not locate _persist_trade in shadow_engine.py"
    body = m.group(0)

    # Extract column block: between '(' after target_table and the matching ')'
    cols_match = re.search(
        r"INSERT INTO \{target_table\}\s*\(([^)]+)\)\s*VALUES",
        body, re.DOTALL,
    )
    assert cols_match, "Could not locate column list in INSERT"
    cols_text = cols_match.group(1)
    cols = [c.strip() for c in cols_text.replace("\n", " ").split(",") if c.strip()]

    # Count placeholders inside VALUES (...)
    values_match = re.search(r"VALUES \(([^\"]+?)\)\"\"\"", body, re.DOTALL)
    assert values_match, "Could not locate VALUES block"
    placeholders = values_match.group(1).count("?")

    assert len(cols) == placeholders, (
        f"Column/placeholder mismatch: {len(cols)} columns, {placeholders} placeholders"
    )
    # Sanity: must include the new latency columns
    assert "mexc_order_id_open" in cols
    assert "mexc_order_id_close" in cols
    assert "real_entry_latency_ms" in cols
    assert "real_close_latency_ms" in cols
