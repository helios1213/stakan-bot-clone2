"""One live Telegram message that shows what warming is doing right now.

The problem it solves: you press the button and then have no idea whether
anything is happening. Logs are on the server; the operator is on a phone.

Behaviour (per the operator's description): keep ONE message per slot. On every
action, delete the previous one and post a fresh one containing the new action
plus the recent history. Deleting-and-reposting rather than editing is
deliberate — an edit produces no notification, so a silent edit would defeat the
whole point of "let me see that it is working".

The message carries:
  * what just happened, with a timestamp,
  * the last few actions before it,
  * campaign progress (day N of 3), spend against the ceiling, and whether a
    futures position is currently held,
  * whether this is DRY-RUN or live — so a dry-run is never mistaken for real
    trading.

Failure policy: reporting is decoration. Every path is wrapped, and a Telegram
failure NEVER propagates into the trading loop — a warm-up must not stop because
a message could not be delivered.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_HISTORY = 8


@dataclass
class Action:
    ts: float
    icon: str
    text: str

    def line(self) -> str:
        return f"{time.strftime('%H:%M:%S', time.localtime(self.ts))} {self.icon} {self.text}"


class SoftStartReporter:
    """Keeps one live status message per slot in Telegram."""

    def __init__(self, alerts, slot_id: int, dry_run: bool,
                 max_history: int = MAX_HISTORY) -> None:
        self.alerts = alerts            # TelegramAlerts, or None to disable
        self.slot_id = slot_id
        self.dry_run = dry_run
        self.history: deque[Action] = deque(maxlen=max_history)
        self._message_id: int | None = None
        self.started_at = time.time()
        # Counters for the closing report. The rolling history only keeps the
        # last few lines, so totals have to be tallied as they happen.
        self.stats = {
            "spot_buys": 0, "spot_sells": 0,
            "futures_opens": 0, "futures_closes": 0,
            "skips": 0, "errors": 0,
        }

    def note_error(self, what: str) -> None:
        """Record a failure for the closing report without posting a message."""
        self.stats["errors"] += 1
        logger.warning("soft-start slot %d: %s", self.slot_id, what)

    # ---- recording ------------------------------------------------------

    async def action(self, icon: str, text: str, **status) -> None:
        """Record an action and repost the live message."""
        self.history.append(Action(time.time(), icon, text))
        await self._repost(**status)

    async def spot_buy(self, symbol: str, usdt: float, qty: str, **st) -> None:
        self.stats["spot_buys"] += 1
        await self.action("🟢", f"SPOT BUY {symbol} ~{usdt:.2f} USDT (qty {qty})", **st)

    async def spot_sell(self, symbol: str, qty: str, **st) -> None:
        self.stats["spot_sells"] += 1
        await self.action("🔴", f"SPOT SELL {symbol} qty {qty}", **st)

    async def futures_open(self, symbol: str, side: int, leverage: int,
                           hold_min: int, **st) -> None:
        self.stats["futures_opens"] += 1
        s = "LONG" if side == 1 else "SHORT"
        await self.action("📈", f"FUTURES OPEN {symbol} {s} {leverage}x "
                                f"— closing in {hold_min}min", **st)

    async def futures_close(self, symbol: str, held_min: float, **st) -> None:
        self.stats["futures_closes"] += 1
        await self.action("📉", f"FUTURES CLOSE {symbol} after {held_min:.0f}min", **st)

    async def campaign_started(self, days: int, **st) -> None:
        await self.action("🌱", f"Soft-start campaign began — {days} days", **st)

    async def skipped(self, why: str, **st) -> None:
        self.stats["skips"] += 1
        await self.action("⏭", f"skipped: {why}", **st)

    # ---- closing report -------------------------------------------------

    def render_final(self, reason: str, *, spent: float = 0.0,
                     ceiling: float = 0.0, position_left: bool = False) -> str:
        s = self.stats
        elapsed_h = (time.time() - self.started_at) / 3600
        spot_total = s["spot_buys"] + s["spot_sells"]
        fut_total = s["futures_opens"] + s["futures_closes"]

        # "All clear" means: nothing errored AND nothing was left open. Those
        # are the two things that would need the operator to go look.
        clean = s["errors"] == 0 and not position_left
        verdict = ("✅ <b>All clear</b>" if clean
                   else "⚠️ <b>Finished with issues — check below</b>")

        lines = [
            f"🏁 <b>Soft-start finished — slot {self.slot_id}</b>",
            f"{verdict}",
            "",
            f"Reason: {reason}",
            f"Ran for: {elapsed_h:.1f}h",
            "",
            "<b>What it did</b>",
            f"<code>spot    : {s['spot_buys']} buys, {s['spot_sells']} sells "
            f"({spot_total} orders)</code>",
            f"<code>futures : {s['futures_opens']} opened, "
            f"{s['futures_closes']} closed</code>",
            f"<code>skipped : {s['skips']}</code>",
        ]
        if ceiling:
            pct = (spent / ceiling * 100) if ceiling else 0
            lines.append(f"<code>cost    : {spent:.4f} / {ceiling:.2f} USDT "
                         f"({pct:.0f}%)</code>")
        lines.append("")

        if self.dry_run:
            lines.append("<i>DRY-RUN — no real orders were placed.</i>")
        if s["errors"]:
            lines.append(f"⚠️ {s['errors']} error(s) were logged — see the bot log.")
        if position_left:
            lines.append("⚠️ A futures position could NOT be closed — check the "
                         "exchange manually.")
        if clean and not self.dry_run:
            lines.append("Every position was closed and no errors occurred.")
        return "\n".join(lines)

    async def final_report(self, reason: str, **kw) -> None:
        """Post the closing summary as a message that STAYS.

        The rolling status message is removed first — the campaign is over, so a
        live status line would be stale — and the summary is posted without
        tracking its id, so nothing deletes it later.
        """
        if self.alerts is None:
            return
        bot = getattr(self.alerts, "bot", None)
        owner = getattr(self.alerts, "owner_id", None)
        if bot is None or owner is None:
            return

        if self._message_id is not None:
            try:
                await bot.delete_message(chat_id=owner, message_id=self._message_id)
            except Exception as e:
                logger.debug("soft-start reporter: final delete failed (%s)", e)
            self._message_id = None

        try:
            from telegram.constants import ParseMode
            await bot.send_message(chat_id=owner, text=self.render_final(reason, **kw),
                                   parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
        except Exception as e:
            logger.warning("soft-start reporter: final report failed (%s)", e)

    # ---- rendering ------------------------------------------------------

    def render(self, *, day: int | None = None, days: int | None = None,
               spent: float | None = None, ceiling: float | None = None,
               position: str | None = None) -> str:
        head = f"🌱 <b>Soft-start — slot {self.slot_id}</b>"
        if self.dry_run:
            head += "  <i>(DRY-RUN — nothing is sent)</i>"
        lines = [head, ""]

        meta = []
        if day is not None and days:
            meta.append(f"day {day}/{days}")
        if spent is not None and ceiling:
            meta.append(f"spent {spent:.3f}/{ceiling:.2f} USDT")
        meta.append(f"position: {position}" if position else "position: none")
        lines.append(" · ".join(meta))
        lines.append("")

        if self.history:
            lines.append("<b>Recent actions</b>")
            # newest first — the thing that just happened should be at the top
            for a in reversed(self.history):
                lines.append(f"<code>{a.line()}</code>")
        else:
            lines.append("<i>no actions yet</i>")
        return "\n".join(lines)

    # ---- delivery -------------------------------------------------------

    async def _repost(self, **status) -> None:
        """Delete the previous live message, post a fresh one.

        Wrapped end to end: warming must never stop because Telegram did.
        """
        if self.alerts is None:
            return
        text = self.render(**status)
        bot = getattr(self.alerts, "bot", None)
        owner = getattr(self.alerts, "owner_id", None)
        if bot is None or owner is None:
            return

        if self._message_id is not None:
            try:
                await bot.delete_message(chat_id=owner, message_id=self._message_id)
            except Exception as e:
                # Already gone, too old to delete, or the chat was cleared —
                # none of that is worth a warning every tick.
                logger.debug("soft-start reporter: delete failed (%s)", e)
            self._message_id = None

        try:
            from telegram.constants import ParseMode
            msg = await bot.send_message(chat_id=owner, text=text,
                                         parse_mode=ParseMode.HTML,
                                         disable_web_page_preview=True)
            self._message_id = getattr(msg, "message_id", None)
        except Exception as e:
            logger.warning("soft-start reporter: send failed (%s) — continuing", e)
