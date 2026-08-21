"""
SQLite database layer — initialization, schema migrations.

We use aiosqlite for fully async access. Schema is versioned so we can
add columns without breaking existing data.

v1: api_credentials, pairs_universe, walls, signals,
                shadow_trades (legacy), bot_state, schema_version
v2: pair_states, pair_configs, state_transitions
                + extended shadow_trades columns
v3 (Webkey):    webkey_credentials (single-row encrypted store, deprecated)
v4 (Multi-slot): webkey_slots (5-slot multi-account: webkey + dolos + proxy
                 per slot, all encrypted). v3 data migrated into slot 1.
v5 (Webkey-only): webkey_slots simplified — dolos_blob dropped, replaced
                  with visitor_blob (auto-generated visitor_id only).
                  member_id/chash/mhash no longer stored (chash is hardcoded
                  bootstrap, mhash = MD5(visitor), member_id placeholder).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 6


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS pairs_universe (
    symbol             TEXT PRIMARY KEY,
    binance_listed     INTEGER NOT NULL DEFAULT 1,
    mexc_listed        INTEGER NOT NULL DEFAULT 0,
    mexc_max_leverage  INTEGER,
    score              REAL,
    volume_24h_usdt    REAL,
    spread_pct         REAL,
    depth_usdt         REAL,
    atr_5m_pct         REAL,
    is_active          INTEGER NOT NULL DEFAULT 0,
    last_evaluated     INTEGER,
    notes              TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    source              TEXT NOT NULL,
    binance_impulse_pct REAL,
    mexc_lag_pct        REAL,
    confidence          REAL,
    metadata_json       TEXT,
    created_at          INTEGER NOT NULL,
    consumed            INTEGER NOT NULL DEFAULT 0,
    consumed_by         TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_time ON signals(symbol, created_at);

CREATE TABLE IF NOT EXISTS shadow_trades (
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
    mfe_pct             REAL,
    mae_pct             REAL,
    peak_roi_pct        REAL,
    trough_roi_pct      REAL,
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);
CREATE INDEX IF NOT EXISTS idx_shadow_symbol_time ON shadow_trades(symbol, opened_at);
CREATE INDEX IF NOT EXISTS idx_shadow_closed ON shadow_trades(closed_at);

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  INTEGER NOT NULL
);
"""


SCHEMA_V2 = """
-- Pair lifecycle state machine
CREATE TABLE IF NOT EXISTS pair_states (
    symbol                       TEXT PRIMARY KEY,
    state                        TEXT NOT NULL,
    state_since                  INTEGER NOT NULL,
    discovered_at                INTEGER,
    shadow_started_at            INTEGER,
    live_started_at              INTEGER,
    paused_at                    INTEGER,
    rejected_at                  INTEGER,
    paused_until                 INTEGER,
    last_24h_signals             INTEGER NOT NULL DEFAULT 0,
    last_24h_trades              INTEGER NOT NULL DEFAULT 0,
    last_24h_winrate             REAL NOT NULL DEFAULT 0,
    last_24h_pnl                 REAL NOT NULL DEFAULT 0,
    last_24h_profit_factor       REAL NOT NULL DEFAULT 0,
    last_24h_avg_edge_pct        REAL NOT NULL DEFAULT 0,
    last_6h_drawdown_pct         REAL NOT NULL DEFAULT 0,
    total_shadow_trades          INTEGER NOT NULL DEFAULT 0,
    total_shadow_pnl             REAL NOT NULL DEFAULT 0,
    total_live_trades            INTEGER NOT NULL DEFAULT 0,
    total_live_pnl               REAL NOT NULL DEFAULT 0,
    last_state_change_reason     TEXT,
    pause_reason                 TEXT,
    updated_at                   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pair_states_state ON pair_states(state);

-- Per-pair tunable strategy parameters
CREATE TABLE IF NOT EXISTS pair_configs (
    symbol                       TEXT PRIMARY KEY,

    -- Strategy type: only 'sniper' is supported.
    -- Kept for backward compat / future strategies.
    strategy_type                TEXT NOT NULL DEFAULT 'sniper',

    min_confidence               REAL NOT NULL DEFAULT 0.40,
    min_mexc_lag_pct             REAL NOT NULL DEFAULT 0.005,
    max_mexc_lag_pct             REAL NOT NULL DEFAULT 0.05,
    ioc_offset_ticks             INTEGER NOT NULL DEFAULT 0,
    ioc_max_attempts             INTEGER NOT NULL DEFAULT 2,
    ioc_attempt_interval_ms      INTEGER NOT NULL DEFAULT 80,

    -- Position sizing (LEGACY). These columns are DROPPED at init (2c) — sizing
    -- is resolved from the pair YAML (ConfigLoader), the single source of truth.
    -- Kept in the base schema only for fresh-DB create→drop; never read.
    margin_min_usdt              REAL NOT NULL DEFAULT 23,
    margin_max_usdt              REAL NOT NULL DEFAULT 30,
    leverage_min                 INTEGER NOT NULL DEFAULT 50,
    leverage_max                 INTEGER NOT NULL DEFAULT 80,

    max_hold_sec                 INTEGER NOT NULL DEFAULT 600,

    cooldown_after_loss_sec      INTEGER NOT NULL DEFAULT 10,
    cooldown_after_win_sec       INTEGER NOT NULL DEFAULT 5,
    notes                        TEXT
);

-- Audit log of state transitions
CREATE TABLE IF NOT EXISTS state_transitions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL,
    from_state          TEXT,
    to_state            TEXT NOT NULL,
    reason              TEXT,
    metrics_snapshot    TEXT,
    triggered_by        TEXT NOT NULL,
    created_at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_symbol ON state_transitions(symbol, created_at);
"""


# Idempotent column additions for shadow_trades
SHADOW_TRADES_V2_COLUMNS = [
    ("signal_uid", "INTEGER"),   # per-signal join key = signal.created_at_ms (no FK, unlike signal_id)
    ("entry_target_price", "REAL"),
    ("entry_filled_pct", "REAL"),
    ("entry_status", "TEXT"),
    ("entry_attempted_at", "INTEGER"),
    ("detector_source", "TEXT"),
    ("confidence", "REAL"),
    ("binance_price_at_entry", "REAL"),
    ("mexc_price_at_entry", "REAL"),
    ("mexc_lag_at_entry_pct", "REAL"),
    ("entry_fees_usdt", "REAL"),
    ("exit_fees_usdt", "REAL"),
    ("net_pnl_usdt", "REAL"),
    ("time_to_max_favorable_sec", "INTEGER"),
    ("mode", "TEXT"),
    ("account_label", "TEXT"),
    ("duration_ms", "INTEGER"),
    # latency tracking (live only; shadow stores NULL)
    ("mexc_order_id_open", "TEXT"),
    ("mexc_order_id_close", "TEXT"),
    ("real_entry_latency_ms", "INTEGER"),
    ("real_close_latency_ms", "INTEGER"),
    ("latency_signal_to_pickup_ms", "INTEGER"),
    ("latency_submit_ms", "INTEGER"),
    ("latency_response_ms", "INTEGER"),
    ("latency_fill_poll_ms", "INTEGER"),
    ("latency_close_submit_ms", "INTEGER"),
    ("latency_close_response_ms", "INTEGER"),
    # peak_ticks snapshots at fixed milestones (warmup hypothesis)
    ("peak_ticks_at_500ms", "REAL"),
    ("peak_ticks_at_1000ms", "REAL"),
    ("peak_ticks_at_1500ms", "REAL"),
    ("peak_ticks_at_2000ms", "REAL"),
    # Instantaneous adverse excursion at 1000ms (ticks, positive = against
    # us). _persist_trade shares one INSERT across both tables, so this
    # MUST stay in step with db_live.py.
    ("adverse_ticks_at_1000ms", "REAL"),
]


# v5: Webkey-only multi-slot. Each row contains the MINIMUM creds for one
# MEXC account:
#   - webkey (Fernet-encrypted)
#   - visitor_id (Fernet-encrypted, auto-generated)
#   - proxy URL (Fernet-encrypted, nullable)
# Empty slots have webkey_blob=NULL.
SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS webkey_slots (
    slot_id                 INTEGER PRIMARY KEY,
    label                   TEXT,                       -- optional user-friendly name
    enabled                 INTEGER NOT NULL DEFAULT 0,
    webkey_blob             BLOB,                       -- Fernet(webkey) | NULL = empty slot
    visitor_blob            BLOB,                       -- Fernet(visitor_id), auto-generated
    proxy_blob              BLOB,                       -- Fernet(proxy_url), nullable
    last_health_check       INTEGER,
    last_latency_ms         INTEGER,
    last_balance_usdt       TEXT,
    last_error              TEXT,
    webkey_refreshed_at     INTEGER,
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL
);
"""


async def _migrate_v3_to_v4(
    db: aiosqlite.Connection,
    env_dolos: dict[str, str] | None = None,
) -> None:
    """One-shot migration: copy v3 single-row data into slot 1 of v4.

    Preserved for installs that still have legacy webkey_credentials table.
    Brings webkey + proxy into webkey_slots[1]; user must re-run /webkey_setup.
    """
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='webkey_credentials'"
    )
    if not await cursor.fetchone():
        return

    cursor = await db.execute("SELECT * FROM webkey_credentials WHERE id=1")
    row = await cursor.fetchone()
    if not row:
        await db.execute("DROP TABLE IF EXISTS webkey_credentials")
        return

    cursor = await db.execute("PRAGMA table_info(webkey_credentials)")
    cols = [r[1] for r in await cursor.fetchall()]
    data = dict(zip(cols, row))

    cursor = await db.execute(
        "SELECT webkey_blob FROM webkey_slots WHERE slot_id=1"
    )
    slot1 = await cursor.fetchone()
    if slot1 and slot1[0] is not None:
        logger.info("Slot 1 already has webkey — skipping v3 migration")
        await db.execute("DROP TABLE IF EXISTS webkey_credentials")
        return

    now_ts = int(time.time())
    await db.execute(
        """
        INSERT OR REPLACE INTO webkey_slots
            (slot_id, enabled, webkey_blob, proxy_blob,
             last_health_check, last_latency_ms, last_balance_usdt,
             webkey_refreshed_at, created_at, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get("enabled", 0),
            data.get("blob"),
            data.get("proxy_blob"),
            data.get("last_health_check"),
            data.get("last_latency_ms"),
            data.get("last_balance_usdt"),
            data.get("webkey_refreshed_at"),
            data.get("created_at", now_ts),
            now_ts,
        ),
    )
    await db.execute("DROP TABLE webkey_credentials")
    logger.info(
        "Migrated v3 webkey_credentials → v4+ webkey_slots[1]. "
        "NOTE: webkey will need to be re-entered via /webkey_setup "
        "(visitor_id auto-generates on next set)."
    )


async def _migrate_v4_to_v5(db: aiosqlite.Connection) -> None:
    """v4 → v5: drop dolos_blob column, add visitor_blob.

    SQLite doesn't support DROP COLUMN before 3.35 reliably, so we use
    table-rename + recreate strategy. Webkey + proxy are preserved; user
    must re-run /webkey_setup to regenerate visitor_id (one-time cost).
    """
    cursor = await db.execute("PRAGMA table_info(webkey_slots)")
    cols = [r[1] for r in await cursor.fetchall()]

    if not cols:
        # Table doesn't exist yet — SCHEMA_V5 will create it fresh.
        return

    if "visitor_blob" in cols and "dolos_blob" not in cols:
        # Already on v5
        return

    if "dolos_blob" not in cols:
        # Some unexpected state — recreating fresh is the safest choice.
        await db.execute("DROP TABLE webkey_slots")
        return

    logger.info("Migrating webkey_slots schema v4 → v5 (drop dolos, add visitor)")

    # Save webkey + proxy + meta from v4 rows
    cursor = await db.execute(
        """
        SELECT slot_id, label, enabled, webkey_blob, proxy_blob,
               last_health_check, last_latency_ms, last_balance_usdt,
               last_error, webkey_refreshed_at, created_at, updated_at
          FROM webkey_slots
        """
    )
    rows = await cursor.fetchall()

    await db.execute("DROP TABLE webkey_slots")
    await db.executescript(SCHEMA_V5)

    now_ts = int(time.time())
    for r in rows:
        await db.execute(
            """
            INSERT OR REPLACE INTO webkey_slots
                (slot_id, label, enabled, webkey_blob, visitor_blob, proxy_blob,
                 last_health_check, last_latency_ms, last_balance_usdt, last_error,
                 webkey_refreshed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
             r[9], r[10], r[11] or now_ts),
        )

    n_with_webkey = sum(1 for r in rows if r[3] is not None)
    if n_with_webkey:
        logger.info(
            "Migrated v4 → v5: %d slot(s) with webkey preserved. "
            "NOTE: visitor_id will auto-regenerate on next /webkey_setup or "
            "first health check that calls set_webkey().",
            n_with_webkey,
        )


async def _migrate_v5_to_v6(db: aiosqlite.Connection) -> None:
    """v5 → v6: consolidate sizing config to a single source of truth.

    Drops legacy sizing columns from three tables:
      - webkey_slots: live_margin_min/max_usdt, live_leverage_min/max
        (per-slot overrides — now ignored in favor of pair_configs)
      - live_pair_whitelist: margin_min/max_usdt, leverage_min/max
        (pair-level defaults — replaced by pair_configs)
      - pair_configs: margin_usdt, leverage (legacy single-value fields —
        replaced by margin_min/max + leverage_min/max)

    After this migration, `pair_configs` is the ONLY source for live and
    shadow trade sizing. Same row drives both modes, so shadow PnL and
    live PnL are directly comparable.

    SQLite doesn't reliably support DROP COLUMN before 3.35, so we use
    the table-rename + recreate strategy for each affected table.
    Transactions are used to ensure atomicity — a failed migration
    leaves the DB in its pre-migration state.
    """
    cursor = await db.execute("PRAGMA table_info(webkey_slots)")
    cols = [r[1] for r in await cursor.fetchall()]
    if not cols:
        return  # fresh DB, will be created without legacy cols

    has_legacy_webkey = "live_margin_min_usdt" in cols
    cursor = await db.execute("PRAGMA table_info(live_pair_whitelist)")
    wl_cols = [r[1] for r in await cursor.fetchall()]
    has_legacy_whitelist = "margin_min_usdt" in wl_cols
    cursor = await db.execute("PRAGMA table_info(pair_configs)")
    pc_cols = [r[1] for r in await cursor.fetchall()]
    has_legacy_pairconfigs = "margin_usdt" in pc_cols  # the single-value legacy

    if not (has_legacy_webkey or has_legacy_whitelist or has_legacy_pairconfigs):
        return  # already on v6

    logger.info(
        "Migrating schema v5 → v6: consolidating sizing to pair_configs "
        "(webkey_legacy=%s, whitelist_legacy=%s, pairconfigs_legacy=%s)",
        has_legacy_webkey, has_legacy_whitelist, has_legacy_pairconfigs,
    )

    await db.execute("BEGIN")
    try:
        # ── 1. webkey_slots: drop live_margin_*, live_leverage_* ──────
        if has_legacy_webkey:
            await db.execute("""
                CREATE TABLE webkey_slots_v6 (
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
                    live_enabled            INTEGER NOT NULL DEFAULT 0
                )
            """)
            await db.execute("""
                INSERT INTO webkey_slots_v6
                    (slot_id, label, enabled, webkey_blob, visitor_blob, proxy_blob,
                     last_health_check, last_latency_ms, last_balance_usdt, last_error,
                     webkey_refreshed_at, created_at, updated_at,
                     assigned_pair, live_enabled)
                SELECT
                    slot_id, label, enabled, webkey_blob, visitor_blob, proxy_blob,
                    last_health_check, last_latency_ms, last_balance_usdt, last_error,
                    webkey_refreshed_at, created_at, updated_at,
                    assigned_pair, live_enabled
                FROM webkey_slots
            """)
            await db.execute("DROP TABLE webkey_slots")
            await db.execute("ALTER TABLE webkey_slots_v6 RENAME TO webkey_slots")
            logger.info("  ✓ webkey_slots: dropped 4 legacy sizing columns")

        # ── 2. live_pair_whitelist: drop margin/leverage columns ─────
        if has_legacy_whitelist:
            await db.execute("""
                CREATE TABLE live_pair_whitelist_v6 (
                    symbol                       TEXT PRIMARY KEY,
                    description                  TEXT NOT NULL,
                    recommended_min_balance_usdt REAL NOT NULL DEFAULT 50.0,
                    added_at                     INTEGER NOT NULL
                )
            """)
            await db.execute("""
                INSERT INTO live_pair_whitelist_v6
                    (symbol, description, recommended_min_balance_usdt, added_at)
                SELECT symbol, description, recommended_min_balance_usdt, added_at
                FROM live_pair_whitelist
            """)
            await db.execute("DROP TABLE live_pair_whitelist")
            await db.execute(
                "ALTER TABLE live_pair_whitelist_v6 RENAME TO live_pair_whitelist"
            )
            logger.info("  ✓ live_pair_whitelist: dropped 4 sizing columns "
                        "(now admission-control only)")

        # ── 3. pair_configs: drop margin_usdt + leverage (legacy single) ──
        # This table has MANY columns (ADD COLUMN'd over time), so we
        # use the "introspect-and-rebuild" approach: read current columns,
        # build new schema without legacy fields, copy data.
        if has_legacy_pairconfigs:
            # Get all current pair_configs columns and types
            cursor = await db.execute("PRAGMA table_info(pair_configs)")
            all_cols_info = await cursor.fetchall()
            # Keep all columns EXCEPT the two legacy ones
            keep_cols = [
                (r[1], r[2], r[3], r[4], r[5])  # name, type, notnull, default, pk
                for r in all_cols_info
                if r[1] not in ("margin_usdt", "leverage")
            ]

            # Build CREATE TABLE statement
            col_defs = []
            for name, typ, notnull, default, pk in keep_cols:
                parts = [name, typ]
                if pk:
                    parts.append("PRIMARY KEY")
                if notnull and not pk:
                    parts.append("NOT NULL")
                if default is not None:
                    parts.append(f"DEFAULT {default}")
                col_defs.append(" ".join(parts))

            await db.execute(
                f"CREATE TABLE pair_configs_v6 ({', '.join(col_defs)})"
            )

            # Copy data, skipping legacy columns
            keep_names = [c[0] for c in keep_cols]
            col_list = ", ".join(keep_names)
            await db.execute(
                f"INSERT INTO pair_configs_v6 ({col_list}) "
                f"SELECT {col_list} FROM pair_configs"
            )
            await db.execute("DROP TABLE pair_configs")
            await db.execute("ALTER TABLE pair_configs_v6 RENAME TO pair_configs")
            logger.info("  ✓ pair_configs: dropped 2 legacy single-value sizing fields")

        await db.commit()
        logger.info("Migrated v5 → v6: sizing consolidation complete")
    except Exception:
        await db.execute("ROLLBACK")
        logger.exception("v5 → v6 migration failed, rolled back")
        raise


async def init_db(db_path: str) -> None:
    """Create database file and apply migrations idempotently."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")

        await db.executescript(SCHEMA_V1)
        await db.executescript(SCHEMA_V2)
        await _add_columns_idempotent(db, "shadow_trades", SHADOW_TRADES_V2_COLUMNS)
        # 2026-08-21: the limit price actually submitted. Added to BOTH trade
        # tables because shadow and live share one INSERT statement — omitting it
        # here would make that statement fail for shadow rows. In shadow it
        # simply mirrors entry_target_price (which there really IS the submitted
        # limit); in live it is the fix for entry_slippage_pct being write-only.
        await _add_columns_idempotent(db, "shadow_trades", [("entry_limit_price", "REAL")])
        # 2026-08-21 (T2.2): парне порівняння shadow проти live на ОДНОМУ сигналі.
        # Досі перетин signal_uid між shadow_trades і live_trades був РІВНО НУЛЬ:
        # пара або live, або shadow, тож будь-яке «чи стало чесніше» порівнювало
        # різні календарні вікна, а не однакові умови. Тут для КОЖНОЇ живої
        # спроби пишеться, що вирішив би симулятор на ТІЙ САМІЙ книзі і з ТИМ
        # САМИМ лімітом. Окрема таблиця, не shadow_trades — щоб жоден наявний
        # агрегат, алерт чи відбір пар не зачепило.
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS shadow_twin (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                INTEGER NOT NULL,
                signal_uid        INTEGER,
                symbol            TEXT NOT NULL,
                direction         TEXT,
                slot_id           INTEGER,
                limit_price       REAL,
                live_filled       INTEGER,
                live_price        REAL,
                live_filled_pct   REAL,
                live_error        TEXT,
                shadow_filled     INTEGER,
                shadow_price      REAL,
                shadow_filled_pct REAL,
                shadow_reason     TEXT,
                notional_usdt     REAL
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_twin_sym_ts
                ON shadow_twin(symbol, ts);
        """)
        # 2026-08-21: КРИВА ВІДГУКУ замість одного числа.
        # Стара форма таблиці була ТАВТОЛОГІЄЮ: знімок книги брався в t0, а
        # ліміт live виводився з ТОГО САМОГО обʼєкта книги мікросекундами
        # пізніше, тож умова філу min(asks) <= best_ask+offset*tick була
        # тотожно істинна — shadow_filled=0 не траплялось ЖОДНОГО разу
        # (0 рядків із 360). «Головне число» дорівнювало 1/(живий fill-rate)
        # і про симулятор не казало нічого.
        # Тепер вердикт рахується на ТРЬОХ затримках від миті ціноутворення:
        #   d0    — 0мс, стара тавтологія, лишена як КОНТРОЛЬ (має бути ~100%)
        #   draw  — uniform(entry_latency_min_ms, max_ms) = продакшн-shadow
        #   rtt   — реальний submit RTT цього ж ордера
        # Стара колонка shadow_filled лишається = d0, щоб історія не поламалась.
        await _add_columns_idempotent(db, "shadow_twin", [
            ("shadow_filled_d0", "INTEGER"),
            ("shadow_filled_draw", "INTEGER"),
            ("shadow_filled_rtt", "INTEGER"),
            # Строгий поріг: status='partial' зараз означає БУДЬ-ЯКЕ ненульове
            # заповнення (у даних є рядок із shadow_filled_pct=0.0341, і він
            # рахувався філом). Від цієї угоди відношення рухається на ~25%,
            # тож рахуємо обидва пороги й вирішуємо на даних, а не на смаку.
            ("shadow_strict_draw", "INTEGER"),
            ("shadow_pct_draw", "REAL"),
            ("shadow_reason_draw", "TEXT"),
            ("delay_draw_ms", "INTEGER"),
            ("delay_rtt_ms", "INTEGER"),
            # Якість стрічки: наскільки книга, яку реально знайшли, відрізняється
            # віком від замовленої. Без цього неможливо відрізнити «симулятор
            # протух» від «стрічка не дала потрібного кадру».
            ("tape_status", "TEXT"),
            ("tape_age_ms", "INTEGER"),
        ])
        # v2.1: add strategy_type to existing pair_configs (for existing installs)
        await _add_columns_idempotent(db, "pair_configs",
                                       [("strategy_type", "TEXT NOT NULL DEFAULT 'sniper'")])
        # NOTE: pair_configs.mode is no longer added — the live authority is
        # pair_states.state (is_in_live); mode was a redundant mirror and is
        # dropped below (existing DBs get it removed by the idempotent loop).
        # Last-modified metadata marker (kept for bookkeeping).
        await _add_columns_idempotent(db, "pair_configs", [
            ("updated_at", "INTEGER"),
        ])
        # Per-pair auto_demotion toggle. When 0, live → paused auto-demotion
        # is bypassed (gives a pair a fair window after a config change).
        await _add_columns_idempotent(db, "pair_configs", [
            ("auto_demotion_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ])

        # ── Batch D (2026-06-15): static pair config moved to per-pair YAML
        # (single source of truth, read by ConfigLoader). These pair_configs
        # columns are now DEAD — read only via the dead config_loader-absent
        # fallback / graceful _g, never in live logic. Drop them.
        for _dead_col in (
            "ioc_offset_bps",            # obsolete bps offset (pre-existing)
            "sl_grace_sec",
            "stop_loss_ticks",
            "phase0_micro_stop_ticks",
            "gap_min_ticks",
            "gap_cooldown_sec",
        ):
            await _drop_column_idempotent(db, "pair_configs", _dead_col)

        # 2026-06-15: the remaining pair_configs sizing/exec columns + the
        # redundant `mode` mirror are now fully dead. Sizing/exec resolve from
        # per-pair YAML (ConfigLoader); the live authority is pair_states.state
        # (is_in_live), and every reader/maintenance op (webpanel + Telegram
        # display, kill-all, per-pair demote) reads state, not mode. Drop them.
        # KEEP: symbol, notes, strategy_type, updated_at, auto_demotion_enabled.
        for _dead_col in (
            "mode",
            "margin_min_usdt", "margin_max_usdt",
            "leverage_min", "leverage_max",
            "min_confidence", "min_mexc_lag_pct", "max_mexc_lag_pct",
            "ioc_max_attempts", "ioc_attempt_interval_ms", "ioc_offset_ticks",
            "max_hold_sec",
            "cooldown_after_loss_sec", "cooldown_after_win_sec",
        ):
            await _drop_column_idempotent(db, "pair_configs", _dead_col)

        # v3 (legacy) → v4 (multi-slot with dolos) → v5 (webkey-only)
        await _migrate_v4_to_v5(db)              # drop dolos_blob if present
        await db.executescript(SCHEMA_V5)        # ensure v5 schema exists
        await _migrate_v3_to_v4(db, env_dolos=None)  # legacy v3 fallback

        # v5.3 → v6: multi-slot pair assignment.
        # Sizing columns (live_margin_*, live_leverage_*) were REMOVED in v6 —
        # sizing is now read exclusively from pair_configs, which is the
        # single source of truth for both shadow and live modes.
        await _add_columns_idempotent(db, "webkey_slots", [
            ("assigned_pair",        "TEXT DEFAULT NULL"),
            ("live_enabled",         "INTEGER NOT NULL DEFAULT 0"),
        ])
        # 2026-06: per-slot recovery mode — read the account's accumulated loss
        # on enable, refuse if too deep (cap), auto-stop once recovered (target).
        await _add_columns_idempotent(db, "webkey_slots", [
            ("recovery_mode",         "INTEGER NOT NULL DEFAULT 0"),   # 0=off (opt-in per slot; only for low-activity recovery accounts)
            ("recovery_cap_usdt",     "REAL NOT NULL DEFAULT 105"),    # refuse-to-enable drawdown cap
            ("recovery_buffer_usdt",  "REAL NOT NULL DEFAULT 1"),      # stop $N short of breakeven
            ("recovery_baseline_ts",  "INTEGER"),                      # NULL=not armed
            ("recovery_target_usdt",  "REAL"),                         # profit to recover; NULL=no auto-stop
        ])
        # 2026-07-19: per-slot margin/leverage OVERRIDE (NULL = inherit pair YAML).
        # Lets two accounts on the same pair trade different sizing.
        # ⚠️ МЕРТВІ КОЛОНКИ. Замінені тим самим днем таблицею slot_pair_sizing;
        # усі NULL, жоден код їх не читає, жоден UI не пише. Лишені тільки щоб
        # не перебудовувати sqlite-таблицю з зашифрованими блобами.
        # ПАСТКА: імена збігаються з ключами, які СПРАВДІ сайзять угоду
        # (shadow_engine бере slot_margin_min_usdt із slot_pair_sizing через
        # live_pool). UPDATE webkey_slots SET slot_margin_min_usdt=... запишеться
        # чисто, переживе рестарт і не змінить розмір позиції.
        # Розмір міняти ТІЛЬКИ через slot_pair_sizing (Telegram/панель).
        # 2026-08-19 Ship 2: slot_margin_*/slot_leverage_* ВИДАЛЕНО. Були
        # МЕРТВІ (ніколи не читались; сайзинг = slot_pair_sizing через
        # live_pool) і FOOTGUN — імена збігались із живими ключами slot_cfg,
        # тож сирий UPDATE тут писався чисто, але розмір НЕ міняв. Дропаємо.
        for _dead in ("slot_margin_min_usdt", "slot_margin_max_usdt",
                      "slot_leverage_min", "slot_leverage_max"):
            await _drop_column_idempotent(db, "webkey_slots", _dead)
        # 2026-07-28: the soft-start warm-up columns were removed. The premise
        # — that MEXC restricts accounts which open too fast in their first day
        # — did not survive measurement across 11 key installations: at first
        # restriction, trade counts ranged 0 to 10,735, PnL $0 to ~$900, peak
        # request rate 2 to 1,425/h and age 0.0h to 131.9h; one key arrived
        # already limited (0 trades, 2 req/h). Existing databases keep the two
        # columns orphaned — SQLite has no cheap DROP COLUMN and webkey_slots
        # holds the encrypted keys. Do NOT add them back.
        await _add_columns_idempotent(db, "webkey_slots", [
            # Epoch until which MEXC has this account under a 10014 open-rate
            # limit. Persisted because the in-memory latch died on every
            # restart, and a restart is routine (deploy, WS re-warm) — the
            # bot then hammered a limited account until the next rejection.
            ("open_throttle_until",     "INTEGER"),
        ])
        # 2026-07-19: per-(slot, pair) margin/leverage OVERRIDE. A row here means
        # "when slot S trades pair `symbol`, use this margin/leverage instead of
        # the pair YAML". Absent row / NULL fields = inherit the pair YAML (exact
        # prior behaviour). Keyed by (slot_id, symbol).
        await db.execute(
            """CREATE TABLE IF NOT EXISTS slot_pair_sizing (
                slot_id         INTEGER NOT NULL,
                symbol          TEXT    NOT NULL,
                margin_min_usdt REAL,
                margin_max_usdt REAL,
                leverage_min    INTEGER,
                leverage_max    INTEGER,
                updated_at      INTEGER,
                PRIMARY KEY (slot_id, symbol)
            )"""
        )

        # 2026-06: per-pair SHADOW IOC-expiry log (mirror of live_open_misses in
        # the live DB) — shadow_trades only stores FILLS, so we need this to show
        # the shadow IOC-expired % per pair in the Trades Today view.
        await db.execute(
            """CREATE TABLE IF NOT EXISTS shadow_open_misses (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                reason TEXT NOT NULL
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shadow_misses_sym_ts ON shadow_open_misses(symbol, ts)"
        )

        # Run v5 → v6 migration: drop legacy sizing columns from 3 tables.
        # Idempotent — no-op if already migrated.
        await _migrate_v5_to_v6(db)

        # v7: per-slot soft-start switch, driven by the panel button.
        # Deliberately a separate flag from live_enabled: soft-start is account
        # WARMING (tiny, slow, 0%-fee pairs only), not the arb strategy, and the
        # operator must be able to run one without the other.
        await _add_columns_idempotent(db, "webkey_slots", [
            ("soft_start_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ])

        # Whitelist of pairs available for live trading.
        # In v6, this is ADMISSION CONTROL ONLY — sizing comes from pair_configs.
        # "Pair X is allowed for live" / "user should have ≥ N balance" — that's it.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS live_pair_whitelist (
                symbol                       TEXT PRIMARY KEY,
                description                  TEXT NOT NULL,
                recommended_min_balance_usdt REAL NOT NULL DEFAULT 50.0,
                added_at                     INTEGER NOT NULL
            )
        """)

        # Seed initial whitelist with curated pairs (admission only, no sizing).
        whitelist_seed = [
            # symbol, description, min_balance
            ("ZECUSDT",   "King — best 21:00-04:00 UTC, 60%+ winrate", 50.0),
            ("TAOUSDT",   "Stable overnight 21-08 UTC, 70% winrate",   50.0),
            ("HYPEUSDT",  "Morning 04-09 UTC, tight SL recommended",   50.0),
            ("BCHUSDT",   "Solid 05-10 UTC, slow but reliable",        50.0),
            ("LINKUSDT",  "Conservative 07-10 UTC, low volatility",    50.0),
        ]
        for sym, desc, min_bal in whitelist_seed:
            await db.execute(
                """INSERT OR IGNORE INTO live_pair_whitelist
                   (symbol, description, recommended_min_balance_usdt, added_at)
                   VALUES (?, ?, ?, strftime('%s','now'))""",
                (sym, desc, min_bal),
            )

        await db.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, strftime('%s','now'))",
            (CURRENT_SCHEMA_VERSION,),
        )
        await db.commit()


    logger.info("Database initialized at %s (schema v%d)", db_path, CURRENT_SCHEMA_VERSION)


async def _add_columns_idempotent(
    db: aiosqlite.Connection,
    table: str,
    columns: list[tuple[str, str]],
) -> None:
    """Add columns to table if they don't already exist."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cursor.fetchall()}

    for col_name, col_type in columns:
        if col_name in existing:
            continue
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
            logger.info("Added column %s.%s (%s)", table, col_name, col_type)
        except Exception as e:
            logger.warning("Failed to add %s.%s: %s", table, col_name, e)


async def _drop_column_idempotent(
    db: aiosqlite.Connection,
    table: str,
    col: str,
) -> None:
    """Drop a column if it exists (SQLite ≥3.35). No-op if already gone."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cursor.fetchall()}
    if col not in existing:
        return
    try:
        await db.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        logger.info("Dropped column %s.%s", table, col)
    except Exception as e:
        logger.warning("Failed to drop %s.%s: %s", table, col, e)


class Database:
    """Thin wrapper around aiosqlite for connection reuse."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # Wait (up to 5s) for the write lock instead of failing immediately —
        # concurrent writers (prune thread, signal writer) otherwise lost rows
        # ('database is locked' -> 'signal lost'). (2026-08-13.)
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = aiosqlite.Row

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def execute_write(self, sql: str, params: tuple[Any, ...] = (),
                            *, retries: int = 5, base_delay: float = 0.2) -> None:
        """execute() for rare CONTROL writes that must survive a busy DB: retries
        on 'database is locked' with exponential backoff. A concurrent process
        (prune thread / host panel / WAL checkpoint) can hold the single writer
        lock longer than busy_timeout, which otherwise makes a per-slot leverage
        change or a live_enabled toggle fail with an ugly error exactly when the
        operator needs it. NOT for the hot signal path (that stays on execute()/
        conn to avoid queue back-up). Statements must be idempotent (a retry
        re-runs them). (2026-08-13.)"""
        import asyncio
        import sqlite3
        for attempt in range(retries):
            try:
                await self.conn.execute(sql, params)
                await self.conn.commit()
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < retries - 1:
                    await asyncio.sleep(base_delay * (2 ** attempt))
                    continue
                raise

    async def executemany(self, sql: str, params_list: list[tuple[Any, ...]]) -> None:
        await self.conn.executemany(sql, params_list)
        await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        # Flush any orphaned write txn so this read gets a FRESH WAL snapshot.
        # Else an uncommitted txn from a prior write freezes this connection's
        # snapshot and reads go stale until restart (the /webkey ghost bug).
        if self.conn.in_transaction:
            await self.conn.commit()
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        if self.conn.in_transaction:
            await self.conn.commit()
        async with self.conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())
