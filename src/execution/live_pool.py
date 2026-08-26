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
import time

from src.execution.live_executor import LiveExecutor
from src.execution.live_safety import LiveSafetyController

logger = logging.getLogger(__name__)

# Скільки живе запит на зняття кіла з вебпанелі. Цикл синку йде раз на ~30с,
# тож 10 хвилин — це з великим запасом на «бот саме перезапускався», але
# набагато менше за проміжок до НАСТУПНОГО кіла просадки. Без цієї межі
# маркер лежав у `live_state` вічно (prune його не чистить) і знімав кіл,
# якого оператор ніколи не бачив.
KILL_RELEASE_REQ_TTL_SEC = 600.0


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

    @staticmethod
    def _kill_release_key(slot_id: int) -> str:
        return f"kill_released_at:slot{slot_id}"

    async def persist_kill_release(self, slot_id: int) -> None:
        """Запамʼятати, що оператор ЗНЯВ кіл вручну.

        Контролер живе в памʼяті, а `_hydrate_safety` після рестарту переграє
        всі денні угоди наново — і разом із ними відновлює ДОРЕЛІЗНИЙ пік, після
        чого просадка знову виявляється пробитою і кіл вмикається САМ. Тобто
        кнопка «зняти» діяла рівно до наступного перезапуску. Маркер у live_state
        (таблиця саме для recovery) дає відновленню знати, де перебазувати пік.
        Best-effort: не змогли записати — гірший наслідок рівно старий.
        """
        if self.live_db is None:
            return
        try:
            await self.live_db.execute(
                "INSERT OR REPLACE INTO live_state (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (self._kill_release_key(slot_id), str(int(time.time())),
                 int(time.time())),
            )
        except Exception:
            logger.exception("[KILL RESET] slot %d: не вдалось зберегти маркер",
                             slot_id)

    async def release_kill(self, slot_id: int) -> tuple[bool, str]:
        """Зняти кіл І зафіксувати це так, щоб воно пережило рестарт.

        Єдина точка входу: раніше знімали напряму через контролер із двох різних
        місць UI, і жодне з них нічого не зберігало.
        """
        ctl = self._safety_controllers.get(slot_id)
        if ctl is None:
            return False, ""
        was, why = ctl.release_kill()
        await self.persist_kill_release(slot_id)
        return was, why

    async def _hydrate_safety(self, slot_id: int) -> None:
        """Відновити добу слота з live_trades (00:00 локальних → зараз)."""
        ctl = self._safety_controllers.get(slot_id)
        if ctl is None or self.live_db is None:
            return
        try:
            released_at = 0
            try:
                row = await self.live_db.fetchone(
                    "SELECT value FROM live_state WHERE key = ?",
                    (self._kill_release_key(slot_id),),
                )
                if row and row[0]:
                    _ts = int(row[0])
                    # Маркер діє лише в межах ТІЄЇ САМОЇ доби: вчорашнє зняття не
                    # має гасити сьогоднішній запобіжник.
                    if _ts >= ctl.session_start_ts():
                        released_at = _ts
            except Exception:
                logger.exception("[SESSION] slot %d: маркер зняття не прочитано",
                                 slot_id)
            rows = await self.live_db.fetchall(
                "SELECT net_pnl_usdt, notional_usdt, closed_at FROM live_trades "
                "WHERE account_label = ? AND closed_at IS NOT NULL "
                "  AND closed_at >= ? ORDER BY closed_at",
                (f"slot{slot_id}", ctl.session_start_ts()),
            )
            if rows:
                ctl.hydrate_session([(r[0], r[1], r[2]) for r in rows],
                                    released_at=released_at)
                if released_at:
                    logger.warning(
                        "[SESSION] slot %d: враховано ручне зняття кіла о %d — "
                        "пік перебазовано, кіл НЕ вмикається повторно",
                        slot_id, released_at)
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
        # Кіл живе В ПАМʼЯТІ контролера, тож зовні його не видно НІДЕ: вебпанель
        # окремий процес (на іншій машині для клона) і в цю памʼять не заглядає.
        # Тут стан дзеркалиться в live_state, а звідти ж забирається запит на
        # зняття. Цикл rebuild іде раз на 30с — саме така затримка і в кнопки.
        await self.sync_kill_state()

    # ---- Кіл назовні: дзеркало стану + запит на зняття з панелі ----

    @staticmethod
    def _kill_state_key(slot_id: int) -> str:
        return f"kill_state:slot{slot_id}"

    @staticmethod
    def _kill_request_key(slot_id: int) -> str:
        return f"kill_release_req:slot{slot_id}"

    @staticmethod
    def _release_req_is_stale(value, *, now: float | None = None) -> bool:
        """Чи протермінований запит на зняття кіла.

        Нечитабельне значення вважаємо ПРОТЕРМІНОВАНИМ: невідомий вік не має
        знімати запобіжник, що стереже реальні гроші.
        """
        try:
            ts = int(value)
        except (TypeError, ValueError):
            return True
        if ts <= 0:
            return True
        _now = time.time() if now is None else now
        return (_now - ts) > KILL_RELEASE_REQ_TTL_SEC

    async def sync_kill_state(self) -> None:
        """Дзеркалити кіл у `live_state` і виконувати запити на зняття з панелі.

        ЧОМУ ЧЕРЕЗ БД. Панель — окремий процес, а для клона ще й інша машина;
        дістатись до `SafetyController` у памʼяті бота вона не може в принципі.
        `live_state` уже використовується як канал для маркера ручного зняття
        (`persist_kill_release`), тож це не нова інфраструктура.

        ПОРЯДОК ВАЖЛИВИЙ: спершу виконати запит на зняття, і лише потім писати
        стан. У зворотному порядку панель показувала б «кіл активний» ще 30
        секунд після успішного зняття.

        Ніколи не кидає назовні: це діагностика і зручність, а не торгівля —
        збій тут не має валити цикл rebuild.
        """
        if self.live_db is None:
            return
        # Слоти БЕЗ контролера теж треба обійти. Контролер існує лише поки слот
        # live-активний; якщо оператор вимкнув live, у `_safety_controllers`
        # порожньо — і тоді (а) запит із панелі не споживався б НІКОЛИ, тобто
        # кнопка мовчки не працювала б, і (б) протухлий kill_state від минулої
        # сесії висів би вічно, а панель показувала б ФАНТОМНИЙ халт.
        # Немає контролера — немає й халту, тож маркер прибираємо.
        sids = set(self._safety_controllers)
        try:
            for _r in await self.live_db.fetchall(
                    "SELECT key FROM live_state WHERE key LIKE 'kill_state:slot%' "
                    "   OR key LIKE 'kill_release_req:slot%'"):
                try:
                    sids.add(int(str(_r[0]).rsplit("slot", 1)[1]))
                except (IndexError, ValueError):
                    continue
        except Exception:
            logger.exception("[KILL SYNC] не вдалось перелічити маркери")
        for sid in sorted(sids):
            ctl = self._safety_controllers.get(sid)
            try:
                # 1. Запит на зняття від панелі.
                req_key = self._kill_request_key(sid)
                row = await self.live_db.fetchone(
                    "SELECT value FROM live_state WHERE key = ?", (req_key,))
                if row and row[0] and self._release_req_is_stale(row[0]):
                    # ПРОТЕРМІНОВАНИЙ ЗАПИТ НЕ ЗНІМАЄ КІЛ. Таймстемп писався з
                    # самого початку, але не читався: маркер віком у ТИЖНІ знімав
                    # щойно ввімкнений кіл просадки — тобто слот повертався до
                    # живих грошей без жодної дії оператора. `live_state` не
                    # входить у RET_LIVE, prune його не чистить, тож самé воно
                    # не зникало. Та сама рамка вже стоїть на `kill_released_at`
                    # (див. ~:129) — сюди її просто не застосували.
                    logger.warning(
                        "[KILL RESET] slot %d: запит із панелі ПРОТЕРМІНОВАНИЙ "
                        "(старший за %dс) — ігнорую і прибираю", sid,
                        int(KILL_RELEASE_REQ_TTL_SEC))
                    await self.live_db.execute(
                        "DELETE FROM live_state WHERE key = ?", (req_key,))
                elif row and row[0]:
                    if ctl is not None and ctl.is_killed():
                        was, why = await self.release_kill(sid)
                        logger.warning(
                            "[KILL RESET] slot %d: знято з ВЕБПАНЕЛІ (%s)",
                            sid, why or "без причини")
                    elif ctl is None:
                        logger.info("[KILL RESET] slot %d: запит із панелі, але "
                                    "слот не live-активний — халту немає", sid)
                    else:
                        logger.info("[KILL RESET] slot %d: запит із панелі, але "
                                    "активного кіла немає", sid)
                    # Маркер прибираємо в БУДЬ-ЯКОМУ разі, інакше запит
                    # відпрацьовував би на кожному циклі знову.
                    await self.live_db.execute(
                        "DELETE FROM live_state WHERE key = ?", (req_key,))

                # 2. Дзеркало поточного стану.
                st_key = self._kill_state_key(sid)
                if ctl is not None and ctl.is_killed():
                    reason = getattr(ctl.state, "kill_reason", "") or "peak drawdown"
                    until = int(getattr(ctl.state, "kill_until_ts", 0) or 0)
                    await self.live_db.execute(
                        "INSERT OR REPLACE INTO live_state (key, value, updated_at) "
                        "VALUES (?, ?, ?)",
                        (st_key, f"{until}|{reason}", int(time.time())),
                    )
                else:
                    await self.live_db.execute(
                        "DELETE FROM live_state WHERE key = ?", (st_key,))
            except Exception:
                logger.exception("[KILL SYNC] slot %d: не вдалось синхронізувати", sid)

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

    def get_or_create_executor(self, slot_id: int) -> LiveExecutor:
        """Executor for a slot, creating an idle one if none exists.

        Reconcile must reach EVERY keyed account — including a slot that is not
        live-active (after a restart, or deactivated while a position was open)
        but may still hold a real MEXC position. Such a slot would otherwise be
        invisible to reconcile and bleed unmanaged (audit #7 / the -$42 class).
        The executor never trades unless the slot is in _active_slot_ids; here it
        is used only to fetch positions and market-close orphans, gated by
        slot_has_key.
        """
        ex = self._executors.get(slot_id)
        if ex is None:
            ex = LiveExecutor(
                client_pool=self.client_pool,
                slot_id=slot_id,
                webkey_store=self.webkey_store,
                alerts=self.alerts,
                private_ws_pool=self.private_ws_pool,
            )
            self._executors[slot_id] = ex
        return ex

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
