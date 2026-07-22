"""
Telegram command handlers — WEBKEY (multi-slot live trading credentials).

Architecture (webkey-only, direct mode):
    - Up to MAX_SLOTS (=2) accounts, each with minimal creds (webkey + visitor)
    - 1-step wizard: webkey only (bot connects to futures.mexc.com directly)
    - All commands accept slot_id; defaults shown below

Commands:
    /webkey                    — list all slots overview
    /webkey_setup [N]          — start wizard for slot N (default: first empty)
    /webkey_test [N]           — health check (single slot or "all")
    /webkey_enable N           — enable slot N (must have webkey)
    /webkey_disable N          — disable slot N
    /webkey_remove N           — clear slot N (with confirmation)
    /webkey_label N "name"     — set friendly label for slot N
    /webkey_cancel             — abort active wizard
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.execution.webkey import (
    MAX_SLOTS,
    MexcWebClient,
    WebkeyError,
    WebkeySlot,
    WebkeyStore,
    validate_webkey,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-user FSM
# ---------------------------------------------------------------------------

# Wizard step constants — shared with bot.py for inline-button entry
STEP_WEBKEY = "webkey"
STEP_REMOVE_CONFIRM = "remove_confirm"

WIZARD_TIMEOUT_SEC = 600


@dataclass
class _WizardState:
    step: str | None = None        # None = idle
    slot_id: int | None = None
    started_at: float = 0.0
    target_slot_id: int | None = None    # for removal confirmation
    pending: dict[str, str] = field(default_factory=dict)

    def is_idle(self) -> bool:
        return self.step is None

    def is_expired(self) -> bool:
        return self.step is not None and (time.time() - self.started_at) > WIZARD_TIMEOUT_SEC


_FSM: dict[int, _WizardState] = {}


def _fsm(user_id: int) -> _WizardState:
    s = _FSM.setdefault(user_id, _WizardState())
    if s.is_expired():
        s.step = None
        s.slot_id = None
        s.target_slot_id = None
        s.pending = {}
    return s


def _enter_step(user_id: int, step: str, slot_id: int | None) -> _WizardState:
    s = _WizardState(step=step, slot_id=slot_id, started_at=time.time())
    _FSM[user_id] = s
    return s


def _reset(user_id: int) -> None:
    _FSM[user_id] = _WizardState()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(context: ContextTypes.DEFAULT_TYPE) -> WebkeyStore:
    s = context.application.bot_data.get("webkey_store")
    if s is None:
        raise RuntimeError("webkey_store not registered in bot_data — main.py wiring")
    return s


def _client_pool(context: ContextTypes.DEFAULT_TYPE):
    """
    Returns the persistent WebkeyClientPool (or None if not wired).

    When available, _run_one_health_check uses it to avoid TLS handshake
    overhead. Falls back to ad-hoc MexcWebClient if pool is missing.
    """
    return context.application.bot_data.get("webkey_client_pool")


def _md_escape(s: str) -> str:
    """Escape MarkdownV1 special chars in user-provided strings."""
    if not s:
        return ""
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s


def _slot_line(s: WebkeySlot) -> str:
    """One-line summary for the /webkey overview."""
    label = f" — _{_md_escape(s.label)}_" if s.label else ""
    head = f"*Slot {s.slot_id}*{label}"

    if s.is_empty:
        return f"{head}: ⚪ empty"

    parts = [f"`{s.masked_webkey()}`"]
    # one slot = one pair — show the pair this slot trades.
    # No enable/disable mark here: it's the slot's live toggle (set when a
    # pair is chosen), shown on the slot detail screen, not in this overview.
    parts.append(s.assigned_pair if s.assigned_pair else "no pair")

    return f"{head}: " + " | ".join(parts)


def _fmt_overview(slots: list[WebkeySlot]) -> str:
    lines = ["🔑 *Webkey slots*", ""]
    # "Active" = actually live-trading slots (live_enabled + pair + complete).
    enabled = sum(1 for s in slots if s.is_live_active)
    configured = sum(1 for s in slots if not s.is_empty)
    lines.append(f"_Configured: {configured}/{MAX_SLOTS}  •  Active: {enabled}_")
    lines.append("")
    for s in slots:
        lines.append(_slot_line(s))
    return "\n".join(lines)


def _fmt_slot_detail(s: WebkeySlot) -> str:
    if s.is_empty:
        return _fmt_empty_slot(s.slot_id)

    lines = [f"🔑 *Slot {s.slot_id}*"]
    if s.label:
        lines.append(f"_{_md_escape(s.label)}_")
    lines.append("")
    lines.append(f"webkey:    `{s.masked_webkey()}`")
    # "enabled" = the slot's LIVE toggle (live_enabled), i.e. the enable/disable
    # you set on the slot when choosing a pair — that's what actually gates live.
    lines.append(f"enabled:   {'✅ yes' if s.live_enabled else '⛔️ no'}")
    lines.append(f"pair:      {s.assigned_pair or '—'}")

    return "\n".join(lines)


def _fmt_empty_slot(slot_id: int) -> str:
    # Note: Telegram uses legacy Markdown (not V2). The * and _ chars are
    # special and must be balanced. We avoid `_xxx_` for italic if xxx
    # contains underscores or hyphens that confuse the parser.
    return (
        f"🔑 *Slot {slot_id}: empty*\n\n"
        "This slot is not configured.\n\n"
        "Setup will ask for 1 field:\n"
        "  • *webkey* — `WEB` + 64 hex chars (from cookie `u_id`)\n\n"
        "visitor id, chash, mhash auto-generate — no manual entry.\n"
        "_(direct mode — no proxy)_"
    )


def _parse_slot_arg(args: list[str], default_slot: int | None = None) -> int | None:
    """Extract slot_id from `args[0]`. Returns None if no number provided."""
    if not args:
        return default_slot
    try:
        n = int(args[0])
    except ValueError:
        return None
    if n < 1 or n > MAX_SLOTS:
        return None
    return n


async def _delete_message_safely(update: Update) -> None:
    """Try to delete the user message with sensitive content (webkey/proxy).

    Silently ignores failures (bot might lack delete permission in some chats).
    """
    try:
        await update.message.delete()
    except Exception as e:
        logger.debug("Could not delete sensitive message: %s", e)


# ---------------------------------------------------------------------------
# Commands — overview
# ---------------------------------------------------------------------------

async def cmd_webkey_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/webkey` or `/webkey N` — overview or single-slot detail."""
    args = context.args
    store = _store(context)

    if args:
        n = _parse_slot_arg(args)
        if n is None:
            await update.message.reply_text(
                f"Slot must be 1..{MAX_SLOTS}.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        slot = await store.get(n)
        await update.message.reply_text(
            _fmt_slot_detail(slot),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    slots = await store.list_all()
    await update.message.reply_text(
        _fmt_overview(slots),
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Commands — setup wizard
# ---------------------------------------------------------------------------

async def cmd_webkey_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/webkey_setup [N]` — start setup wizard (1-step: webkey)."""
    user_id = update.effective_user.id
    store = _store(context)

    n = _parse_slot_arg(context.args)
    if n is None and context.args:
        await update.message.reply_text(
            f"Usage: `/webkey_setup [1..{MAX_SLOTS}]`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if n is None:
        n = await store.first_empty_slot()
        if n is None:
            await update.message.reply_text(
                "All slots taken. Specify which to overwrite: "
                f"`/webkey_setup 1..{MAX_SLOTS}` or use /webkey\\_remove first.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    _enter_step(user_id, STEP_WEBKEY, slot_id=n)
    await update.message.reply_text(
        f"🔧 *Slot {n} setup* — *webkey*\n\n"
        "Paste your webkey as the next message.\n\n"
        "Format: `WEB` + 64 hex chars (67 chars total).\n"
        "Source: Chrome → mexc.com → DevTools → Application → "
        "Cookies → `u_id` value.\n\n"
        f"⏳ Timeout: {WIZARD_TIMEOUT_SEC // 60} min  •  /webkey\\_cancel to abort",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_webkey_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/webkey_cancel` — abort active wizard."""
    user_id = update.effective_user.id
    fsm = _fsm(user_id)
    if fsm.is_idle():
        await update.message.reply_text("No active wizard.")
        return
    _reset(user_id)
    await update.message.reply_text("✋ Wizard cancelled.")


# ---------------------------------------------------------------------------
# Commands — label
# ---------------------------------------------------------------------------

async def cmd_webkey_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/webkey_label N name…` — set friendly label for slot N."""
    args = context.args
    if not args:
        await update.message.reply_text(
            f"Usage: `/webkey_label N some name` (N=1..{MAX_SLOTS}); "
            f"empty name = clear label.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    n = _parse_slot_arg([args[0]])
    if n is None:
        await update.message.reply_text(
            f"Slot must be 1..{MAX_SLOTS}.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    label = " ".join(args[1:]).strip() or None
    await _store(context).set_label(n, label)
    if label:
        await update.message.reply_text(f"✅ Slot {n} label: _{_md_escape(label)}_",
                                         parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"✅ Slot {n}: label cleared")


# ---------------------------------------------------------------------------
# Commands — test (single + all)
# ---------------------------------------------------------------------------

async def cmd_webkey_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/webkey_test [N]` — N=1..MAX or omitted (=test all configured)."""
    store = _store(context)
    pool = _client_pool(context)
    n = _parse_slot_arg(context.args)

    if n is not None:
        slot = await store.get(n)
        if slot is None or slot.is_empty:
            await update.message.reply_text(f"Slot {n}: empty.")
            return

        msg = await update.message.reply_text(f"⏳ Testing slot {n}…")
        result = await _run_one_health_check(store, slot, pool=pool)
        await msg.edit_text(
            f"🩺 *Slot {n}*\n{result}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Test all configured slots (direct mode, no proxy)
    slots = [s for s in await store.list_all() if s.is_complete]
    if not slots:
        await update.message.reply_text(
            "No slots ready for testing (need webkey).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    msg = await update.message.reply_text(
        f"⏳ Testing {len(slots)} slot(s) in parallel…"
    )

    t0 = time.monotonic()
    results = await asyncio.gather(
        *[_run_one_health_check(store, s, pool=pool) for s in slots],
        return_exceptions=False,
    )
    elapsed = time.monotonic() - t0

    lines = ["🩺 *Health check results*", ""]
    for slot, result in zip(slots, results):
        lines.append(f"*Slot {slot.slot_id}*: {result}")
    lines.append("")
    lines.append(f"_Tested {len(slots)} slot(s) in {elapsed:.1f}s_")

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def _run_one_health_check(
    store: WebkeyStore,
    slot: WebkeySlot,
    pool: Any = None,
) -> str:
    """
    Returns a one-line markdown summary of the health check result.

    If `pool` is provided, uses the persistent client (no TLS handshake
    on each call). Otherwise falls back to creating a fresh client.
    """
    try:
        if pool is not None:
            # Persistent path — TLS reuse, cookies cached, ~2-3x faster
            client = await pool.get(slot.slot_id)
            report = await client.health_check()
        else:
            # Fallback for direct script use without a pool
            async with MexcWebClient.from_slot(slot) as client:
                report = await client.health_check()
    except Exception as e:
        logger.exception("Slot %d health check raised", slot.slot_id)
        await store.update_health(slot.slot_id, None, None, error=str(e)[:100])
        return f"❌ `{_md_escape(str(e)[:60])}`"

    balance_str: str | None = None
    if isinstance(report.get("balance"), dict):
        b = report["balance"]
        # Total account equity (wallet incl. unrealized PnL), not free-margin.
        # See main.py slot_balance_refresh_loop for why availableBalance is bad
        # for display — it drops by locked margin during an open trade.
        balance_str = str(
            b.get("equity") or b.get("cashBalance")
            or b.get("balance") or b.get("availableBalance") or ""
        )

    if report["valid"]:
        await store.update_health(slot.slot_id, report.get("latency_ms"),
                                   balance_str, error=None)
        return (
            f"✅ {report['latency_ms']}ms / balance "
            f"`{_md_escape(balance_str or '?')}` USDT / "
            f"open {report.get('open_positions') or 0}"
        )
    else:
        err = (report.get("error") or "unknown")[:60]
        await store.update_health(slot.slot_id, report.get("latency_ms"),
                                   None, error=err)
        return f"❌ `{_md_escape(err)}`"


# ---------------------------------------------------------------------------
# Commands — enable / disable / remove
# ---------------------------------------------------------------------------

async def cmd_webkey_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = _parse_slot_arg(context.args)
    if n is None:
        await update.message.reply_text(
            f"Usage: `/webkey_enable N` (N=1..{MAX_SLOTS})",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    store = _store(context)
    slot = await store.get(n)
    if slot.is_empty:
        await update.message.reply_text(f"Slot {n}: empty.")
        return
    # Enable just needs a complete slot (webkey); direct mode, no proxy.
    try:
        await store.set_enabled(n, True)
    except WebkeyError as e:
        await update.message.reply_text(f"❌ `{e}`", parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text(
        f"✅ Slot {n} *enabled*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_webkey_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = _parse_slot_arg(context.args)
    if n is None:
        await update.message.reply_text(
            "Usage: `/webkey_disable N`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await _store(context).set_enabled(n, False)
    # Drop persistent client so subsequent /webkey_test re-creates fresh
    pool = _client_pool(context)
    if pool is not None:
        await pool.invalidate(n)
    await update.message.reply_text(
        f"⛔️ Slot {n} *disabled*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_webkey_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = _parse_slot_arg(context.args)
    if n is None:
        await update.message.reply_text(
            f"Usage: `/webkey_remove N` (N=1..{MAX_SLOTS})",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    store = _store(context)
    slot = await store.get(n)
    if slot.is_empty:
        await update.message.reply_text(f"Slot {n}: already empty.")
        return

    user_id = update.effective_user.id
    s = _enter_step(user_id, STEP_REMOVE_CONFIRM, slot_id=None)
    s.target_slot_id = n
    await update.message.reply_text(
        f"⚠️ *Remove slot {n}?*\n\n"
        f"webkey: `{slot.masked_webkey()}`\n\n"
        "Reply with `yes` to confirm. Anything else cancels.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Free-text router (wizard FSM)
# ---------------------------------------------------------------------------

async def webkey_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if the message was consumed by an active wizard."""
    user_id = update.effective_user.id
    fsm = _fsm(user_id)
    if fsm.is_idle():
        return False

    text = (update.message.text or "").strip()
    if not text:
        return False

    if fsm.step == STEP_WEBKEY:
        await _step_webkey(update, context, text, fsm)
    elif fsm.step == STEP_REMOVE_CONFIRM:
        await _step_remove_confirm(update, context, text, fsm)
    else:
        return False

    return True


# ---------------------------------------------------------------------------
# Wizard step handlers
# ---------------------------------------------------------------------------

async def _step_webkey(update, context, text, fsm) -> None:
    try:
        validate_webkey(text)
    except WebkeyError as e:
        await update.message.reply_text(
            f"❌ `{e}`. Try again or /webkey\\_cancel.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        await _store(context).set_webkey(fsm.slot_id, text)
    except WebkeyError as e:
        await update.message.reply_text(
            f"❌ Could not save: `{e}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        _reset(update.effective_user.id)
        return

    # New webkey → existing persistent client is stale
    pool = _client_pool(context)
    if pool is not None:
        await pool.invalidate(fsm.slot_id)

    # Delete sensitive webkey message from chat
    await _delete_message_safely(update)

    # v6.1: wizard finishes here. Proxy step removed — bot connects to
    # futures.mexc.com directly without a SOCKS hop. Slot is ready to test
    # / enable as soon as webkey is saved.
    slot_id = fsm.slot_id
    _reset(update.effective_user.id)

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"✅ *Slot {slot_id} configured.*\n\n"
            f"Run /webkey\\_test {slot_id} to verify, "
            f"then /webkey\\_enable {slot_id} when ready."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _step_remove_confirm(update, context, text, fsm) -> None:
    user_id = update.effective_user.id
    n = fsm.target_slot_id
    _reset(user_id)
    if text.strip().lower() != "yes" or n is None:
        await update.message.reply_text("Cancelled.")
        return
    deleted = await _store(context).delete(n)
    # Drop persistent client (creds gone)
    pool = _client_pool(context)
    if pool is not None:
        await pool.invalidate(n)
    await update.message.reply_text(
        f"🗑 Slot {n}: cleared." if deleted else f"Slot {n}: nothing to delete.",
    )
