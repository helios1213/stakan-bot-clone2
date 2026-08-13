"""
LiveExecutorPool — manages multiple LiveExecutor instances, one per webkey slot.

Architecture:
  - Each slot can be assigned ONE pair to trade live
  - One LiveExecutor per slot (stateful: stats, slot_level_error, etc.)
  - Routing: when a signal for pair P arrives, find slot S where
    `assigned_pair == P AND live_enabled`, get its executor, place order.

Thread safety: all operations are async; pool dict is mutated only on
slot config changes (rare). Executors themselves are independent.
"""
from __future__ import annotations

import asyncio

import logging

from src.execution.live_executor import LiveExecutor
from src.execution.live_safety import LiveSafetyController

logger = logging.getLogger(__name__)


class LiveExecutorPool:
    """
    Map slot_id → LiveExecutor instance.

    Each slot ALSO has its own SafetyController, so one account's drawdown
    halt never stops the other.

    Lifecycle:
      - Created at boot (empty)
      - rebuild_from_store() called periodically to sync with DB state
      - Executors are created/destroyed as slots are assigned/unassigned
    """

    def __init__(
        self,
        client_pool,        # WebkeyClientPool
        webkey_store,       # WebkeyStore
        alerts=None,        # Optional[TelegramAlerts]
        private_ws_pool=None,  # Optional[MexcPrivateWSPool] — push-based fills
        live_db=None,       # Optional[LiveDatabase] — щоб відновити сесію після рестарту
        # Просадка: межа = min(стеля, pct × нотіонал).
        # env LIVE_MAX_DRAWDOWN (main.py). Плоский стоп від піку сесії.
        default_max_drawdown_usdt: float = 25.0,
        default_max_per_symbol: int = 1,
        default_max_total: int = 1,
        default_max_margin_usdt: float = 10.0,
    ) -> None:
        self.default_max_drawdown_usdt = default_max_drawdown_usdt
        self.client_pool = client_pool
        self.webkey_store = webkey_store
        self.alerts = alerts
        self.private_ws_pool = private_ws_pool
        self.live_db = live_db
        self.default_max_per_symbol = default_max_per_symbol
        self.default_max_total = default_max_total
        self.default_max_margin_usdt = default_max_margin_usdt

        self._executors: dict[int, LiveExecutor] = {}
        self._slot_locks: dict[int, asyncio.Lock] = {}
        self._safety_controllers: dict[int, LiveSafetyController] = {}
        # Mapping: pair symbol → list of slot_ids configured for it
        # (Multiple slots can be assigned to the same pair for parallel positions)
        self._pair_to_slots: dict[str, list[int]] = {}
        # Slots that are live-active as of the last rebuild. Executors
        # for deactivated slots are deliberately KEPT in _executors so
        # their stats stay readable — this set is what says which of
        # them may still be used to touch an account.
        self._active_slot_ids: set[int] = set()

    async def _hydrate_safety(self, slot_id: int) -> None:
        """Відновити добу слота з live_trades (00:00 локальних → зараз)."""
        ctl = self._safety_controllers.get(slot_id)
        if ctl is None or self.live_db is None:
            return
        try:
            rows = await self.live_db.fetchall(
                "SELECT net_pnl_usdt, notional_usdt FROM live_trades "
                "WHERE account_label = ? AND closed_at IS NOT NULL "
                "  AND closed_at >= ? ORDER BY closed_at",
                (f"slot{slot_id}", ctl.session_start_ts()),
            )
            if rows:
                ctl.hydrate_session([(r[0], r[1]) for r in rows])
        except Exception:
            # Не даємо збою читання завалити підняття слота: гірший наслідок —
            # день починається з нуля, тобто рівно стара поведінка.
            logger.exception(
                "[SESSION] slot %d: не вдалось відновити добу", slot_id)

    async def rebuild_from_store(self) -> None:
        """
        Sync executor pool with current DB state.

        Call this:
          - At boot
          - Periodically (e.g. every 60s) to pick up config changes
          - After explicit slot config update via Telegram
        """
        slots = await self.webkey_store.list_live_active()

        # Build new pair → slots mapping
        new_pair_to_slots: dict[str, list[int]] = {}
        active_slot_ids: set[int] = set()
        for s in slots:
            sid = s.slot_id
            active_slot_ids.add(sid)
            if s.assigned_pair:
                new_pair_to_slots.setdefault(s.assigned_pair, []).append(sid)

        # Create executors for newly-active slots
        for slot in slots:
            sid = slot.slot_id
            if sid not in self._executors:
                self._executors[sid] = LiveExecutor(
                    client_pool=self.client_pool,
                    slot_id=sid,
                    webkey_store=self.webkey_store,
                    alerts=self.alerts,
                    private_ws_pool=self.private_ws_pool,
                )
                self._safety_controllers[sid] = LiveSafetyController(
                    max_drawdown_usdt=self.default_max_drawdown_usdt,
                    max_concurrent_per_symbol=self.default_max_per_symbol,
                    max_concurrent_total=self.default_max_total,
                    max_margin_per_trade_usdt=self.default_max_margin_usdt,
                )
                # Стан запобіжника живе лише в памʼяті, тож новий контролер
                # приходить із нульовим днем. Відновлюємо добу з угод — інакше
                # рестарт стирав пік сесії і знімав кіл.
                await self._hydrate_safety(sid)
                logger.info(
                    "LiveExecutorPool: spawned executor for slot %d (pair=%s)",
                    sid, slot.assigned_pair,
                )

        # Deactivate slots that are no longer live-active. The executor
        # object stays so its stats remain readable, but the slot leaves
        # _active_slot_ids and nothing may reach the exchange through it again.
        # It previously only left pair routing, which meant reconcile — reading
        # _executors directly — kept closing positions on an account whose key
        # had been deleted.
        stale = [sid for sid in self._executors if sid not in active_slot_ids]
        for sid in stale:
            if sid in self._active_slot_ids:
                logger.warning(
                    "LiveExecutorPool: slot %d deactivated — bot will no longer "
                    "touch this account", sid,
                )

        self._active_slot_ids = active_slot_ids
        self._pair_to_slots = new_pair_to_slots

    def active_executors(self) -> dict[int, "LiveExecutor"]:
        """Executors that may still act on an exchange account.

        Anything reaching out to MEXC must use this, never _executors: the
        latter deliberately retains deactivated slots for their statistics.
        """
        return {sid: ex for sid, ex in self._executors.items()
                if sid in self._active_slot_ids}

    async def slot_has_key(self, slot_id: int) -> bool:
        """Live check straight against the store.

        rebuild_from_store runs on a timer, so between a key deletion and the
        next rebuild _active_slot_ids is stale. Anything about to close a
        position asks this first.
        """
        try:
            slot = await self.webkey_store.get(slot_id)
        except Exception:
            return False
        return bool(slot and getattr(slot, "enabled", False)
                    and getattr(slot, "webkey", None) is not None)

    def get_executor(self, slot_id: int) -> LiveExecutor | None:
        return self._executors.get(slot_id)

    def reset_fee_guard(self, slot_id: int) -> bool:
        """Clear the fee-guard halt on a slot's executor (manual override).
        Returns True if it had been halted. No-op if the slot has no executor."""
        ex = self._executors.get(slot_id)
        return ex.reset_fee_guard() if ex is not None else False

    def get_safety(self, slot_id: int) -> LiveSafetyController | None:
        return self._safety_controllers.get(slot_id)

    def find_slots_for_pair(self, symbol: str) -> list[int]:
        """
        Return slot_ids whose assigned_pair matches this symbol.

        One slot = one pair. The pair→slots map is rebuilt from each slot's
        assigned_pair in rebuild_from_store(); a pair with no assigned slot
        returns [] (shadow-only, no live execution).
        """
        return self._pair_to_slots.get(symbol, [])

    async def get_slot_config(self, slot_id: int, symbol: str | None = None) -> dict | None:
        """
        Admission check for a slot + pair combination.

        Reads the pair_configs row (by symbol, or the slot's assigned_pair) and
        returns None if there is no row — the caller treats None as "pair not
        configured for live → skip the slot".

        Розмір несуть ТІЛЬКИ поля slot_* — вони приходять з таблиці
        slot_pair_sizing (slot_id, symbol). shadow_engine бере їх, а якщо поле
        None — падає на pair YAML через ConfigLoader (НЕ на pair_configs).

        Рядок pair_configs читається лише як допуск: None = пара не
        налаштована для live → пропустити слот. Його margin_*/leverage_*
        колись теж клались у цей dict і не читались ніде (grep: нуль
        звернень) — прибрані, щоб не було третьої копії тієї самої величини.
        """
        slot = await self.webkey_store.get(slot_id)
        if symbol is None:
            if slot is None or not slot.assigned_pair:
                return None
            symbol = slot.assigned_pair

        sizing = await self.webkey_store.get_pair_sizing(symbol)

        if sizing is None:
            logger.warning(
                "[SLOT CONFIG] slot=%d no pair_configs row for %s — skipping",
                slot_id, symbol,
            )
            return None

        # Per-(slot, pair) sizing OVERRIDE (None = inherit the pair YAML above).
        # A row in slot_pair_sizing means "when THIS slot trades THIS pair, use
        # this margin/leverage instead of the pair YAML" — lets two accounts on
        # the same pair size differently, set per-pair in the Telegram wizard.
        ovr = await self.webkey_store.get_slot_pair_sizing(slot_id, symbol) or {}
        return {
            "symbol": symbol,
            # ЄДИНЕ джерело розміру. None = успадкувати pair YAML у двигуні.
            "slot_margin_min_usdt": ovr.get("margin_min_usdt"),
            "slot_margin_max_usdt": ovr.get("margin_max_usdt"),
            "slot_leverage_min": ovr.get("leverage_min"),
            "slot_leverage_max": ovr.get("leverage_max"),
            "open_throttle_until": getattr(slot, "open_throttle_until", None),
            # Changes on webkey delete (-> NULL) and add (-> now); the
            # engine uses it to notice the account underneath changed.
            "webkey_refreshed_at": getattr(slot, "webkey_refreshed_at", None),
        }

    def get_slot_lock(self, slot_id: int) -> asyncio.Lock:
        if slot_id not in self._slot_locks:
            self._slot_locks[slot_id] = asyncio.Lock()
        return self._slot_locks[slot_id]

    @property
    def is_empty(self) -> bool:
        return len(self._pair_to_slots) == 0
