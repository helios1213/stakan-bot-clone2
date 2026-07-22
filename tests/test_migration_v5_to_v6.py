"""Test for v5 → v6 schema migration.

Critical: this verifies that an existing production DB with legacy
sizing columns successfully migrates to v6 schema (legacy columns
dropped, data preserved) WITHOUT data loss.

Real production DB has:
  - webkey_slots with live_margin_min/max_usdt, live_leverage_min/max columns
  - live_pair_whitelist with margin_min/max_usdt, leverage_min/max columns
  - pair_configs with margin_usdt + leverage (legacy single-value)

Migration must:
  - Drop the legacy columns from all 3 tables
  - Preserve all other data (webkey blobs, pair assignments, configs, etc.)
  - Be idempotent (running twice = no-op on second run)
  - Be atomic (failure rolls back, no half-state)
"""
from __future__ import annotations

import os
import tempfile

import aiosqlite
import pytest

from src.storage.db import _migrate_v5_to_v6


# ──────────────────────────────────────────────────────────────────────
# Helpers — build a v5-shaped DB with legacy columns
# ──────────────────────────────────────────────────────────────────────

async def _build_v5_db(db_path: str) -> None:
    """Create a v5-shaped DB with legacy sizing columns populated."""
    async with aiosqlite.connect(db_path) as db:
        # webkey_slots with legacy live_margin/leverage columns
        await db.execute("""
            CREATE TABLE webkey_slots (
                slot_id                 INTEGER PRIMARY KEY,
                label                   TEXT,
                enabled                 INTEGER NOT NULL DEFAULT 0,
                webkey_blob             BLOB,
                visitor_blob            BLOB,
                proxy_blob              BLOB,
                last_health_check       INTEGER,
                last_latency_ms         INTEGER,
                last_balance_usdt       TEXT,
                last_error              TEXT,
                webkey_refreshed_at     INTEGER,
                created_at              INTEGER NOT NULL,
                updated_at              INTEGER NOT NULL,
                assigned_pair           TEXT DEFAULT NULL,
                live_enabled            INTEGER NOT NULL DEFAULT 0,
                live_margin_min_usdt    REAL DEFAULT NULL,
                live_margin_max_usdt    REAL DEFAULT NULL,
                live_leverage_min       INTEGER DEFAULT NULL,
                live_leverage_max       INTEGER DEFAULT NULL
            )
        """)
        # Insert sample data with legacy values populated
        await db.execute("""
            INSERT INTO webkey_slots
                (slot_id, label, enabled, webkey_blob, visitor_blob, proxy_blob,
                 last_health_check, last_latency_ms, last_balance_usdt, last_error,
                 webkey_refreshed_at, created_at, updated_at,
                 assigned_pair, live_enabled,
                 live_margin_min_usdt, live_margin_max_usdt,
                 live_leverage_min, live_leverage_max)
            VALUES
                (1, 'main', 1, X'DEADBEEF', X'CAFE', NULL,
                 0, 200, '35.5', NULL,
                 1000000, 1000000, 1000000,
                 'PENGUUSDT', 1,
                 15.0, 23.0, 50, 70)
        """)

        # live_pair_whitelist with legacy sizing
        await db.execute("""
            CREATE TABLE live_pair_whitelist (
                symbol TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                margin_min_usdt REAL NOT NULL DEFAULT 22.0,
                margin_max_usdt REAL NOT NULL DEFAULT 30.0,
                leverage_min INTEGER NOT NULL DEFAULT 50,
                leverage_max INTEGER NOT NULL DEFAULT 85,
                recommended_min_balance_usdt REAL NOT NULL DEFAULT 50.0,
                added_at INTEGER NOT NULL
            )
        """)
        await db.execute("""
            INSERT INTO live_pair_whitelist VALUES
                ('PENGUUSDT', 'Meme L1', 2.0, 7.0, 50, 100, 20.0, 1000000),
                ('SUIUSDT', 'High volume', 2.0, 8.0, 50, 100, 20.0, 1000000)
        """)

        # pair_configs with legacy margin_usdt + leverage (single-value)
        await db.execute("""
            CREATE TABLE pair_configs (
                symbol                       TEXT PRIMARY KEY,
                strategy_type                TEXT NOT NULL DEFAULT 'sniper',
                disabled_detectors           TEXT NOT NULL DEFAULT '',
                min_confidence               REAL NOT NULL DEFAULT 0.40,
                margin_usdt                  REAL NOT NULL DEFAULT 25,
                margin_min_usdt              REAL NOT NULL DEFAULT 23,
                margin_max_usdt              REAL NOT NULL DEFAULT 30,
                leverage                     INTEGER NOT NULL DEFAULT 50,
                leverage_min                 INTEGER NOT NULL DEFAULT 50,
                leverage_max                 INTEGER NOT NULL DEFAULT 80,
                stop_loss_ticks              INTEGER DEFAULT 5,
                max_hold_sec                 INTEGER NOT NULL DEFAULT 600
            )
        """)
        await db.execute("""
            INSERT INTO pair_configs (symbol, margin_usdt, margin_min_usdt, margin_max_usdt,
                                       leverage, leverage_min, leverage_max,
                                       stop_loss_ticks, min_confidence)
            VALUES
                ('PENGUUSDT', 5.0, 2.0, 7.0, 50, 50, 100, 5, 0.55),
                ('SUIUSDT',   5.0, 2.0, 8.0, 75, 50, 100, 8, 0.55)
        """)
        await db.commit()


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────

class TestMigrationV5ToV6:
    @pytest.mark.asyncio
    async def test_drops_webkey_slots_legacy_columns(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            await _build_v5_db(db_path)

            async with aiosqlite.connect(db_path) as db:
                await _migrate_v5_to_v6(db)

                # Verify legacy columns are GONE
                cursor = await db.execute("PRAGMA table_info(webkey_slots)")
                cols = {r[1] for r in await cursor.fetchall()}
                assert "live_margin_min_usdt" not in cols
                assert "live_margin_max_usdt" not in cols
                assert "live_leverage_min" not in cols
                assert "live_leverage_max" not in cols

                # Verify other columns preserved
                assert "slot_id" in cols
                assert "assigned_pair" in cols
                assert "live_enabled" in cols
                assert "webkey_blob" in cols
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_drops_whitelist_legacy_columns(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            await _build_v5_db(db_path)

            async with aiosqlite.connect(db_path) as db:
                await _migrate_v5_to_v6(db)

                cursor = await db.execute("PRAGMA table_info(live_pair_whitelist)")
                cols = {r[1] for r in await cursor.fetchall()}
                # Legacy sizing gone
                assert "margin_min_usdt" not in cols
                assert "margin_max_usdt" not in cols
                assert "leverage_min" not in cols
                assert "leverage_max" not in cols
                # Admission-only fields preserved
                assert "symbol" in cols
                assert "description" in cols
                assert "recommended_min_balance_usdt" in cols
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_drops_pair_configs_legacy_single_value_fields(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            await _build_v5_db(db_path)

            async with aiosqlite.connect(db_path) as db:
                await _migrate_v5_to_v6(db)

                cursor = await db.execute("PRAGMA table_info(pair_configs)")
                cols = {r[1] for r in await cursor.fetchall()}
                # Legacy single-value fields gone
                assert "margin_usdt" not in cols
                assert "leverage" not in cols
                # Range fields preserved
                assert "margin_min_usdt" in cols
                assert "margin_max_usdt" in cols
                assert "leverage_min" in cols
                assert "leverage_max" in cols
                # Other config preserved
                assert "stop_loss_ticks" in cols
                assert "min_confidence" in cols
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_data_preserved_after_migration(self):
        """Crucial: webkey blobs, pair configs, etc. must survive migration."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            await _build_v5_db(db_path)

            async with aiosqlite.connect(db_path) as db:
                await _migrate_v5_to_v6(db)

                # webkey blob preserved
                cursor = await db.execute(
                    "SELECT webkey_blob, assigned_pair, live_enabled FROM webkey_slots WHERE slot_id=1"
                )
                row = await cursor.fetchone()
                assert row[0] == b"\xde\xad\xbe\xef"
                assert row[1] == "PENGUUSDT"
                assert row[2] == 1

                # pair_configs sizing preserved (without legacy fields)
                cursor = await db.execute(
                    "SELECT margin_min_usdt, margin_max_usdt, leverage_min, leverage_max, "
                    "stop_loss_ticks FROM pair_configs WHERE symbol='PENGUUSDT'"
                )
                row = await cursor.fetchone()
                assert row[0] == 2.0
                assert row[1] == 7.0
                assert row[2] == 50
                assert row[3] == 100
                assert row[4] == 5

                # whitelist description preserved
                cursor = await db.execute(
                    "SELECT description FROM live_pair_whitelist WHERE symbol='PENGUUSDT'"
                )
                row = await cursor.fetchone()
                assert row[0] == "Meme L1"
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_idempotent_safe_to_run_twice(self):
        """Migration must be idempotent — running on already-v6 DB is no-op."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            await _build_v5_db(db_path)

            async with aiosqlite.connect(db_path) as db:
                # First run — actual migration
                await _migrate_v5_to_v6(db)

                # Capture state after first migration
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM webkey_slots"
                )
                count_before = (await cursor.fetchone())[0]

                # Second run — should be no-op
                await _migrate_v5_to_v6(db)

                cursor = await db.execute(
                    "SELECT COUNT(*) FROM webkey_slots"
                )
                count_after = (await cursor.fetchone())[0]

                assert count_before == count_after, (
                    "Idempotent migration must not change row count"
                )
        finally:
            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_fresh_db_no_legacy_columns_skips_migration(self):
        """If schema is already v6 (e.g. fresh install), migration is no-op."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            async with aiosqlite.connect(db_path) as db:
                # Build v6-shaped tables directly (no legacy columns)
                await db.execute("""
                    CREATE TABLE webkey_slots (
                        slot_id INTEGER PRIMARY KEY,
                        label TEXT,
                        assigned_pair TEXT,
                        live_enabled INTEGER NOT NULL DEFAULT 0
                    )
                """)
                await db.execute("""
                    CREATE TABLE live_pair_whitelist (
                        symbol TEXT PRIMARY KEY,
                        description TEXT NOT NULL,
                        recommended_min_balance_usdt REAL NOT NULL DEFAULT 50.0,
                        added_at INTEGER NOT NULL
                    )
                """)
                await db.execute("""
                    CREATE TABLE pair_configs (
                        symbol TEXT PRIMARY KEY,
                        margin_min_usdt REAL DEFAULT 2.0,
                        margin_max_usdt REAL DEFAULT 7.0,
                        leverage_min INTEGER DEFAULT 50,
                        leverage_max INTEGER DEFAULT 100
                    )
                """)
                await db.commit()

                # Migration is no-op
                await _migrate_v5_to_v6(db)

                # Schema unchanged
                cursor = await db.execute("PRAGMA table_info(webkey_slots)")
                cols = {r[1] for r in await cursor.fetchall()}
                assert "live_margin_min_usdt" not in cols
        finally:
            os.unlink(db_path)
