"""
Sizing UI — change margin/leverage ranges for whitelisted pairs.

Flow:
  Inline Menu → 💰 Sizing
    → picker: list of whitelisted pairs
      → pair view: shows current margin/leverage ranges + Edit buttons
        → wizard: prompts for "min-max" input, validates, updates pair_configs

Callback data scheme:
  m:siz:pick                  — show pair picker
  m:siz:p:<SYMBOL>            — show pair detail
  m:siz:e:<SYMBOL>:margin     — start margin edit wizard
  m:siz:e:<SYMBOL>:leverage   — start leverage edit wizard
  m:siz:back                  — back to picker from pair view

Wizard state lives in context.user_data["sizing_wizard"]:
  {"symbol": "BCHUSDT", "param": "margin"}  ← awaiting user text reply
The text handler in bot.py checks for this state and routes to
handle_wizard_text().
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.config_writer import read_pair_sizing, set_pair_execution

logger = logging.getLogger(__name__)

# Safety bounds — must match LiveSafetyController's max_margin_per_trade
# and MEXC platform limits. Wizards reject input outside these ranges.
MARGIN_MIN_FLOOR = 1.0
MARGIN_MAX_CEILING = 500.0  # raised â safety is enforced by LIVE_MAX_MARGIN env
LEVERAGE_MIN_FLOOR = 1
LEVERAGE_MAX_CEILING = 125  # MEXC platform max


# ─────────────────────── DB helpers ────────────────────────

async def _fetch_whitelist_with_sizing(db) -> list[dict]:
    """Whitelist pairs (symbol/description from DB) enriched with margin/leverage
    from the pair YAML (read_pair_sizing — the single source of truth, fresh read).

    Pairs without a YAML file get global-default sizing from read_pair_sizing.
    """
    # Symbol/description from the whitelist (DB); margin/leverage from YAML
    # (single source of truth — fresh read so it reflects the latest edit).
    rows = await db.fetchall(
        "SELECT symbol, description FROM live_pair_whitelist ORDER BY symbol"
    )
    out = []
    for r in rows:
        d = dict(r)
        d.update(read_pair_sizing(d["symbol"]))
        out.append(d)
    return out


async def _fetch_pair_sizing(db, symbol: str) -> dict | None:
    """Single pair sizing. Returns None if pair not in whitelist.
    margin/leverage come from YAML (single source of truth)."""
    row = await db.fetchone(
        "SELECT symbol, description FROM live_pair_whitelist WHERE symbol = ?",
        (symbol,),
    )
    if row is None:
        return None
    d = dict(row)
    d.update(read_pair_sizing(symbol))
    return d


async def _update_margin(db, symbol: str, mn: float, mx: float) -> bool:
    """Update margin range in the pair YAML (single source of truth; runtime
    reads it from there, hot-reload ≤30s). Returns False if the pair has no
    YAML file. `db` is unused (kept for signature compatibility)."""
    return set_pair_execution(symbol, margin_min_usdt=float(mn), margin_max_usdt=float(mx))


async def _update_leverage(db, symbol: str, mn: int, mx: int) -> bool:
    return set_pair_execution(symbol, leverage_min=int(mn), leverage_max=int(mx))


# ── per-(slot, pair) sizing OVERRIDE (slot_pair_sizing; absent = inherit YAML) ──
# Flow: pick a pair -> pick slot 1/2 -> set margin/leverage FOR THAT PAIR on that
# slot. A row in slot_pair_sizing means "when this slot trades this pair, use this
# sizing instead of the pair YAML". Every account slot is shown for every pair, so
# the slots are always selectable (not only for the pair a slot is assigned to).

async def _account_slots(db) -> list[dict]:
    """All account slots (both accounts) + the pair each currently trades."""
    rows = await db.fetchall(
        "SELECT slot_id, assigned_pair FROM webkey_slots ORDER BY slot_id")
    return [dict(r) for r in rows]


def _merge_slot_override(slot_id: int, assigned_pair, ov) -> dict:
    """Shape a slot + its (slot,pair) override row into the dict the keyboards/
    formatters expect (slot_* keys; None = inherit the pair YAML)."""
    return {
        "slot_id": slot_id,
        "assigned_pair": assigned_pair,
        "slot_margin_min_usdt": ov["margin_min_usdt"] if ov else None,
        "slot_margin_max_usdt": ov["margin_max_usdt"] if ov else None,
        "slot_leverage_min": ov["leverage_min"] if ov else None,
        "slot_leverage_max": ov["leverage_max"] if ov else None,
    }


async def _slots_for_pair(db, symbol: str) -> list[dict]:
    """EVERY account slot, each with its per-(slot, pair) override for `symbol`
    (None fields = inherit the pair YAML). Slots show for every pair so they are
    always selectable."""
    out = []
    for sr in await _account_slots(db):
        ov = await db.fetchone(
            "SELECT margin_min_usdt, margin_max_usdt, leverage_min, leverage_max "
            "FROM slot_pair_sizing WHERE slot_id=? AND symbol=?", (sr["slot_id"], symbol))
        out.append(_merge_slot_override(sr["slot_id"], sr["assigned_pair"], ov))
    return out


async def _slot_row(db, slot_id: int, symbol: str) -> dict | None:
    """One slot's per-(slot, pair) override for `symbol`; None if the slot does
    not exist. A missing override row = all-None fields (inherits the pair)."""
    sr = await db.fetchone(
        "SELECT slot_id, assigned_pair FROM webkey_slots WHERE slot_id=?", (slot_id,))
    if sr is None:
        return None
    ov = await db.fetchone(
        "SELECT margin_min_usdt, margin_max_usdt, leverage_min, leverage_max "
        "FROM slot_pair_sizing WHERE slot_id=? AND symbol=?", (slot_id, symbol))
    return _merge_slot_override(slot_id, sr["assigned_pair"], ov)


async def _upsert_slot_pair(db, slot_id: int, symbol: str, *,
                            margin: tuple | None = None,
                            leverage: tuple | None = None) -> None:
    """UPSERT one param (margin OR leverage) of the (slot, pair) override, leaving
    the other param untouched (partial overrides are allowed)."""
    await db.execute(
        "INSERT OR IGNORE INTO slot_pair_sizing (slot_id, symbol) VALUES (?, ?)",
        (slot_id, symbol))
    if margin is not None:
        await db.execute(
            "UPDATE slot_pair_sizing SET margin_min_usdt=?, margin_max_usdt=?, "
            "updated_at=? WHERE slot_id=? AND symbol=?",
            (float(margin[0]), float(margin[1]), int(time.time()), slot_id, symbol))
    if leverage is not None:
        await db.execute(
            "UPDATE slot_pair_sizing SET leverage_min=?, leverage_max=?, "
            "updated_at=? WHERE slot_id=? AND symbol=?",
            (int(leverage[0]), int(leverage[1]), int(time.time()), slot_id, symbol))


async def _update_slot_margin(db, slot_id: int, symbol: str, mn: float, mx: float) -> bool:
    await _upsert_slot_pair(db, slot_id, symbol, margin=(mn, mx))
    return True


async def _update_slot_leverage(db, slot_id: int, symbol: str, mn: int, mx: int) -> bool:
    await _upsert_slot_pair(db, slot_id, symbol, leverage=(mn, mx))
    return True


async def _reset_slot(db, slot_id: int, symbol: str) -> bool:
    """Drop the (slot, pair) override so the slot inherits the pair YAML again."""
    await db.execute(
        "DELETE FROM slot_pair_sizing WHERE slot_id=? AND symbol=?", (slot_id, symbol))
    return True


# ─────────────────────── Formatting ────────────────────────

def _fmt_sizing_row(row: dict) -> str:
    """One-line summary for a pair: symbol + ranges, or '(no config)'."""
    sym = row["symbol"]
    mn, mx = row.get("margin_min"), row.get("margin_max")
    ln, lx = row.get("leverage_min"), row.get("leverage_max")
    if mn is None or mx is None or ln is None or lx is None:
        return f"<b>{sym}</b> — <i>no config (tap to set)</i>"
    return (
        f"<b>{sym}</b> — margin <code>${mn:g}–${mx:g}</code>, "
        f"lev <code>{ln}–{lx}x</code>"
    )


def _fmt_pair_detail(row: dict) -> str:
    """Detail screen text for a single pair."""
    sym = row["symbol"]
    desc = row.get("description") or ""
    mn, mx = row.get("margin_min"), row.get("margin_max")
    ln, lx = row.get("leverage_min"), row.get("leverage_max")

    lines = [f"<b>💰 Sizing — {sym}</b>"]
    if desc:
        lines.append(f"<i>{desc}</i>")
    lines.append("")

    if mn is not None and mx is not None:
        lines.append(f"<b>Margin:</b> <code>${mn:g} – ${mx:g}</code> USDT")
    else:
        lines.append("<b>Margin:</b> <i>not set</i>")

    if ln is not None and lx is not None:
        lines.append(f"<b>Leverage:</b> <code>{ln}x – {lx}x</code>")
    else:
        lines.append("<b>Leverage:</b> <i>not set</i>")

    lines.append("")
    lines.append(
        f"<i>Limits: margin ${MARGIN_MIN_FLOOR:g}–${MARGIN_MAX_CEILING:g}, "
        f"leverage {LEVERAGE_MIN_FLOOR}–{LEVERAGE_MAX_CEILING}x</i>"
    )
    return "\n".join(lines)


# ─────────────────────── Keyboards ─────────────────────────

def _kb_picker(pairs: list[dict]) -> InlineKeyboardMarkup:
    """List of pairs as 2-col grid + Back button."""
    rows: list[list[InlineKeyboardButton]] = []
    cur_row: list[InlineKeyboardButton] = []
    for p in pairs:
        cur_row.append(InlineKeyboardButton(
            p["symbol"],
            callback_data=f"m:siz:p:{p['symbol']}",
        ))
        if len(cur_row) == 2:
            rows.append(cur_row)
            cur_row = []
    if cur_row:  # odd count — last single-button row
        rows.append(cur_row)
    rows.append([InlineKeyboardButton("← Back to menu", callback_data="m:status")])
    return InlineKeyboardMarkup(rows)


def _kb_pair_detail(symbol: str, slots: list[dict] | None = None) -> InlineKeyboardMarkup:
    """Slot picker only: one compact button per account slot (Slot 1 | Slot 2
    side by side), then Back. Pair-level margin/leverage editing is intentionally
    NOT here — sizing is set per slot; the pair range is the inherited default."""
    slot_btns = []
    for s in (slots or []):
        sid = s["slot_id"]
        has_ovr = s.get("slot_leverage_min") is not None or s.get("slot_margin_min_usdt") is not None
        lbl = f"⚙️ Slot {sid}" + (" ✏️" if has_ovr else "")
        slot_btns.append(InlineKeyboardButton(lbl, callback_data=f"m:siz:s:{symbol}:{sid}"))
    # Two slots per row (side by side), not stacked.
    rows = [slot_btns[i:i + 2] for i in range(0, len(slot_btns), 2)]
    rows.append([InlineKeyboardButton("← Back to picker", callback_data="m:siz:pick")])
    return InlineKeyboardMarkup(rows)


def _kb_slot_detail(symbol: str, slot_id: int) -> InlineKeyboardMarkup:
    """Per-slot: edit margin | edit leverage, then back-to-pair."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Margin", callback_data=f"m:siz:se:{symbol}:{slot_id}:margin"),
            InlineKeyboardButton("⚡ Leverage", callback_data=f"m:siz:se:{symbol}:{slot_id}:leverage"),
        ],
        [InlineKeyboardButton("← Back to pair", callback_data=f"m:siz:p:{symbol}")],
    ])


def _fmt_slot_detail(symbol: str, srow: dict, pair_row: dict) -> str:
    """Detail text for one slot's sizing — just the effective values (the slot's
    own range if set, otherwise the pair default). No annotations."""
    sid = srow["slot_id"]
    lines = [f"<b>⚙️ Slot {sid} sizing — {symbol}</b>", ""]
    smn, smx = srow.get("slot_margin_min_usdt"), srow.get("slot_margin_max_usdt")
    sln, slx = srow.get("slot_leverage_min"), srow.get("slot_leverage_max")
    pmn, pmx = pair_row.get("margin_min"), pair_row.get("margin_max")
    pln, plx = pair_row.get("leverage_min"), pair_row.get("leverage_max")
    mmn = smn if smn is not None else pmn
    mmx = smx if smx is not None else pmx
    lmn = sln if sln is not None else pln
    lmx = slx if slx is not None else plx
    lines.append(f"<b>Margin:</b> <code>${mmn:g}–${mmx:g}</code> USDT"
                 if mmn is not None else "<b>Margin:</b> <i>not set</i>")
    lines.append(f"<b>Leverage:</b> <code>{lmn}x–{lmx}x</code>"
                 if lmn is not None else "<b>Leverage:</b> <i>not set</i>")
    return "\n".join(lines)


# ─────────────────────── Wizard state ──────────────────────

def _set_wizard(context: ContextTypes.DEFAULT_TYPE, symbol: str, param: str,
                slot: int | None = None) -> None:
    context.user_data["sizing_wizard"] = {"symbol": symbol, "param": param, "slot": slot}


def _get_wizard(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    return context.user_data.get("sizing_wizard")


def _clear_wizard(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("sizing_wizard", None)


def is_in_wizard(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Public helper for bot.py text-router to detect wizard mode."""
    return _get_wizard(context) is not None


# ─────────────────────── Input parsing ─────────────────────

_RANGE_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[-–—]\s*(-?\d+(?:\.\d+)?)\s*$")


def _parse_range(text: str) -> tuple[float, float] | None:
    """Parse 'min-max' or 'min–max' (en-dash) or 'min — max' (em-dash).

    Returns (min, max) as floats, or None if format invalid.
    """
    m = _RANGE_RE.match(text)
    if not m:
        return None
    try:
        a = float(m.group(1))
        b = float(m.group(2))
    except ValueError:
        return None
    return (a, b)


def _validate_margin(mn: float, mx: float) -> str | None:
    """Returns error message or None if valid."""
    if mn > mx:
        return f"min ({mn:g}) must be ≤ max ({mx:g})"
    if mn < MARGIN_MIN_FLOOR:
        return f"min must be ≥ ${MARGIN_MIN_FLOOR:g}"
    if mx > MARGIN_MAX_CEILING:
        return (
            f"max must be ≤ ${MARGIN_MAX_CEILING:g} "
            f"(safety: max_margin_per_trade)"
        )
    return None


def _validate_leverage(mn: float, mx: float) -> str | None:
    if mn != int(mn) or mx != int(mx):
        return "leverage must be a whole number"
    if mn > mx:
        return f"min ({mn:g}) must be ≤ max ({mx:g})"
    if mn < LEVERAGE_MIN_FLOOR:
        return f"min must be ≥ {LEVERAGE_MIN_FLOOR}"
    if mx > LEVERAGE_MAX_CEILING:
        return f"max must be ≤ {LEVERAGE_MAX_CEILING} (MEXC limit)"
    return None


# ─────────────────────── Callback handlers ─────────────────

async def handle_sizing_callback(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> None:
    """Dispatcher for all m:siz:* callbacks. Called from bot.py."""
    db = context.bot_data.get("db")
    if db is None:
        await query.message.reply_text("⚠️ DB not available.")
        return

    parts = data.split(":")
    # parts: ["m", "siz", action, ...]
    action = parts[2] if len(parts) >= 3 else "pick"

    # m:siz:pick — show picker
    if action == "pick":
        _clear_wizard(context)  # cancel any pending input on navigation
        pairs = await _fetch_whitelist_with_sizing(db)
        if not pairs:
            await query.message.reply_text(
                "⚠️ Live pair whitelist is empty. Nothing to size.",
            )
            return
        lines = ["<b>💰 Sizing — pick a pair</b>", ""]
        for p in pairs:
            lines.append(_fmt_sizing_row(p))
        text = "\n".join(lines)
        await query.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_picker(pairs),
        )
        return

    # m:siz:p:<SYMBOL> — show pair detail
    if action == "p" and len(parts) >= 4:
        symbol = parts[3]
        _clear_wizard(context)
        row = await _fetch_pair_sizing(db, symbol)
        if row is None:
            await query.message.reply_text(
                f"⚠️ {symbol} not in whitelist.",
            )
            return
        slots = await _slots_for_pair(db, symbol)
        await query.message.reply_text(
            _fmt_pair_detail(row),
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_pair_detail(symbol, slots),
        )
        return

    # m:siz:s:<SYMBOL>:<slot> — show per-slot sizing detail
    if action == "s" and len(parts) >= 5:
        symbol = parts[3]
        _clear_wizard(context)
        try:
            sid = int(parts[4])
        except ValueError:
            await query.message.reply_text("⚠️ Bad slot id.")
            return
        pair_row = await _fetch_pair_sizing(db, symbol)
        srow = await _slot_row(db, sid, symbol)
        if pair_row is None or srow is None:
            await query.message.reply_text("⚠️ Slot or pair not found.")
            return
        await query.message.reply_text(
            _fmt_slot_detail(symbol, srow, pair_row),
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_slot_detail(symbol, sid),
        )
        return

    # m:siz:sr:<SYMBOL>:<slot> — reset a slot back to inherit the pair
    if action == "sr" and len(parts) >= 5:
        symbol = parts[3]
        try:
            sid = int(parts[4])
        except ValueError:
            await query.message.reply_text("⚠️ Bad slot id.")
            return
        await _reset_slot(db, sid, symbol)
        pair_row = await _fetch_pair_sizing(db, symbol)
        srow = await _slot_row(db, sid, symbol)
        if pair_row is None or srow is None:
            # Slot deleted / pair de-whitelisted between render and tap (a webkey
            # delete auto-shadows the pair). The reset UPDATE hit 0 rows; just
            # tell the user instead of crashing _fmt_slot_detail on a None row.
            await query.message.reply_text("⚠️ Slot no longer exists.")
            return
        await query.message.reply_text(
            f"↺ Slot {sid} reset — now inherits the pair range.\n\n"
            + _fmt_slot_detail(symbol, srow, pair_row),
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_slot_detail(symbol, sid),
        )
        return

    # m:siz:se:<SYMBOL>:<slot>:<param> — start per-slot edit wizard
    if action == "se" and len(parts) >= 6:
        symbol = parts[3]
        try:
            sid = int(parts[4])
        except ValueError:
            await query.message.reply_text("⚠️ Bad slot id.")
            return
        param = parts[5]
        if param not in ("margin", "leverage"):
            await query.message.reply_text("⚠️ Unknown sizing parameter.")
            return
        _set_wizard(context, symbol, param, slot=sid)
        unit = "margin as $min-max (e.g. 13-17)" if param == "margin" else "leverage as min-max (e.g. 45-50)"
        allowed = (f"${MARGIN_MIN_FLOOR:g}–${MARGIN_MAX_CEILING:g}" if param == "margin"
                   else f"{LEVERAGE_MIN_FLOOR}–{LEVERAGE_MAX_CEILING}x")
        await query.message.reply_text(
            f"<b>⚙️ Slot {sid} {param} — {symbol}</b>\n\n"
            f"Reply with new {unit}.\nAllowed: <code>{allowed}</code>.\n\n"
            f"Send <code>cancel</code> to abort.",
            parse_mode=ParseMode.HTML,
        )
        return

    # m:siz:e:<SYMBOL>:<param> — start edit wizard
    if action == "e" and len(parts) >= 5:
        symbol = parts[3]
        param = parts[4]
        if param not in ("margin", "leverage"):
            await query.message.reply_text("⚠️ Unknown sizing parameter.")
            return

        # Pair must exist before we let user edit — avoids creating
        # orphan pair_configs rows by typing into the wizard.
        row = await _fetch_pair_sizing(db, symbol)
        if row is None:
            await query.message.reply_text(f"⚠️ {symbol} not in whitelist.")
            return

        _set_wizard(context, symbol, param)
        if param == "margin":
            cur = (
                f"current: ${row.get('margin_min'):g}–${row.get('margin_max'):g}"
                if row.get('margin_min') is not None else "no current value"
            )
            prompt = (
                f"<b>💰 Edit margin — {symbol}</b>\n"
                f"<i>{cur}</i>\n\n"
                f"Reply with new range as <code>min-max</code> "
                f"(e.g. <code>13-17</code>).\n"
                f"Allowed: <code>${MARGIN_MIN_FLOOR:g}–${MARGIN_MAX_CEILING:g}</code>.\n\n"
                f"Send <code>cancel</code> to abort."
            )
        else:  # leverage
            cur = (
                f"current: {row.get('leverage_min')}x–{row.get('leverage_max')}x"
                if row.get('leverage_min') is not None else "no current value"
            )
            prompt = (
                f"<b>⚡ Edit leverage — {symbol}</b>\n"
                f"<i>{cur}</i>\n\n"
                f"Reply with new range as <code>min-max</code> "
                f"(e.g. <code>40-50</code>).\n"
                f"Allowed: <code>{LEVERAGE_MIN_FLOOR}–{LEVERAGE_MAX_CEILING}x</code>.\n\n"
                f"Send <code>cancel</code> to abort."
            )
        await query.message.reply_text(prompt, parse_mode=ParseMode.HTML)
        return

    # Fallback
    await query.message.reply_text("⚠️ Unknown sizing action.")


# ─────────────────────── Wizard text handler ───────────────

async def handle_wizard_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    """Handle user's text reply while in sizing wizard mode.

    Called from bot.py's text router when is_in_wizard() returns True.
    Validates input, applies update, clears wizard state.
    """
    wiz = _get_wizard(context)
    if wiz is None:
        return  # defensive; shouldn't happen if router checked

    symbol = wiz["symbol"]
    param = wiz["param"]

    # Allow user to bail out without leaving the wizard half-set.
    if text.strip().lower() in ("cancel", "стоп", "відмiна", "відміна"):
        _clear_wizard(context)
        await update.message.reply_text("✋ Edit cancelled.")
        return

    parsed = _parse_range(text)
    if parsed is None:
        await update.message.reply_text(
            "⚠️ Format must be <code>min-max</code> (e.g. <code>13-17</code>). "
            "Try again or send <code>cancel</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    mn, mx = parsed
    db = context.bot_data.get("db")
    if db is None:
        _clear_wizard(context)
        await update.message.reply_text("⚠️ DB not available.")
        return

    slot = wiz.get("slot")

    if param == "margin":
        err = _validate_margin(mn, mx)
        if err:
            await update.message.reply_text(
                f"⚠️ {err}. Try again or send <code>cancel</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        if slot is not None:
            await _update_slot_margin(db, slot, symbol, mn, mx)
            _clear_wizard(context)
            pair_row = await _fetch_pair_sizing(db, symbol)
            srow = await _slot_row(db, slot, symbol)
            if pair_row is None or srow is None:
                # Slot deleted / pair de-whitelisted mid-wizard — don't crash
                # _fmt_slot_detail on a None row; just confirm the write plainly.
                await update.message.reply_text(
                    f"✅ Slot {slot} margin saved, but the slot/pair is no longer active."
                )
                return
            await update.message.reply_text(
                f"✅ <b>{symbol}</b> slot {slot} margin → <code>${mn:g}–${mx:g}</code>\n\n"
                + _fmt_slot_detail(symbol, srow, pair_row),
                parse_mode=ParseMode.HTML,
                reply_markup=_kb_slot_detail(symbol, slot),
            )
            return
        ok = await _update_margin(db, symbol, mn, mx)
        if not ok:
            _clear_wizard(context)
            await update.message.reply_text(
                f"⚠️ Failed to update — no pair_configs row for {symbol}.",
            )
            return
        _clear_wizard(context)
        # Pull fresh row so the confirmation reflects what's actually
        # stored (defends against rounding or migration surprises).
        row = await _fetch_pair_sizing(db, symbol)
        await update.message.reply_text(
            f"✅ <b>{symbol}</b> margin updated\n"
            f"New: <code>${row['margin_min']:g}–${row['margin_max']:g}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_pair_detail(symbol),
        )
        return

    # leverage
    err = _validate_leverage(mn, mx)
    if err:
        await update.message.reply_text(
            f"⚠️ {err}. Try again or send <code>cancel</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    if slot is not None:
        await _update_slot_leverage(db, slot, symbol, int(mn), int(mx))
        _clear_wizard(context)
        pair_row = await _fetch_pair_sizing(db, symbol)
        srow = await _slot_row(db, slot, symbol)
        if pair_row is None or srow is None:
            # Slot deleted / pair de-whitelisted mid-wizard — don't crash
            # _fmt_slot_detail on a None row; just confirm the write plainly.
            await update.message.reply_text(
                f"✅ Slot {slot} leverage saved, but the slot/pair is no longer active."
            )
            return
        await update.message.reply_text(
            f"✅ <b>{symbol}</b> slot {slot} leverage → <code>{int(mn)}x–{int(mx)}x</code>\n\n"
            + _fmt_slot_detail(symbol, srow, pair_row),
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_slot_detail(symbol, slot),
        )
        return
    ok = await _update_leverage(db, symbol, int(mn), int(mx))
    if not ok:
        _clear_wizard(context)
        await update.message.reply_text(
            f"⚠️ Failed to update — no pair_configs row for {symbol}.",
        )
        return
    _clear_wizard(context)
    row = await _fetch_pair_sizing(db, symbol)
    await update.message.reply_text(
        f"✅ <b>{symbol}</b> leverage updated\n"
        f"New: <code>{row['leverage_min']}x–{row['leverage_max']}x</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_pair_detail(symbol),
    )
