"""
Encrypted webkey credentials store — webkey-only edition (v5).

Architecture (v5, post-discovery):
    N slots (slot_id 1..MAX_SLOTS), each holds the MINIMUM creds for ONE
    MEXC account:
      - webkey                 (Fernet-encrypted)
      - visitor_id             (Fernet-encrypted, auto-generated on setup)

    Auto-generated/derived (NOT user-provided, NOT stored as user data):
      - chash       — bootstrap constant from /dolos/config, identical for all users
      - mhash       — MD5(visitor_id), computed at request time
      - member_id   — server-side identification via webkey; we send "0" as
                      a placeholder in the trochilus-uid header (MEXC doesn't
                      validate it).

    Akamai cookies are NOT stored — MexcWebClient acquires them per-slot
    via cold GET /futures/{symbol} on first request.

Empty slot = `webkey_blob IS NULL`. The MAX_SLOTS rows are always present in
DB (seeded on first init) so UI iteration is straightforward.
"""
from __future__ import annotations

import logging
import re
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from src.storage.db import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SLOTS = 2

# Bootstrap chash returned by /dolos/config for all users (
# via fp_decrypt_test.py). MEXC's order/create accepts this value with any
# valid webkey — no per-account chash is required.
BOOTSTRAP_CHASH = "973e5a66902be9ff97f3e916b71d4535c47b8a30c5f4122a7683d6ef701f30dd"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class WebkeyError(Exception):
    """Base exception for webkey-credential operations."""


class WebkeyNotFound(WebkeyError):
    pass


class WebkeyDecryptError(WebkeyError):
    pass


class InvalidSlotError(WebkeyError):
    pass


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_WEBKEY_RE = re.compile(r"^WEB[0-9a-fA-F]{64}$")


def _validate_slot_id(slot_id: int) -> None:
    if not isinstance(slot_id, int) or slot_id < 1 or slot_id > MAX_SLOTS:
        raise InvalidSlotError(f"slot_id must be 1..{MAX_SLOTS}, got {slot_id!r}")


def validate_webkey(webkey: str) -> None:
    if not isinstance(webkey, str):
        raise WebkeyError("Webkey must be a string")
    webkey = webkey.strip()
    if not webkey:
        raise WebkeyError("Webkey is empty")
    if not _WEBKEY_RE.match(webkey):
        raise WebkeyError(
            "Webkey must look like WEB followed by 64 hex chars (67 chars total)"
        )


def generate_visitor_id() -> str:
    """Generate a 20-char alphanumeric visitor_id (matches browser fingerprint format)."""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(20))


def _mask_webkey(webkey: str) -> str:
    if len(webkey) < 12:
        return webkey[:2] + "..." + webkey[-2:]
    return f"{webkey[:6]}...{webkey[-4:]}"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class WebkeySlot:
    """Decrypted slot snapshot — what `.get()` and `.list_all()` return.

    `webkey is None` means the slot is empty (never configured or deleted).
    `visitor_id` is auto-generated when webkey is first set; never None for
    a non-empty slot.
    """
    slot_id: int
    label: str | None
    enabled: bool

    # Credentials
    webkey: str | None              # None = empty slot
    visitor_id: str | None          # None iff webkey is None

    # Health metadata
    last_health_check: int | None = None
    last_latency_ms: int | None = None
    last_balance_usdt: str | None = None
    last_error: str | None = None
    webkey_refreshed_at: int | None = None
    created_at: int = 0
    updated_at: int = 0

    # Live trading config (v6).
    # Sizing (margin/leverage) is resolved from the pair YAML (ConfigLoader) at
    # trade time — not stored on the slot, not from pair_configs.
    assigned_pair: str | None = None      # which pair this slot trades
    live_enabled: bool = False             # is live ON for this slot
    # УВАГА: колонок slot_margin_*/slot_leverage_* тут БІЛЬШЕ НЕМАЄ.
    # Вони існують у таблиці webkey_slots (2026-07-19), але були замінені тим
    # самим днем на slot_pair_sizing і не читались ніде — при цьому мали ті
    # самі імена, що й ключі, які реально сайзять угоду в shadow_engine.
    # Розмір: slot_pair_sizing (slot_id, symbol) → live_pool.get_slot_config.
    # Epoch until which this account is under a MEXC 10014 open-rate limit.
    open_throttle_until: int | None = None

    @property
    def is_live_active(self) -> bool:
        """True if this slot is configured to place real orders.

        The bot connects to futures.mexc.com directly (see
        MexcWebClient._BASE_URL); proxy support was removed entirely.
        """
        return (
            self.live_enabled
            and self.assigned_pair is not None
            and self.is_complete
        )

    @property
    def is_empty(self) -> bool:
        return self.webkey is None

    @property
    def is_complete(self) -> bool:
        """True if slot has webkey (visitor_id is auto-set alongside)."""
        return self.webkey is not None

    def masked_webkey(self) -> str | None:
        return _mask_webkey(self.webkey) if self.webkey else None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class WebkeyStore:
    """Multi-slot encrypted webkey store (webkey-only edition).

    Lifecycle:
        store = WebkeyStore(db, master_key)
        await store.ensure_slots_seeded()   # once at startup
        # ... use .set_webkey, .get, etc.
    """

    MAX_SLOTS = MAX_SLOTS

    def __init__(self, db: Database, master_key: str) -> None:
        self.db = db
        try:
            self._fernet = Fernet(master_key.encode())
        except Exception as e:
            raise WebkeyError(f"Invalid master key: {e}") from e

    # ---- seed ----
    async def ensure_slots_seeded(self) -> None:
        """Create empty rows for slots 1..MAX_SLOTS if not present."""
        existing_rows = await self.db.fetchall(
            "SELECT slot_id FROM webkey_slots"
        )
        existing = {r["slot_id"] for r in existing_rows}
        now = int(time.time())
        for sid in range(1, MAX_SLOTS + 1):
            if sid in existing:
                continue
            await self.db.execute(
                """
                INSERT INTO webkey_slots (slot_id, enabled, created_at, updated_at)
                VALUES (?, 0, ?, ?)
                """,
                (sid, now, now),
            )
        if len(existing) < MAX_SLOTS:
            logger.info("Seeded %d empty webkey slots", MAX_SLOTS - len(existing))

    # ---- write: webkey (also auto-generates visitor_id if absent) ----
    async def set_webkey(self, slot_id: int, webkey: str) -> str:
        """Set the webkey for a slot. Auto-generates visitor_id on first set.

        Returns the visitor_id used for the slot (newly generated or preserved).
        """
        _validate_slot_id(slot_id)
        validate_webkey(webkey)
        await self._warn_if_key_reused(slot_id, webkey)
        webkey = webkey.strip()
        encrypted_webkey = self._fernet.encrypt(webkey.encode("utf-8"))
        now = int(time.time())

        # Preserve existing visitor_id if present (so mhash stays stable
        # across webkey rotations within the same slot).
        row = await self.db.fetchone(
            "SELECT visitor_blob FROM webkey_slots WHERE slot_id=?", (slot_id,)
        )
        if row and row["visitor_blob"] is not None:
            try:
                visitor_id = self._fernet.decrypt(row["visitor_blob"]).decode("utf-8")
            except InvalidToken:
                visitor_id = generate_visitor_id()
        else:
            visitor_id = generate_visitor_id()
        encrypted_visitor = self._fernet.encrypt(visitor_id.encode("utf-8"))

        await self.db.execute(
            """
            UPDATE webkey_slots
               SET webkey_blob = ?,
                   visitor_blob = ?,
                   webkey_refreshed_at = ?,
                   -- Засувка відкриттів належить АКАУНТУ, не слоту. Новий ключ =
                   -- інший акаунт, тож він не успадковує чужі 6 годин. delete()
                   -- це вже робив, а ЗАМІНА ключа — ні, і саме заміна є типовим
                   -- сценарієм: оператор просто вставляє новий ключ. Знята тут,
                   -- а не в гарячому шляху відкриття, бо там зняття настає лише
                   -- коли по парі слота приходить сигнал.
                   open_throttle_until = NULL,
                   last_error = NULL,
                   updated_at = ?
             WHERE slot_id = ?
            """,
            (encrypted_webkey, encrypted_visitor, now, now, slot_id),
        )
        logger.info(
            "Slot %d: webkey set (masked=%s, visitor=%s…) — open-rate latch and "
            "last_error cleared with the old account",
            slot_id, _mask_webkey(webkey), visitor_id[:8],
        )
        return visitor_id

    # ---- write: meta ----
    async def set_label(self, slot_id: int, label: str | None) -> bool:
        _validate_slot_id(slot_id)
        clean = label.strip()[:50] if label else None
        await self.db.execute(
            "UPDATE webkey_slots SET label=?, updated_at=? WHERE slot_id=?",
            (clean, int(time.time()), slot_id),
        )
        return True

    async def set_enabled(self, slot_id: int, enabled: bool) -> bool:
        _validate_slot_id(slot_id)
        if enabled and not await self._slot_has_webkey(slot_id):
            raise WebkeyError(
                f"Slot {slot_id} has no webkey — run /webkey_setup first"
            )
        await self.db.execute(
            "UPDATE webkey_slots SET enabled=?, updated_at=? WHERE slot_id=?",
            (1 if enabled else 0, int(time.time()), slot_id),
        )
        logger.info("Slot %d: enabled=%s", slot_id, enabled)
        return True

    # NOTE: per-slot margin/leverage overrides are written by the Telegram
    # wizard's validated helpers (_update_slot_margin / _update_slot_leverage in
    # cmd_sizing.py), which enforce min<=max BEFORE writing. A general
    # set_slot_sizing() writer was removed as dead code — it had zero callers and
    # no min<=max validation, an easy footgun if ever wired up. Add a validated
    # writer here only if a non-wizard caller is ever needed.

    async def update_health(
        self,
        slot_id: int,
        latency_ms: int | None,
        balance_usdt: str | None,
        error: str | None = None,
    ) -> None:
        _validate_slot_id(slot_id)
        await self.db.execute(
            """
            UPDATE webkey_slots
               SET last_health_check=?,
                   last_latency_ms=?,
                   last_balance_usdt=?,
                   last_error=?,
                   updated_at=?
             WHERE slot_id=?
            """,
            (int(time.time()), latency_ms, balance_usdt, error,
             int(time.time()), slot_id),
        )

    async def clear_slot_error(self, slot_id: int) -> None:
        """Clear only the persisted last_error (keeps latency/balance).

        Called when a slot recovers (e.g. a live IOC fills after an
        account-level block lifts), so /balance stops showing a stale error.
        """
        _validate_slot_id(slot_id)
        await self.db.execute(
            "UPDATE webkey_slots SET last_error=NULL, updated_at=? WHERE slot_id=?",
            (int(time.time()), slot_id),
        )

    # ---- delete ----
    async def delete(self, slot_id: int) -> bool:
        """Wipe webkey/visitor/proxy/health from a slot but keep the row.

        Also AUTO-TRANSITIONS the pair assigned to this slot back to SHADOW
        mode and clears live_enabled — a pair cannot trade live without a
        webkey, so flipping the account (deleting the webkey) must not leave
        the pair stranded as 'live' (which would just error/no-fill silently).
        """
        _validate_slot_id(slot_id)
        row = await self.db.fetchone(
            "SELECT webkey_blob, assigned_pair FROM webkey_slots WHERE slot_id=?",
            (slot_id,),
        )
        if not row or row["webkey_blob"] is None:
            return False
        assigned_pair = row["assigned_pair"]
        now = int(time.time())
        await self.db.execute(
            """
            UPDATE webkey_slots
               SET webkey_blob=NULL,
                   visitor_blob=NULL,
                   proxy_blob=NULL,
                   enabled=0,
                   live_enabled=0,
                   last_health_check=NULL,
                   last_latency_ms=NULL,
                   last_balance_usdt=NULL,
                   last_error=NULL,
                   webkey_refreshed_at=NULL,
                   label=NULL,
                   -- An open-rate limit belongs to the ACCOUNT, not the slot.
                   -- Leaving it behind made the next webkey inherit the
                   -- previous account's six-hour throttle.
                   open_throttle_until=NULL,
                   updated_at=?
             WHERE slot_id=?
            """,
            (now, slot_id),
        )
        # Auto-shadow the assigned pair: no webkey → can't trade live.
        if assigned_pair and await self._pair_has_another_slot(assigned_pair, slot_id):
            logger.info(
                "Slot %d deleted — pair %s stays LIVE, another slot still trades it",
                slot_id, assigned_pair,
            )
        elif assigned_pair:
            await self.demote_pair_to_shadow(assigned_pair, "webkey deleted — auto-shadow")
            logger.info(
                "Slot %d deleted — pair %s auto-transitioned to SHADOW (no webkey)",
                slot_id, assigned_pair,
            )
        else:
            logger.info("Slot %d: deleted (cleared)", slot_id)
        return True

    # ---- read ----
    async def first_empty_slot(self) -> int | None:
        rows = await self.db.fetchall(
            "SELECT slot_id, webkey_blob FROM webkey_slots ORDER BY slot_id"
        )
        for r in rows:
            if r["webkey_blob"] is None:
                return r["slot_id"]
        return None

    async def get(self, slot_id: int) -> WebkeySlot | None:
        _validate_slot_id(slot_id)
        row = await self.db.fetchone(
            """
            SELECT slot_id, label, enabled, webkey_blob, visitor_blob,
                   last_health_check, last_latency_ms, last_balance_usdt,
                   last_error, webkey_refreshed_at, created_at, updated_at,
                   assigned_pair, live_enabled,
                   open_throttle_until
              FROM webkey_slots
             WHERE slot_id=?
            """,
            (slot_id,),
        )
        if row is None:
            return None
        return self._row_to_slot(row)

    async def list_all(self) -> list[WebkeySlot]:
        rows = await self.db.fetchall(
            """
            SELECT slot_id, label, enabled, webkey_blob, visitor_blob,
                   last_health_check, last_latency_ms, last_balance_usdt,
                   last_error, webkey_refreshed_at, created_at, updated_at,
                   assigned_pair, live_enabled,
                   open_throttle_until
              FROM webkey_slots
             ORDER BY slot_id
            """,
        )
        return [self._row_to_slot(r) for r in rows]

    async def list_enabled_complete(self) -> list[WebkeySlot]:
        """Slots that are enabled AND have webkey. For trading.

        Proxy is not required (direct mode hits futures.mexc.com without
        a SOCKS hop), so the filter is just enabled + is_complete.
        """
        return [
            s for s in await self.list_all()
            if s.enabled and s.is_complete
        ]

    async def list_live_active(self) -> list[WebkeySlot]:
        """Slots that are configured to place real orders.

        Requires: enabled, complete, has assigned_pair, live_enabled=1.
        (Proxy was removed in v6.1 — direct mode hits futures.mexc.com.)
        """
        return [s for s in await self.list_all() if s.is_live_active]

    async def assign_pair(
        self,
        slot_id: int,
        pair: str | None,
    ) -> bool:
        """
        Assign a pair to a slot. pair=None unassigns.

        Sizing (margin/leverage) is NOT stored per-slot in v6 — it's
        resolved from the pair YAML (ConfigLoader) at trade time. The same
        YAML drives both shadow and live, so they trade identical sizes.

        AUTO-SHADOWS the displaced pair: unassigning (pair=None) or
        reassigning to a different pair MAY leave the OLD pair with no slot
        to execute it — but only when no other slot is assigned to it, since
        two slots can trade the same pair as independent accounts. Like delete(), drive BOTH pair_configs.mode AND
        pair_states.state to 'shadow' — the state machine is the REAL
        live/shadow determinant, so leaving the old pair in 'live' state
        with no slot strands it (silent [SKIP SHADOW] errors, no trades).
        _load_states_from_db (≤60s) reloads pair_states into memory.

        Returns True if updated.
        """
        _validate_slot_id(slot_id)
        prev = await self.db.fetchone(
            "SELECT assigned_pair FROM webkey_slots WHERE slot_id=?",
            (slot_id,),
        )
        old_pair = prev["assigned_pair"] if prev else None
        now = int(time.time())
        await self.db.execute(
            """
            UPDATE webkey_slots
               SET assigned_pair=?,
                   updated_at=?
             WHERE slot_id=?
            """,
            (pair, now, slot_id),
        )
        if old_pair and old_pair != pair:
            if await self._pair_has_another_slot(old_pair, slot_id):
                logger.info(
                    "Slot %d reassigned off %s — pair stays LIVE, another slot "
                    "still trades it",
                    slot_id, old_pair,
                )
            else:
                await self.demote_pair_to_shadow(
                    old_pair, "slot unassigned — auto-shadow")
                logger.info(
                    "Slot %d unassigned/reassigned — pair %s auto-transitioned to "
                    "SHADOW (no slot to execute it)",
                    slot_id, old_pair,
                )
        return True

    async def _warn_if_key_reused(self, slot_id: int, webkey: str) -> list[int]:
        """Log loudly if this webkey is already installed in another slot.

        Every live slot acts on every signal independently, so the same account
        in two slots opens two positions per signal — double exposure on one
        balance and double the open-rate towards MEXC's limit. Warn rather than
        refuse: the operator may be deliberately moving a key between slots,
        and a hard block here would strand them mid-swap.

        Returns the slots that already hold this key.
        """
        dupes: list[int] = []
        try:
            rows = await self.db.fetchall(
                "SELECT slot_id, webkey_blob FROM webkey_slots "
                "WHERE slot_id<>? AND webkey_blob IS NOT NULL", (slot_id,))
        except Exception:
            logger.exception("dup-key check: could not read slots")
            return dupes
        for row in rows or []:
            try:
                if self._decrypt_optional(row["webkey_blob"]) == webkey:
                    dupes.append(int(row["slot_id"]))
            except Exception:
                # An undecryptable blob is someone else's problem; skip it.
                continue
        if dupes:
            logger.warning(
                "⚠️ Slot %d gets a webkey ALREADY in slot(s) %s — both slots act "
                "on every signal, so that one account will open two positions "
                "per signal (double exposure, double open-rate).",
                slot_id, ", ".join(str(d) for d in dupes),
            )
        return dupes

    async def _pair_has_another_slot(self, pair: str, slot_id: int) -> bool:
        """Is some OTHER slot able to keep trading this pair?

        Two slots may trade the same pair as independent accounts, so losing
        one does not strand the pair. But the sibling only counts if it could
        actually execute: assignment alone would leave the pair LIVE behind a
        slot with no webkey or live trading switched off, which reproduces the
        silent no-trades outage this check exists to prevent — just inverted.
        """
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM webkey_slots "
            "WHERE assigned_pair=? AND slot_id<>? "
            "  AND live_enabled=1 "
            "  AND webkey_blob IS NOT NULL AND webkey_blob<>''",
            (pair, slot_id),
        )
        return bool(row and row["n"])

    async def set_live_enabled(self, slot_id: int, enabled: bool) -> bool:
        """Toggle live trading on/off for a slot.

        Enabling live ALSO sets the `enabled` display flag (a live-trading slot
        must be visible to /balance — they used to desync: the slot UI set
        live_enabled=1 but left enabled=0, so /balance reported "no enabled
        slots" while the slot traded). Disabling live leaves `enabled` untouched
        so a fee-guard halt (set_live_enabled False) doesn't hide the slot.
        """
        _validate_slot_id(slot_id)
        await self.db.execute(
            """
            UPDATE webkey_slots
               SET live_enabled=?,
                   enabled=CASE WHEN ?=1 THEN 1 ELSE enabled END,
                   updated_at=strftime('%s','now')
             WHERE slot_id=?
            """,
            (1 if enabled else 0, 1 if enabled else 0, slot_id),
        )
        return True

    async def demote_pair_to_shadow(self, symbol: str, reason: str) -> None:
        """Flip a pair to SHADOW durably via pair_states.state — the REAL live
        determinant (PairStateManager.is_in_live checks pair_states.state=='live';
        the old pair_configs.mode mirror is gone). The periodic
        _load_states_from_db (≤60s) reloads pair_states into memory. `symbol` must
        be Binance format (e.g. 'ZECUSDT', no underscore). Used by the
        webkey-delete / slot-unassign paths and the fee-guard auto-shadow.

        Does NOT close open positions — it blocks NEW live entries; existing
        positions exit via the normal exit rules.
        """
        now = int(time.time())
        await self.db.execute(
            "UPDATE pair_states SET state='shadow', state_since=?, updated_at=?, "
            "last_state_change_reason=? WHERE symbol=?",
            (now, now, reason, symbol),
        )
        logger.info("Pair %s auto-transitioned to SHADOW (%s)", symbol, reason)

    async def list_live_whitelist(self) -> list[dict]:
        """Curated list of pairs ALLOWED for live trading (admission control).

        Sizing (margin/leverage) is resolved from the pair YAML (ConfigLoader)
        — the single source of truth — and merged into each row here, the same
        pattern as cmd_sizing._fetch_pair_sizing. The Telegram slot picker
        (_fmt_pair_picker) reads margin_*/leverage_* off these dicts, so they
        must be present and REAL (it formats + multiplies them).

        2026-06-16: this previously LEFT JOINed the pair_configs sizing columns,
        but 2c dropped them (margin_*/leverage_*) → the JOIN raised
        "no such column: pc.margin_min_usdt" on every call (breaking all /slot
        commands). Replaced the dead JOIN with the YAML merge. Same class of
        bug as the get_pair_sizing fix (commit be80f9d).
        """
        from src.config_writer import read_pair_sizing
        rows = await self.db.fetchall(
            """
            SELECT lpw.symbol,
                   lpw.description,
                   lpw.recommended_min_balance_usdt
              FROM live_pair_whitelist lpw
             ORDER BY lpw.symbol
            """
        )
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            d.update(read_pair_sizing(d["symbol"]))  # margin_*/leverage_* from YAML
            out.append(d)
        return out

    async def get_pair_sizing(self, symbol: str) -> dict | None:
        """Admission gate: does a pair_configs row exist for this symbol?

        NOT the source of truth for sizing. Margin/leverage used at trade time
        are resolved from the pair YAML (ConfigLoader), the same for shadow and
        live. This result is consumed only by LiveExecutorPool.get_slot_config
        as an admission gate: a None return means the pair has no pair_configs
        row → the live slot is skipped. The returned margin/leverage VALUES are
        not read by any caller (shadow_engine only None-checks the slot_cfg).

        2026-06-16: the sizing columns (margin_*/leverage_*) were dropped from
        pair_configs in 2c — this now SELECTs `symbol` (pure existence check) so
        it can't raise "no such column". The dict keeps its 4 keys (get_slot_
        config copies them) with None placeholders, since no caller uses them.

        NOTE (deferred, separate step): with pair_states.state as the live
        authority and pair_configs created 1:1 with every pair (pair_state_
        manager), this row-existence gate is now vestigial — it can't skip a
        state='live' pair (the row always exists). Folding admission onto
        state+slot and dropping this gate is the "(b)" follow-up.

        Returns:
            dict (4 keys, None values) if the pair_configs row exists, else None.
        """
        row = await self.db.fetchone(
            "SELECT symbol FROM pair_configs WHERE symbol = ?",
            (symbol,),
        )
        if row is None:
            return None
        return {
            "margin_min_usdt": None,
            "margin_max_usdt": None,
            "leverage_min":    None,
            "leverage_max":    None,
        }

    async def set_open_throttle_until(self, slot_id: int, until_ts: int | None) -> None:
        """Remember that MEXC is rate-limiting opens on this slot until `until_ts`.

        Stored as an epoch so it survives a restart; the engine converts it back
        into a monotonic deadline on first use (never compare the two clocks).
        """
        _validate_slot_id(slot_id)
        await self.db.execute(
            "UPDATE webkey_slots SET open_throttle_until=?, updated_at=? WHERE slot_id=?",
            (until_ts, int(time.time()), slot_id),
        )


    async def get_slot_pair_sizing(self, slot_id: int, symbol: str) -> dict | None:
        """Per-(slot, pair) sizing OVERRIDE for `slot_id` trading `symbol`.

        Returns a dict of {margin_min_usdt, margin_max_usdt, leverage_min,
        leverage_max} (any field may be None) if an override row exists, else
        None (→ inherit the pair YAML). Consumed by
        LiveExecutorPool.get_slot_config; the Telegram wizard writes these rows.
        """
        row = await self.db.fetchone(
            "SELECT margin_min_usdt, margin_max_usdt, leverage_min, leverage_max "
            "FROM slot_pair_sizing WHERE slot_id = ? AND symbol = ?",
            (slot_id, symbol),
        )
        return dict(row) if row is not None else None

    # ---- internals ----
    async def _slot_has_webkey(self, slot_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT webkey_blob FROM webkey_slots WHERE slot_id=?", (slot_id,)
        )
        return bool(row and row["webkey_blob"] is not None)

    def _decrypt_optional(self, blob: bytes | None) -> str | None:
        if blob is None:
            return None
        try:
            return self._fernet.decrypt(blob).decode("utf-8")
        except InvalidToken as e:
            raise WebkeyDecryptError(
                "Cannot decrypt — master key changed or data corrupted"
            ) from e

    def _row_to_slot(self, row: Any) -> WebkeySlot:
        webkey = self._decrypt_optional(row["webkey_blob"])
        visitor = self._decrypt_optional(row["visitor_blob"])

        # Multi-slot fields (added v5.3) — handle gracefully on old rows
        def _safe(name: str, default=None):
            try:
                return row[name]
            except (KeyError, IndexError):
                return default

        return WebkeySlot(
            slot_id=row["slot_id"],
            label=row["label"],
            enabled=bool(row["enabled"]),
            webkey=webkey,
            visitor_id=visitor,
            last_health_check=row["last_health_check"],
            last_latency_ms=row["last_latency_ms"],
            last_balance_usdt=row["last_balance_usdt"],
            last_error=row["last_error"],
            webkey_refreshed_at=row["webkey_refreshed_at"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            assigned_pair=_safe("assigned_pair"),
            live_enabled=bool(_safe("live_enabled", 0)),
            open_throttle_until=_safe("open_throttle_until"),
        )
