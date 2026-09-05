"""
Universe provider — replaces the old PairScanner / StubPairScanner.

Stakan-bot is a whitelist-only trading bot: the set of tradeable pairs is
defined explicitly in `config.yaml` under the `universe:` section.

There is no discovery, no scoring, no HTTP fetch to Binance/MEXC for ticker
data. The previous scanner pipeline (~6s on each refresh, then thrown away
because `whitelist_strict: true` made the whole pipeline a no-op) was removed
.

This module produces a single `UniverseResult` at startup. It is NOT called
periodically — the universe is static for the bot's lifetime. To change pairs,
edit `config.yaml`, rebuild, restart.

Responsibilities:
  1. Read whitelist from cfg.universe.whitelist_priority
  2. Filter against cfg.universe.blacklist_symbols (safety net)
  3. Build PairCandidate objects with binance_scale for cross-exchange notation
     (1000PEPE is the only known case where Binance/MEXC scales differ)
  4. Persist to pairs_universe DB table (for /pair_status, audit, etc.)

"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from src.storage.db import Database

logger = logging.getLogger(__name__)


# ---------------- Data models (formerly in src/scanner/pair_scanner.py) ----------------

@dataclass
class PairCandidate:
    """One pair in the trading universe. Kept compatible with old PairCandidate
    shape so apply_universe_change() and any external consumers don't break."""
    symbol: str                            # Binance format (BTCUSDT)
    mexc_max_leverage: int = 300
    mexc_contract_size: float = 0.0
    binance_volume_24h: float = 0.0
    binance_depth_usdt: float = 0.0
    binance_spread_pct: float = 0.0
    atr_5m_pct: float = 0.0
    binance_scale: float = 1.0             # MEXC raw → Binance-equivalent multiplier
    score: float = 0.5
    rejected_reason: str | None = None


@dataclass
class UniverseResult:
    """Outcome of building the universe. Replaces the old ScanResult."""
    timestamp: int
    proposed_universe: list[str]
    candidates_full: list[PairCandidate] = field(default_factory=list)

    # Diff vs current DB state — used by apply_universe_change to subscribe/unsubscribe.
    additions: list[str] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


# Hardcoded binance_scale overrides for pairs with different notation across exchanges.
# `binance_scale` = multiplier to convert MEXC raw price → Binance-equivalent.
# For 1000PEPE: Binance trades 1000PEPEUSDT (price ×1000 of PEPE), MEXC trades PEPE_USDT.
# Other pairs default to 1.0 (same notation).
# If you add a pair to whitelist that has different scaling on the two exchanges,
# add it here.
KNOWN_BINANCE_SCALES: dict[str, float] = {
    "1000PEPEUSDT": 1000.0,
}


class UniverseProvider:
    """Builds the trading universe from config whitelist. No HTTP, no scoring."""

    def __init__(self, cfg, db: Database) -> None:
        """cfg is UniverseConf (see src/config.py)."""
        self.cfg = cfg
        self.db = db

    async def build(self) -> UniverseResult:
        """Read whitelist, persist to pairs_universe, prune stale rows, return UniverseResult."""
        start_ts = time.time()

        whitelist = list(self.cfg.whitelist_priority)
        blacklist = set(self.cfg.blacklist_symbols)

        # Apply blacklist (safety net — explicit overrides config mistakes)
        filtered = [s for s in whitelist if s not in blacklist]
        if len(filtered) < len(whitelist):
            removed = [s for s in whitelist if s in blacklist]
            logger.warning(
                "Universe: %d pairs removed by blacklist: %s",
                len(removed), removed,
            )

        # Build PairCandidate objects (needed by apply_universe_change for scales)
        candidates: list[PairCandidate] = [
            PairCandidate(
                symbol=sym,
                binance_scale=KNOWN_BINANCE_SCALES.get(sym, 1.0),
            )
            for sym in filtered
        ]

        # Diff vs DB — only used at startup; after that, universe is static.
        current = await self._load_current_universe()
        additions = [s for s in filtered if s not in current]
        removals = [s for s in current if s not in filtered]
        kept = [s for s in filtered if s in current]

        # Persist for /pair_status and audit. Marks whitelist as is_active=1.
        await self._save_universe(filtered, candidates)

        # Sync live_pair_whitelist with universe.whitelist_priority.
        # live_pair_whitelist is admission control for slot assignment (the
        # picker shown when assigning a slot to a pair). It MUST be a subset
        # of universe.whitelist_priority — assigning a slot to a non-universe
        # pair would create an unreachable slot (no WS subscription, no
        # pair_state).
        # Exclude reference_only_symbols (BTCUSDT) — these are in universe
        # for price context but never traded.
        reference_only = set(self.cfg.reference_only_symbols)
        live_whitelist_target = {s for s in filtered if s not in reference_only}
        live_sync_stats = await self._sync_live_pair_whitelist(live_whitelist_target)

        # Auto-prune: delete DB rows for any pair NOT in the current whitelist.
        # This keeps the DB in sync with config.yaml without manual SQL.
        # Safety constraint: ONLY prunes pairs whose state is shadow/paused/
        # rejected/discovered — pairs in `live` state are NEVER pruned (they
        # may have open positions on MEXC). If you removed a live pair from
        # the whitelist, the bot logs a WARNING and leaves the row alone.
        # Historical data (signals, shadow_trades, walls, state_transitions)
        # is also preserved — auto-prune only touches the lightweight
        # registry tables (pair_states, pair_configs, pairs_universe).
        # For full historical cleanup there is NO script — the file this
        # line used to point at is not in the repo (checked 2026-09-05):
        #     scripts/migrations/prune_non_whitelist_pairs.sql
        # scripts/migrations/ holds only nizar_pengu_strategy.sql,
        # scanner_removal.sql and _applied/. Clean historical tables by hand.
        prune_stats = await self._prune_non_whitelist(set(filtered))

        elapsed = time.time() - start_ts
        logger.info(
            "Universe built in %.3fs: %d pairs (+%d/-%d kept=%d); "
            "live_whitelist=%d (+%d/-%d); pruned %d stale rows%s",
            elapsed, len(filtered), len(additions), len(removals), len(kept),
            live_sync_stats["final_count"],
            live_sync_stats["added"],
            live_sync_stats["removed"],
            prune_stats["total_pruned"],
            f" (skipped {prune_stats['skipped_live']} live)" if prune_stats["skipped_live"] else "",
        )

        return UniverseResult(
            timestamp=int(start_ts),
            proposed_universe=filtered,
            candidates_full=candidates,
            additions=additions,
            removals=removals,
            kept=kept,
        )

    # ---- Auto-prune ----

    async def _prune_non_whitelist(self, whitelist_set: set[str]) -> dict[str, int]:
        """Delete DB rows for any symbol not in the whitelist.

        Tables affected:
          - pair_states     (only if state IN shadow/paused/rejected/discovered;
                             live rows are SKIPPED with a WARNING)
          - pair_configs    (deleted only if pair_states row was deleted, to keep
                             configs in sync; orphans are also cleaned)
          - pairs_universe  (always deleted for non-whitelist symbols)

        Historical tables (signals, shadow_trades, walls, state_transitions)
        are NEVER touched here — and no migration script does it either:
        prune_non_whitelist_pairs.sql is not in the repo (checked 2026-09-05).
        Clean them by hand if it is ever needed.

        Returns dict with counters for logging.
        """
        stats = {
            "pair_states_pruned": 0,
            "pair_configs_pruned": 0,
            "pairs_universe_pruned": 0,
            "skipped_live": 0,
            "total_pruned": 0,
        }

        # Empty whitelist is suspicious — refuse to prune anything to avoid
        # nuking the entire registry if config is malformed.
        if not whitelist_set:
            logger.warning(
                "Auto-prune: skipping — whitelist is empty (config error?)"
            )
            return stats

        wl_placeholders = ",".join(["?"] * len(whitelist_set))
        wl_params = tuple(whitelist_set)

        # 1. pair_states — identify victims, skip live rows for safety.
        rows = await self.db.fetchall(
            f"SELECT symbol, state FROM pair_states "
            f"WHERE symbol NOT IN ({wl_placeholders})",
            wl_params,
        )

        to_delete: list[str] = []
        for row in rows:
            sym, state = row["symbol"], row["state"]
            if state == "live":
                logger.warning(
                    "Auto-prune: SKIPPING %s (state=live) — not in whitelist but "
                    "has live state; remove from whitelist only after closing "
                    "positions and demoting to shadow",
                    sym,
                )
                stats["skipped_live"] += 1
            else:
                to_delete.append(sym)

        if to_delete:
            del_placeholders = ",".join(["?"] * len(to_delete))
            await self.db.execute(
                f"DELETE FROM pair_states WHERE symbol IN ({del_placeholders})",
                tuple(to_delete),
            )
            stats["pair_states_pruned"] = len(to_delete)
            logger.info(
                "Auto-prune: removed %d row(s) from pair_states: %s",
                len(to_delete), to_delete,
            )

        # 2. pair_configs — count first, then delete.
        #    Keeps live pairs' configs intact (via NOT IN pair_states.live join).
        count_row = await self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM pair_configs "
            f"WHERE symbol NOT IN ({wl_placeholders}) "
            f"  AND symbol NOT IN (SELECT symbol FROM pair_states WHERE state='live')",
            wl_params,
        )
        n_configs = count_row["n"] if count_row else 0
        if n_configs:
            await self.db.execute(
                f"DELETE FROM pair_configs "
                f"WHERE symbol NOT IN ({wl_placeholders}) "
                f"  AND symbol NOT IN (SELECT symbol FROM pair_states WHERE state='live')",
                wl_params,
            )
            stats["pair_configs_pruned"] = n_configs
            logger.info("Auto-prune: removed %d row(s) from pair_configs", n_configs)

        # 3. pairs_universe — registry audit table, safe to clean.
        count_row = await self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM pairs_universe WHERE symbol NOT IN ({wl_placeholders})",
            wl_params,
        )
        n_univ = count_row["n"] if count_row else 0
        if n_univ:
            await self.db.execute(
                f"DELETE FROM pairs_universe WHERE symbol NOT IN ({wl_placeholders})",
                wl_params,
            )
            stats["pairs_universe_pruned"] = n_univ
            logger.info("Auto-prune: removed %d row(s) from pairs_universe", n_univ)

        stats["total_pruned"] = (
            stats["pair_states_pruned"]
            + stats["pair_configs_pruned"]
            + stats["pairs_universe_pruned"]
        )
        return stats

    # ---- Live whitelist sync ----

    # Default description for newly-added live whitelist entries. Operator can
    # edit the description in SQL later — sync only touches missing/extra rows.
    _DEFAULT_LIVE_DESCRIPTION = "auto-added by universe sync"
    _DEFAULT_LIVE_MIN_BALANCE = 50.0

    async def _sync_live_pair_whitelist(self, target: set[str]) -> dict[str, int]:
        """Bring live_pair_whitelist in line with target set.

        Adds missing pairs (with default description/min_balance — operator
        can customize later via SQL).
        Removes pairs that are no longer in target, UNLESS the pair is
        currently assigned to a webkey_slot (safety guard: removing it
        would leave the slot pointing at a non-whitelist pair).

        Existing rows with custom description/recommended_min_balance_usdt
        are PRESERVED — sync never overwrites user-customized values.
        """
        stats = {
            "added": 0,
            "removed": 0,
            "kept_assigned": 0,
            "final_count": 0,
        }

        if not target:
            logger.warning(
                "Live whitelist sync: skipping — target is empty "
                "(check universe.whitelist_priority and reference_only_symbols)"
            )
            rows = await self.db.fetchall("SELECT COUNT(*) AS n FROM live_pair_whitelist")
            stats["final_count"] = rows[0]["n"] if rows else 0
            return stats

        # 1. Read current state.
        current_rows = await self.db.fetchall(
            "SELECT symbol FROM live_pair_whitelist"
        )
        current = {row["symbol"] for row in current_rows}

        to_add = target - current
        to_remove = current - target

        # 2. Add missing pairs with default metadata.
        for sym in sorted(to_add):
            await self.db.execute(
                """INSERT OR IGNORE INTO live_pair_whitelist
                   (symbol, description, recommended_min_balance_usdt, added_at)
                   VALUES (?, ?, ?, strftime('%s','now'))""",
                (sym, self._DEFAULT_LIVE_DESCRIPTION, self._DEFAULT_LIVE_MIN_BALANCE),
            )
        if to_add:
            stats["added"] = len(to_add)
            logger.info(
                "Live whitelist sync: ADDED %d pair(s): %s",
                len(to_add), sorted(to_add),
            )

        # 3. Identify which to_remove entries are currently slot-assigned.
        #    We refuse to remove those — operator must reassign first.
        if to_remove:
            assigned_rows = await self.db.fetchall(
                "SELECT DISTINCT assigned_pair FROM webkey_slots "
                "WHERE assigned_pair IS NOT NULL"
            )
            assigned = {row["assigned_pair"] for row in assigned_rows}

            actually_remove = []
            for sym in sorted(to_remove):
                if sym in assigned:
                    logger.warning(
                        "Live whitelist sync: REFUSING to remove %s — "
                        "currently assigned to a webkey_slot. Reassign the "
                        "slot via Telegram first, or remove %s from config.",
                        sym, sym,
                    )
                    stats["kept_assigned"] += 1
                else:
                    actually_remove.append(sym)

            if actually_remove:
                placeholders = ",".join(["?"] * len(actually_remove))
                await self.db.execute(
                    f"DELETE FROM live_pair_whitelist WHERE symbol IN ({placeholders})",
                    tuple(actually_remove),
                )
                stats["removed"] = len(actually_remove)
                logger.info(
                    "Live whitelist sync: REMOVED %d pair(s): %s",
                    len(actually_remove), actually_remove,
                )

        # 4. Final count (post-sync).
        final_rows = await self.db.fetchall("SELECT COUNT(*) AS n FROM live_pair_whitelist")
        stats["final_count"] = final_rows[0]["n"] if final_rows else 0
        return stats



    async def _load_current_universe(self) -> set[str]:
        rows = await self.db.fetchall(
            "SELECT symbol FROM pairs_universe WHERE is_active = 1"
        )
        return {row["symbol"] for row in rows}

    async def _save_universe(
        self,
        universe: list[str],
        all_candidates: list[PairCandidate],
    ) -> None:
        """Update pairs_universe table — mark active, store metadata."""
        now = int(time.time())
        active_set = set(universe)

        # Reset all to inactive, then mark new universe as active
        await self.db.execute("UPDATE pairs_universe SET is_active = 0")

        for c in all_candidates:
            await self.db.execute(
                """
                INSERT INTO pairs_universe (
                    symbol, binance_listed, mexc_listed, mexc_max_leverage,
                    score, volume_24h_usdt, spread_pct, depth_usdt,
                    atr_5m_pct, is_active, last_evaluated, notes
                ) VALUES (?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    mexc_max_leverage=excluded.mexc_max_leverage,
                    score=excluded.score,
                    volume_24h_usdt=excluded.volume_24h_usdt,
                    spread_pct=excluded.spread_pct,
                    depth_usdt=excluded.depth_usdt,
                    atr_5m_pct=excluded.atr_5m_pct,
                    is_active=excluded.is_active,
                    last_evaluated=excluded.last_evaluated,
                    notes=excluded.notes
                """,
                (
                    c.symbol,
                    c.mexc_max_leverage,
                    c.score,
                    c.binance_volume_24h,
                    c.binance_spread_pct,
                    c.binance_depth_usdt,
                    c.atr_5m_pct,
                    1 if c.symbol in active_set else 0,
                    now,
                    c.rejected_reason or "",
                ),
            )
