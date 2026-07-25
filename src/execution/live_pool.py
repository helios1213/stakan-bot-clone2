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

    Each slot ALSO has its own SafetyController (isolated kill switches,
    isolated daily loss tracking).

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
        # Default safety params (per slot, can be overridden per slot later).
        # SINGLE SOURCE OF TRUTH = env LIVE_DAILY_LOSS_KILL (read in main.py
        # and passed in here). This -10.0 is ONLY the fallback if env is unset;
        # keep it aligned with main.py's env-default so there's no divergent
        # value floating around. To change the threshold, edit the .env file.
        default_daily_loss_kill_usdt: float = -10.0,
        default_max_drawdown_usdt: float = 30.0,
        default_max_consec_losses: int = 5,
        default_max_per_symbol: int = 1,
        default_max_total: int = 1,
        default_max_margin_usdt: float = 10.0,
    ) -> None:
        self.client_pool = client_pool
        self.webkey_store = webkey_store
        self.alerts = alerts
        self.private_ws_pool = private_ws_pool
        self.default_daily_loss_kill_usdt = default_daily_loss_kill_usdt
        self.default_max_drawdown_usdt = default_max_drawdown_usdt
        self.default_max_consec_losses = default_max_consec_losses
        self.default_max_per_symbol = default_max_per_symbol
        self.default_max_total = default_max_total
        self.default_max_margin_usdt = default_max_margin_usdt

        self._executors: dict[int, LiveExecutor] = {}
        self._slot_locks: dict[int, asyncio.Lock] = {}
        self._safety_controllers: dict[int, LiveSafetyController] = {}
        # Mapping: pair symbol → list of slot_ids configured for it
        # (Multiple slots can be assigned to the same pair for parallel positions)
        self._pair_to_slots: dict[str, list[int]] = {}

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
                    daily_loss_kill_threshold_usdt=self.default_daily_loss_kill_usdt,
                    max_drawdown_usdt=self.default_max_drawdown_usdt,
                    max_consecutive_losses=self.default_max_consec_losses,
                    max_concurrent_per_symbol=self.default_max_per_symbol,
                    max_concurrent_total=self.default_max_total,
                    max_margin_per_trade_usdt=self.default_max_margin_usdt,
                )
                logger.info(
                    "LiveExecutorPool: spawned executor for slot %d (pair=%s)",
                    sid, slot.assigned_pair,
                )

        # Remove executors for slots that are no longer live-active
        # (We keep stats but mark as unused)
        stale = [sid for sid in self._executors if sid not in active_slot_ids]
        for sid in stale:
            logger.info(
                "LiveExecutorPool: deactivating slot %d (no longer live)",
                sid,
            )
            # Don't actually delete — keep stats accessible
            # Just remove from pair routing

        self._pair_to_slots = new_pair_to_slots

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

        The returned dict carries BOTH the pair YAML base (margin_*/leverage_*)
        AND this (slot, pair) override (slot_margin_*/slot_leverage_*, None =
        inherit). shadow_engine's live-open MERGES them (override wins, YAML
        fallback) and randomizes within that effective range to size the REAL
        order — so the slot_* fields DO size the trade when set. It also gates
        admission: None here = pair not configured for live → skip the slot.
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
            "margin_min_usdt": sizing["margin_min_usdt"],
            "margin_max_usdt": sizing["margin_max_usdt"],
            "leverage_min": sizing["leverage_min"],
            "leverage_max": sizing["leverage_max"],
            "slot_margin_min_usdt": ovr.get("margin_min_usdt"),
            "slot_margin_max_usdt": ovr.get("margin_max_usdt"),
            "slot_leverage_min": ovr.get("leverage_min"),
            "slot_leverage_max": ovr.get("leverage_max"),
            # Soft start, carried here so the trading path never has to hit the
            # DB (and never has to await) after a fill — a cancel between the
            # fill and the watcher start would leave the position unmanaged.
            "soft_start_until": getattr(slot, "soft_start_until", None),
            "soft_start_max_per_hour": getattr(slot, "soft_start_max_per_hour", None),
            "open_throttle_until": getattr(slot, "open_throttle_until", None),
        }

    def get_slot_lock(self, slot_id: int) -> asyncio.Lock:
        if slot_id not in self._slot_locks:
            self._slot_locks[slot_id] = asyncio.Lock()
        return self._slot_locks[slot_id]

    @property
    def is_empty(self) -> bool:
        return len(self._pair_to_slots) == 0
