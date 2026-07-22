"""
PairStateManager — central authority for pair lifecycle.

Responsibilities:
  - Persist pair states in DB (`pair_states` table)
  - Initialize default `pair_configs` rows for new pairs
  - Run periodic transition evaluation loop
  - Apply state transitions atomically (DB + audit log)
  - Provide is_tradeable() check used by shadow_engine
  - Allow manual override (Telegram /promote, /pause, etc)

Lifecycle:
  Bot startup → seed all current universe pairs as DISCOVERED
  Every 60s   → recompute metrics, evaluate transitions
  Every 5min  → log summary of all pair states
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from src.state.metrics_calculator import MetricsCalculator, PairMetrics
from src.state.pair_state import (
    PairState,
    DISCOVERED, SHADOW, LIVE, PAUSED, REJECTED,
    TRADEABLE_STATES,
)
from src.state.transitions import TransitionCriteria, evaluate_transition
from src.storage.db import Database

logger = logging.getLogger(__name__)


class PairStateManager:
    """Owns the state machine for all pairs."""

    def __init__(
        self,
        db: Database,
        criteria: TransitionCriteria | None = None,
        evaluation_interval_sec: int = 60,
        summary_interval_sec: int = 300,
        live_db=None,
    ) -> None:
        self.db = db
        self.live_db = live_db
        self.criteria = criteria or TransitionCriteria()
        self.evaluation_interval_sec = evaluation_interval_sec
        self.summary_interval_sec = summary_interval_sec
        self.metrics_calc = MetricsCalculator(db, live_db=live_db)

        # In-memory cache of states (refreshed each loop)
        self._states: dict[str, PairState] = {}
        self._last_summary_ts = 0

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        # Optional alert callback (set by Telegram bot)
        self.on_state_change = None  # async fn(symbol, from_state, to_state, reason)

    # ============================================================
    # Initialization & seed
    # ============================================================

    async def initialize(self, universe_symbols: list[str]) -> None:
        """
        Seed pair_states and pair_configs for each universe symbol.
        Called once at bot startup AFTER universe provider builds the universe.

        New pairs are seeded directly into SHADOW state (
        There is no discovery phase;
        the whitelist IS the universe and every whitelist pair starts
        shadow-trading immediately).
        """
        await self._load_states_from_db()

        now = int(time.time())
        for symbol in universe_symbols:
            if symbol in self._states:
                # Already known — just update updated_at
                continue
            # New pair → SHADOW
            await self._insert_pair_state(symbol, SHADOW, now)
            await self._insert_default_pair_config(symbol)
            logger.info("Initialized pair state: %s → shadow", symbol)

        await self._load_states_from_db()
        logger.info(
            "PairStateManager initialized: %d pairs (%s)",
            len(self._states),
            ", ".join(f"{s}={sum(1 for ps in self._states.values() if ps.state==s)}"
                      for s in (SHADOW, LIVE, PAUSED, REJECTED, DISCOVERED)),
        )

    # ============================================================
    # Public query API (used by shadow_engine, telegram bot)
    # ============================================================

    def get_state(self, symbol: str) -> PairState | None:
        return self._states.get(symbol)

    def is_tradeable(self, symbol: str) -> bool:
        """True if pair is in shadow or live state (signals will execute)."""
        ps = self._states.get(symbol)
        return ps is not None and ps.state in TRADEABLE_STATES

    def is_in_live(self, symbol: str) -> bool:
        ps = self._states.get(symbol)
        return ps is not None and ps.state == LIVE

    def all_states(self) -> dict[str, PairState]:
        return dict(self._states)

    def states_by_status(self, status: str) -> list[PairState]:
        return [ps for ps in self._states.values() if ps.state == status]

    # ============================================================
    # Manual overrides (called from Telegram bot)
    # ============================================================

    async def manual_promote(self, symbol: str, target: str = LIVE, reason: str = "") -> bool:
        ps = self._states.get(symbol)
        if not ps:
            return False
        if target not in (SHADOW, LIVE):
            return False
        await self._apply_transition(ps, target, reason or f"manual promote → {target}", "manual_telegram")
        return True

    async def manual_pause(self, symbol: str, duration_sec: int | None = None,
                           reason: str = "manual pause") -> bool:
        """
        Pause a pair manually.

        duration_sec semantics:
          - int > 0  → pause for that many seconds, then auto-resume to shadow
          - None     → PERSISTENT pause (no auto-resume; user must resume manually)
          - 0        → use default pause duration from criteria

        The previous behavior of fallback-to-default for None was changed because
        Shadow OFF was unintentionally re-enabling pairs after 12-24h.
        """
        ps = self._states.get(symbol)
        if not ps:
            return False
        if duration_sec is None:
            # Persistent pause — no auto-resume, user must explicitly resume
            await self._apply_transition(
                ps, PAUSED, reason, "manual_telegram",
                persistent_pause=True,
            )
        elif duration_sec == 0:
            # Use default duration from criteria
            await self._apply_transition(
                ps, PAUSED, reason, "manual_telegram",
            )
        else:
            # Explicit duration in seconds
            await self._apply_transition(
                ps, PAUSED, reason, "manual_telegram",
                paused_until_override=int(time.time()) + duration_sec,
            )
        return True

    async def manual_resume(self, symbol: str, reason: str = "manual resume") -> bool:
        ps = self._states.get(symbol)
        if not ps:
            return False
        await self._apply_transition(ps, SHADOW, reason, "manual_telegram")
        return True

    # ============================================================
    # Background loop
    # ============================================================

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop(), name="pair_state_manager")
        logger.info("PairStateManager loop started (eval every %ds)", self.evaluation_interval_sec)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self._evaluate_all()
                except Exception as e:
                    logger.exception("State evaluation error: %s", e)

                # Summary log periodically
                now = int(time.time())
                if now - self._last_summary_ts >= self.summary_interval_sec:
                    self._log_summary()
                    self._last_summary_ts = now

                await asyncio.sleep(self.evaluation_interval_sec)
        except asyncio.CancelledError:
            return

    async def _evaluate_all(self) -> None:
        """Recompute metrics and evaluate transitions for every pair."""
        await self._load_states_from_db()

        symbols = list(self._states.keys())
        if not symbols:
            return

        # Build states map so live pairs read from live_db
        states_map = {sym: ps.state for sym, ps in self._states.items()}
        metrics_map = await self.metrics_calc.compute_for_all_active(
            symbols, states=states_map
        )

        # Pre-fetch per-pair auto_demotion_enabled flag in one query.
        # Default 1 (enabled) for any pair without an entry.
        # Wrapped in try/except so missing column on old schema doesn't break.
        demotion_enabled_map: dict[str, bool] = {}
        try:
            rows = await self.db.fetchall(
                "SELECT symbol, auto_demotion_enabled FROM pair_configs"
            )
            for r in rows:
                demotion_enabled_map[r["symbol"]] = bool(r["auto_demotion_enabled"])
        except Exception:
            # Column may not exist yet (pre-migration) — treat all as enabled
            pass

        for symbol, ps in self._states.items():
            metrics = metrics_map.get(symbol)
            if metrics is None:
                continue

            # Persist updated metrics regardless of transition
            await self._persist_metrics(ps, metrics)

            # Evaluate transition (per-pair auto_demotion_enabled, default True)
            auto_demote = demotion_enabled_map.get(symbol, True)
            decision = evaluate_transition(
                ps, metrics, self.criteria,
                auto_demotion_enabled=auto_demote,
            )
            if decision.should_transition and decision.target_state:
                await self._apply_transition(
                    ps, decision.target_state,
                    decision.reason, "auto",
                    metrics_snapshot=metrics,
                )

    # ============================================================
    # State change internals
    # ============================================================

    async def _apply_transition(
        self,
        ps: PairState,
        target_state: str,
        reason: str,
        triggered_by: str,
        metrics_snapshot: PairMetrics | None = None,
        paused_until_override: int | None = None,
        persistent_pause: bool = False,
    ) -> None:
        """Atomically: update pair_states, log to state_transitions, fire alert.

        For target_state=PAUSED:
          - persistent_pause=True              → paused_until=NULL (no auto-resume)
          - paused_until_override given (int)  → use that exact unix-ts
          - neither                            → use criteria.pause_duration_sec default
        """
        if ps.state == target_state:
            return

        from_state = ps.state
        now = int(time.time())

        # Compute paused_until if entering PAUSED
        paused_until: int | None = None
        if target_state == PAUSED:
            if persistent_pause:
                paused_until = None  # explicit: no auto-resume
            elif paused_until_override is not None:
                paused_until = paused_until_override
            else:
                paused_until = now + self.criteria.pause_duration_sec

        # Build update fields based on target state
        timestamp_col = {
            DISCOVERED: "discovered_at",
            SHADOW: "shadow_started_at",
            LIVE: "live_started_at",
            PAUSED: "paused_at",
            REJECTED: "rejected_at",
        }.get(target_state)

        updates_sql = "UPDATE pair_states SET state=?, state_since=?, updated_at=?, last_state_change_reason=?"
        params: list = [target_state, now, now, reason]

        if timestamp_col:
            updates_sql += f", {timestamp_col}=?"
            params.append(now)

        if target_state == PAUSED:
            updates_sql += ", paused_until=?, pause_reason=?"
            params.extend([paused_until, reason])
        elif from_state == PAUSED:
            # Clear pause_until when leaving paused
            updates_sql += ", paused_until=NULL, pause_reason=NULL"

        updates_sql += " WHERE symbol=?"
        params.append(ps.symbol)

        await self.db.execute(updates_sql, tuple(params))

        # Audit log
        snap_json = None
        if metrics_snapshot:
            snap_json = json.dumps({
                "trades_24h": metrics_snapshot.trades_24h,
                "winrate_24h": metrics_snapshot.winrate_24h,
                "pnl_24h": metrics_snapshot.pnl_24h,
                "profit_factor_24h": metrics_snapshot.profit_factor_24h,
                "avg_edge_pct_24h": metrics_snapshot.avg_edge_pct_24h,
                "drawdown_6h_pct": metrics_snapshot.drawdown_6h_pct,
                "signals_24h": metrics_snapshot.signals_24h,
            })

        await self.db.execute(
            """INSERT INTO state_transitions
               (symbol, from_state, to_state, reason, metrics_snapshot, triggered_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ps.symbol, from_state, target_state, reason, snap_json, triggered_by, now),
        )

        logger.warning(
            "[STATE] %s: %s → %s (%s) [%s]",
            ps.symbol, from_state, target_state, reason, triggered_by,
        )

        # Refresh in-memory cache so subsequent reads see new state
        await self._load_states_from_db()

        # Fire alert callback (Telegram notification etc)
        if self.on_state_change:
            try:
                await self.on_state_change(ps.symbol, from_state, target_state, reason)
            except Exception as e:
                logger.exception("on_state_change callback failed: %s", e)

    async def _persist_metrics(self, ps: PairState, m: PairMetrics) -> None:
        """Update rolling metric columns on pair_states."""
        await self.db.execute(
            """UPDATE pair_states SET
                last_24h_signals=?, last_24h_trades=?, last_24h_winrate=?,
                last_24h_pnl=?, last_24h_profit_factor=?, last_24h_avg_edge_pct=?,
                last_6h_drawdown_pct=?, updated_at=?
               WHERE symbol=?""",
            (
                m.signals_24h, m.trades_24h, m.winrate_24h,
                m.pnl_24h, m.profit_factor_24h, m.avg_edge_pct_24h,
                m.drawdown_6h_pct, int(time.time()),
                ps.symbol,
            ),
        )

    async def _insert_pair_state(self, symbol: str, state: str, ts: int) -> None:
        await self.db.execute(
            """INSERT OR IGNORE INTO pair_states
               (symbol, state, state_since, discovered_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (symbol, state, ts, ts, ts),
        )

    async def _insert_default_pair_config(self, symbol: str) -> None:
        """
        Insert default config row.

        All pairs get the SAME defaults — strategy is uniform across pairs.
        Per-pair analytics will tune values based on real shadow data.

        Defaults:
          margin: random $23-30 per trade
          leverage: random 50-80x per trade
          SL: -1.5% ROI (fixed)
          TP: disabled (trailing handles profit-taking)
          Quick scalp: +2.5% ROI within first 5 seconds → exit
          Trailing: activates at +1.0% ROI, distance 0.4%
          max_hold: 600s (safety net)
        """
        existing = await self.db.fetchone(
            "SELECT symbol FROM pair_configs WHERE symbol=?", (symbol,)
        )
        if existing:
            return

        # Insert with explicit values (relies on column defaults defined in schema)
        await self.db.execute(
            """INSERT INTO pair_configs (symbol, notes) VALUES (?, ?)""",
            (symbol, "auto-init: stage4 v2 defaults"),
        )
        logger.info("Pair config initialized: %s (uniform defaults)", symbol)

    async def _load_states_from_db(self) -> None:
        """Reload all states from DB into memory cache."""
        rows = await self.db.fetchall("SELECT * FROM pair_states")
        new_cache: dict[str, PairState] = {}
        for r in rows:
            new_cache[r["symbol"]] = PairState(
                symbol=r["symbol"],
                state=r["state"],
                state_since=r["state_since"],
                discovered_at=r["discovered_at"],
                shadow_started_at=r["shadow_started_at"],
                live_started_at=r["live_started_at"],
                paused_at=r["paused_at"],
                rejected_at=r["rejected_at"],
                paused_until=r["paused_until"],
                last_24h_signals=r["last_24h_signals"],
                last_24h_trades=r["last_24h_trades"],
                last_24h_winrate=r["last_24h_winrate"],
                last_24h_pnl=r["last_24h_pnl"],
                last_24h_profit_factor=r["last_24h_profit_factor"],
                last_24h_avg_edge_pct=r["last_24h_avg_edge_pct"],
                last_6h_drawdown_pct=r["last_6h_drawdown_pct"],
                total_shadow_trades=r["total_shadow_trades"],
                total_shadow_pnl=r["total_shadow_pnl"],
                total_live_trades=r["total_live_trades"],
                total_live_pnl=r["total_live_pnl"],
                last_state_change_reason=r["last_state_change_reason"],
                pause_reason=r["pause_reason"],
                updated_at=r["updated_at"],
            )
        self._states = new_cache

    def _log_summary(self) -> None:
        if not self._states:
            return
        groups: dict[str, list[str]] = {s: [] for s in (DISCOVERED, SHADOW, LIVE, PAUSED, REJECTED)}
        for ps in self._states.values():
            groups.setdefault(ps.state, []).append(ps.symbol)

        parts = []
        for state in (LIVE, SHADOW, DISCOVERED, PAUSED, REJECTED):
            syms = groups.get(state, [])
            if syms:
                parts.append(f"{state.upper()}={len(syms)} ({','.join(syms[:5])}{'...' if len(syms)>5 else ''})")
        logger.info("[STATE SUMMARY] %s", " | ".join(parts) if parts else "empty")
