"""
Telegram bot — minimalist version.

Total commands: 6 (down from 23)

Philosophy:
  - Few commands, all working
  - All actions accessible via /menu inline buttons
  - /pair_status has its own button-based actions

Commands:
  /menu          — main interactive menu (entry point)
  /status        — overview of all pairs
  /pair_status SYMBOL — detail + buttons for pair actions
  /kill_all      — emergency (with confirm)
  /help          — this list
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    BotCommandScopeChat,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.execution.webkey import WebkeyError
from src.state.pair_state_manager import PairStateManager
from src.storage.db import Database
from src.strategy.shadow_engine import ShadowEngine
from src.telegram_bot.alerts import TelegramAlerts
from src.telegram_bot.cmd_balance import cmd_balance
from src.telegram_bot.cmd_slot import cmd_slot, handle_slot_callback
from src.telegram_bot.cmd_webkey import (
    cmd_webkey_cancel,
    cmd_webkey_disable,
    cmd_webkey_enable,
    cmd_webkey_label,
    cmd_webkey_remove,
    cmd_webkey_setup,
    cmd_webkey_status,
    cmd_webkey_test,
    webkey_text_handler,
)
from src.telegram_bot.reports import ReportScheduler

logger = logging.getLogger(__name__)


def _state_emoji(state: str) -> str:
    return {
        "discovered": "🔍", "shadow": "📊", "live": "🟢",
        "paused": "⏸", "rejected": "🚫",
    }.get(state, "❔")


def _normalize_symbol(s: str) -> str:
    s = s.upper()
    return s if s.endswith("USDT") else s + "USDT"


# ============================================================
# Inline keyboards
# ============================================================

def _kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ Status",          callback_data="m:status"),
            InlineKeyboardButton("📋 Trades Today",   callback_data="m:trades_today"),
        ],
        [
            InlineKeyboardButton("🔔 Last Signals",   callback_data="m:signals"),
            InlineKeyboardButton("💵 Balance",         callback_data="m:balance"),
        ],
        [
            InlineKeyboardButton("💰 Sizing",          callback_data="m:siz:pick"),
            InlineKeyboardButton("🗂️ Shadow Pairs",   callback_data="m:shp:list"),
        ],
        [
            InlineKeyboardButton("▶️ Shadow ON",       callback_data="m:shadow:on"),
            InlineKeyboardButton("⏹️ Shadow OFF",      callback_data="m:shadow:off"),
        ],
        [
            InlineKeyboardButton("🔑 Webkey",          callback_data="m:webkey:menu"),
            InlineKeyboardButton("💀 KILL ALL",        callback_data="m:kill_all:confirm"),
        ],
    ])


def _kb_webkey_overview(slots: list[Any]) -> InlineKeyboardMarkup:
    """Top-level overview keyboard: list of MAX_SLOTS slots + global Test all + Back."""
    rows: list[list[InlineKeyboardButton]] = []

    # Action row at top
    has_anything = any(not s.is_empty for s in slots)
    if has_anything:
        rows.append([
            InlineKeyboardButton("🩺 Test all", callback_data="m:webkey:test_all"),
            InlineKeyboardButton("🔄 Refresh", callback_data="m:webkey:menu"),
        ])

    # One row per slot
    for s in slots:
        if s.is_empty:
            label = f"Slot {s.slot_id}: ⚪ empty"
        else:
            # ✅ = slot is LIVE (live_enabled) — matches the slot's Enable/Disable
            # live toggle. (The webkey `enabled` flag doesn't gate live trading.)
            tail = " ✅" if s.live_enabled else " ⛔️"
            preview = s.label or s.masked_webkey() or ""
            label = f"Slot {s.slot_id}: {preview}{tail}"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"m:webkey:slot:{s.slot_id}"),
        ])

    rows.append([InlineKeyboardButton("« Back to menu", callback_data="m:back")])
    return InlineKeyboardMarkup(rows)


def _kb_webkey_slot(slot: Any) -> InlineKeyboardMarkup:
    """Per-slot submenu — adaptive to slot state."""
    rows: list[list[InlineKeyboardButton]] = []
    sid = slot.slot_id

    if slot.is_empty:
        rows.append([
            InlineKeyboardButton(
                f"🔧 Setup slot {sid}",
                callback_data=f"m:webkey:setup:{sid}",
            ),
        ])
    else:
        # Test available once the slot has a webkey.
        if slot.is_complete:
            rows.append([
                InlineKeyboardButton("🩺 Test", callback_data=f"m:webkey:test:{sid}"),
            ])

        # NOTE: no Enable/Disable here. The webkey `enabled` flag does NOT gate
        # live trading (is_live_active = live_enabled + assigned_pair + complete).
        # The real enable/disable lives on the slot screen ("Enable/Disable live"
        # in cmd_slot, m:slot:N), shown when a pair is assigned.

        # Re-setup options
        rows.append([
            InlineKeyboardButton(
                "🔄 Re-paste webkey",
                callback_data=f"m:webkey:setup:{sid}",
            ),
        ])

        # Proxy support fully removed (direct mode): the bot connects to
        # futures.mexc.com directly from a Tokyo VPS, no SOCKS proxy.

        # Remove
        rows.append([
            InlineKeyboardButton(f"🗑 Remove slot {sid}", callback_data=f"m:webkey:remove:{sid}"),
        ])

        # Configure live trading (only if complete — proxy no longer needed)
        if slot.is_complete:
            rows.append([
                InlineKeyboardButton(
                    "⚙️ Configure live trading",
                    callback_data=f"m:slot:{sid}",
                ),
            ])

    rows.append([InlineKeyboardButton("« Back to slots", callback_data="m:webkey:menu")])
    return InlineKeyboardMarkup(rows)


def _kb_pair_actions(symbol: str, state: str) -> InlineKeyboardMarkup:
    rows = []
    if state == "shadow":
        rows.append([
            InlineKeyboardButton("🚀 → live", callback_data=f"p:promote_live:{symbol}"),
            InlineKeyboardButton("⏸ Pause",  callback_data=f"p:pause:{symbol}"),
        ])
    elif state == "live":
        rows.append([
            InlineKeyboardButton("⬇️ → shadow", callback_data=f"p:demote:{symbol}"),
            InlineKeyboardButton("⏸ Pause",     callback_data=f"p:pause:{symbol}"),
        ])
    elif state == "paused":
        rows.append([
            InlineKeyboardButton("▶️ Resume", callback_data=f"p:resume:{symbol}"),
        ])
    # `discovered` and `rejected` states are legacy (
    # Pairs are now seeded directly into `shadow` from the universe
    # whitelist at startup. Old DB rows in those states still display, but
    # have no UI actions — use SQL if you need to change them.

    rows.append([
        InlineKeyboardButton("💀 Kill positions", callback_data=f"p:kill:{symbol}"),
        InlineKeyboardButton("🔄 Refresh",         callback_data=f"p:refresh:{symbol}"),
    ])
    return InlineKeyboardMarkup(rows)


def _kb_persistent() -> ReplyKeyboardMarkup:
    """
    Persistent reply keyboard — always visible under the input field.

    Layout (4 rows × 2 cols):
      📊 Menu          📋 Trades Today
      🧪 Shadow PnL    🔴 Live PnL
      🔔 Signals       💵 Balance
      🔑 Webkey        ❓ Help
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Menu"),         KeyboardButton("📋 Trades Today")],
            [KeyboardButton("🧪 Shadow PnL"),   KeyboardButton("🔴 Live PnL")],
            [KeyboardButton("🔔 Signals"),      KeyboardButton("💵 Balance")],
            [KeyboardButton("🔑 Webkey"),       KeyboardButton("❓ Help")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )



# ============================================================
# COMMANDS (only 6)
# ============================================================

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Sectioned help text. The "━" rule lines render as thin separators in
    # Telegram on iOS/Android — visual structure without HTML <hr>.
    # Keep commands as <code> for tap-to-copy; descriptions as plain text.
    text = (
        "<b>🤖 stakan-bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>📱 Базове</b>\n"
        "<code>/menu</code>  →  головне меню з кнопками\n"
        "<code>/balance</code>  →  💰 баланс і позиції\n"
        "<code>/status</code>  →  короткий звіт\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🎯 Керування парою</b>\n"
        "<code>/pair_status SYMBOL</code>  →  деталі + дії\n"
        "<code>/get_config SYMBOL</code>  →  повний конфіг\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🔑 Webkey (live)</b>\n"
        "Усе через <code>/menu</code> → 🔑 Webkey\n"
        "<code>/webkey</code>  →  альтернативний overview\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🚨 Emergency</b>\n"
        "<code>/kill_all</code>  →  закрити всі позиції\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Налаштування виходів (TP/SL):</i>\n"
        "<code>config/pairs/&lt;SYMBOL&gt;.yaml</code>\n"
        "<i>автоматичний reload кожні 30s</i>\n\n"
        "<i>Більшість дій зручніше робити через кнопки — "
        "тапни</i> <code>/menu</code> <i>щоб відкрити панель.</i>"
    )
    # Send help text + activate persistent keyboard
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_persistent(),
    )
    # Send menu inline keyboard as a separate message
    await update.message.reply_text(
        "🎛 <b>Швидкий доступ:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_main_menu(),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state_manager = context.bot_data["state_manager"]
    states = state_manager.all_states()
    counts = {"live": 0, "shadow": 0, "paused": 0}
    for s in states.values():
        if s.state in counts:
            counts[s.state] += 1

    shadow_engine = context.bot_data["shadow_engine"]
    trading_status = "✅ ON" if shadow_engine.cfg.enabled else "🛑 OFF"
    open_count = sum(len(p) for p in shadow_engine._open_positions.values())

    text = (
        f"<b>🎛 stakan-bot menu</b>\n\n"
        f"Trading: <b>{trading_status}</b>\n"
        f"Open positions: <b>{open_count}</b>\n\n"
        f"States: 🟢{counts['live']} 📊{counts['shadow']} ⏸{counts['paused']}"
    )
    # Activate persistent keyboard (always visible) + show inline menu
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_persistent(),
    )
    await update.message.reply_text(
        "Дії:",
        reply_markup=_kb_main_menu(),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state_manager = context.bot_data["state_manager"]
    states = state_manager.all_states()
    if not states:
        await update.message.reply_text("📭 No pairs.")
        return

    groups: dict[str, list] = {}
    for ps in states.values():
        groups.setdefault(ps.state, []).append(ps)

    lines = ["<b>📊 Status</b>"]
    for state in ("live", "shadow", "paused"):
        items = groups.get(state, [])
        if not items:
            continue
        lines.append(f"\n{_state_emoji(state)} <b>{state.upper()}</b> ({len(items)})")
        items.sort(key=lambda x: x.last_24h_pnl, reverse=True)
        for ps in items[:8]:
            wr = f"{ps.last_24h_winrate*100:.0f}%" if ps.last_24h_trades > 0 else "—"
            lines.append(
                f"  <code>{ps.symbol:<10}</code> "
                f"{ps.last_24h_trades}t WR={wr} "
                f"PnL=<b>${ps.last_24h_pnl:+.2f}</b>"
            )
        if len(items) > 8:
            lines.append(f"  <i>...and {len(items)-8} more</i>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                    reply_markup=_kb_main_menu())


async def cmd_pair_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: <code>/pair_status SYMBOL</code>",
                                        parse_mode=ParseMode.HTML)
        return
    symbol = _normalize_symbol(context.args[0])
    await _send_pair_status(update.message, context, symbol)


async def _send_pair_status(message_or_query, context, symbol: str, edit: bool = False) -> None:
    state_manager = context.bot_data["state_manager"]
    db = context.bot_data["db"]
    ps = state_manager.get_state(symbol)
    if not ps:
        msg = f"❌ Pair <code>{symbol}</code> not found."
        if edit and hasattr(message_or_query, "edit_message_text"):
            await message_or_query.edit_message_text(msg, parse_mode=ParseMode.HTML)
        else:
            await message_or_query.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    cfg_row = await db.fetchone("SELECT * FROM pair_configs WHERE symbol=?", (symbol,))
    rows = await db.fetchall(
        """SELECT direction, exit_reason, net_pnl_usdt, roi_pct, duration_sec
           FROM shadow_trades WHERE symbol=? AND closed_at IS NOT NULL
           ORDER BY closed_at DESC LIMIT 5""",
        (symbol,),
    )

    # Tuning (SL, max_hold) from the SAME resolver the trading loop uses — NOT
    # the dead pair_configs columns (stop_loss_ticks was dropped → DB read lied
    # "5"; max_hold drifted). strategy_type stays in DB (it's metadata, not yaml).
    ec = context.bot_data["shadow_engine"]._config_loader.get(symbol).execution
    strategy = (cfg_row["strategy_type"] if cfg_row else "?")
    sl_str = (f"{ec.stop_loss_ticks}t"
              if ec.stop_loss_ticks and ec.stop_loss_ticks > 0 else "off")
    max_hold = ec.max_hold_sec
    mode = ps.state
    mode_emoji = "🟢" if state_manager.is_in_live(symbol) else "📊"
    exit_mode = "simple_trail"  # the only exit model (exit_strategy.mode is dead)

    text = (
        f"{_state_emoji(ps.state)} <b>{symbol}</b> — <i>{ps.state.upper()}</i> {mode_emoji}<code>{mode}</code>\n\n"
        f"<b>24h:</b> {ps.last_24h_trades}t  "
        f"WR={ps.last_24h_winrate*100:.1f}%  "
        f"PnL=<b>${ps.last_24h_pnl:+.3f}</b>  "
        f"PF={ps.last_24h_profit_factor:.2f}\n"
        f"<b>Signals 24h:</b> {ps.last_24h_signals}\n"
        f"<b>Strategy:</b> <code>{strategy}</code> "
        f"(SL={sl_str}, max_hold={max_hold}s)\n"
        f"<b>Exit:</b> <code>{exit_mode}</code> "
        f"(see config/pairs/{symbol}.yaml)"
    )
    if rows:
        text += "\n\n<b>Last 5 trades:</b>"
        for r in rows:
            emoji = "🟢" if r["net_pnl_usdt"] > 0 else "🔴" if r["net_pnl_usdt"] < 0 else "➖"
            d = "L" if r["direction"] == "long" else "S"
            text += (f"\n  {emoji} {d} ${r['net_pnl_usdt']:+.3f} "
                     f"({r['roi_pct']:+.2f}%) {r['duration_sec']}s "
                     f"<i>{r['exit_reason']}</i>")

    kb = _kb_pair_actions(symbol, ps.state)
    if edit and hasattr(message_or_query, "edit_message_text"):
        try:
            await message_or_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except Exception:
            pass
    if hasattr(message_or_query, "message"):
        await message_or_query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await message_or_query.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


def _cfg_val(x) -> str:
    """Значення у вигляді, у якому воно стоїть у ямлі."""
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, float):
        return f"{x:g}"
    return str(x)


def _cfg_gate(x) -> str:
    """Ворота: 0 означає «вимкнено», і це має читатись з першого погляду."""
    return _cfg_val(x) if x else "—  вимкнено"


def _fmt_pair_config_full(
    symbol: str,
    pc,
    *,
    live: bool,
    epoch_ts: int | None = None,
    overrides: list[tuple[int, dict]] | None = None,
    tick: float | None = None,
    price: float | None = None,
) -> str:
    """Весь конфіг пари одним повідомленням.

    `overrides` — [(slot_id, {margin_min_usdt, ...})] для слотів, що торгують
    цю пару. Вони ПЕРЕКРИВАЮТЬ ямл, тому показані окремим рядком: розмір із
    ямла сам по собі нікого не цікавить, якщо слот торгує іншим.
    `tick`/`price` — щоб перевести bps-пороги в тіки; без ціни просто не
    перекладаємо, замість того щоб вигадувати.
    """
    import dataclasses
    import datetime as _dt

    d, ex, ec = pc.detector, pc.exit_strategy, pc.execution
    shown: set[tuple[str, str]] = set()

    def row(section, obj, field, label=None, fmt=_cfg_val, suffix=""):
        shown.add((section, field))
        return f"  {(label or field):<28}{fmt(getattr(obj, field))}{suffix}"

    def in_ticks(bps: float) -> str:
        """bps → тіки за останньою відомою ціною."""
        if not bps or not tick or not price:
            return ""
        return f"  ≈{bps / 1e4 * price / tick:.1f}т"

    out: list[str] = []
    out.append(f"<b>⚙️ {symbol}</b>  {'🟢 LIVE' if live else '📊 SHADOW'}")
    hdr = f"<i>config/pairs/{symbol}.yaml</i>"
    if epoch_ts:
        _age = (_dt.datetime.now() - _dt.datetime.fromtimestamp(epoch_ts)).days
        hdr += (f"\n<i>тюнінг не мінявся з "
                f"{_dt.datetime.fromtimestamp(epoch_ts):%m-%d %H:%M}"
                f" ({_age}д)</i>")
    out.append(hdr)

    # ── ВХІД ──────────────────────────────────────────────────────────
    ent = [
        row("d", d, "min_ticks", fmt=_cfg_gate),
        row("d", d, "min_mid_gap_ticks", fmt=_cfg_gate),
        row("d", d, "max_mid_gap_ticks", fmt=_cfg_gate),
        row("d", d, "min_exec_ticks", fmt=_cfg_gate),
        row("d", d, "max_spread_ticks", fmt=_cfg_gate),
        row("d", d, "max_spread_bps", fmt=_cfg_gate,
            suffix=in_ticks(d.max_spread_bps)),
        row("c", ec, "min_mexc_lag_pct", fmt=_cfg_gate),
        row("c", ec, "max_mexc_lag_pct", fmt=_cfg_gate),
        row("d", d, "long_only"),
        row("d", d, "short_only"),
        row("d", d, "cooldown_sec", suffix="s"),
    ]
    out.append("<b>📥 ВХІД — ворота</b>\n<pre>" + "\n".join(ent) + "</pre>")

    # ── ВИКОНАННЯ ────────────────────────────────────────────────────
    ex_ = [
        row("c", ec, "ioc_offset_ticks"),
        row("c", ec, "ioc_max_attempts"),
        row("c", ec, "ioc_attempt_interval_ms", suffix="ms"),
    ]
    for sid, ov in (overrides or []):
        mn = ov.get("margin_min_usdt")
        mx = ov.get("margin_max_usdt")
        lo = ov.get("leverage_min")
        hi = ov.get("leverage_max")
        if mn is None and lo is None:
            continue
        mn = mn if mn is not None else ec.margin_min_usdt
        mx = mx if mx is not None else ec.margin_max_usdt
        lo = lo if lo is not None else ec.leverage_min
        hi = hi if hi is not None else ec.leverage_max
        # Розмір живе лише в БД (slot_pair_sizing); ямлових margin/leverage
        # більше немає. Показуємо ОБИДВА слоти (кожен акаунт) для пари, із
        # поміткою: хто реально торгує зараз / призначена-не-торгує / інша пара.
        if ov.get("_active"):
            mark = " ◀ торгує"
        elif ov.get("_assigned"):
            mark = " (призначена, не торгує)"
        else:
            mark = " (не на цій парі)"
        ex_.append(f"  {('слот ' + str(sid)):<28}${mn:g}-{mx:g} x {lo}-{hi}{mark}")
        ex_.append(f"  {'  → нотіонал':<28}${mn * lo:,.0f}-{mx * hi:,.0f}")
    out.append("<b>🎯 ВИКОНАННЯ</b>\n<pre>" + "\n".join(ex_) + "</pre>")

    # ── ВИХІД: пороги ────────────────────────────────────────────────
    def bps_row(field, tick_field, obj=ex):
        # bps=0 не означає «вимкнено» — воно падає на тіковий фолбек, і саме
        # це колись з'їло ворота мовчки. Тому показуємо, що реально діє.
        shown.add(("e", field))
        v = getattr(obj, field)
        if v and v > 0:
            return f"  {field:<28}{v:g} bps ({v / 100:.3f}%){in_ticks(v)}"
        return (f"  {field:<28}0 → діє {tick_field}="
                f"{_cfg_val(getattr(ex, tick_field))}т")

    th = [
        bps_row("stop_adverse_bps", "stop_adverse_ticks"),
        bps_row("trail_distance_bps", "trail_distance_ticks"),
        bps_row("breakeven_trigger_bps", "breakeven_trigger_ticks"),
        row("c", ec, "stop_loss_ticks", fmt=_cfg_gate),
        row("c", ec, "binance_reversal_ticks", fmt=_cfg_gate),
        row("c", ec, "gap_retrace_frac", fmt=_cfg_gate),
        row("e", ex, "nevergreen_cut_ms", fmt=_cfg_gate),
        row("e", ex, "nevergreen_adverse_ticks"),
        row("e", ex, "nevergreen_peak_ticks"),
        row("e", ex, "stop_adverse_ticks", "  фолбек: adverse_ticks"),
        row("e", ex, "trail_distance_ticks", "  фолбек: trail_ticks"),
        row("e", ex, "breakeven_trigger_ticks", "  фолбек: breakeven_ticks"),
    ]
    out.append("<b>📤 ВИХІД — пороги</b>\n<pre>" + "\n".join(th) + "</pre>")

    # ── ВИХІД: час ───────────────────────────────────────────────────
    tm = [
        row("e", ex, "min_hold_ms", suffix="ms"),
        row("e", ex, "stall_timeout_ms", fmt=_cfg_gate, suffix="ms"),
        row("e", ex, "dead_on_arrival_timeout_ms", fmt=_cfg_gate, suffix="ms"),
        row("e", ex, "binance_reversal_max_ms", fmt=_cfg_gate, suffix="ms"),
        row("c", ec, "sl_grace_sec", suffix="s"),
        row("c", ec, "max_hold_sec", fmt=_cfg_gate, suffix="s"),
        row("c", ec, "cooldown_after_loss_sec", suffix="s"),
        row("c", ec, "cooldown_after_win_sec", suffix="s"),
    ]
    out.append("<b>⏱ ВИХІД — час і кулдауни</b>\n<pre>" + "\n".join(tm) + "</pre>")

    # ── сторож повноти ───────────────────────────────────────────────
    # Будь-яке поле, не показане вище, з'являється тут. Це те, чого бракувало:
    # раніше новий ключ конфігу просто не потрапляв на екран.
    # GLOBAL-ONLY / dead knobs — never per-pair, hidden from this dump:
    _HIDE = {("d", "enabled"), ("d", "scan_interval_sec"),
             ("c", "momentum_filter"), ("c", "momentum_tau_sec"),
             ("c", "momentum_threshold_bps"),
             ("c", "margin_min_usdt"), ("c", "margin_max_usdt"),
             ("c", "leverage_min"), ("c", "leverage_max")}
    rest = []
    for tag, obj in (("d", d), ("e", ex), ("c", ec)):
        for f in dataclasses.fields(obj):
            if (tag, f.name) not in shown and (tag, f.name) not in _HIDE:
                rest.append(f"  {f.name:<28}{_cfg_val(getattr(obj, f.name))}")
    if rest:
        out.append("<b>❔ ІНШЕ (не згруповано)</b>\n<pre>"
                   + "\n".join(rest) + "</pre>")

    legend = "<i>ⓘ «—» = 0, ворота вимкнені</i>"
    if tick and price:
        legend += f"\n<i>ⓘ тік {tick:g}, ціна {price:g} → 1 тік = " \
                  f"{1e4 * tick / price:.2f} bps</i>"
    out.append(legend)
    return "\n".join(out)


async def cmd_get_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current pair_config for a symbol — full breakdown including
    adaptive params, mode, SL details, cooldowns."""
    if not context.args:
        await update.message.reply_text("Usage: <code>/get_config SYMBOL</code>",
                                        parse_mode=ParseMode.HTML)
        return

    symbol = _normalize_symbol(context.args[0])

    eng = context.bot_data["shadow_engine"]
    state_manager = context.bot_data["state_manager"]
    # SAME resolver the trading loop reads — NOT a parallel SELECT/parse. What
    # you see here is exactly what the bot acts on.
    if symbol not in eng._config_loader.list_pairs():
        await update.message.reply_text(
            f"❌ No <code>config/pairs/{symbol}.yaml</code>",
            parse_mode=ParseMode.HTML)
        return
    pc = eng._config_loader.get(symbol)

    # Mode = authoritative runtime state (pair_states.state), not pair_configs.mode.
    live = state_manager.is_in_live(symbol)

    # behavioral_epoch живе в meta ямла і не потрапляє в PairConfig (торгівля
    # його не читає) — беремо з файлу, м'яко.
    epoch_ts = None
    try:
        import yaml as _yaml
        from pathlib import Path as _Path
        _raw = _yaml.safe_load(
            _Path(f"/app/config/pairs/{symbol}.yaml").read_text(encoding="utf-8"))
        epoch_ts = int((_raw.get("meta") or {}).get("behavioral_epoch") or 0) or None
    except Exception:
        pass

    # Перекриття розміру зі slot_pair_sizing. Ямл — не те, чим торгують:
    # саме через це подвоєння маржі PENGU було невидиме тут.
    overrides: list[tuple[int, dict]] = []
    _store = context.bot_data.get("webkey_store")
    if _store is not None:
        try:
            from src.execution.webkey.credentials import MAX_SLOTS as _MS
            for _sid in range(1, _MS + 1):
                _slot = await _store.get(_sid)
                if _slot is None:
                    continue
                _ov = await _store.get_slot_pair_sizing(_sid, symbol)
                if not _ov:
                    continue
                _assigned = (_slot.assigned_pair == symbol)
                _ov = {**_ov, "_assigned": _assigned, "_active": bool(
                    _assigned
                    and getattr(_slot, "enabled", 0)
                    and getattr(_slot, "live_enabled", 0))}
                overrides.append((_sid, _ov))
        except Exception:
            logger.exception("[GET_CONFIG] не вдалось прочитати перекриття розміру")

    # Тік і остання відома ціна — щоб перевести bps-пороги в тіки. Поріг у bps
    # на фіксованому тіку повзе разом із ціною, тож без цього число оманливе.
    tick = price = None
    try:
        from src.exchanges.mexc_rest import get_binance_scale, to_mexc
        from src.execution.live_executor import get_tick_size
        _mx = to_mexc(symbol)
        tick = get_tick_size(_mx) * get_binance_scale(_mx)
    except Exception:
        tick = None
    for _db, _tbl in ((context.bot_data.get("live_db"), "live_trades"),
                      (context.bot_data.get("db"), "shadow_trades")):
        if price is not None or _db is None:
            continue
        try:
            _r = await _db.fetchone(
                f"SELECT binance_price_at_entry FROM {_tbl} WHERE symbol = ? "
                "AND binance_price_at_entry > 0 ORDER BY id DESC LIMIT 1",
                (symbol,))
            if _r and _r[0]:
                price = float(_r[0])
        except Exception:
            pass

    await update.message.reply_text(
        _fmt_pair_config_full(symbol, pc, live=live, epoch_ts=epoch_ts,
                              overrides=overrides, tick=tick, price=price),
        parse_mode=ParseMode.HTML)


async def cmd_kill_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ YES, kill", callback_data="m:kill_all:yes"),
        InlineKeyboardButton("❌ Cancel", callback_data="m:cancel"),
    ]])
    await update.message.reply_text(
        "⚠️ <b>Confirm kill_all</b>\n\nClose ALL open positions and DISABLE trading?",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )


async def cmd_unkill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Release the automatic peak-drawdown halt on all live slots. The kill
    engages by itself but otherwise only clears on a restart — this lifts it
    without one, and hands each slot its full drawdown allowance back. It does NOT re-open positions or
    re-enable a manually disabled slot; it only clears the safety halt so the
    slot can trade live again."""
    live_pool = context.bot_data.get("live_pool")
    if live_pool is None:
        await update.message.reply_text("⚪ Live pool не активний — kill-switch немає де знімати.")
        return
    released: list[int] = []
    for sid in sorted(getattr(live_pool, "_safety_controllers", {})):
        safety = live_pool.get_safety(sid)
        if safety is not None and safety.is_killed():
            # Через пул, а не напряму через контролер: пул ще й ЗБЕРІГАЄ факт
            # зняття, інакше найближчий рестарт відновить дорелізний пік і
            # ввімкне кіл назад.
            if (await live_pool.release_kill(sid))[0]:
                released.append(sid)
    # 💀 KILL ALL flips shadow_engine.cfg.enabled and nothing else ever flips it
    # back — the engine stayed dead until the container restarted, while this
    # command happily answered "no kill active". Undo it here too.
    engine = context.bot_data.get("shadow_engine")
    engine_was_off = engine is not None and not engine.cfg.enabled
    if engine_was_off:
        engine.cfg.enabled = True
        logger.warning("KILL ALL undone via /unkill — signal processing resumed")
    if engine_was_off:
        await update.message.reply_text(
            "▶️ <b>Торгівлю відновлено</b> — знято 💀 KILL ALL"
            + (f"\nSafety-халт знято на слот(ах): "
               + ", ".join(f"#{s}" for s in released) if released else "")
            + "\n<i>Позиції НЕ відкриваються самі — бот знову обробляє сигнали.</i>",
            parse_mode=ParseMode.HTML)
        return
    if released:
        msg = (
            "♻️ <b>Kill-switch ЗНЯТО</b> на слот(ах): "
            + ", ".join(f"#{s}" for s in released)
            + "\n<i>Халт знято. Позиції НЕ відкриваються, слот НЕ вмикається — "
            "лише safety-халт.</i>"
        )
    else:
        msg = "✅ Активного kill-switch на жодному слоті немає."
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def keyboard_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle text messages from persistent reply keyboard.
    Maps button text to corresponding command handler.
    """
    if not update.message or not update.message.text:
        return

    # Webkey wizard takes priority — if a setup/remove flow is in progress,
    # the user's free-text message is consumed by it.
    if await webkey_text_handler(update, context):
        return

    # Sizing wizard — when user tapped "Edit margin" / "Edit leverage",
    # the next text reply must be parsed as a range and applied. Wizard
    # state lives in context.user_data["sizing_wizard"].
    from src.telegram_bot.cmd_sizing import is_in_wizard, handle_wizard_text
    if is_in_wizard(context):
        await handle_wizard_text(update, context, update.message.text)
        return

    text = update.message.text.strip()

    # Map button text to command function
    if text == "📊 Menu":
        await cmd_menu(update, context)
        return
    if text == "📋 Trades Today":
        # Reuse callback helper via fake query-like wrapper
        class FakeQuery:
            def __init__(self, msg):
                self.message = msg
        await _send_trades_today(FakeQuery(update.message), context)
        return
    if text == "🔴 Live PnL":
        await _send_live_pnl(update, context)
        return
    if text == "🧪 Shadow PnL":
        await _send_shadow_pnl(update, context)
        return
    if text == "🔔 Signals":
        class FakeQuery:
            def __init__(self, msg):
                self.message = msg
        await _send_signals_now(FakeQuery(update.message), context)
        return
    if text == "💵 Balance":
        await cmd_balance(update, context)
        return
    if text == "❓ Help":
        await cmd_help(update, context)
        return
    if text == "🔑 Webkey":
        # Show overview with all slots + nav buttons
        from src.telegram_bot.cmd_webkey import _fmt_overview, _store as _wk_store
        store = _wk_store(context)
        slots = await store.list_all()
        kb = _kb_webkey_overview(slots)
        overview_text = _fmt_overview(slots)
        try:
            await update.message.reply_text(
                overview_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
            )
        except Exception as e:
            if "parse entities" in str(e).lower():
                logger.warning("Webkey overview: Markdown parse error, sending plain")
                await update.message.reply_text(
                    overview_text,
                    parse_mode=None,
                    reply_markup=kb,
                )
            else:
                raise
        return

    # Unknown text — ignore silently (don't spam user)


# ============================================================
# Callback router
# ============================================================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    state_manager = context.bot_data["state_manager"]
    shadow_engine = context.bot_data["shadow_engine"]

    try:
        # ============ MENU actions ============
        if data == "m:status":
            await _send_status_text(query, context)
            return

        if data == "m:trades_today":
            await _send_trades_today(query, context)
            return

        if data == "m:signals":
            await _send_signals_now(query, context)
            return

        if data == "m:balance":
            # Reuse cmd_balance logic — wrap query into a message-like object
            class FakeUpdate:
                def __init__(self, msg):
                    self.message = msg
            await cmd_balance(FakeUpdate(query.message), context)
            return

        # Multi-slot pair assignment UI
        if data.startswith("m:slot:"):
            await handle_slot_callback(query, context, data)
            return

        # Per-pair sizing wizard (margin/leverage editor)
        if data.startswith("m:siz:"):
            from src.telegram_bot.cmd_sizing import handle_sizing_callback
            await handle_sizing_callback(query, context, data)
            return

        # Per-pair shadow toggle (shadow ↔ paused)
        if data.startswith("m:shp:"):
            from src.telegram_bot.cmd_shadow_pairs import handle_shadow_pairs_callback
            await handle_shadow_pairs_callback(query, context, data)
            return

        if data == "m:shadow:off":
            # Pause all shadow pairs (live unaffected). Added by shadow-toggle patch.
            from src.state.pair_state import SHADOW
            shadow_pairs = [
                ps.symbol for ps in state_manager.all_states().values()
                if ps.state == SHADOW
            ]
            paused_count = 0
            for sym in shadow_pairs:
                ok = await state_manager.manual_pause(
                    sym,
                    duration_sec=None,  # persistent — no auto-resume
                    reason="manual: shadow toggle off",
                )
                if ok:
                    paused_count += 1
            await query.message.reply_text(
                f"🌙 <b>Shadow PAUSED</b>\n{paused_count} shadow pair(s) paused indefinitely. Live pairs unaffected.\n\nUse '📊 Shadow ON' to resume.",
                parse_mode=ParseMode.HTML)
            return

        if data == "m:shadow:on":
            # Resume only pairs that were paused via shadow toggle
            from src.state.pair_state import PAUSED
            paused_by_toggle = [
                ps.symbol for ps in state_manager.all_states().values()
                if ps.state == PAUSED and (ps.pause_reason or "") == "manual: shadow toggle off"
            ]
            resumed_count = 0
            for sym in paused_by_toggle:
                ok = await state_manager.manual_resume(sym, reason="manual: shadow toggle on")
                if ok:
                    resumed_count += 1
            await query.message.reply_text(
                f"📊 <b>Shadow RESUMED</b>\n{resumed_count} shadow pair(s) reactivated. Other paused pairs (e.g. manual pauses) remain paused.",
                parse_mode=ParseMode.HTML)
            return

        if data == "m:kill_all:confirm":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ YES kill", callback_data="m:kill_all:yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="m:cancel"),
            ]])
            await query.edit_message_text(
                "⚠️ Close ALL open positions and DISABLE trading?",
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )
            return

        if data == "m:kill_all:yes":
            shadow_engine.cfg.enabled = False
            closed = 0
            for symbol, positions in list(shadow_engine._open_positions.items()):
                for pos in list(positions):
                    try:
                        await shadow_engine._close_position(pos, reason="kill_switch")
                        closed += 1
                    except Exception as e:
                        logger.exception("kill failed: %s", e)
            alerts = context.bot_data.get("alerts")
            if alerts:
                await alerts.kill_switch_triggered(reason=f"manual ({closed} pos)")
            await query.edit_message_text(
                f"💀 <b>KILL SWITCH</b>\nClosed: <b>{closed}</b> positions",
                parse_mode=ParseMode.HTML,
            )
            return

        if data == "m:cancel":
            try:
                await query.edit_message_text("❌ Cancelled")
            except Exception:
                pass
            return

        if data == "m:back":
            # User pressed "« Back to menu" — show main menu in place
            try:
                await query.edit_message_text("Дії:", reply_markup=_kb_main_menu())
            except Exception:
                # Fall back to a fresh message if the original is too old to edit
                await query.message.reply_text("Дії:", reply_markup=_kb_main_menu())
            return

        # ============ WEBKEY actions ============
        if data == "m:webkey:menu":
            await _send_webkey_menu(query, context, edit=True)
            return

        # m:webkey:slot:N — open per-slot detail screen
        if data.startswith("m:webkey:slot:"):
            try:
                sid = int(data.split(":", 3)[3])
            except (ValueError, IndexError):
                return
            await _send_webkey_slot(query, context, sid, edit=True)
            return

        # m:webkey:setup or m:webkey:setup:N — start wizard
        if data.startswith("m:webkey:setup"):
            from src.telegram_bot.cmd_webkey import (
                STEP_WEBKEY, _enter_step, _store as _wk_store,
            )
            store = _wk_store(context)
            parts = data.split(":")
            sid = None
            if len(parts) >= 4:
                try:
                    sid = int(parts[3])
                except ValueError:
                    sid = None
            if sid is None:
                sid = await store.first_empty_slot()
                if sid is None:
                    await query.message.reply_text(
                        "All slots taken. Pick a slot to overwrite from the slot list, or use /webkey_remove.",
                    )
                    return
            _enter_step(query.from_user.id, STEP_WEBKEY, slot_id=sid)
            await query.message.reply_text(
                f"🔧 <b>Slot {sid} setup</b> — <b>webkey</b>\n\n"
                "Встав webkey як <b>наступне повідомлення</b>.\n\n"
                "Формат: <code>WEB</code> + 64 hex chars (67 total).\n"
                "Де: Chrome → mexc.com → DevTools → Application → "
                "Cookies → <code>u_id</code>.\n\n"
                "<i>Повідомлення видалиться після збереження.</i>\n"
                "/webkey_cancel щоб скасувати. ⏳",
                parse_mode=ParseMode.HTML,
            )
            return

        # m:webkey:test:N — single slot test
        if data.startswith("m:webkey:test:") and not data.startswith("m:webkey:test_"):
            try:
                sid = int(data.split(":", 3)[3])
            except (ValueError, IndexError):
                return
            await _run_webkey_test(query, context, sid)
            return

        if data == "m:webkey:test_all":
            await _run_webkey_test_all(query, context)
            return

        # m:webkey:enable:N
        if data.startswith("m:webkey:enable:"):
            from src.telegram_bot.cmd_webkey import _store as _wk_store
            try:
                sid = int(data.split(":", 3)[3])
            except (ValueError, IndexError):
                return
            store = _wk_store(context)
            slot = await store.get(sid)
            if slot is None or slot.is_empty:
                await query.message.reply_text(f"Slot {sid}: empty.")
                return
            # v6.1: proxy no longer required to enable.
            try:
                await store.set_enabled(sid, True)
            except WebkeyError as e:
                await query.message.reply_text(f"❌ {e}")
                return
            await query.message.reply_text(
                f"✅ Slot {sid} <b>enabled</b>.",
                parse_mode=ParseMode.HTML,
            )
            await _send_webkey_slot(query, context, sid, edit=False)
            return

        # m:webkey:disable:N
        if data.startswith("m:webkey:disable:"):
            from src.telegram_bot.cmd_webkey import _store as _wk_store
            try:
                sid = int(data.split(":", 3)[3])
            except (ValueError, IndexError):
                return
            await _wk_store(context).set_enabled(sid, False)
            await query.message.reply_text(
                f"⛔️ Slot {sid} <b>disabled</b>.",
                parse_mode=ParseMode.HTML,
            )
            await _send_webkey_slot(query, context, sid, edit=False)
            return

        # m:webkey:remove:N
        if data.startswith("m:webkey:remove:") and not data.endswith(":yes"):
            try:
                sid = int(data.split(":", 3)[3])
            except (ValueError, IndexError):
                return
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ YES, remove", callback_data=f"m:webkey:remove:{sid}:yes"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"m:webkey:slot:{sid}"),
            ]])
            await query.edit_message_text(
                f"⚠️ <b>Remove slot {sid}?</b>\n\n"
                "Це видалить webkey, dolos, proxy цього слоту. Незворотньо.",
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )
            return

        if data.startswith("m:webkey:remove:") and data.endswith(":yes"):
            from src.telegram_bot.cmd_webkey import _store as _wk_store
            parts = data.split(":")
            try:
                sid = int(parts[3])
            except (ValueError, IndexError):
                return
            deleted = await _wk_store(context).delete(sid)
            await query.edit_message_text(
                f"🗑 Slot {sid}: cleared." if deleted else f"Slot {sid}: nothing to delete.",
            )
            return

        # ============ PAIR-SPECIFIC actions ============
        if data.startswith("p:"):
            parts = data.split(":")
            action = parts[1]

            if action == "refresh":
                symbol = parts[2]
                await _send_pair_status(query, context, symbol, edit=True)
                return

            if action == "promote_live":
                symbol = parts[2]
                ok = await state_manager.manual_promote(symbol, target="live",
                                                       reason="manual button")
                await query.message.reply_text(
                    f"{'✅' if ok else '❌'} <code>{symbol}</code> "
                    f"{'→ LIVE' if ok else 'failed'}",
                    parse_mode=ParseMode.HTML,
                )
                return

            if action == "demote":
                symbol = parts[2]
                ok = await state_manager.manual_promote(symbol, target="shadow",
                                                       reason="manual demote button")
                await query.message.reply_text(
                    f"{'⬇️' if ok else '❌'} <code>{symbol}</code> "
                    f"{'→ shadow' if ok else 'failed'}",
                    parse_mode=ParseMode.HTML,
                )
                return

            if action == "pause":
                symbol = parts[2]
                ok = await state_manager.manual_pause(symbol, duration_sec=12*3600,
                                                     reason="manual button")
                await query.message.reply_text(
                    f"{'⏸' if ok else '❌'} <code>{symbol}</code> paused 12h",
                    parse_mode=ParseMode.HTML,
                )
                return

            if action == "resume":
                symbol = parts[2]
                ok = await state_manager.manual_resume(symbol, reason="manual button")
                await query.message.reply_text(
                    f"{'▶️' if ok else '❌'} <code>{symbol}</code> resumed",
                    parse_mode=ParseMode.HTML,
                )
                return

            if action == "kill":
                symbol = parts[2]
                positions = shadow_engine._open_positions.get(symbol, [])
                if not positions:
                    await query.message.reply_text(
                        f"📭 No open positions on <code>{symbol}</code>",
                        parse_mode=ParseMode.HTML)
                    return
                closed = 0
                for pos in list(positions):
                    try:
                        await shadow_engine._close_position(pos, reason="kill_pair_button")
                        closed += 1
                    except Exception as e:
                        logger.exception("kill failed: %s", e)
                await query.message.reply_text(
                    f"💀 Closed <b>{closed}</b> on <code>{symbol}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

        logger.warning("Unknown callback data: %s", data)
        await query.message.reply_text(f"❓ Unknown: <code>{data}</code>",
                                       parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.exception("Callback error: %s", e)
        try:
            await query.message.reply_text(f"❌ Error: {str(e)[:200]}")
        except Exception:
            pass


# ============================================================
# Helpers
# ============================================================

async def _send_status_text(query, context) -> None:
    state_manager = context.bot_data["state_manager"]
    states = state_manager.all_states()
    groups: dict[str, list] = {}
    for ps in states.values():
        groups.setdefault(ps.state, []).append(ps)
    lines = ["<b>📊 Status</b>"]
    for state in ("live", "shadow", "paused"):
        items = groups.get(state, [])
        if not items: continue
        lines.append(f"\n{_state_emoji(state)} <b>{state.upper()}</b> ({len(items)})")
        items.sort(key=lambda x: x.last_24h_pnl, reverse=True)
        for ps in items[:8]:
            wr = f"{ps.last_24h_winrate*100:.0f}%" if ps.last_24h_trades > 0 else "—"
            lines.append(
                f"  <code>{ps.symbol:<10}</code> "
                f"{ps.last_24h_trades}t WR={wr} "
                f"PnL=<b>${ps.last_24h_pnl:+.2f}</b>"
            )
    await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def slot_no(label: Any) -> str:
    """'slot2' / 2 / '2' -> '2'.  None or unparseable -> '?'.

    Live rows carry the slot as a STRING in `live_trades.account_label`
    ('slot1'), while `live_open_misses` carries it as an INT in `slot_id`.
    Both are funnelled through here so the two can be keyed together.
    Never returns None: an unlabelled trade must still be SHOWN (under '?'),
    because silently dropping it would understate a slot's PnL.
    """
    if label is None:
        return "?"
    m = re.search(r"(\d+)", str(label))
    return m.group(1) if m else "?"


def render_live_section(title: str, rows, expired_map: dict) -> list[str]:
    """LIVE, split per slot. `expired_map` is keyed (symbol, slot)."""
    n = sum(r["n"] for r in rows)
    if n == 0:
        return [f"{title} — <i>none</i>"]
    pnl = sum((r["pnl"] or 0) for r in rows)
    wins = sum((r["wins"] or 0) for r in rows)
    tot_exp = sum(expired_map.get((r["symbol"], slot_no(r["account_label"])), 0)
                  for r in rows)
    sec_exp = tot_exp / (n + tot_exp) * 100 if (n + tot_exp) else 0
    out = [f"{title} ({n} trades, WR={wins / n * 100:.1f}%, "
           f"exp={sec_exp:.0f}%, PnL=<b>${pnl:+.3f}</b>)"]

    by_slot: dict[str, list] = {}
    for r in rows:
        by_slot.setdefault(slot_no(r["account_label"]), []).append(r)

    # '?' (unlabelled) sorts last so real slots read first.
    for slot in sorted(by_slot, key=lambda s: (s == "?", s)):
        srows = by_slot[slot]
        sn = sum(r["n"] for r in srows)
        spnl = sum((r["pnl"] or 0) for r in srows)
        swins = sum((r["wins"] or 0) for r in srows)
        se = "🟢" if spnl > 0 else "🔴" if spnl < 0 else "➖"
        name = f"slot {slot}" if slot != "?" else "unlabelled"
        out.append(f"  {se} <b>{name}</b> — {sn} trades, "
                   f"WR={swins / sn * 100:.0f}%, PnL=<b>${spnl:+.3f}</b>")
        for r in srows:
            p = r["pnl"] or 0
            rwr = r["wins"] / r["n"] * 100 if r["n"] else 0
            ex = expired_map.get((r["symbol"], slot), 0)
            att = r["n"] + ex
            epct = ex / att * 100 if att else 0
            e = "🟢" if p > 0 else "🔴" if p < 0 else "➖"
            out.append(
                f"     {e} <code>{r['symbol']:<10}</code> n={r['n']:<3} "
                f"WR={rwr:>3.0f}% exp={epct:>3.0f}% PnL=<b>${p:+.3f}</b>"
            )
    return out



async def _send_trades_today(query, context) -> None:
    # Live and Shadow trades live in separate DBs: live → live_db.live_trades,
    # shadow → db.shadow_trades. Show them as two separate sections.
    #
    # LIVE is broken down PER SLOT: the same pair can be assigned to slot 1 and
    # slot 2 at once, and summing them into one row hides which account made or
    # lost the money. (shadow_engine's heartbeat already keys by
    # (symbol, account_label) for the same reason — see the note there.)
    # SHADOW is NOT split: shadow trades are not slot-bound, `account_label` is
    # NULL on every one of them and `shadow_open_misses` has no slot column.
    db = context.bot_data["db"]
    live_db = context.bot_data.get("live_db")
    today_start = int(datetime.now(ZoneInfo("Europe/Kyiv")).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    SQL = (
        "SELECT symbol, COUNT(*) as n, "
        "SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) as wins, "
        "SUM(net_pnl_usdt) as pnl "
        "FROM {table} WHERE closed_at >= ? AND closed_at IS NOT NULL "
        "GROUP BY symbol ORDER BY pnl DESC"
    )
    LIVE_SQL = (
        "SELECT symbol, account_label, COUNT(*) as n, "
        "SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) as wins, "
        "SUM(net_pnl_usdt) as pnl "
        "FROM live_trades WHERE closed_at >= ? AND closed_at IS NOT NULL "
        "GROUP BY symbol, account_label ORDER BY pnl DESC"
    )

    def _section(title: str, rows, expired_map: dict) -> list[str]:
        n = sum(r["n"] for r in rows)
        if n == 0:
            return [f"{title} — <i>none</i>"]
        pnl = sum((r["pnl"] or 0) for r in rows)
        wins = sum((r["wins"] or 0) for r in rows)
        wr = wins / n * 100 if n else 0
        # IOC-expired % = expired / (filled + expired), section + per pair.
        tot_exp = sum(expired_map.get(r["symbol"], 0) for r in rows)
        sec_exp = tot_exp / (n + tot_exp) * 100 if (n + tot_exp) else 0
        out = [f"{title} ({n} trades, WR={wr:.1f}%, exp={sec_exp:.0f}%, PnL=<b>${pnl:+.3f}</b>)"]
        for r in rows:
            p = r["pnl"] or 0
            rwr = r["wins"] / r["n"] * 100 if r["n"] else 0
            ex = expired_map.get(r["symbol"], 0)
            att = r["n"] + ex
            epct = ex / att * 100 if att else 0
            e = "🟢" if p > 0 else "🔴" if p < 0 else "➖"
            out.append(
                f"  {e} <code>{r['symbol']:<10}</code> n={r['n']:<3} "
                f"WR={rwr:>3.0f}% exp={epct:>3.0f}% PnL=<b>${p:+.3f}</b>"
            )
        return out

    shadow_rows = await db.fetchall(SQL.format(table="shadow_trades"), (today_start,))
    live_rows = []
    if live_db is not None:
        try:
            live_rows = await live_db.fetchall(LIVE_SQL, (today_start,))
        except Exception:
            logger.debug("live trades query failed", exc_info=True)

    if sum(r["n"] for r in shadow_rows) == 0 and sum(r["n"] for r in live_rows) == 0:
        await query.message.reply_text("📭 No trades today.")
        return

    # IOC-expired counts per pair today: live → live_open_misses,
    # shadow → shadow_open_misses. exp% shown = expired / (filled + expired).
    _EXP_SQL = ("SELECT symbol, COUNT(*) c FROM {t} "
                "WHERE ts >= ? AND reason='ioc_expired_no_fill' GROUP BY symbol")
    # live_open_misses HAS a slot_id, so live exp% can be attributed per slot.
    _LIVE_EXP_SQL = ("SELECT symbol, slot_id, COUNT(*) c FROM live_open_misses "
                     "WHERE ts >= ? AND reason='ioc_expired_no_fill' "
                     "GROUP BY symbol, slot_id")

    async def _exp_map(conn, table):
        try:
            rows = await conn.fetchall(_EXP_SQL.format(t=table), (today_start,))
            return {r["symbol"]: r["c"] for r in rows}
        except Exception:
            return {}

    async def _live_exp_map(conn):
        try:
            rows = await conn.fetchall(_LIVE_EXP_SQL, (today_start,))
            return {(r["symbol"], slot_no(r["slot_id"])): r["c"] for r in rows}
        except Exception:
            logger.debug("live misses query failed", exc_info=True)
            return {}

    live_exp = await _live_exp_map(live_db) if live_db is not None else {}
    shadow_exp = await _exp_map(db, "shadow_open_misses")

    lines = ["<b>📋 Today</b>  <i>(exp = IOC expired %)</i>", ""]
    lines += render_live_section("🔴 <b>LIVE</b>", live_rows, live_exp)
    lines.append("")
    lines += _section("⚡ <b>SHADOW</b>", shadow_rows, shadow_exp)
    await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _send_signals_now(query, context) -> None:
    db = context.bot_data["db"]
    rows = await db.fetchall(
        """SELECT symbol, direction, source, confidence, mexc_lag_pct,
                  datetime(created_at,'unixepoch') as ts
           FROM signals ORDER BY id DESC LIMIT 10"""
    )
    if not rows:
        await query.message.reply_text("📭 No signals.")
        return
    lines = ["<b>🔔 Last 10 signals:</b>\n"]
    for r in rows:
        arrow = "🔼" if r["direction"] == "long" else "🔽"
        lines.append(
            f"  <i>{r['ts']}</i> {arrow} <code>{r['symbol']:<10}</code> "
            f"{r['source']:<10} c={r['confidence']:.2f}"
        )
    await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _send_webkey_menu(query, context, *, edit: bool) -> None:
    """Render the multi-slot overview screen."""
    from src.telegram_bot.cmd_webkey import _fmt_overview, _store as _wk_store
    store = _wk_store(context)
    slots = await store.list_all()

    text = _fmt_overview(slots)
    kb = _kb_webkey_overview(slots)

    # Try Markdown first, fallback to plain text on parse error.
    async def _try_send(use_markdown: bool):
        pm = ParseMode.MARKDOWN if use_markdown else None
        if edit:
            try:
                await query.edit_message_text(text, parse_mode=pm, reply_markup=kb)
                return True
            except Exception as e:
                if "parse entities" in str(e).lower() and use_markdown:
                    return False
        await query.message.reply_text(text, parse_mode=pm, reply_markup=kb)
        return True

    if not await _try_send(use_markdown=True):
        logger.warning("Webkey menu: Markdown parse error, falling back to plain")
        await _try_send(use_markdown=False)


async def _send_webkey_slot(query, context, slot_id: int, *, edit: bool) -> None:
    """Render a single-slot detail screen with its action submenu."""
    from src.telegram_bot.cmd_webkey import _fmt_slot_detail, _store as _wk_store
    store = _wk_store(context)
    slot = await store.get(slot_id)
    if slot is None:
        await query.message.reply_text(f"Slot {slot_id}: not found.")
        return

    text = _fmt_slot_detail(slot)
    kb = _kb_webkey_slot(slot)

    # Try Markdown first, fallback to plain text on parse error.
    # Markdown is fragile when text contains special chars (_, *, [, ], etc).
    async def _try_send(use_markdown: bool):
        pm = ParseMode.MARKDOWN if use_markdown else None
        if edit:
            try:
                await query.edit_message_text(text, parse_mode=pm, reply_markup=kb)
                return True
            except Exception as e:
                if "parse entities" in str(e).lower() and use_markdown:
                    return False  # try plain
                # Other error (message not modified, etc) — fall through to reply
        await query.message.reply_text(text, parse_mode=pm, reply_markup=kb)
        return True

    if not await _try_send(use_markdown=True):
        # Markdown failed, try plain text
        logger.warning("Webkey slot %d: Markdown parse error, falling back to plain", slot_id)
        await _try_send(use_markdown=False)


async def _run_webkey_test(query, context, slot_id: int) -> None:
    """Inline-button health check for a specific slot."""
    from src.telegram_bot.cmd_webkey import (
        _run_one_health_check, _store as _wk_store, _client_pool as _wk_pool,
    )
    store = _wk_store(context)
    pool = _wk_pool(context)
    slot = await store.get(slot_id)
    if slot is None or slot.is_empty:
        await query.message.reply_text(f"Slot {slot_id}: empty.")
        return
    # v6.1: proxy no longer required for test.

    msg = await query.message.reply_text(f"⏳ Testing slot {slot_id}…")
    result = await _run_one_health_check(store, slot, pool=pool)
    await msg.edit_text(
        f"🩺 *Slot {slot_id}*\n{result}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _run_webkey_test_all(query, context) -> None:
    """Inline-button health check for all configured slots in parallel."""
    import asyncio
    from src.telegram_bot.cmd_webkey import (
        _run_one_health_check, _store as _wk_store, _client_pool as _wk_pool,
    )
    store = _wk_store(context)
    pool = _wk_pool(context)
    # v6.1: proxy no longer required.
    slots = [s for s in await store.list_all() if s.is_complete]

    if not slots:
        await query.message.reply_text(
            "No slots ready for testing (need webkey).",
        )
        return

    msg = await query.message.reply_text(
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


# ============================================================
# Bot main class
# ============================================================

class StakanTelegramBot:
    def __init__(self, token: str, owner_id: int, db: Database,
                 state_manager: PairStateManager, shadow_engine: ShadowEngine,
                 alerts: TelegramAlerts, daily_report_hour_utc: int = 6,
                 webkey_store: Any = None,
                 webkey_client_pool: Any = None,
                 live_pool: Any = None,
                 live_db: Any = None) -> None:
        self.token = token
        self.owner_id = owner_id
        self.db = db
        self.live_db = live_db
        self.state_manager = state_manager
        self.shadow_engine = shadow_engine
        self.alerts = alerts
        self.daily_report_hour_utc = daily_report_hour_utc
        self.webkey_store = webkey_store
        self.webkey_client_pool = webkey_client_pool
        self.live_pool = live_pool
        self.application: Application | None = None
        self.report_scheduler: ReportScheduler | None = None

    async def _post_init(self, app: Application) -> None:
        """Run once after Application is initialized (BEFORE polling starts)."""
        try:
            commands = [
                BotCommand("menu",        "🎛 Головне меню"),
                BotCommand("status",      "📊 Overview всіх пар"),
                BotCommand("pair_status", "🔍 Деталі пари + кнопки"),
                BotCommand("get_config",  "⚙️ Конфіг пари (yaml)"),
                BotCommand("slot",        "🎰 Слоти / live-керування"),
                BotCommand("balance",     "💰 Баланс"),
                BotCommand("kill_all",    "💀 Emergency stop"),
                BotCommand("unkill",      "♻️ Зняти kill-switch"),
                BotCommand("help",        "❓ Допомога"),
                BotCommand("webkey",             "🔑 Webkey overview"),
                BotCommand("webkey_setup",       "🔧 Setup wizard (paste webkey)"),
                BotCommand("webkey_test",        "🩺 Health check"),
                BotCommand("webkey_enable",      "✅ Enable webkey"),
                BotCommand("webkey_disable",     "⛔️ Disable webkey"),
                BotCommand("webkey_remove",      "🗑 Remove webkey"),
                BotCommand("webkey_label",       "🏷 Label webkey"),
            ]
            await app.bot.set_my_commands(
                commands,
                scope=BotCommandScopeChat(chat_id=self.owner_id),
            )
            await app.bot.set_my_commands(commands)
            logger.info("Telegram commands registered (chat + default scope): %d commands",
                       len(commands))
        except Exception as e:
            logger.warning("set_my_commands failed: %s", e)

    def _register_handlers(self, app: Application) -> None:
        auth = filters.User(user_id=self.owner_id)
        app.add_handler(CommandHandler("menu",        cmd_menu,        filters=auth))
        app.add_handler(CommandHandler("status",      cmd_status,      filters=auth))
        app.add_handler(CommandHandler("pair_status", cmd_pair_status, filters=auth))
        app.add_handler(CommandHandler("get_config",  cmd_get_config,  filters=auth))
        app.add_handler(CommandHandler("kill_all",    cmd_kill_all,    filters=auth))
        app.add_handler(CommandHandler("unkill",      cmd_unkill,      filters=auth))
        app.add_handler(CommandHandler("balance",     cmd_balance,     filters=auth))
        app.add_handler(CommandHandler("slot",        cmd_slot,        filters=auth))
        app.add_handler(CommandHandler("help",        cmd_help,        filters=auth))
        app.add_handler(CommandHandler("start",       cmd_help,        filters=auth))
        # Webkey lifecycle (Phase 2)
        app.add_handler(CommandHandler("webkey",            cmd_webkey_status,      filters=auth))
        app.add_handler(CommandHandler("webkey_setup",      cmd_webkey_setup,       filters=auth))
        app.add_handler(CommandHandler("webkey_test",       cmd_webkey_test,        filters=auth))
        app.add_handler(CommandHandler("webkey_enable",     cmd_webkey_enable,      filters=auth))
        app.add_handler(CommandHandler("webkey_disable",    cmd_webkey_disable,     filters=auth))
        app.add_handler(CommandHandler("webkey_remove",     cmd_webkey_remove,      filters=auth))
        app.add_handler(CommandHandler("webkey_cancel",     cmd_webkey_cancel,      filters=auth))
        app.add_handler(CommandHandler("webkey_label",      cmd_webkey_label,       filters=auth))
        # Inline keyboard buttons
        app.add_handler(CallbackQueryHandler(callback_router))
        # Persistent reply keyboard buttons (text messages)
        app.add_handler(MessageHandler(
            auth & filters.TEXT & ~filters.COMMAND,
            keyboard_text_handler,
        ))

    async def _on_handler_error(self, update, context) -> None:
        """Логувати і жити далі. НІКОЛИ не піднімати виняток звідси.

        Це остання лінія: усе, що не спіймали хендлери, приходить сюди. Якщо
        цей метод сам кине — PTB знову покладе процес, тож він обгорнутий.
        """
        try:
            logger.error("Telegram handler failed (update=%s): %s",
                         getattr(update, "update_id", "?"),
                         context.error, exc_info=context.error)
        except Exception:
            pass

    async def start(self) -> None:
        self.application = (
            Application.builder()
            .token(self.token)
            .post_init(self._post_init)
            .build()
        )
        self.application.bot_data["db"] = self.db
        self.application.bot_data["live_db"] = self.live_db
        self.application.bot_data["state_manager"] = self.state_manager
        self.application.bot_data["shadow_engine"] = self.shadow_engine
        self.application.bot_data["alerts"] = self.alerts
        if self.webkey_store is not None:
            self.application.bot_data["webkey_store"] = self.webkey_store
        if self.webkey_client_pool is not None:
            self.application.bot_data["webkey_client_pool"] = self.webkey_client_pool
        if self.live_pool is not None:
            self.application.bot_data["live_pool"] = self.live_pool

        self._register_handlers(self.application)

        # ГЛОБАЛЬНИЙ ОБРОБНИК ПОМИЛОК — його не було ЗОВСІМ до 2026-09-04.
        #
        # Без нього виняток із будь-якого хендлера йшов угору через
        # `Application._update_fetcher` і клав `asyncio.run(main())`, тобто
        # ВЕСЬ торговий бот. Спіймано на живому: незловлений
        # `httpx.ConnectTimeout` до `api.telegram.org` із `reply_text`.
        # Мережевий таймаут до Telegram не має зупиняти торгівлю.
        self.application.add_error_handler(self._on_handler_error)

        await self.application.initialize()
        # We drive the lifecycle manually (initialize/start/start_polling) instead
        # of run_polling(), and PTB only invokes the builder's post_init hook from
        # run_polling/run_webhook — so it never ran here (the "/" command menu was
        # never registered). Call it explicitly.
        await self._post_init(self.application)
        await self.application.start()
        await self.application.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )

        self.report_scheduler = ReportScheduler(
            db=self.db, alerts=self.alerts,
            daily_report_hour_utc=self.daily_report_hour_utc,
        )
        await self.report_scheduler.start()
        logger.info("Telegram bot ready (owner=%d)", self.owner_id)

        # ВАЖЛИВО: startup alert ПІСЛЯ полінгу, з retry
        # це гарантує що бот реально готовий приймати команди
        import asyncio as _asyncio
        for attempt in range(3):
            try:
                await self.alerts.startup(
                    state_manager=self.state_manager,
                    db=self.db,
                )
                logger.info("Startup alert sent successfully")
                break
            except Exception as e:
                logger.warning("Startup alert attempt %d/3 failed: %s", attempt + 1, e)
                if attempt < 2:
                    await _asyncio.sleep(2)
                else:
                    logger.error("All startup alert attempts failed — bot is running but no notification sent")

    async def stop(self) -> None:
        if self.report_scheduler:
            await self.report_scheduler.stop()
        if self.application:
            try:
                await self.alerts.shutdown()
            except Exception:
                pass
            try:
                if self.application.updater.running:
                    await self.application.updater.stop()
                if self.application.running:
                    await self.application.stop()
                await self.application.shutdown()
            except Exception as e:
                logger.warning("Telegram shutdown warning: %s", e)
        logger.info("Telegram bot stopped")



# ============================================================
# Live PnL handlers (separate isolated DB)
# ============================================================

def fmt_pnl_row(row, label: str) -> str:
    """One PnL line for the Shadow/Live summaries — in $ AND in bps of notional.

    bps is the only figure that compares the two: shadow and live trade
    DIFFERENT notional (measured 2026-08-21: live SOXL 2933 vs shadow 1470), so
    a dollar total silently rewards whoever traded bigger. On SOXL that made the
    shadow-vs-live gap read as 1.9x when in bps it is 3.7x — the dollars were
    hiding it. Both summaries share this renderer so they cannot drift apart.
    """
    if not row or not row["n"]:
        return f"<b>{label}:</b> no trades"
    n = row["n"]
    pnl = row["pnl"] or 0.0
    noc = row["noc"] if "noc" in row.keys() else None
    wr = ((row["wins"] or 0) / n * 100) if n else 0
    bps = f", {pnl / noc * 1e4:+.3f} bps" if noc else ""
    return f"<b>{label}:</b> {n} trades, WR {wr:.1f}%, PnL ${pnl:+.2f}{bps}"


async def _send_shadow_pnl(update, context) -> None:
    """Show shadow PnL summary (today + last 7d + all-time) from shadow_trades.

    Mirror of _send_live_pnl but reads from the main shadow DB. Shadow
    represents the bot's simulation results — same detector signals, but
    fills are computed against MEXC orderbook snapshots rather than sent
    to the exchange. Useful to compare shadow vs live edge over time.
    """
    db = context.bot_data.get("db")
    if db is None:
        await update.message.reply_text("⚠️ Shadow DB not available.")
        return

    today_start = int(
        datetime.now(ZoneInfo("Europe/Kyiv"))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    week_ago = int(time.time()) - 86400 * 7

    try:
        today_row = await db.fetchone(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END) AS wins,
                      SUM(pnl_usdt) AS pnl,
                      SUM(notional_usdt) AS noc,
                      SUM(CASE WHEN pnl_usdt>0 THEN pnl_usdt ELSE 0 END) AS gp,
                      SUM(CASE WHEN pnl_usdt<0 THEN ABS(pnl_usdt) ELSE 0 END) AS gl
                 FROM shadow_trades
                WHERE closed_at >= ? AND closed_at IS NOT NULL""",
            (today_start,),
        )
        week_row = await db.fetchone(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END) AS wins,
                      SUM(pnl_usdt) AS pnl,
                      SUM(notional_usdt) AS noc
                 FROM shadow_trades
                WHERE closed_at >= ? AND closed_at IS NOT NULL""",
            (week_ago,),
        )
        total_row = await db.fetchone(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END) AS wins,
                      SUM(pnl_usdt) AS pnl,
                      SUM(notional_usdt) AS noc
                 FROM shadow_trades
                WHERE closed_at IS NOT NULL"""
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Shadow DB query failed: {e}")
        return

    fmt = fmt_pnl_row

    text = "<b>🧪 SHADOW PnL</b>  <i>(sim)</i>\n\n"
    text += fmt(today_row, "Today (Kyiv)") + "\n"
    text += fmt(week_row, "Last 7 days") + "\n"
    text += fmt(total_row, "All time") + "\n"

    if today_row and today_row["n"]:
        gp = today_row["gp"] or 0
        gl = today_row["gl"] or 1
        pf = (gp / gl) if gl else 0
        text += f"\nToday PF: <b>{pf:.2f}</b>"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def _send_live_pnl(update, context) -> None:
    """Show live trading PnL summary (today + last 7d + all-time) from isolated live_db."""
    live_db = context.bot_data.get("live_db")
    if live_db is None:
        await update.message.reply_text("⚠️ Live DB not available.")
        return

    today_start = int(
        datetime.now(ZoneInfo("Europe/Kyiv"))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    week_ago = int(time.time()) - 86400 * 7

    try:
        today_row = await live_db.fetchone(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN net_pnl_usdt>0 THEN 1 ELSE 0 END) AS wins,
                      SUM(net_pnl_usdt) AS pnl,
                      SUM(notional_usdt) AS noc,
                      SUM(CASE WHEN net_pnl_usdt>0 THEN net_pnl_usdt ELSE 0 END) AS gp,
                      SUM(CASE WHEN net_pnl_usdt<0 THEN ABS(net_pnl_usdt) ELSE 0 END) AS gl
                 FROM live_trades
                WHERE closed_at >= ? AND closed_at IS NOT NULL""",
            (today_start,),
        )
        week_row = await live_db.fetchone(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN net_pnl_usdt>0 THEN 1 ELSE 0 END) AS wins,
                      SUM(net_pnl_usdt) AS pnl,
                      SUM(notional_usdt) AS noc
                 FROM live_trades
                WHERE closed_at >= ? AND closed_at IS NOT NULL""",
            (week_ago,),
        )
        total_row = await live_db.fetchone(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN net_pnl_usdt>0 THEN 1 ELSE 0 END) AS wins,
                      SUM(net_pnl_usdt) AS pnl,
                      SUM(notional_usdt) AS noc
                 FROM live_trades
                WHERE closed_at IS NOT NULL"""
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Live DB query failed: {e}")
        return

    fmt = fmt_pnl_row

    text = "<b>🔴 LIVE PnL</b>  <i>(isolated DB)</i>\n\n"
    text += fmt(today_row, "Today (Kyiv)") + "\n"
    text += fmt(week_row, "Last 7 days") + "\n"
    text += fmt(total_row, "All time") + "\n"

    if today_row and today_row["n"]:
        gp = today_row["gp"] or 0
        gl = today_row["gl"] or 1
        pf = (gp / gl) if gl else 0
        text += f"\nToday PF: <b>{pf:.2f}</b>"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
