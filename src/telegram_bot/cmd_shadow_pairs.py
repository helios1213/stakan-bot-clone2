"""
Shadow-pair toggle UI — turn shadow simulation on/off per pair.

Lets the user pick which whitelisted pairs are actively shadowing the
market. Each pair is shown as a toggle button:
  ✅ <SYMBOL>   ← currently shadow → tap pauses
  ⏸ <SYMBOL>   ← currently paused → tap resumes to shadow

Live pairs are intentionally OMITTED from the picker. Live state is
managed via /slot N → assign pair (different flow). Touching live state
from here would risk silently un-routing a real position.

Callback data scheme:
  m:shp:list                — render the picker
  m:shp:t:<SYMBOL>          — toggle shadow ↔ paused for one pair

After every toggle we re-render the same screen so the user can flip
multiple pairs in a row without backing out.
"""
from __future__ import annotations

import logging
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


# ─────────────────────── State enumeration ─────────────────

# Imported lazily inside handlers to avoid circular imports at module load.
# pair_state defines SHADOW, PAUSED, LIVE constants.


async def _list_togglable(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    """Return whitelisted pairs that are shadow or paused (NOT live).

    Each dict: {symbol, state, paused_reason}. Sorted alphabetically.
    """
    from src.state.pair_state import SHADOW, PAUSED

    state_manager = context.bot_data.get("state_manager")
    db = context.bot_data.get("db")
    if state_manager is None or db is None:
        return []

    # Whitelist defines the universe of pairs we can shadow at all.
    # Filtering by whitelist keeps the picker focused on tradeable pairs;
    # the broader pair_states table also includes "discovered" candidates
    # that aren't ready for shadow yet.
    whitelist_rows = await db.fetchall(
        "SELECT symbol FROM live_pair_whitelist ORDER BY symbol"
    )
    whitelist = {r["symbol"] for r in whitelist_rows}

    result = []
    for sym in sorted(whitelist):
        ps = state_manager.get_state(sym)
        if ps is None:
            continue
        if ps.state not in (SHADOW, PAUSED):
            continue  # skip live, rejected, discovered
        result.append({
            "symbol": sym,
            "state": ps.state,
            "paused_reason": ps.pause_reason,
        })
    return result


def _fmt_row(row: dict) -> str:
    """One-line label for the picker text body."""
    from src.state.pair_state import SHADOW
    if row["state"] == SHADOW:
        return f"✅ <b>{row['symbol']}</b> — shadow active"
    reason = row.get("paused_reason") or ""
    # Trim long reasons so the message stays readable in mobile Telegram.
    if reason and len(reason) > 60:
        reason = reason[:60] + "…"
    suffix = f" <i>({reason})</i>" if reason else ""
    return f"⏸ <b>{row['symbol']}</b> — paused{suffix}"


def _btn_label(row: dict) -> str:
    """Button caption — emoji + symbol, no extra text."""
    from src.state.pair_state import SHADOW
    emoji = "✅" if row["state"] == SHADOW else "⏸"
    return f"{emoji} {row['symbol']}"


def _kb_picker(rows: list[dict]) -> InlineKeyboardMarkup:
    """2-col grid of toggle buttons + Back."""
    kb_rows: list[list[InlineKeyboardButton]] = []
    cur: list[InlineKeyboardButton] = []
    for r in rows:
        cur.append(InlineKeyboardButton(
            _btn_label(r),
            callback_data=f"m:shp:t:{r['symbol']}",
        ))
        if len(cur) == 2:
            kb_rows.append(cur)
            cur = []
    if cur:
        kb_rows.append(cur)
    kb_rows.append([InlineKeyboardButton("← Back to menu", callback_data="m:status")])
    return InlineKeyboardMarkup(kb_rows)


def _fmt_picker_body(rows: list[dict]) -> str:
    if not rows:
        return (
            "<b>🗂️ Shadow Pairs</b>\n\n"
            "<i>No whitelisted pairs in shadow/paused state.</i>"
        )
    from src.state.pair_state import SHADOW
    active = sum(1 for r in rows if r["state"] == SHADOW)
    lines = [
        f"<b>🗂️ Shadow Pairs</b>  <i>(active: {active}/{len(rows)})</i>",
        "",
        "Tap a pair to toggle:",
        "",
    ]
    for r in rows:
        lines.append(_fmt_row(r))
    return "\n".join(lines)


# ─────────────────────── Callback dispatcher ───────────────

async def handle_shadow_pairs_callback(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    """Dispatch m:shp:* callbacks. Called from bot.py main router."""
    state_manager = context.bot_data.get("state_manager")
    if state_manager is None:
        await query.message.reply_text("⚠️ State manager not available.")
        return

    parts = data.split(":")
    action = parts[2] if len(parts) >= 3 else "list"

    # m:shp:list — render picker
    if action == "list":
        rows = await _list_togglable(context)
        await query.message.reply_text(
            _fmt_picker_body(rows),
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_picker(rows),
        )
        return

    # m:shp:t:<SYMBOL> — toggle one pair
    if action == "t" and len(parts) >= 4:
        symbol = parts[3]
        from src.state.pair_state import SHADOW, PAUSED, LIVE

        ps = state_manager.get_state(symbol)
        if ps is None:
            await query.answer(f"{symbol} not found", show_alert=True)
            return

        # Safety: refuse to touch live pairs from this UI.
        if ps.state == LIVE:
            await query.answer(
                f"{symbol} is LIVE — use /slot to manage.",
                show_alert=True,
            )
            return

        if ps.state == SHADOW:
            # shadow → paused (persistent, no auto-resume)
            ok = await state_manager.manual_pause(
                symbol,
                duration_sec=None,
                reason="manual: shadow toggle off (per-pair)",
            )
            ack = f"⏸ {symbol} paused" if ok else f"❌ Failed to pause {symbol}"
        elif ps.state == PAUSED:
            # paused → shadow
            ok = await state_manager.manual_resume(
                symbol,
                reason="manual: shadow toggle on (per-pair)",
            )
            ack = f"✅ {symbol} resumed (shadow)" if ok else f"❌ Failed to resume {symbol}"
        else:
            await query.answer(
                f"{symbol} state={ps.state} — not togglable here.",
                show_alert=True,
            )
            return

        # Brief in-place ack (no new message — toast on the tapped button).
        try:
            await query.answer(ack)
        except Exception:
            # answer() can fail if the query is stale; not fatal.
            pass

        # Edit the SAME message in place so the picker doesn't pile up
        # in chat history. edit_message_text on the inline-button's
        # parent message just swaps content + keyboard, no new bubble.
        # Falls back to a fresh reply if the edit fails (rare — e.g.
        # message older than 48h, or content identical so Telegram
        # rejects the no-op edit).
        rows = await _list_togglable(context)
        body = _fmt_picker_body(rows)
        kb = _kb_picker(rows)
        try:
            await query.edit_message_text(
                body,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception as e:
            logger.debug("edit_message_text failed, falling back to reply: %s", e)
            await query.message.reply_text(
                body,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        return

    # Unknown action — render picker as a safe default.
    rows = await _list_togglable(context)
    await query.message.reply_text(
        _fmt_picker_body(rows),
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_picker(rows),
    )
