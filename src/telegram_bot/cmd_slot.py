"""
/slot N — show and configure a webkey slot for live trading.

UI flow:
  /slot 2  →  shows slot info + "Assign pair" / "Enable live" buttons
  Click "Assign pair"  →  shows whitelist (5 pairs) as buttons
  Click "ZECUSDT"  →  pair assigned, returns to slot view
  Click "Enable live"  →  flips live_enabled=1
  Click "Disable live"  →  flips live_enabled=0

Callbacks:
  m:slot:N         — show slot N
  m:slot:N:assign  — show pair picker
  m:slot:N:pick:SYMBOL  — assign SYMBOL to slot N
  m:slot:N:unassign  — clear assignment
  m:slot:N:live_on   — enable live
  m:slot:N:live_off  — disable live
  m:slot:N:softstart_on / :softstart_off  — account warming (see below)

Soft-start is a SEPARATE switch from live. Live runs the arb strategy on the
slot's assigned pair; soft-start warms the ACCOUNT with tiny spot orders and
rare futures open→hold→close, only on pairs this account trades at 0%. A slot
can run either, both, or neither, and warming needs no assigned pair.
"""
from __future__ import annotations

import logging
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.config_writer import pair_config_exists, read_pair_sizing
from src.execution.webkey.credentials import MAX_SLOTS

logger = logging.getLogger(__name__)


def _soft_start_live_allowed() -> bool:
    """Whether soft-start would actually SEND orders.

    The panel switch alone is not enough: the env flag is the second,
    independent gate, so flipping the button on a running bot can never start
    placing real orders by itself. Read live rather than imported once, so the
    UI reflects the environment the bot is actually running in.
    """
    return os.environ.get("SOFT_START_LIVE", "") in ("1", "true", "yes")


async def _get_slot_pnl(live_db, slot_id: int) -> dict | None:
    """Cumulative live PnL for a slot since its last reset.

    Sums net_pnl_usdt over closed live trades tagged account_label='slot{N}'
    with closed_at >= the slot's reset marker (0 = never reset). This is a
    running total — NOT bucketed by day/time — until the user taps Reset PnL.
    """
    if live_db is None:
        return None
    label = f"slot{slot_id}"
    rrow = await live_db.fetchone(
        "SELECT reset_at FROM slot_pnl_reset WHERE slot_id = ?", (slot_id,)
    )
    reset_at = (rrow["reset_at"] if rrow else 0) or 0
    row = await live_db.fetchone(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
                  SUM(net_pnl_usdt) AS pnl
             FROM live_trades
            WHERE account_label = ?
              AND closed_at IS NOT NULL
              AND closed_at >= ?""",
        (label, reset_at),
    )
    return {
        "n": (row["n"] if row else 0) or 0,
        "wins": (row["wins"] if row else 0) or 0,
        "pnl": (row["pnl"] if row else 0.0) or 0.0,
        "reset_at": reset_at,
    }


async def _reset_slot_pnl(live_db, slot_id: int) -> None:
    """Mark 'now' as the slot's PnL reset point — the running total starts over.
    Historical trades are NOT deleted; they're just excluded from the slot view.
    """
    if live_db is None:
        return
    await live_db.execute(
        "INSERT OR REPLACE INTO slot_pnl_reset (slot_id, reset_at) VALUES (?, ?)",
        (slot_id, int(time.time())),
    )


def _fmt_slot_config(slot, whitelist_lookup: dict | None = None,
                      sizing: dict | None = None, pnl: dict | None = None,
                      override: dict | None = None) -> str:
    """Format slot config for display. Plain text, no markdown.

    `sizing` (optional): dict from config_writer.read_pair_sizing() (YAML —
    the single source of truth) for the slot's assigned pair. Caller fetches it
    before calling this sync formatter. NOTE: NOT WebkeyStore.get_pair_sizing —
    that's only an existence gate (returns None placeholders, not real sizing).
    If None and slot has a pair, we show a warning instead of sizing details.
    """
    lines = [f"⚙️ Slot {slot.slot_id}"]

    if slot.label:
        lines.append(f"Label: {slot.label}")

    if slot.is_empty:
        lines.append("")
        lines.append("⚪ Empty — no webkey configured")
        lines.append("Use /webkey_setup " + str(slot.slot_id) + " first.")
        return "\n".join(lines)

    # v6.1: proxy is no longer required (direct mode hits futures.mexc.com
    # without a SOCKS hop). If a stale proxy is still on the slot it's
    # silently ignored by MexcWebClient when BASE_URL is futures.mexc.com.

    lines.append("")
    lines.append("=== Trading Configuration ===")

    # Warming status. Shown even with no assigned pair, because soft-start does
    # not need one — and if the env gate is missing, say so here rather than let
    # the operator believe orders are going out.
    if getattr(slot, "soft_start_enabled", False):
        if _soft_start_live_allowed():
            lines.append("🌱 Soft-start: ON (LIVE — orders are being placed)")
        else:
            lines.append("🌱 Soft-start: ON (DRY-RUN — SOFT_START_LIVE is not set)")
        lines.append("   account warming, 0%-fee pairs only, 3-day campaign")

    if slot.assigned_pair:
        lines.append(f"💱 Pair: {slot.assigned_pair}")
        wl = whitelist_lookup.get(slot.assigned_pair) if whitelist_lookup else None
        if wl:
            lines.append(f"   {wl['description']}")

        # Sizing comes from the pair YAML (read_pair_sizing, the single source
        # of truth) — the caller fetches it since this is a sync formatter.
        if sizing is not None:
            # Effective sizing = this (slot, pair) override where set, else the
            # pair YAML. Shows the values the slot ACTUALLY trades with.
            ovr = override or {}
            margin_min = ovr["margin_min_usdt"] if ovr.get("margin_min_usdt") is not None else sizing["margin_min_usdt"]
            margin_max = ovr["margin_max_usdt"] if ovr.get("margin_max_usdt") is not None else sizing["margin_max_usdt"]
            leverage_min = ovr["leverage_min"] if ovr.get("leverage_min") is not None else sizing["leverage_min"]
            leverage_max = ovr["leverage_max"] if ovr.get("leverage_max") is not None else sizing["leverage_max"]
            has_ovr = any(ovr.get(k) is not None for k in ("margin_min_usdt", "margin_max_usdt", "leverage_min", "leverage_max"))
            src = "this slot's override" if has_ovr else "inherited from pair YAML"
            lines.append(f"💰 Margin: ${margin_min:.0f}-{margin_max:.0f} (random per trade)")
            lines.append(f"📊 Leverage: {leverage_min}x-{leverage_max}x (random per trade)")
            lines.append(f"📈 Max notional: ${margin_max * leverage_max:.0f}")
            lines.append(f"   ({src})")
        else:
            lines.append("⚠️  No pair_configs entry — sizing will use hardcoded fallback")
    else:
        lines.append("💱 Pair: not assigned")

    if slot.live_enabled:
        if slot.is_live_active:
            lines.append("🟢 Live: ENABLED — real orders will be placed")
        else:
            lines.append("🟡 Live: enabled but slot not ready (missing webkey or pair)")
    else:
        lines.append("⚫ Live: disabled (shadow only)")

    if slot.last_error and slot.last_error.startswith("⚠️"):
        lines.append("")
        lines.append(slot.last_error)
        lines.append("(resolve on MEXC, then bot resumes)")

    # Cumulative account PnL for this slot (running total, not time-bucketed).
    if pnl is not None:
        lines.append("")
        n = pnl["n"]
        if n:
            wr = pnl["wins"] / n * 100
            lines.append(f"📒 PnL (total): ${pnl['pnl']:+.2f}  •  {n} trades, WR {wr:.0f}%")
        else:
            lines.append("📒 PnL (total): $0.00 — no trades yet")
        if pnl.get("reset_at"):
            ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(pnl["reset_at"]))
            lines.append(f"   counting since reset: {ts}")

    return "\n".join(lines)


def _kb_slot_config(slot) -> InlineKeyboardMarkup:
    """Build inline keyboard for slot config screen."""
    rows: list[list[InlineKeyboardButton]] = []

    if slot.is_empty:
        # Empty — only show setup button
        rows.append([
            InlineKeyboardButton(
                "🔧 Setup webkey",
                callback_data=f"m:webkey:setup:{slot.slot_id}",
            ),
        ])
    else:
        sid = slot.slot_id

        # Assign / change pair
        if slot.assigned_pair:
            rows.append([
                InlineKeyboardButton(
                    f"💱 Change pair (now: {slot.assigned_pair})",
                    callback_data=f"m:slot:{sid}:assign",
                ),
                InlineKeyboardButton(
                    "❌ Unassign",
                    callback_data=f"m:slot:{sid}:unassign",
                ),
            ])
        else:
            rows.append([
                InlineKeyboardButton(
                    "💱 Assign pair",
                    callback_data=f"m:slot:{sid}:assign",
                ),
            ])

        # Enable / disable live
        if slot.live_enabled:
            rows.append([
                InlineKeyboardButton(
                    "🛑 Disable live",
                    callback_data=f"m:slot:{sid}:live_off",
                ),
            ])
        else:
            # Only allow enabling if pair assigned
            if slot.assigned_pair:
                rows.append([
                    InlineKeyboardButton(
                        "🟢 Enable live",
                        callback_data=f"m:slot:{sid}:live_on:confirm",
                    ),
                ])

        # Soft-start (account warming). Independent of live and of the assigned
        # pair: it warms the ACCOUNT, not a strategy pair, so it is offered on
        # any slot that has a webkey.
        if slot.soft_start_enabled:
            rows.append([
                InlineKeyboardButton(
                    "🛑 Disable soft-start",
                    callback_data=f"m:slot:{sid}:softstart_off",
                ),
            ])
        else:
            rows.append([
                InlineKeyboardButton(
                    "🌱 Enable soft-start",
                    callback_data=f"m:slot:{sid}:softstart_on",
                ),
            ])

    if not slot.is_empty:
        rows.append([
            InlineKeyboardButton(
                "♻️ Reset PnL",
                callback_data=f"m:slot:{slot.slot_id}:pnl_reset",
            ),
            InlineKeyboardButton(
                "📉 Account PnL",
                callback_data=f"m:slot:{slot.slot_id}:acct_pnl",
            ),
        ])
        # Manual fee-guard reset: this account is 0%-fee on SOME pairs but not
        # others, so a fee-trip on one pair wrongly blocks a 0% pair on the
        # same slot. Lets the operator clear the halt + re-enable live.
        rows.append([
            InlineKeyboardButton(
                "🛡 Reset fee-guard",
                callback_data=f"m:slot:{slot.slot_id}:fee_reset",
            ),
        ])

    if not slot.is_empty:
        # The kill switch lives only in the engine's memory, so without this the
        # only way to lift one is restarting the container — which also stops
        # the other, healthy slot.
        rows.append([
            InlineKeyboardButton(
                "🔓 Reset kill switch",
                callback_data=f"m:slot:{slot.slot_id}:kill_reset",
            ),
        ])
    rows.append([
        InlineKeyboardButton("🔑 Webkey details", callback_data=f"m:webkey:slot:{slot.slot_id}"),
        InlineKeyboardButton("« Back", callback_data="m:webkey:menu"),
    ])

    return InlineKeyboardMarkup(rows)


def _kb_pair_picker(slot_id: int, whitelist: list[dict]) -> InlineKeyboardMarkup:
    """Show curated pairs as buttons."""
    rows: list[list[InlineKeyboardButton]] = []
    for entry in whitelist:
        sym = entry["symbol"]
        rows.append([
            InlineKeyboardButton(
                f"💱 {sym}",
                callback_data=f"m:slot:{slot_id}:pick:{sym}",
            ),
        ])
    rows.append([
        InlineKeyboardButton("« Cancel", callback_data=f"m:slot:{slot_id}"),
    ])
    return InlineKeyboardMarkup(rows)


def _fmt_pair_picker(slot_id: int, whitelist: list[dict]) -> str:
    lines = [f"💱 Choose a pair for Slot {slot_id}"]
    lines.append("")
    lines.append("Curated pairs (proven edge):")
    lines.append("Margin/leverage are RANDOMIZED per trade in given ranges")
    lines.append("(stealth — no fixed values)")
    lines.append("")
    for w in whitelist:
        lines.append(f"• {w['symbol']}")
        lines.append(f"  {w['description']}")
        lines.append(
            f"  margin ${w['margin_min_usdt']:.0f}-{w['margin_max_usdt']:.0f} × "
            f"{w['leverage_min']}-{w['leverage_max']}x | "
            f"min balance ${w['recommended_min_balance_usdt']:.0f}"
        )
        max_notional = w['margin_max_usdt'] * w['leverage_max']
        lines.append(f"  max position: ${max_notional:.0f}")
        lines.append("")
    return "\n".join(lines)


async def cmd_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /slot N — show config screen for slot N.
    """
    if not update.message or not update.message.text:
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            f"Usage: /slot <N>  (where N is 1..{MAX_SLOTS})\n\n"
            "Example: /slot 2"
        )
        return

    try:
        slot_id = int(args[0])
        if slot_id < 1 or slot_id > MAX_SLOTS:
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"Slot must be a number 1..{MAX_SLOTS}.")
        return

    store = context.bot_data.get("webkey_store")
    if store is None:
        await update.message.reply_text("❌ Webkey store not configured.")
        return

    slot = await store.get(slot_id)
    if slot is None:
        await update.message.reply_text(f"Slot {slot_id}: not found.")
        return

    # Build whitelist lookup for description display
    wl = await store.list_live_whitelist()
    wl_lookup = {w["symbol"]: w for w in wl}

    # fetch sizing from the pair YAML (read_pair_sizing, single source of truth)
    sizing = read_pair_sizing(slot.assigned_pair) if slot.assigned_pair else None
    slot_ovr = await store.get_slot_pair_sizing(slot.slot_id, slot.assigned_pair) if slot.assigned_pair else None
    pnl = await _get_slot_pnl(context.bot_data.get("live_db"), slot_id)

    text = _fmt_slot_config(slot, wl_lookup, sizing=sizing, override=slot_ovr, pnl=pnl)
    kb = _kb_slot_config(slot)
    await update.message.reply_text(text, reply_markup=kb)


# ============================================================
# Callback handlers (called from bot.py callback_router)
# ============================================================

async def handle_slot_callback(query, context, data: str) -> None:
    """
    Route m:slot:* callbacks.

    data examples:
      m:slot:2                  — show slot 2
      m:slot:2:assign           — show pair picker for slot 2
      m:slot:2:pick:ZECUSDT     — assign ZEC to slot 2
      m:slot:2:unassign         — clear assignment
      m:slot:2:live_on:confirm  — enable live (with confirmation)
      m:slot:2:live_off         — disable live
    """
    parts = data.split(":")
    # parts: ['m', 'slot', '2', ...action]
    if len(parts) < 3:
        return
    try:
        slot_id = int(parts[2])
    except ValueError:
        return

    store = context.bot_data.get("webkey_store")
    if store is None:
        await query.message.reply_text("❌ Webkey store not configured.")
        return

    action = parts[3] if len(parts) >= 4 else ""

    # Show slot config (default action)
    if action == "":
        slot = await store.get(slot_id)
        if slot is None:
            await query.message.reply_text(f"Slot {slot_id}: not found.")
            return
        wl = await store.list_live_whitelist()
        wl_lookup = {w["symbol"]: w for w in wl}
        sizing = read_pair_sizing(slot.assigned_pair) if slot.assigned_pair else None
        slot_ovr = await store.get_slot_pair_sizing(slot.slot_id, slot.assigned_pair) if slot.assigned_pair else None
        pnl = await _get_slot_pnl(context.bot_data.get("live_db"), slot_id)
        text = _fmt_slot_config(slot, wl_lookup, sizing=sizing, override=slot_ovr, pnl=pnl)
        kb = _kb_slot_config(slot)
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)
        return

    # Read the ACCOUNT's 360d realized PnL via the slot's webkey (same source
    # the recovery monitor uses) — "how much minus is on the account".
    if action == "acct_pnl":
        try:
            await query.answer("Reading account PnL…")
        except Exception:
            pass
        pool = context.bot_data.get("webkey_client_pool")
        if pool is None:
            await query.message.reply_text("❌ Webkey client pool not available.")
            return
        try:
            client = await pool.get(slot_id)
            acct_pnl = await client.get_account_pnl_usdt(window_days=359)
        except Exception as e:
            await query.message.reply_text(
                f"❌ Slot {slot_id}: account PnL read failed ({type(e).__name__})."
            )
            return
        if acct_pnl is None:
            await query.message.reply_text(
                f"⚠️ Slot {slot_id}: account PnL read returned nothing (transient) — try again."
            )
            return
        await query.message.reply_text(
            f"📉 Slot {slot_id} — account 360d realized PnL: ${acct_pnl:+.2f}"
        )
        return

    # Reset this slot's cumulative PnL — running total starts over from now.
    # Historical trades are not deleted; they're just excluded going forward.
    if action == "pnl_reset":
        await _reset_slot_pnl(context.bot_data.get("live_db"), slot_id)
        try:
            await query.answer("PnL reset ✅")
        except Exception:
            pass
        slot = await store.get(slot_id)
        if slot is None:
            return
        wl = await store.list_live_whitelist()
        wl_lookup = {w["symbol"]: w for w in wl}
        sizing = read_pair_sizing(slot.assigned_pair) if slot.assigned_pair else None
        slot_ovr = await store.get_slot_pair_sizing(slot.slot_id, slot.assigned_pair) if slot.assigned_pair else None
        pnl = await _get_slot_pnl(context.bot_data.get("live_db"), slot_id)
        text = _fmt_slot_config(slot, wl_lookup, sizing=sizing, override=slot_ovr, pnl=pnl)
        kb = _kb_slot_config(slot)
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)
        return

    if action == "kill_reset":
        # Lift an active safety kill on THIS slot only. The state is in-memory,
        # so it is reached through the live pool rather than the store.
        live_pool = context.bot_data.get("live_pool")
        _was, _why, _err = False, "", None
        _summary = None
        if live_pool is None:
            _err = "live pool не активний"
        else:
            try:
                _safety = live_pool.get_safety(slot_id)
                if _safety is None:
                    _err = f"слот {slot_id} не має контролера безпеки"
                else:
                    _was, _why = _safety.release_kill()
                    _summary = _safety.state_summary()
            except Exception as _e:
                # Раніше тут стояв `pass`, і будь-яка помилка перевдягалась у
                # «kill не був активний» — оператор думав, що кнопка спрацювала.
                _err = f"{type(_e).__name__}: {_e}"
                logger.exception("[KILL RESET] slot=%s не вдалось зняти", slot_id)
        try:
            await query.answer(
                "Не вдалось ⚠️" if _err else
                ("Kill switch знято ✅" if _was else "Кіла не було — базу оновлено")
            )
        except Exception:
            pass
        # Повідомлення в ЧАТ, а не спливна підказка: підказка живе секунду й
        # нічого не лишає. Коротко: слот, що сталось, і межа наступної зупинки.
        if _err:
            _msg = (f"⚠️ <b>Слот {slot_id}</b> — кіл НЕ знято\n"
                    f"<code>{_err[:160]}</code>")
        else:
            _lim = (_summary or {}).get("drawdown_limit_usdt")
            _pnl = (_summary or {}).get("today_pnl", 0.0) or 0.0
            _bits = [f"♻️ <b>Слот {slot_id}</b> — кіл знято" if _was
                     else f"✅ <b>Слот {slot_id}</b> — кіла не було"]
            if _lim is not None:
                # круглу межу без копійок: «$25», а не «$25.00»
                _bits.append("межа $" + (f"{_lim:.0f}"
                                         if abs(_lim - round(_lim)) < 0.005
                                         else f"{_lim:.2f}"))
            # Нульовий PnL нічого не каже — показуємо лише коли є що показати.
            if abs(_pnl) >= 0.01:
                _bits.append(f"PnL ${_pnl:+.2f}")
            _msg = " · ".join(_bits)
            if _was and _why:
                _msg += f"\n<i>{_why[:90]}</i>"
        try:
            await query.message.reply_text(_msg, parse_mode="HTML")
        except Exception:
            logger.exception("[KILL RESET] не вдалось надіслати підтвердження")
        slot = await store.get(slot_id)
        if slot is None:
            return
        wl = await store.list_live_whitelist()
        wl_lookup = {w["symbol"]: w for w in wl}
        sizing = read_pair_sizing(slot.assigned_pair) if slot.assigned_pair else None
        slot_ovr = await store.get_slot_pair_sizing(slot.slot_id, slot.assigned_pair) if slot.assigned_pair else None
        pnl = await _get_slot_pnl(context.bot_data.get("live_db"), slot_id)
        text = _fmt_slot_config(slot, wl_lookup, sizing=sizing, override=slot_ovr, pnl=pnl)
        kb = _kb_slot_config(slot)
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)
        return

    if action == "fee_reset":
        # Clear the in-memory fee-guard halt on this slot's executor + re-enable
        # live, so a 0%-fee pair isn't blocked by another pair's fee-trip.
        live_pool = context.bot_data.get("live_pool")
        was_halted = False
        if live_pool is not None:
            try:
                was_halted = live_pool.reset_fee_guard(slot_id)
            except Exception:
                pass
        try:
            await store.set_live_enabled(slot_id, True)
        except Exception:
            pass
        try:
            await query.answer(
                "Fee-guard reset ✅ — live re-enabled" if was_halted
                else "Fee-guard was not active — live re-enabled"
            )
        except Exception:
            pass
        slot = await store.get(slot_id)
        if slot is None:
            return
        wl = await store.list_live_whitelist()
        wl_lookup = {w["symbol"]: w for w in wl}
        sizing = read_pair_sizing(slot.assigned_pair) if slot.assigned_pair else None
        slot_ovr = await store.get_slot_pair_sizing(slot.slot_id, slot.assigned_pair) if slot.assigned_pair else None
        pnl = await _get_slot_pnl(context.bot_data.get("live_db"), slot_id)
        text = _fmt_slot_config(slot, wl_lookup, sizing=sizing, override=slot_ovr, pnl=pnl)
        kb = _kb_slot_config(slot)
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)
        return

    # Show pair picker
    if action == "assign":
        wl = await store.list_live_whitelist()
        text = _fmt_pair_picker(slot_id, wl)
        kb = _kb_pair_picker(slot_id, wl)
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)
        return

    # Pick a specific pair
    if action == "pick":
        if len(parts) < 5:
            return
        symbol = parts[4]

        # Validate symbol is in whitelist
        wl = await store.list_live_whitelist()
        wl_lookup = {w["symbol"]: w for w in wl}
        if symbol not in wl_lookup:
            await query.message.reply_text(f"❌ {symbol} not in whitelist.")
            return

        # Auto-demote: if this slot ALREADY has a different
        # pair assigned and live, demote it to shadow before reassigning.
        # Without this, both old + new pair sit in pair_states.state='live'
        # and the engine tries to trade both — but max_concurrent=1, so
        # whichever signal arrives first wins, the other stays as dead-line
        # live config that confuses analytics and state machine.
        slot_before = await store.get(slot_id)
        demoted_pair: str | None = None
        old_pair = slot_before.assigned_pair if slot_before else None
        if old_pair and old_pair != symbol:
            # Only demote the displaced pair if it is NOT still assigned and
            # live on ANOTHER slot. Two accounts each trade their own pair;
            # reassigning one slot must not yank a pair another live slot is
            # still running (bug: reassigning slot 1 demoted a pair live on slot 2).
            other_live = [
                sl for sl in await store.list_all()
                if sl.slot_id != slot_id and sl.assigned_pair == old_pair and sl.live_enabled
            ]
            if other_live:
                logger.info(
                    "[SLOT REASSIGN] slot=%d: %s still live on slot(s) %s — NOT demoting",
                    slot_id, old_pair, [sl.slot_id for sl in other_live],
                )
            else:
                demoted_pair = old_pair
                state_manager_demote = context.bot_data.get("state_manager")
                if state_manager_demote is not None:
                    try:
                        await state_manager_demote.manual_promote(
                            demoted_pair, target="shadow",
                            reason=f"UI: slot {slot_id} reassigned to {symbol}",
                        )
                        logger.info(
                            "[SLOT REASSIGN] slot=%d: demoted %s → shadow (replaced by %s)",
                            slot_id, demoted_pair, symbol,
                        )
                    except Exception:
                        logger.exception(
                            "auto-demote failed for %s on slot reassign", demoted_pair,
                        )

        # sizing comes from the pair YAML, no need to pass to assign_pair
        await store.assign_pair(
            slot_id=slot_id,
            pair=symbol,
        )
        # Auto-enable live if slot is fully configured (has webkey).
        # v6.1: proxy no longer required — bot connects to futures.mexc.com
        # directly. Also force pair_states.state='live' and
        # pair_configs.mode='live' — otherwise even with live_enabled=1 the
        # pair sits in 'paused' (e.g. after global Shadow OFF toggle) and
        # signals get filtered. Mirrors the logic in 'live_on do' action below.
        slot = await store.get(slot_id)
        auto_enabled = False
        promoted = False
        if slot.is_complete:
            # 1. Slot-level live toggle
            if not slot.live_enabled:
                await store.set_live_enabled(slot_id, True)
                slot = await store.get(slot_id)
                auto_enabled = True
            # 2. State machine: paused/shadow → live
            state_manager = context.bot_data.get("state_manager")
            db = context.bot_data.get("db")
            if state_manager is not None:
                try:
                    await state_manager.manual_promote(
                        symbol, target="live",
                        reason=f"UI: assign {symbol} → slot {slot_id} (auto-promote)",
                    )
                    promoted = True
                except Exception:
                    logger.exception("state_manager.manual_promote failed for %s", symbol)
            # live_pool rebuild so changes take effect now
            live_pool = context.bot_data.get("live_pool")
            if live_pool is not None:
                try:
                    await live_pool.rebuild_from_store()
                except Exception:
                    logger.exception("live_pool rebuild failed")
        # Refresh slot view
        if auto_enabled and promoted:
            text = "✅ Assigned " + symbol + " to slot " + str(slot_id) + " (live auto-ON, promoted)\n\n"
        elif auto_enabled:
            text = "✅ Assigned " + symbol + " to slot " + str(slot_id) + " (live auto-ON)\n\n"
        else:
            text = "✅ Assigned " + symbol + " to slot " + str(slot_id) + "\n\n"
        if demoted_pair:
            text = "⬇️ " + demoted_pair + " demoted to SHADOW\n" + text
        sizing = read_pair_sizing(slot.assigned_pair) if slot.assigned_pair else None
        slot_ovr = await store.get_slot_pair_sizing(slot.slot_id, slot.assigned_pair) if slot.assigned_pair else None
        text += _fmt_slot_config(slot, wl_lookup, sizing=sizing, override=slot_ovr)
        kb = _kb_slot_config(slot)
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)
        return

    if action == "unassign":
        prev = await store.get(slot_id)
        prev_pair = prev.assigned_pair if prev else None
        await store.assign_pair(slot_id=slot_id, pair=None)
        # Auto-disable live since there's no pair anymore
        await store.set_live_enabled(slot_id, False)
        slot = await store.get(slot_id)
        wl = await store.list_live_whitelist()
        wl_lookup = {w["symbol"]: w for w in wl}
        if prev_pair:
            text = f"✅ Unassigned. Live disabled + {prev_pair} → SHADOW.\n\n"
        else:
            text = "✅ Unassigned. Live also disabled.\n\n"
        sizing = read_pair_sizing(slot.assigned_pair) if slot.assigned_pair else None
        slot_ovr = await store.get_slot_pair_sizing(slot.slot_id, slot.assigned_pair) if slot.assigned_pair else None
        text += _fmt_slot_config(slot, wl_lookup, sizing=sizing, override=slot_ovr)
        kb = _kb_slot_config(slot)
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)
        return

    # Enable live (confirmation step)
    if action == "live_on" and len(parts) >= 5 and parts[4] == "confirm":
        slot = await store.get(slot_id)
        wl = await store.list_live_whitelist()
        wl_lookup = {w["symbol"]: w for w in wl}
        wl_entry = wl_lookup.get(slot.assigned_pair)

        if not slot.assigned_pair or not wl_entry:
            await query.message.reply_text("❌ Pair not assigned — use 'Assign pair' first.")
            return

        # Sizing source of truth is the pair YAML. Refuse go-live if the pair
        # has no YAML (read_pair_sizing would silently return global defaults).
        if not pair_config_exists(slot.assigned_pair):
            await query.message.reply_text(
                f"❌ No config/pairs/{slot.assigned_pair}.yaml. "
                f"Cannot enable live without sizing config."
            )
            return
        sizing = read_pair_sizing(slot.assigned_pair)
        # Show the EFFECTIVE sizing this slot will actually trade with (its
        # (slot,pair) override where set, else the pair YAML) so the go-live
        # confirmation matches what shadow_engine really sizes — not the raw
        # pair default.
        _ov = await store.get_slot_pair_sizing(slot.slot_id, slot.assigned_pair) or {}
        margin_min = _ov["margin_min_usdt"] if _ov.get("margin_min_usdt") is not None else sizing["margin_min_usdt"]
        margin_max = _ov["margin_max_usdt"] if _ov.get("margin_max_usdt") is not None else sizing["margin_max_usdt"]
        leverage_min = _ov["leverage_min"] if _ov.get("leverage_min") is not None else sizing["leverage_min"]
        leverage_max = _ov["leverage_max"] if _ov.get("leverage_max") is not None else sizing["leverage_max"]
        max_notional = margin_max * leverage_max
        _src = "slot override" if any(_ov.get(k) is not None for k in ("margin_min_usdt", "margin_max_usdt", "leverage_min", "leverage_max")) else "pair default"

        text = (
            f"🔴 Confirm: Enable LIVE trading\n\n"
            f"Slot: {slot_id}\n"
            f"Pair: {slot.assigned_pair}\n"
            f"Margin per trade: ${margin_min:.0f}-{margin_max:.0f} (random, {_src})\n"
            f"Leverage per trade: {leverage_min}x-{leverage_max}x (random, {_src})\n"
            f"Max position size: ${max_notional:.0f}\n"
            f"Min balance recommended: ${wl_entry['recommended_min_balance_usdt']:.0f}\n\n"
            f"⚠️ Real money will be at risk!\n"
            f"Make sure you've completed all MEXC verifications."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "✅ YES, enable live",
                callback_data=f"m:slot:{slot_id}:live_on:do",
            )],
            [InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"m:slot:{slot_id}",
            )],
        ])
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)
        return

    # Actually flip the switch
    if action == "live_on" and len(parts) >= 5 and parts[4] == "do":
        await store.set_live_enabled(slot_id, True)

        # Also promote pair_states.state='live' and
        # pair_configs.mode='live' for the assigned pair, otherwise bot will
        # still shadow-trade despite slot being live_enabled.
        slot_for_promote = await store.get(slot_id)
        promoted_pair: str | None = None
        if slot_for_promote and slot_for_promote.assigned_pair:
            promoted_pair = slot_for_promote.assigned_pair
            state_manager = context.bot_data.get("state_manager")
            db = context.bot_data.get("db")
            if state_manager is not None:
                try:
                    await state_manager.manual_promote(
                        promoted_pair, target="live",
                        reason=f"UI: enable live for slot {slot_id}",
                    )
                except Exception:
                    logger.exception("state_manager.manual_promote failed for %s", promoted_pair)

        # Trigger live_pool rebuild so changes take effect immediately
        live_pool = context.bot_data.get("live_pool")
        if live_pool is not None:
            try:
                await live_pool.rebuild_from_store()
            except Exception:
                logger.exception("live_pool rebuild failed")

        slot = await store.get(slot_id)
        wl = await store.list_live_whitelist()
        wl_lookup = {w["symbol"]: w for w in wl}
        text = "🟢 LIVE TRADING ENABLED for slot " + str(slot_id)
        if promoted_pair:
            text += "\n✅ " + promoted_pair + " promoted to LIVE state"
        text += "\n\n"
        sizing = read_pair_sizing(slot.assigned_pair) if slot.assigned_pair else None
        slot_ovr = await store.get_slot_pair_sizing(slot.slot_id, slot.assigned_pair) if slot.assigned_pair else None
        text += _fmt_slot_config(slot, wl_lookup, sizing=sizing, override=slot_ovr)
        kb = _kb_slot_config(slot)
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)
        return

    if action in ("softstart_on", "softstart_off"):
        want_on = action == "softstart_on"
        slot_now = await store.get(slot_id)
        # Warming needs a credential; it does NOT need an assigned pair.
        if want_on and (slot_now is None or not slot_now.webkey):
            await query.answer(
                f"Slot {slot_id}: no webkey — nothing to warm.",
                show_alert=True,
            )
            return

        await store.set_soft_start(slot_id, want_on)
        logger.info("[SLOT SOFTSTART] slot=%d -> %s", slot_id, "ON" if want_on else "OFF")

        slot = await store.get(slot_id)
        wl = await store.list_live_whitelist()
        wl_lookup = {w["symbol"]: w for w in wl}
        if want_on:
            text = f"🌱 Soft-start enabled for slot {slot_id}.\n"
            text += (
                "Account warming, 3-DAY campaign — it switches itself off.\n"
                "Small spot orders (buy / hold / sell) plus rare futures "
                "positions, STRICTLY on pairs where this account pays 0% fee "
                "(re-checked before every open).\n"
                "Everything is randomised: how many actions per day, which "
                "ones, in what order, timing, sizes, leverage, hold time.\n"
                "Spend ceiling: 5 USDT for the whole campaign — it stops early "
                "if that runs out.\n"
            )
            if not _soft_start_live_allowed():
                text += (
                    "\n⚠️ DRY-RUN: SOFT_START_LIVE is not set, so the bot only "
                    "LOGS what it would do and sends no orders.\n"
                )
        else:
            text = f"⚪ Soft-start disabled for slot {slot_id}.\n"
        text += "\n"
        sizing = read_pair_sizing(slot.assigned_pair) if slot.assigned_pair else None
        slot_ovr = (await store.get_slot_pair_sizing(slot.slot_id, slot.assigned_pair)
                    if slot.assigned_pair else None)
        text += _fmt_slot_config(slot, wl_lookup, sizing=sizing, override=slot_ovr)
        kb = _kb_slot_config(slot)
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)
        return

    if action == "live_off":
        # Also demote pair_states.state to shadow
        # and pair_configs.mode to shadow for the assigned pair.
        slot_before = await store.get(slot_id)
        demoted_pair: str | None = None
        old_pair = slot_before.assigned_pair if slot_before else None
        if old_pair:
            # Don't demote the pair's state if it's still live on another slot
            # (the other account keeps trading it). Only this slot goes live-off.
            other_live = [
                sl for sl in await store.list_all()
                if sl.slot_id != slot_id and sl.assigned_pair == old_pair and sl.live_enabled
            ]
            if other_live:
                logger.info(
                    "[SLOT LIVE_OFF] slot=%d: %s still live on slot(s) %s — NOT demoting state",
                    slot_id, old_pair, [sl.slot_id for sl in other_live],
                )
            else:
                demoted_pair = old_pair
                state_manager = context.bot_data.get("state_manager")
                db = context.bot_data.get("db")
                if state_manager is not None:
                    try:
                        await state_manager.manual_promote(
                            demoted_pair, target="shadow",
                            reason=f"UI: disable live for slot {slot_id}",
                        )
                    except Exception:
                        logger.exception("state_manager demote failed for %s", demoted_pair)

        await store.set_live_enabled(slot_id, False)
        live_pool = context.bot_data.get("live_pool")
        if live_pool is not None:
            try:
                await live_pool.rebuild_from_store()
            except Exception:
                logger.exception("live_pool rebuild failed")

        slot = await store.get(slot_id)
        wl = await store.list_live_whitelist()
        wl_lookup = {w["symbol"]: w for w in wl}
        text = "⚫ Live disabled for slot " + str(slot_id) + "."
        if demoted_pair:
            text += "\n⬇️ " + demoted_pair + " demoted back to SHADOW state"
        text += "\n\n"
        sizing = read_pair_sizing(slot.assigned_pair) if slot.assigned_pair else None
        slot_ovr = await store.get_slot_pair_sizing(slot.slot_id, slot.assigned_pair) if slot.assigned_pair else None
        text += _fmt_slot_config(slot, wl_lookup, sizing=sizing, override=slot_ovr)
        kb = _kb_slot_config(slot)
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb)
        return
