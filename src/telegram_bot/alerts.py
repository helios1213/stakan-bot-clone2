"""
Telegram alerts dispatcher.

Provides a simple async interface to push messages to the Telegram owner.
All async senders use HTML parse mode for formatting.

Throttling:
  - Trade alerts: 1 per second per type to prevent spam
  - State changes: never throttled (rare events)
  - Errors: throttled to 1 per minute per error type

Used by:
  - ShadowEngine.on_state_change → state transition alerts
  - ShadowEngine on trade open/close → trade alerts
  - WebSocket clients on disconnect → error alerts
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError, NetworkError, RetryAfter

logger = logging.getLogger(__name__)


class TelegramAlerts:
    """Async dispatcher for sending alerts to a single owner."""

    def __init__(
        self,
        bot_token: str,
        owner_id: int,
        quiet_hours: tuple[int, int] | None = None,
        alert_min_pnl_usdt: float = 0.0,
    ) -> None:
        self.bot = Bot(token=bot_token)
        self.owner_id = owner_id
        self.quiet_hours = quiet_hours  # e.g. (22, 6) = 22:00 → 06:00
        self.alert_min_pnl_usdt = alert_min_pnl_usdt

        self._last_sent: dict[str, int] = defaultdict(int)
        # Per-category "blocked until" timestamp written when Telegram returns
        # RetryAfter. send() bails fast if `now < _retry_after_until[cat]`
        # without acquiring the lock or hitting the network, so a Flood Control
        # bounce doesn't cascade into stuck async tasks.
        self._retry_after_until: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

        self.total_sent = 0
        self.total_failed = 0
        self.total_throttled = 0

    def _is_quiet_hour(self) -> bool:
        if not self.quiet_hours:
            return False
        from datetime import datetime
        now_h = datetime.utcnow().hour
        start, end = self.quiet_hours
        if start <= end:
            return start <= now_h < end
        # Wraps midnight (e.g. 22-6)
        return now_h >= start or now_h < end

    async def send(
        self,
        text: str,
        category: str = "general",
        throttle_sec: int = 0,
        suppress_during_quiet: bool = True,
    ) -> bool:
        """
        Send a message.

        Args:
          category: used for throttling — same-category messages within throttle_sec are dropped.
          throttle_sec: 0 = no throttle, >0 = wait this many seconds between same-category sends.
          suppress_during_quiet: if True, skip during quiet hours (errors override).

        Returns: True if sent, False if dropped.
        """
        if suppress_during_quiet and self._is_quiet_hour():
            return False

        # Fast-path: if Telegram recently returned RetryAfter for this
        # category, skip the network call entirely until that window
        # expires. Without this gate, every send during a Flood Control
        # ban would acquire the lock, await the API, get RetryAfter, and
        # block the lock — cascading into stuck callers.
        now = int(time.time())
        retry_until = self._retry_after_until.get(category, 0)
        if now < retry_until:
            self.total_throttled += 1
            return False

        # Throttle
        if throttle_sec > 0:
            last = self._last_sent.get(category, 0)
            if now - last < throttle_sec:
                self.total_throttled += 1
                return False

        async with self._lock:
            try:
                await self.bot.send_message(
                    chat_id=self.owner_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                self._last_sent[category] = now
                self.total_sent += 1
                return True
            except RetryAfter as e:
                # CRITICAL: do NOT sleep while holding the lock — that
                # serialises every other send() behind us. Telegram can
                # return retry_after up to tens of thousands of seconds
                # under Flood Control, which would freeze the alert
                # pipeline for hours. Instead we mark this category as
                # blocked-until via _retry_after_until; subsequent sends
                # see the timestamp at the top of send() and bail cheaply
                # without acquiring the lock or hitting the network.
                retry = max(1, int(e.retry_after))
                # Cap stored window at 5 minutes so a multi-hour Flood ban
                # doesn't silence this category forever — the next send
                # after 5 minutes will probe Telegram again. If the ban
                # is still active we just re-store the window. This bounds
                # the bad-state memory and prevents accidental permanent
                # silence on a single bad event.
                effective_window = min(retry, 300)
                self._retry_after_until[category] = now + effective_window
                self.total_failed += 1
                logger.warning(
                    "Telegram rate limit on %s: retry_after=%ds "
                    "(capped probe window=%ds)",
                    category, retry, effective_window,
                )
                return False
            except (NetworkError, TelegramError) as e:
                logger.warning("Telegram send failed: %s", e)
                self.total_failed += 1
                return False
            except Exception as e:
                logger.exception("Unexpected Telegram error: %s", e)
                self.total_failed += 1
                return False

    # ------------- Convenience helpers -------------

    async def state_change(self, symbol: str, from_state: str, to_state: str, reason: str) -> None:
        """Fire on state machine transition."""
        emoji = {
            "shadow":     "📈",
            "live":       "🟢",
            "paused":     "⏸",
            "rejected":   "🚫",
            "discovered": "🔍",
        }.get(to_state, "📊")

        # Trim reason
        reason_short = reason[:200] + "..." if len(reason) > 200 else reason

        text = (
            f"{emoji} <b>[STATE]</b> <code>{symbol}</code>\n"
            f"<b>{from_state}</b> → <b>{to_state}</b>\n\n"
            f"<i>{reason_short}</i>"
        )
        await self.send(text, category="state_change", throttle_sec=0,
                        suppress_during_quiet=False)

    async def trade_open(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        leverage: int,
        margin_usdt: float,
        notional_usdt: float,
        gap_ticks: float,
        detector_source: str,
        mode: str = "shadow",  # distinguish [LIVE] vs [SHADOW]
        account_label: str | None = None,  # e.g. "slot2" — which account
    ) -> None:
        """Fire on position opened."""
        # Which slot/account this trade belongs to: with several slots live
        # the pair alone is ambiguous, and MEXC limits accounts individually.
        slot_tag = f" · <b>{account_label.upper()}</b>" if account_label else ""
        arrow = "🟢 LONG" if direction == "long" else "🔴 SHORT"
        # prefix [LIVE] / [SHADOW] for clarity
        if mode == "live":
            prefix = f"🔴 <b>[LIVE OPEN]</b>{slot_tag}"
            margin_label = "isolated"
        else:
            prefix = "⚡ <b>[SHADOW OPEN]</b>"
            margin_label = "sim"
        # Show target vs actual when partial fill occurred.
        # Bug surfaced as "$19.97 × 66x = $164" — math looked broken, but it was
        # margin/leverage as ORDERED vs notional as FILLED (12% partial).
        target_notional = margin_usdt * leverage
        if target_notional > 0 and notional_usdt < target_notional * 0.99:
            # Partial fill: show both target and filled
            fill_pct = 100 * notional_usdt / target_notional
            size_line = (
                f"Target: <b>${margin_usdt:.2f}</b> × <b>{leverage}x</b> = "
                f"<b>${target_notional:.0f}</b>\n"
                f"Filled: <b>${notional_usdt:.0f}</b> ({fill_pct:.0f}% partial, {margin_label})"
            )
        else:
            # Full fill: original single-line format
            size_line = (
                f"Margin: <b>${margin_usdt:.2f}</b> × <b>{leverage}x</b> = "
                f"<b>${notional_usdt:.0f}</b> ({margin_label})"
            )
        # Price decimals derived per-pair from the tick, matching trade_close so
        # OPEN and CLOSE show identical precision (PEPE->7, PENGU->6, TAO/ZEC->2).
        dp = 6
        try:
            import math
            from src.execution.live_executor import get_tick_size
            from src.exchanges.mexc_rest import to_mexc, get_binance_scale
            _msym = to_mexc(symbol)
            _tick_scaled = get_tick_size(_msym) * get_binance_scale(_msym)
            if _tick_scaled > 0:
                dp = max(2, min(10, int(round(-math.log10(_tick_scaled)))))
        except Exception:
            pass
        text = (
            f"{prefix} {arrow} <code>{symbol}</code>\n"
            f"Entry: <code>{entry_price:.{dp}f}</code>\n"
            f"{size_line}\n"
            f"Detector: <i>{detector_source}</i> gap={gap_ticks:.1f}t"
        )
        # Throttle key carries BOTH the pair and the account: two live slots
        # now fan out onto the SAME pair, so a per-pair key made two genuine
        # opens one category and silently dropped one of them.
        # don't drop each other's alert (a shared "trade_open" did).
        await self.send(
            text, category=f"trade_open_{symbol}_{account_label or '-'}",
            throttle_sec=1)

    async def trade_close(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        exit_reason: str,
        roi_pct: float,
        net_pnl_usdt: float,
        duration_sec: int,
        mfe_pct: float,
        mae_pct: float,
        duration_ms: int | None = None,
        mode: str = "shadow",
        account_label: str | None = None,  # e.g. "slot2" — which account
    ) -> None:
        """Fire on shadow position closed."""
        slot_tag = f" · <b>{account_label.upper()}</b>" if account_label else ""
        # Stash mode for the format string lookup below
        self._last_close_mode = mode
        # Filter low-PnL trades if user set a threshold
        if abs(net_pnl_usdt) < self.alert_min_pnl_usdt:
            return

        # Choose emoji based on PnL
        if net_pnl_usdt > 0:
            emoji = "✅"
        elif net_pnl_usdt < 0:
            emoji = "❌"
        else:
            emoji = "➖"

        # Reason emoji — only active exit reasons.
        reason_emoji = {
            "stop_loss":                "🛑",
            "time_limit":               "⏱",
            "adaptive_stalled":         "⏸",
            "adaptive_reversal":        "🔄",
            "reverse_signal":           "↩️",
            # 3-phase exit reasons (stage 7)
            "phase_0_micro_stop":       "🛑",  # 0-1s tight stop, mimics friend's instant exits
            "phase_1_gap_collapse":     "💨",  # arb opportunity vanished
            "phase_2_dead_impulse":     "💤",  # never moved enough
            "phase_3_breakeven_floor":  "🛡",  # locked micro-profit at floor
            "phase_3_profit_reversal":  "📉",  # pullback from peak in profit
            "phase_3_profit_stalled":   "🎯",  # in profit but stuck
            "phase_3_breakeven_stalled":"⏸",  # drifting at breakeven
            "phase_hard_stop":          "🚨",  # catastrophic safety net
        }.get(exit_reason, "❔")

        arrow = "LONG" if direction == "long" else "SHORT"

        # Duration display: prefer ms-precision when available and trade is sub-minute.
        # Falls back to whole seconds for longer trades or when ms not provided.
        if duration_ms is not None and duration_ms < 60_000:
            duration_str = f"{duration_ms / 1000.0:.2f}s"
        elif duration_ms is not None:
            duration_str = f"{duration_ms // 1000}s"
        else:
            duration_str = f"{duration_sec}s"

        # distinguish [LIVE CLOSE] vs [SHADOW CLOSE]
        mode_label = "LIVE CLOSE" if getattr(self, "_last_close_mode", "shadow") == "live" else "SHADOW CLOSE"

        # Show prices to TICK precision so the move is unambiguous. The old
        # 6dp rounding hid the real tick (PEPE tick_scaled=1e-7 → a 40-tick
        # move displayed as "0.000004", read as 4). Decimals are derived
        # per-pair from the tick (PEPE→7, PENGU→6, TAO/ZEC→2) so high-priced
        # pairs don't get a row of trailing zeros. Local import avoids a cycle.
        dp = 6
        # MFE/MAE shown in TICKS (per-pair) for direct readability vs the
        # tick-based config (min_ticks / stop_loss_ticks). Falls back to bps if
        # the tick can't be resolved. ticks = (pct/100)*entry_price / tick_scaled.
        mfe_mae_str = f"MFE {mfe_pct * 100:+.1f} / MAE {mae_pct * 100:+.1f} bps"
        try:
            import math
            from src.execution.live_executor import get_tick_size
            from src.exchanges.mexc_rest import to_mexc, get_binance_scale
            _msym = to_mexc(symbol)
            _tick_scaled = get_tick_size(_msym) * get_binance_scale(_msym)
            if _tick_scaled > 0:
                dp = max(2, min(10, int(round(-math.log10(_tick_scaled)))))
                if entry_price > 0:
                    _mfe_t = (mfe_pct / 100.0) * entry_price / _tick_scaled
                    _mae_t = (mae_pct / 100.0) * entry_price / _tick_scaled
                    mfe_mae_str = f"MFE <b>{_mfe_t:+.0f}т</b> / MAE <b>{_mae_t:+.0f}т</b>"
        except Exception:
            pass

        text = (
            f"{emoji} <b>[{mode_label}]</b>{slot_tag} {arrow} <code>{symbol}</code>\n"
            f"Entry: <code>{entry_price:.{dp}f}</code> → "
            f"Exit: <code>{exit_price:.{dp}f}</code>\n"
            f"PnL: <b>${net_pnl_usdt:+.4f}</b> ROI: <b>{roi_pct:+.2f}%</b>\n"
            f"{reason_emoji} {exit_reason} | duration <b>{duration_str}</b>\n"
            f"{mfe_mae_str}"
        )
        # Throttle key carries BOTH the pair and the account. History: it was
        # once a bare "trade_close" and lost closes across different pairs (BCH
        # behind PENGU), which the symbol fixed; then signals fanned out to both
        # live slots on the SAME pair and the same collision returned inside one
        # symbol. Measured on the clone 2026-07-28 22:07:43 — slot1 and slot2
        # both closed 1000PEPEUSDT in the same second and only slot2 alerted.
        # Intermittent because _last_sent is written after the Telegram
        # round-trip, so concurrent callers sometimes both pass the check.
        await self.send(
            text, category=f"trade_close_{symbol}_{account_label or '-'}",
            throttle_sec=1)

    async def error(self, error_text: str, error_category: str = "general") -> None:
        """Fire on errors. Throttled to 1/min per category."""
        text = f"⚠️ <b>[ERROR]</b>\n<code>{error_text[:500]}</code>"
        await self.send(text, category=f"error_{error_category}", throttle_sec=60,
                        suppress_during_quiet=False)

    async def warning(self, warning_text: str, warning_category: str = "general") -> None:
        """Fire on warnings. Throttled to 1/2min per category."""
        text = f"⚠️ <b>[WARN]</b> {warning_text[:500]}"
        await self.send(text, category=f"warn_{warning_category}", throttle_sec=120)

    async def kill_switch_triggered(self, reason: str) -> None:
        """Fire when emergency kill is triggered."""
        text = (
            f"💀 <b>[KILL SWITCH]</b>\n"
            f"All positions closed.\n"
            f"<i>{reason}</i>"
        )
        await self.send(text, category="kill", throttle_sec=0,
                        suppress_during_quiet=False)

    @staticmethod
    def _path_mode_line() -> str:
        """Один рядок про режим ордерного шляху — у стартове повідомлення.

        НАВІЩО В TELEGRAM, ЯКЩО ВОНО Є В ЛОГАХ. Рядок `[PATH MODE]` пишеться
        при старті, але в логи оператор заглядає рідко, а переплутати, у якому
        режимі піднявся бот, коштує дорого: `full` і `bare` відрізняються тим,
        чи йде dolos на `/order/create`, тобто поведінкою на грошовому шляху.

        Аварійні відкати (`MEXC_CHASH`, `MEXC_DOLOS_LEGACY`) показуються ЛИШЕ
        коли задані — у звичайному стані рядок лишається коротким. Саме вони
        найнебезпечніші, якщо про них забути: обидва тихо міняють підпис.

        Ніколи не кидає: збій тут не має глушити повідомлення про старт.
        """
        try:
            from src.execution.webkey import client as _wc
            from src.execution.webkey import credentials as _cr
            from src.execution.webkey import device_profile as _dp

            mode = getattr(_wc, "_PATH_MODE", "?")
            on_order = "ТАК" if getattr(_wc, "_DOLOS_ON_ORDER", False) else "ні"
            line = f"Шлях: <b>{mode}</b> · dolos на ордері: <b>{on_order}</b>"

            flags = []
            if getattr(_wc, "_DOLOS_LEGACY", False):
                flags.append("legacy chash+поля")
            if getattr(_cr, "CHASH_ENV_OVERRIDE", ""):
                flags.append("MEXC_CHASH override")
            if getattr(_wc, "_CHROME_VER_ENV", ""):
                flags.append(f"Chrome {_wc._CHROME_VER_ENV}")
            if not _dp.offset_is_explicit():
                flags.append("БЕЗ MEXC_DEVICE_OFFSET")
            if flags:
                line += "\n⚠️ " + " · ".join(flags)
            return line
        except Exception:
            return "Шлях: <i>не визначено</i>"

    async def startup(self, state_manager=None, db=None) -> None:
        """Send simple startup notification."""
        await self.send(
            "✅ <b>Бот запущений</b>\n"
            "Статус: <b>Running</b>\n"
            + self._path_mode_line(),
            category="startup",
            throttle_sec=0,
            suppress_during_quiet=False,
        )

    async def shutdown(self) -> None:
        """Send simple shutdown notification."""
        await self.send(
            "🛑 <b>Бот зупинений</b>\n"
            "Статус: <b>Offline</b>",
            category="shutdown",
            throttle_sec=0,
            suppress_during_quiet=False,
        )
