"""
Live Safety Controller — protect real capital from runaway bugs and bad markets.

ONE automatic kill, on purpose: PEAK DRAWDOWN. Halt the slot when PnL falls
`LIVE_MAX_DRAWDOWN` dollars below the session high-water mark. Measuring from
the peak rather than from zero is what lets it catch a real bleed inside a day
that is still net-positive.

ONE FLAT NUMBER, on purpose (2026-08-11). The limit is a plain dollar figure
($25), the same at any position size. The earlier %-of-notional rule and the
separate cumulative daily-loss kill were BOTH removed: the operator asked for a
single stop measured from the session peak, and three numbers on one threshold
meant nobody could say which was binding. avg_notional is kept only as display
context ("position ~$X"), never in the kill math.

A cumulative daily-loss threshold and a consecutive-loss pause used to sit
alongside it. Both were removed 2026-07-28: a profitable day masked the first
and the UTC-midnight reset split it, while the second fired on ordinary variance
(~19 times per 1,330 trades at the observed ~43% losing rate) and paused the
slot for an hour each time. Three kills writing one flag also made a halt hard
to attribute. Do not add them back without new evidence.

The remaining guards are not kills, they are admission checks:
  * per-symbol max position count — one live position per symbol at a time
  * margin sanity — refuse to open if margin exceeds the per-trade cap

All checks are evaluated BEFORE every live entry. State is in-memory
(no DB persistence — restart resets state, which is intentional for safety).
"""
from __future__ import annotations

import logging
import datetime
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SafetyState:
    """Per-day live trading state."""
    today_pnl: float = 0.0
    peak_pnl: float = 0.0        # session high-water mark (for the drawdown kill)
    today_trades: int = 0
    consecutive_losses: int = 0
    # Smoothed notional of a position on this slot — the drawdown limit
    # is a percentage of it, so a resize retunes the kill automatically.
    avg_notional_usdt: float = 0.0
    _logged_dd_step: int = 0        # deepest quarter-of-limit already logged
    _logged_peak: float = 0.0
    _closes_since_log: int = 0
    _last_log_ts: float = 0.0
    kill_active: bool = False
    kill_reason: str = ""
    kill_until_ts: int = 0  # 0 = indefinite
    # Кіл протермінувався САМ і пік перебазовано. Прапорець одноразовий: пул
    # побачить його, збереже маркер у live_state і скине. Без цього природне
    # протермінування не переживало рестарт — `_hydrate_safety` відновлював
    # дорелізний пік і слот халтився знову, аж до півночі.
    kill_auto_released: bool = False
    open_live_positions: dict[str, int] = field(default_factory=dict)  # symbol → count


class LiveSafetyController:
    """
    Gatekeeper for all live trades.

    Configure via constructor. Defaults are conservative — tighten further
    when starting live, loosen as confidence grows.
    """

    def __init__(
        self,
        # ЄДИНИЙ кіл: просадка від піку сесії >= цієї суми (env LIVE_MAX_DRAWDOWN).
        # Плоско, однаково за будь-якого розміру. 0 = кіл вимкнено.
        max_drawdown_usdt: float = 25.0,
        kill_pause_sec: int = 14400,                    # 4h pause

        # Per-symbol limits
        max_concurrent_per_symbol: int = 1,
        max_concurrent_total: int = 2,                  # across ALL symbols

        # Margin sanity
        max_margin_per_trade_usdt: float = 10.0,        # never risk more than this
    ) -> None:
        self.max_drawdown_usdt = max_drawdown_usdt
        self.kill_pause_sec = kill_pause_sec
        self.max_concurrent_per_symbol = max_concurrent_per_symbol
        self.max_concurrent_total = max_concurrent_total
        self.max_margin_per_trade_usdt = max_margin_per_trade_usdt

        self.state = SafetyState()
        # Чи вже застосовано ручне зняття кіла під час відновлення сесії.
        self._release_applied = False
        self._tz = self._resolve_tz()
        self._daily_reset_at_ts = self._next_reset_ts()

        _kill_desc = (
            "просадка $%.2f від піку сесії" % max_drawdown_usdt
            if max_drawdown_usdt > 0 else "⚠️ ЖОДНОГО автоматичного кіла")
        logger.info(
            "LiveSafetyController initialized: кіл — %s | "
            "max_concurrent=%d, max_margin/trade=$%.2f",
            _kill_desc,
            max_concurrent_total,
            max_margin_per_trade_usdt,
        )

    @staticmethod
    def _resolve_tz():
        """Зона, у якій рахується доба сесії — та сама, що в боті (TZ).

        Раніше межа стояла на півночі UTC, тобто «доба» оператора починалась
        о 03:00 за київським часом, і денний звіт ніколи не збігався з тим,
        що показував запобіжник.
        """
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(os.environ.get("TZ") or "UTC")
        except Exception:      # немає tzdata — краще UTC, ніж падіння
            return datetime.timezone.utc

    def session_start_ts(self) -> int:
        """00:00 поточної доби за локальною зоною, unix-секунди."""
        today = datetime.datetime.now(self._tz).date()
        return int(datetime.datetime.combine(
            today, datetime.time(0, 0), tzinfo=self._tz).timestamp())

    def _next_reset_ts(self) -> int:
        """Наступна локальна північ.

        Через zoneinfo, а не +86400: перехід на літній час зсуває добу на
        годину, і арифметика по модулю почала б розʼїжджатись із календарем.
        """
        tomorrow = datetime.datetime.now(self._tz).date() + datetime.timedelta(days=1)
        return int(datetime.datetime.combine(
            tomorrow, datetime.time(0, 0), tzinfo=self._tz).timestamp())

    def hydrate_session(self, closes, released_at: int = 0) -> bool:
        """Відновити сесію з уже закритих угод цієї доби.

        `closes` — [(net_pnl_usdt, notional_usdt, closed_at), ...] у
        хронологічному порядку, від 00:00 локальних до зараз.
        `released_at` — коли оператор ВРУЧНУ зняв кіл цієї доби (0 = не знімав).

        Навіщо: контролер живе лише в памʼяті й створюється заново на кожному
        рестарті, тож перезапуск о 14:00 стирав і денний PnL, і пік — після
        чого просадка рахувалась від нуля, і запобіжник фактично знімався.
        Програємо ті самі кроки, що й record_close, і дістаємо той самий стан.

        Нічого не робить, якщо в сесії вже щось накопичено (щоб повторний
        rebuild_from_store не подвоїв день). Повертає True, якщо відновив.
        """
        if self.state.today_trades or self.state.today_pnl:
            return False
        for pnl, notional, closed_at in closes:
            pnl = float(pnl or 0.0)
            # Оператор зняв кіл вручну о `released_at` — і саме тоді пік було
            # перебазовано на поточний PnL (див. release_kill). Без цього кроку
            # рестарт відновлював ДОРЕЛІЗНИЙ пік, просадка знову виявлялась
            # пробитою, і кіл вмикався сам — тобто кнопка «зняти» діяла лише до
            # наступного перезапуску. Відтворюємо перебазування в тій самій
            # точці історії, тож ручне рішення переживає рестарт.
            if released_at and not self._release_applied and closed_at and \
                    int(closed_at) >= released_at:
                self.state.peak_pnl = self.state.today_pnl
                self._release_applied = True
            self.state.today_pnl += pnl
            self.state.today_trades += 1
            if self.state.today_pnl > self.state.peak_pnl:
                self.state.peak_pnl = self.state.today_pnl
            self.state.consecutive_losses = (
                self.state.consecutive_losses + 1 if pnl < 0 else 0)
            if notional and notional > 0:
                a = self.state.avg_notional_usdt
                self.state.avg_notional_usdt = (
                    float(notional) if a <= 0 else 0.9 * a + 0.1 * float(notional))
        # Зняття сталося вже після останнього закриття цієї доби — перебазувати
        # на кінцевий стан, інакше маркер мовчки б загубився.
        if released_at and not self._release_applied:
            self.state.peak_pnl = self.state.today_pnl
            self._release_applied = True
        self.state._logged_peak = self.state.peak_pnl
        if not self.state.today_trades:
            return False

        dd_limit = self.drawdown_limit()
        drawdown = self.state.peak_pnl - self.state.today_pnl
        logger.warning(
            "[SESSION] відновлено з %d угод доби: PnL=$%.2f пік=$%.2f "
            "просадка=$%.2f межа=$%.2f",
            self.state.today_trades, self.state.today_pnl,
            self.state.peak_pnl, drawdown, dd_limit,
        )
        # Просадка вже пробита — вмикаємо кіл ЗАРАЗ, а не чекаємо наступного
        # закриття. Інакше рестарт лишався б способом купити собі ще угоду.
        if dd_limit > 0 and drawdown >= dd_limit:
            self.engage_kill(
                reason=(f"drawdown ${drawdown:.2f} from session peak "
                        f"${self.state.peak_pnl:.2f} (limit ${dd_limit:.2f})"
                        " — відновлено після рестарту"),
                duration_sec=self.kill_pause_sec,
            )
        return True

    def _maybe_reset_daily(self) -> None:
        """Скинути денні лічильники на локальній півночі (00:00 TZ бота)."""
        if int(time.time()) >= self._daily_reset_at_ts:
            logger.info(
                "Daily safety reset: previous PnL=$%.2f trades=%d",
                self.state.today_pnl, self.state.today_trades,
            )
            self.state.today_pnl = 0.0
            self.state.peak_pnl = 0.0
            self.state.today_trades = 0
            self.state.consecutive_losses = 0
            self.state._logged_dd_step = 0
            self.state._logged_peak = 0.0
            # Don't reset kill_active here — kill persists until kill_until_ts
            self._daily_reset_at_ts = self._next_reset_ts()

    def can_open_live(
        self,
        symbol: str,
        margin_usdt: float,
    ) -> tuple[bool, str]:
        """
        Check if we're allowed to open a live position right now.

        Returns (allowed, reason_if_blocked).
        """
        self._maybe_reset_daily()

        now = int(time.time())

        # Kill switch active
        if self.state.kill_active:
            if self.state.kill_until_ts == 0 or now < self.state.kill_until_ts:
                return False, f"kill_active: {self.state.kill_reason}"
            else:
                # Kill expired — auto-clear
                logger.info("Kill switch expired — resuming live trading")
                self.state.kill_active = False
                self.state.kill_reason = ""
                # ПОЗНАЧАЄМО, ЩО ПЕРЕБАЗУВАННЯ СТАЛОСЬ — щоб воно пережило
                # рестарт. Перебазування нижче живе ЛИШЕ в памʼяті, а
                # `_hydrate_safety` після перезапуску переграє денні угоди
                # наново і відновлює ДОРЕЛІЗНИЙ пік. Тобто природне
                # протермінування кіла скасовувалось будь-яким ребілдом: слот
                # знову халтився, і так до півночі. Ручне зняття цю проблему
                # вже лікує маркером (`persist_kill_release`) — тут його просто
                # не застосували. Прапорець знімає пул у `sync_kill_state`:
                # у контролера немає і не має бути доступу до БД.
                self.state.kill_auto_released = True
                # Re-baseline the drawdown high-water mark to the resume point.
                # Otherwise peak_pnl still holds the pre-kill high, so the FIRST
                # losing close after resume instantly re-crosses the drawdown
                # limit and re-kills — turning a 4h pause into a day-long lockout
                # of a pair the thesis says is profitable. A genuinely NEW bleed
                # is then measured from where we actually resumed.
                self.state.peak_pnl = self.state.today_pnl

        # Margin sanity
        if margin_usdt > self.max_margin_per_trade_usdt:
            return False, (
                f"margin ${margin_usdt:.2f} exceeds max ${self.max_margin_per_trade_usdt:.2f}"
            )

        # Per-symbol concurrent limit
        sym_count = self.state.open_live_positions.get(symbol, 0)
        if sym_count >= self.max_concurrent_per_symbol:
            return False, f"max_concurrent_per_symbol reached for {symbol}"

        # Total concurrent limit
        total_open = sum(self.state.open_live_positions.values())
        if total_open >= self.max_concurrent_total:
            return False, f"max_concurrent_total ({self.max_concurrent_total}) reached"

        return True, ""

    def record_open(self, symbol: str) -> None:
        """Note that a live position was opened (after successful API call)."""
        self.state.open_live_positions[symbol] = (
            self.state.open_live_positions.get(symbol, 0) + 1
        )

    def drawdown_limit(self) -> float:
        """Плоска межа просадки від піку сесії — $LIVE_MAX_DRAWDOWN, однаково за
        будь-якого розміру позиції. 0 = кіл вимкнено."""
        return self.max_drawdown_usdt

    def record_close(self, symbol: str, pnl_usdt: float,
                     notional_usdt: float | None = None) -> None:
        """Note that a live position was closed.

        notional_usdt teaches the controller this slot's position size, which
        sets the drawdown limit. Omitting it leaves the previous estimate.
        """
        # Decrement counter
        if symbol in self.state.open_live_positions:
            self.state.open_live_positions[symbol] = max(
                0, self.state.open_live_positions[symbol] - 1
            )
            if self.state.open_live_positions[symbol] == 0:
                del self.state.open_live_positions[symbol]

        self.state.today_pnl += pnl_usdt
        self.state.today_trades += 1
        if self.state.today_pnl > self.state.peak_pnl:
            self.state.peak_pnl = self.state.today_pnl

        if pnl_usdt < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        # PRIMARY kill — PEAK DRAWDOWN: PnL fell >= limit below the session high.
        # The real "bleed" catch: fires even inside a net-positive day, is not
        # masked by earlier profit, and does not depend on the UTC-midnight reset.
        # Learn the size. EMA because margin and leverage are randomised per
        # trade by ~15% and the limit should not jitter with them.
        if notional_usdt and notional_usdt > 0:
            a = self.state.avg_notional_usdt
            self.state.avg_notional_usdt = (
                notional_usdt if a <= 0 else 0.9 * a + 0.1 * notional_usdt)

        dd_limit = self.drawdown_limit()
        drawdown = self.state.peak_pnl - self.state.today_pnl
        self._log_equity(drawdown, dd_limit)
        if dd_limit > 0 and drawdown >= dd_limit:
            self.engage_kill(
                reason=(f"drawdown ${drawdown:.2f} from session peak "
                        f"${self.state.peak_pnl:.2f} (limit ${dd_limit:.2f})"),
                duration_sec=self.kill_pause_sec,
            )

    def _log_equity(self, drawdown: float, dd_limit: float) -> None:
        """Make the peak and the drawdown visible without spamming the log.

        One line when the peak advances by a quarter of the limit, and one each
        time the drawdown deepens past another quarter of it. At ~2,000 trades a
        day a line per close would be unreadable.
        """
        # Heartbeat: the step thresholds below only fire at 25% of the limit,
        # which at ordinary PnL is once in hours. This keeps the peak visible.
        self.state._closes_since_log += 1
        _now = time.time()
        if self.state._last_log_ts <= 0:
            self.state._last_log_ts = _now
        if (self.state._closes_since_log >= 100
                or _now - self.state._last_log_ts >= 900):
            self.state._closes_since_log = 0
            self.state._last_log_ts = _now
            logger.info(
                "[EQUITY] %d угод: PnL $%+.2f, пік $%+.2f, просадка $%.2f з $%.2f "
                "(позиція ~$%.0f)",
                self.state.today_trades, self.state.today_pnl, self.state.peak_pnl,
                drawdown, dd_limit, self.state.avg_notional_usdt)

        if dd_limit <= 0:
            # peak-drawdown вимкнено — крокові рядки безглузді (спам на
            # кожному піку). Хартбіт вище лишається.
            return
        step = int(drawdown / dd_limit * 4) if dd_limit > 0 else 0
        if step > self.state._logged_dd_step:
            self.state._logged_dd_step = step
            logger.info(
                "[EQUITY] slot PnL $%+.2f, peak $%+.2f, drawdown $%.2f of $%.2f "
                "(%.0f%%, position ~$%.0f)",
                self.state.today_pnl, self.state.peak_pnl, drawdown, dd_limit,
                100 * drawdown / dd_limit, self.state.avg_notional_usdt)
        elif self.state.peak_pnl >= self.state._logged_peak + dd_limit / 4:
            self.state._logged_peak = self.state.peak_pnl
            self.state._logged_dd_step = 0
            logger.info(
                "[EQUITY] slot new peak $%+.2f (limit $%.2f, position ~$%.0f)",
                self.state.peak_pnl, dd_limit, self.state.avg_notional_usdt)
        elif step == 0:
            self.state._logged_dd_step = 0

    def engage_kill(self, reason: str, duration_sec: int = 0) -> None:
        """
        Activate kill switch. duration_sec=0 means indefinite.

        Existing positions are NOT auto-closed by this. Caller must
        explicitly close them via close_all_live_positions().
        """
        now = int(time.time())
        candidate = (now + duration_sec) if duration_sec > 0 else 0  # 0 = indefinite
        # Only ever EXTEND an active halt, never shorten it. With one automatic
        # kill left nothing here collides today, but the rule is what makes a
        # second engage_kill during an active halt safe: a later, shorter
        # deadline must not resume trading early. Keep whichever reaches further
        # into the future (0 = indefinite always wins).
        if self.state.kill_active:
            cur = self.state.kill_until_ts
            if cur == 0:
                keep_existing = True                 # already indefinite
            elif candidate == 0:
                keep_existing = False                 # new indefinite beats finite
            else:
                keep_existing = cur >= candidate      # keep the longer deadline
            if keep_existing:
                logger.info(
                    "Kill already active with a longer/equal halt (until %s) — "
                    "keeping it; not shortening for '%s'",
                    cur or "indefinite", reason,
                )
                return
        self.state.kill_active = True
        self.state.kill_reason = reason
        self.state.kill_until_ts = candidate
        logger.warning(
            "🚨 KILL SWITCH ENGAGED: %s (duration=%ss)", reason, duration_sec or "indefinite"
        )

    def release_kill(self) -> tuple[bool, str]:
        """Operator override: lift the halt on this slot.

        Returns (was_active, the_reason_it_had).

        peak_pnl is re-baselined to the current PnL for the same reason the
        expiry path in can_open_live does it: leaving the pre-kill high-water
        mark standing means the next losing close instantly re-crosses the
        drawdown limit and kills again, so lifting the halt would buy exactly
        one trade. The slot gets its full drawdown allowance
        (pct x avg_notional) back from where it now stands.

        today_pnl and consecutive_losses are NOT reset — the day's real PnL
        stays on the record and in the halt alert. This is an override:
        pressing it repeatedly through a genuine bleed will keep the slot
        trading.
        """
        was_active = self.state.kill_active
        reason = self.state.kill_reason
        self.state.kill_active = False
        self.state.kill_reason = ""
        self.state.kill_until_ts = 0
        self.state.peak_pnl = self.state.today_pnl
        self.state._logged_dd_step = 0
        self.state._logged_peak = self.state.today_pnl
        if was_active:
            logger.warning(
                "Kill switch RELEASED by operator (was: %s) — drawdown baseline "
                "reset to $%.2f", reason, self.state.today_pnl)
        return was_active, reason

    def is_killed(self) -> bool:
        self._maybe_reset_daily()
        if not self.state.kill_active:
            return False
        if self.state.kill_until_ts == 0:
            return True
        return int(time.time()) < self.state.kill_until_ts

    def _effective_kill(self) -> tuple[float, str]:
        """Число й підстава кіла, за яким слот справді стане ЗАРАЗ.

        peak-drawdown (якщо ввімкнений) б'є першим на просадці від піку;
        денний поріг — на сукупному збитку. Показуємо той, що активний, щоб
        екран/алерт не брехали «межа $0», коли %-просадку вимкнено.
        """
        if self.max_drawdown_usdt > 0:
            return (self.max_drawdown_usdt,
                    f"стоп ${self.max_drawdown_usdt:.0f} від піку сесії")
        return 0.0, "кіл вимкнено"

    def state_summary(self) -> dict:
        # drawdown_limit_usdt — ЕФЕКТИВНА межа зараз, а не те, що в .env.
        # Без неї жоден екран не показує число, за яким слот справді стане.
        from datetime import datetime
        kill_until_human = "indefinite"
        if self.state.kill_until_ts > 0:
            kill_until_human = datetime.utcfromtimestamp(self.state.kill_until_ts).strftime("%H:%M:%S UTC")
        return {
            "kill_active": self.state.kill_active,
            "kill_reason": self.state.kill_reason,
            "kill_until_ts": self.state.kill_until_ts,
            "kill_until_human": kill_until_human,
            "today_pnl": round(self.state.today_pnl, 4),
            "peak_pnl": round(self.state.peak_pnl, 4),
            "session_start_ts": self.session_start_ts(),
            "drawdown": round(self.state.peak_pnl - self.state.today_pnl, 4),
            "drawdown_limit_usdt": round(self._effective_kill()[0], 2),
            "drawdown_limit_basis": self._effective_kill()[1],
            "avg_notional_usdt": round(self.state.avg_notional_usdt, 2),
            "today_trades": self.state.today_trades,
            "consecutive_losses": self.state.consecutive_losses,
            "open_live_positions": dict(self.state.open_live_positions),
        }
