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
# Окремий, коротший журнал ФʼЮЧЕРСНИХ подій. Їх одиниці на день проти
# десятків спотових, тож у спільному списку вони гарантовано витісняються.
MAX_FUTURES_LOG = 6


def _hhmm() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


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
        # Фʼючерсні події живуть ОКРЕМО і не витісняються спотом.
        self.futures_log: list[str] = []
        self.max_futures = MAX_FUTURES_LOG
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

    async def spot_sell(self, symbol: str, qty: str, usdt: float = 0.0,
                        **st) -> None:
        self.stats["spot_sells"] += 1
        amt = f" ~{usdt:.2f} USDT" if usdt else ""
        await self.action("🔴", f"SPOT SELL {symbol}{amt} (qty {qty})", **st)

    async def futures_open(self, symbol: str, side: int, leverage: int,
                           hold_min: int, vol: int = 0, notional: float = 0.0,
                           **st) -> None:
        self.stats["futures_opens"] += 1
        s = "LONG" if side == 1 else "SHORT"
        size = f" vol={vol}" if vol else ""
        size += f" ~{notional:.2f} USDT" if notional else ""
        line = (f"FUTURES OPEN {symbol} {s} {leverage}x{size} "
                f"— closing in {hold_min}min")
        # Фʼючерсні події ДУБЛЮЮТЬСЯ в окремий короткий журнал. Спот робить
        # десятки дій на день, фʼючерси — одну-три, тож у спільному списку з 8
        # рядків найважливіше витіснялось спотовим шумом за півгодини: у звіті
        # лишався тільки спот, і оператор не бачив ані відкриття, ані закриття.
        self.futures_log.append(f"{_hhmm()} 📈 {line}")
        del self.futures_log[:-self.max_futures]
        await self.action("📈", line, **st)

    async def futures_close(self, symbol: str, held_min: float,
                            realised: float | None = None, **st) -> None:
        self.stats["futures_closes"] += 1
        held = f" after {held_min:.0f}min" if held_min else " (тримання невідоме)"
        # None — це «не прочитали», НЕ нуль: показати збиткову угоду як
        # безкоштовну гірше, ніж чесно сказати «невідомо».
        pnl = (f" | PnL {realised:+.4f}" if realised is not None
               else " | PnL невідомий")
        line = f"FUTURES CLOSE {symbol}{held}{pnl}"
        self.futures_log.append(f"{_hhmm()} 📉 {line}")
        del self.futures_log[:-self.max_futures]
        await self.action("📉", line, **st)

    async def campaign_started(self, days: int, **st) -> None:
        await self.action("🌱", f"Soft-start campaign began — {days} days", **st)

    async def skipped(self, why: str, **st) -> None:
        self.stats["skips"] += 1
        await self.action("⏭", f"skipped: {why}", **st)

    # ---- closing report -------------------------------------------------

    def render_final(self, reason: str, *, spent: float = 0.0,
                     ceiling: float = 0.0, position_left: bool = False,
                     pnl: float = 0.0, held_value: float = 0.0,
                     futures_pnl: float = 0.0,
                     stats: dict | None = None,
                     elapsed_h: float | None = None) -> str:
        # ПІДСУМКИ КАМПАНІЇ, а не процесу. `self.stats` і `self.started_at`
        # живуть у памʼяті репортера, який створюється наново на КОЖНОМУ
        # рестарті бота: 29.08 звіт показав «spot 0 buys, futures 2 opened,
        # Ran for 14.0h» замість реальних 17/9 і 8/7 за три доби. Коли
        # викликач має персистентні числа — беремо їх.
        s = dict(self.stats)
        if stats:
            s.update({k: v for k, v in stats.items() if v is not None})
        elapsed_h = (elapsed_h if elapsed_h is not None
                     else (time.time() - self.started_at) / 3600)
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
        # ГРОШІ ПОКАЗУЮТЬСЯ ЗАВЖДИ. Було `if ceiling:` — а стелю прибрано, тож
        # підсумковий звіт про кампанію лишився б БЕЗ ЖОДНОЇ цифри про гроші,
        # тобто без головного, заради чого його читають.
        net = spent - pnl - held_value
        lines += [
            "",
            "<b>Скільки коштувало</b>",
            f"<code>комісії+спред   : {spent:+.4f} USDT</code>",
            # ТІЛЬКИ фʼючерси. Спотова частина — кеш-фло, а не PnL, і показана
            # окремо як «у монетах»: змішавши їх, звіт друкував «-4.358» при
            # реальному результаті -0.34 і читався як катастрофа.
            f"<code>фʼючерси (PnL)  : {futures_pnl:+.4f} USDT</code>",
        ]
        if held_value:
            # ЗА ЦІНОЮ КУПІВЛІ, не за ринком — і так і підписано. Ми знаємо,
            # скільки USDT пішло в монети, але не переоцінюємо їх: без цього
            # рядка «разом» читалось би як збиток, яким воно не є (спотова
            # купівля йде в PnL мінусом, поки монета не продана), а з ринковою
            # переоцінкою число мінялось би щохвилини від курсу.
            lines.append(f"<code>у монетах (за купівлею): {held_value:.4f} "
                         f"USDT</code>")
        lines.append(f"<code>РАЗОМ           : {net:+.4f} USDT</code>")
        # Поріг, а не знак: біля нуля казати «в плюс» чи «в мінус» однаково
        # неправдиво, а вердикт із двох станів змушує обирати навмання.
        if net > 0.05:
            lines.append("<i>прогрів коштував грошей</i>")
        elif net < -0.05:
            lines.append("<i>прогрів вийшов у плюс попри витрати</i>")
        else:
            lines.append("<i>прогрів вийшов приблизно в нуль</i>")
        if held_value:
            lines.append("<i>Непроданi монети враховано за ціною купівлі — "
                         "їхній ринковий рух у підсумок не входить.</i>")
        if ceiling:
            pct = (spent / ceiling * 100) if ceiling else 0
            lines.append(f"<code>стеля           : {spent:.4f} / {ceiling:.2f} "
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
               position: str | None = None, pnl: float | None = None,
               held_value: float | None = None,
               futures_pnl: float | None = None) -> str:
        head = f"🌱 <b>Soft-start — slot {self.slot_id}</b>"
        if self.dry_run:
            head += "  <i>(DRY-RUN — nothing is sent)</i>"
        lines = [head, ""]

        meta = []
        if day is not None and days:
            meta.append(f"day {day}/{days}")
        if spent is not None:
            # Без знаменника: стелю витрат прибрано (рішення оператора
            # 2026-08-26), і «0.117/5.00» читалось би як діючий ліміт, якого
            # немає. Облік лишився — саме число досі корисне.
            meta.append(f"комісії+спред {spent:.3f}"
                        + (f" (стеля {ceiling:.2f})" if ceiling else ""))
        if futures_pnl is not None:
            # ОКРЕМИМ ЧИСЛОМ, бо це єдиний справжній прибуток/збиток тут.
            meta.append(f"фʼючерси {futures_pnl:+.3f}")
        if spent is not None and pnl is not None:
            # РАЗОМ = витрати - PnL - те, що ЩЕ ЛЕЖИТЬ У МОНЕТАХ.
            #
            # Без останнього доданка число бреше в наш бік навпаки: спотова
            # купівля йде в PnL мінусом, тож USDT, перетворені на монету,
            # виглядають як збиток. На першому ж живому дні це давало «разом
            # +5.93», хоча реальна вартість прогріву була 0.49 (комісії 0.12 +
            # фʼючерсний мінус 0.37) — решта просто змінила форму.
            #
            # Додатне = прогрів у мінус.
            net = spent - pnl - (held_value or 0.0)
            meta.append(f"разом {net:+.3f} USDT")
        if held_value:
            # Спотова частина НЕ показується як «PnL» узагалі: це кеш-фло,
            # тобто USDT, що змінили форму. Показуємо лише скільки лежить у
            # монетах — число, яке справді щось означає для оператора.
            # Разом із «разом» воно й пояснює, куди пішли гроші.
            meta.append(f"у монетах ~{held_value:.2f}")
        meta.append(f"position: {position}" if position else "position: none")
        lines.append(" · ".join(meta))
        lines.append("")

        # ФʼЮЧЕРСИ ПЕРШИМИ й окремим блоком. У спільному списку з 8 рядків
        # їх за півгодини витісняв спот (десятки дій на день проти одиниць), і
        # у звіті лишався тільки спот — оператор не бачив ані відкриття, ані
        # закриття позиції, тобто найдорожчих подій прогріву.
        if self.futures_log:
            lines.append("<b>Futures</b>")
            for ln in reversed(self.futures_log):
                lines.append(f"<code>{ln}</code>")
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
