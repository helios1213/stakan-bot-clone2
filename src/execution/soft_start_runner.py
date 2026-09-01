"""Wire the soft-start button to the running bot.

One loop watches `webkey_slots.soft_start_enabled` and keeps a warming engine
alive per enabled slot. Flipping the Telegram button is all it takes — no
restart, no redeploy.

Per slot it runs both halves of the spec:
  * SpotSoftStart     — 1-4 tokens/day, 0-10 buys, 0-10 sells, 1-150 USDT,
                        always keeping a baseline hold of each token.
  * FuturesSoftStart  — 1-3 orders/day, hold 10-300min, 3-10h apart, and ONLY
                        on pairs this account trades at 0% (checked per open).

Safety:
  * DRY-RUN unless `SOFT_START_LIVE=1` is set in the environment. The button
    alone can never start sending orders — two independent gates.
  * Turning the button OFF closes any futures position the slot is holding
    before the engine is dropped — and if that close FAILS, the engine is kept
    (idle, never trading) so every poll retries it. Dropping it on a failed
    close is exactly what orphans a live position on the exchange.
  * A slot where the arb strategy is live does NOT get a futures warmer: two
    systems on one account close each other's positions.
  * A slot whose engine raises is isolated: it is logged and skipped, and the
    other slots keep running.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random

from .fee_gate import FeeGate
from .futures_soft_start import FuturesSoftStart, FuturesSoftStartConfig, live_allowed
from .soft_start_budget import (DEFAULT_MAX_COST_USDT, MIN_VIABLE_BALANCE_USDT,
                                SoftStartBudget, scale_spot_config)
from .soft_start_campaign import DEFAULT_CAMPAIGN_DAYS, SoftStartCampaign
from .soft_start_reporter import SoftStartReporter
from .spot_soft_start import SoftStartConfig, SpotSoftStart
from .webkey.spot_client import SpotWebClient

logger = logging.getLogger(__name__)

POLL_SEC = 60

# Скільки вартості монет лишається на балансі після кампанії. Не нуль:
# рахунок, вичищений у нуль рівно в мить завершення прогріву, — це теж
# патерн, і помітніший за невеликий залишок.
SPOT_WIND_DOWN_KEEP = 0.20

# Кандидати на спотовий прогрів. Юніверс був `("MX",)` — один токен, тобто
# всі покупки йшли по MX, а `tokens_per_day_max=4` не мав сенсу взагалі.
#
# Список — ЛІКВІДНІ USDT-пари, кожна перевірена на резолвність currencyId
# (2026-08-26, публічна сторінка пари, без авторизації: усі 17 віддали
# ps/qs). Фʼючерсні «акційні» контракти (SOXL, SNDK, SKHYNIX, SPCX, MU) сюди
# НЕ входять — на споті їх немає.
#
# Із цього списку кампанія розігрує СВІЙ набір (3-5 токенів) один раз і
# памʼятає його: кожен куплений токен лишає базовий залишок, який не можна
# продати, тож нова монета щодня заблокувала б увесь баланс.
SPOT_CANDIDATES = ("MX", "DOGE", "XRP", "SOL", "TRX", "ADA", "LTC", "SHIB",
                   "PEPE", "LINK", "SUI", "ONDO", "ENA", "WLD", "PENGU",
                   "XLM", "AVAX")

# Джерело — spot_soft_start: там її використовує розпродаж, і дві копії
# однієї константи рано чи пізно розійшлись би.
from .spot_soft_start import USDT_CURRENCY_ID  # noqa: E402  (re-export)


class SlotWarmer:
    """Both warming engines for one slot, sized from the slot's real balances
    and sharing one spend ceiling."""

    def __init__(self, slot_id: int, webkey: str, client, universe: list[str],
                 *, dry_run: bool, data_dir: str = "/app/data",
                 max_cost_usdt: float = DEFAULT_MAX_COST_USDT,
                 campaign_days: int = DEFAULT_CAMPAIGN_DAYS,
                 alerts=None, futures_allowed: bool = True) -> None:
        self.slot_id = slot_id
        self.client = client
        self.universe = universe
        self.dry_run = dry_run
        self.data_dir = data_dir
        # False when the arb strategy is live on this slot: two systems opening
        # futures positions on one account fight each other — the reconciler
        # closes the warming position as an orphan, and a symbol-wide close
        # from soft-start takes the arb position down with it.
        self.futures_allowed = futures_allowed
        # Set once OFF has been requested: the slot may still have to be
        # drained, but it must never trade again.
        self.draining = False
        # Останній надрукований зважений план — щоб рядок не повторювався
        # щотіку (тік іде раз на POLL_SEC, а план змінюється раз на добу).
        self._weighted_logged: tuple | None = None
        # Розпродаж наприкінці кампанії робиться один раз; прапорець не дає
        # крутити його вічно, якщо продавати вже нічого.
        self._wound_down = False
        # Виміряна вартість монет на біржі + коли міряли. None = ще не міряли.
        self._held_market: float | None = None
        self._held_market_at: float = 0.0
        self._stop_attempts = 0
        self._final_sent = False
        self.fee_gate = FeeGate(client)
        # ONE ceiling for both halves: the operator's limit is on warming as a
        # whole, not per venue.
        self.budget = SoftStartBudget(
            f"{data_dir}/soft_start_budget_slot{slot_id}.json", max_cost_usdt)
        # Warming is a finite 3-day job, not a permanent mode.
        self.campaign = SoftStartCampaign(
            f"{data_dir}/soft_start_campaign_slot{slot_id}.json", campaign_days)
        # slot_id -> той самий профіль пристрою, що й на фʼючерсному шляху
        # цього слота. Без нього спот ходив під ІНШИМ відбитком з тієї ж
        # IP і того ж акаунта.
        # Відбиток АКАУНТА — не сам ключ. Потрібен, щоб помітити заміну
        # вебкея: стан прогріву лежить per-slot, і новий акаунт успадкував би
        # чужу кампанію разом із її грошима в обліку.
        import hashlib
        self._account_key = hashlib.sha256(
            (webkey or "").encode("utf-8")).hexdigest()[:16]
        self.spot_client = SpotWebClient(webkey, dry_run=dry_run,
                                         slot_id=slot_id)
        self.spot: SpotSoftStart | None = None
        self.futures: FuturesSoftStart | None = None
        # Live status message in Telegram. None disables reporting entirely —
        # warming must work with or without it.
        self.reporter = (SoftStartReporter(alerts, slot_id, dry_run)
                         if alerts is not None else None)

    async def _read_balances(self) -> tuple[float, float]:
        """(spot_usdt, futures_usdt). A read failure returns 0.0, and 0.0 means
        'too small to warm' downstream — failing closed rather than guessing a
        balance and sizing orders off a fiction.

        СВІДОМО НЕ ГЕЙТИТЬСЯ `dry_run`. Так, натискання 🌱 одразу шле
        автентифікований GET — але це ЧИТАННЯ балансу, воно нічого не рухає, і
        без нього дай-ран сайзив би ордери з вигаданого числа, тобто перестав
        би бути репетицією. Аудит 2026-08-26 підняв цей шлях справедливо, але
        проблемою був ВІДБИТОК (спот ходив під `Chrome/151` при TLS 146 і без
        `sec-ch-ua`), а не сам факт запиту. Відбиток полагоджено — спот тепер
        бере профіль того ж слота, що й фʼючерси."""
        spot = fut = 0.0
        try:
            bals = await self.spot_client.balances([USDT_CURRENCY_ID])
            spot = float(bals.get("USDT", {}).get("available", 0) or 0)
        except Exception as e:
            logger.warning("soft-start slot %d: spot balance read failed: %s",
                           self.slot_id, e)
        try:
            r = await self.client._request("GET", "/account/assets")
            for row in (r or {}).get("data") or []:
                if str(row.get("currency")).upper() == "USDT":
                    fut = float(row.get("availableBalance")
                                or row.get("availableCash") or 0)
                    break
        except Exception as e:
            logger.warning("soft-start slot %d: futures balance read failed: %s",
                           self.slot_id, e)
        return spot, fut

    async def _spot_universe(self) -> list[str]:
        """Токени кампанії, звірені з біржею ПЕРЕД використанням.

        Резолвимо лише розіграний набір (3-5), а не всі 17 кандидатів: це
        публічні запити, але зайві. Токен, що не резолвиться (делістинг, зміна
        сторінки), просто випадає — краще гріти меншим набором, ніж кидати
        ордери, які біржа відхилить. Якщо не лишилось нічого, падаємо на MX:
        він і був єдиним юніверсом досі, тобто це рівно стара поведінка.
        """
        pool = self.campaign.token_pool(SPOT_CANDIDATES)
        ok: list[str] = []
        for t in pool:
            try:
                await self.spot_client._resolver.resolve(t)
                ok.append(t)
            except Exception as e:
                logger.warning("soft-start slot %d: токен %s не резолвиться "
                               "(%s) — пропускаю", self.slot_id, t, e)
        if not ok:
            logger.warning("soft-start slot %d: жоден токен набору не "
                           "резолвиться — залишаюсь на MX", self.slot_id)
            return ["MX"]
        logger.info("soft-start slot %d: спотовий набір %s", self.slot_id, ok)
        return ok

    async def start(self) -> None:
        spot_bal, fut_bal = await self._read_balances()
        logger.info("soft-start slot %d: balances spot=%.2f futures=%.2f USDT",
                    self.slot_id, spot_bal, fut_bal)

        # Sizing scales with the balance: 25 USDT warms gently, 50 warms harder,
        # same config object either way.
        sizing = scale_spot_config(spot_bal)
        tokens = await self._spot_universe()
        spot_cfg = SoftStartConfig(
            universe=tuple(tokens),
            state_path=f"{self.data_dir}/spot_soft_start_slot{self.slot_id}.json",
            order_usdt_min=sizing.order_usdt_min,
            order_usdt_max=sizing.order_usdt_max,
            baseline_usdt_per_token=sizing.baseline_usdt_per_token,
            daily_buy_usdt_ceiling=max(sizing.daily_buy_usdt_ceiling,
                                       sizing.order_usdt_max),
        )
        self.spot = SpotSoftStart(self.spot_client, spot_cfg,
                                  rng=random.Random(), budget=self.budget,
                                  on_action=self.campaign.bump)
        self.futures = FuturesSoftStart(
            self.client, self.fee_gate, self.universe,
            FuturesSoftStartConfig(
                state_path=f"{self.data_dir}/futures_soft_start_slot{self.slot_id}.json"),
            dry_run=self.dry_run, rng=random.Random(),
            budget=self.budget, balance_usdt=fut_bal,
            on_action=self.campaign.bump,
        )
        # ВІД УСЬОГО СПОТА, а не від вільного USDT — інакше глухий кут.
        #
        # Було: `spot_bal` це ВІЛЬНИЙ USDT. Спот купував монети, доки USDT не
        # закінчувався, після чого половина вимикалась за порогом — і, будучи
        # вимкненою, не могла ПРОДАТИ, щоб повернути USDT. Живий випадок
        # 31.08, primary слот 2: вільних 3.24, у монетах 22.53, спот 0/9
        # купівель і 0/19 продажів за добу. Гроші є, а половина стоїть.
        #
        # USDT потрібен лише для КУПІВЛІ; для продажу потрібні монети, і їх
        # вистачає. Тому поріг рахується від повної спотової вартості, а
        # неможливість купити гейтиться окремо, у `maybe_buy`.
        coins_val = 0.0
        try:
            coins_val = await self.spot_client_coins_value(tokens)
        except Exception:
            logger.debug("soft-start slot %d: вартість монет не прочитана",
                         self.slot_id, exc_info=True)
        spot_total = spot_bal + coins_val
        self._spot_viable = spot_total >= MIN_VIABLE_BALANCE_USDT
        if coins_val:
            logger.info("soft-start slot %d: спот — вільних %.2f + монет %.2f "
                        "= %.2f USDT", self.slot_id, spot_bal, coins_val,
                        spot_total)
        self._fut_viable = (fut_bal >= MIN_VIABLE_BALANCE_USDT
                            and self.futures_allowed)
        if not self.futures_allowed:
            logger.warning("soft-start slot %d: the arb strategy is LIVE on this "
                           "slot — the futures half stays idle (two systems on "
                           "one account close each other's positions)",
                           self.slot_id)
        for name, ok, bal in (("spot", self._spot_viable, spot_bal),
                              ("futures", self._fut_viable, fut_bal)):
            if not ok:
                logger.warning("soft-start slot %d: %s balance %.2f < %.0f USDT "
                               "— that half stays idle", self.slot_id, name, bal,
                               MIN_VIABLE_BALANCE_USDT)

        started = self.campaign.start_if_new(self._account_key)
        if started:
            # НОВА КАМПАНІЯ -> ЧИСТИЙ ОБЛІК. Бюджет і денний план належали
            # ПОПЕРЕДНІЙ кампанії (а при заміні вебкея — взагалі іншому
            # акаунту): його витрати, його позиції за собівартістю, його
            # legacy-монети. Лишити їх означало б рахувати чужі гроші як свої.
            self._reset_accounting()
        if started and self.reporter is not None:
            try:
                await self.reporter.campaign_started(self.campaign.state.days,
                                                     **self._status())
            except Exception as e:
                logger.debug("soft-start reporter: start notice failed: %s", e)
        logger.info("soft-start slot %d: campaign day %d/%d, %.2f day(s) left",
                    self.slot_id, self.campaign.state.day_index() + 1,
                    self.campaign.state.days, self.campaign.state.remaining_days())
        await self.futures.recover()

    def _reset_accounting(self) -> None:
        """Скинути бюджет і денний план під нову кампанію.

        ФʼЮЧЕРСНИЙ СТАН НЕ ЧІПАЄМО — і це не забудькуватість. Там може лежати
        ВІДКРИТА позиція; стерши запис, ми осиротили б її на біржі назавжди.
        Він і так перекидається за датою. Якщо позиція лишилась від ІНШОГО
        акаунта (замінили вебкей із відкритою позицією) — закрити її новим
        ключем неможливо в принципі, тому про це кричимо, а не приховуємо.
        """
        try:
            pos = (self.futures.state.position if self.futures else None)
            pend = (self.futures.state.pending if self.futures else None)
            if pos or pend:
                logger.critical(
                    "🚨 [SLOT %d] нова кампанія, але у фʼючерсному стані "
                    "лишилась позиція/запит (%s). Якщо вебкей міняли — ця "
                    "позиція належить СТАРОМУ акаунту і новим ключем не "
                    "закриється: перевір біржу руками.",
                    self.slot_id, (pos or pend))
        except Exception:
            pass
        try:
            self.budget.reset()
            logger.info("soft-start slot %d: облік обнулено під нову кампанію",
                        self.slot_id)
        except Exception:
            logger.exception("soft-start slot %d: бюджет не обнулено",
                             self.slot_id)
        try:
            import os
            p = f"{self.data_dir}/spot_soft_start_slot{self.slot_id}.json"
            if os.path.exists(p):
                os.remove(p)          # денний план перерозіграється з нуля
        except Exception:
            logger.debug("soft-start slot %d: денний план не прибрано",
                         self.slot_id, exc_info=True)

    @property
    def held_is_measured(self) -> bool:
        """Звідки взялось «у монетах»: з БІРЖІ чи з обліку.

        Підпис у звіті мусить це розрізняти — «за ціною купівлі» на ринковому
        числі це просто неправда, а різниця між ними буває в рази.
        """
        return getattr(self, "_held_market", None) is not None

    def _snapshot(self) -> tuple:
        """The counters that tell us an action happened, for diffing a tick."""
        sp, fu = self.spot.plan, self.futures.state
        pos = (fu.position or {}).get("symbol")
        return (sp.buys_done, sp.sells_done, fu.orders_done, pos)

    async def _report_diff(self, before: tuple, after: tuple) -> None:
        """Turn a before/after tick diff into operator-facing alerts.

        Diffing rather than calling the reporter from inside the engines: the
        engines stay unaware of Telegram, and there is exactly one place that
        decides what is worth announcing.
        """
        if self.reporter is None or before == after:
            return
        st = self._status()
        b0, s0, o0, p0 = before
        b1, s1, o1, p1 = after
        # ФАКТИЧНІ числа з рушія, а не стеля конфігу. Було
        # `spot_buy("spot", cfg.order_usdt_max, "~")` — тобто в кожному рядку
        # звіту стояла МАКСИМАЛЬНА сума розміру (3.00) незалежно від того, що
        # реально пішло (1.5-2.2), і порожня кількість. Звіт, що показує
        # константу замість виміру, гірший за відсутній: за ним неможливо
        # помітити, що розмір не змінюється.
        act = getattr(self.spot, "last_action", None) or {}
        sym = act.get("symbol") or "spot"
        qty = act.get("qty") or "?"
        # ЛІЧИЛЬНИКИ ТУТ БІЛЬШЕ НЕ РАХУЮТЬСЯ. Вони переїхали в самі рушії
        # (`on_action`), бо тут стояли ЗА перевіркою `reporter is None` — тобто
        # без Telegram не рахувались узагалі — і диференціювання знімків
        # губило дію, що почалась і скінчилась між тіками.

        try:
            if b1 > b0:
                await self.reporter.spot_buy(
                    sym, float(act.get("usdt") or 0.0), str(qty), **st)
            if s1 > s0:
                await self.reporter.spot_sell(
                    sym, str(qty), usdt=float(act.get("usdt") or 0.0), **st)
            if p1 and p1 != p0:
                pos = self.futures.state.position or {}
                hold = int(((pos.get("close_after") or 0) - (pos.get("opened_at") or 0)) / 60)
                await self.reporter.futures_open(
                    p1, int(pos.get("side") or 1), int(pos.get("leverage") or 0),
                    hold, vol=int(pos.get("vol") or 0),
                    notional=float(pos.get("notional") or 0.0), **st)
            elif p0 and not p1:
                # Тримання і PnL — із `last_closed`, який ставить сам рушій.
                # Раніше сюди йшов літерал 0.0, і звіт друкував «after 0min»
                # на позиції, що трималась 53 хвилини: число було не порожнє, а
                # ВИГАДАНЕ, що гірше — воно виглядає як вимір.
                last = getattr(self.futures, "last_closed", None) or {}
                held = (float(last.get("held_min") or 0.0)
                        if last.get("symbol") == p0 else 0.0)
                await self.reporter.futures_close(
                    p0, held, realised=last.get("realised"), **st)
        except Exception as e:
            logger.debug("soft-start reporter diff failed: %s", e)

    def _status(self) -> dict:
        pos = (self.futures.state.position or {}).get("symbol") if self.futures else None
        return {
            "day": self.campaign.state.day_index() + 1,
            "days": self.campaign.state.days,
            "spent": self.budget.spent,
            # None = стелі немає. Облік витрат лишився, ліміт прибрано.
            "ceiling": None,
            # Рух ринку: реалізований PnL фʼючерсів + спотовий кеш-фло.
            "pnl": self.budget.pnl,
            # Окремо: єдиний справжній прибуток/збиток. Спотова частина `pnl`
            # це кеш-фло і в звіті як «PnL» більше не показується.
            "futures_pnl": self.budget.futures_pnl,
            "spot_pnl": self.budget.spot_pnl,
            "held_value": self._held_spot_value(),
            "held_measured": self.held_is_measured,
            "position": pos,
        }

    async def spot_client_coins_value(self, tokens) -> float:
        """Вартість монет на споті — тонка обгортка, щоб `start()` не тягнув
        логіку читання балансів у себе. Порожньо/збій -> 0.0."""
        from .spot_soft_start import SpotSoftStart
        probe = SpotSoftStart.__new__(SpotSoftStart)
        probe.client = self.spot_client
        probe.cfg = SoftStartConfig(state_path="/dev/null",
                                    universe=tuple(tokens or ("MX",)))
        v = await SpotSoftStart.market_value_of_coins(probe, list(tokens or []))
        return float(v or 0.0)

    async def _refresh_held_market(self) -> None:
        """Оновити ВИМІРЯНУ вартість монет. Раз на ~30 хв, не щотіку.

        17 запитів на оновлення — дрібниця раз на пів години і зайвий шум
        щохвилини. Збій лишає попереднє значення, а не обнуляє його.
        """
        import time as _t
        if _t.time() - self._held_market_at < 1800:
            return
        try:
            pool = list(dict.fromkeys(
                list(self.spot.plan.tokens)
                + list(self.campaign.state.tokens or [])
                + list(SPOT_CANDIDATES)))
            v = await self.spot.market_value_of_coins(pool)
        except Exception:
            logger.debug("soft-start slot %d: ринкова вартість монет не "
                         "оновлена", self.slot_id, exc_info=True)
            return
        if v is not None:
            self._held_market = v
            self._held_market_at = _t.time()

    def _held_spot_value(self) -> float:
        """Скільки лежить у монетах — з БЮДЖЕТУ, а не з денного плану.

        Раніше рахувалось як `plan.spent_usdt` (денний план, обнуляється
        щодоби) мінус усі продажі за кампанію. Дві різні часові бази: вже на
        другий день різниця ставала відʼємною, обрізалась у нуль, і «разом»
        показувало +46.57 замість 0.26 — тобто звіт стверджував, що прогрів
        зʼїв 46 USDT, яких ніхто не витрачав.
        """
        # ВИМІРЯНЕ ЗНАЧЕННЯ МАЄ ПРІОРИТЕТ. Облік не може знати дійсності:
        # монети куплені до його появи, оператор докладає кошти, ціни
        # рухаються. На primary облік казав 79.53, а на біржі було 43.96.
        # Облік лишається запасним шляхом, поки біржу не прочитали.
        if self._held_market is not None:
            return float(self._held_market)
        # (джерело позначає `held_is_measured` нижче)
        try:
            return self.budget.held_spot_value
        except Exception:
            return 0.0

    def _apply_day_weight(self) -> None:
        """Reshape today's targets by the day's randomly drawn activity weight.

        Drawn once per campaign-day and persisted, so some days are busy and
        some are nearly quiet — and a restart cannot reroll a quiet day into a
        busy one and double the activity.

        Стеля береться з `campaign.scale_target()`, а не рахується тут наново.
        Раніше та сама формула жила у ДВОХ місцях: `SoftStartCampaign` мав
        `scale_target()`, якого не викликав НІХТО, а тут стояла її копія. Дубль
        нічого не ламав, але наступного разу підказав би неправильну
        відповідь — читаєш `scale_target`, а виконується інше.

        Логування — ТУТ, а не в конструкторі планів. `SpotSoftStart.__init__`
        друкує щойно розіграний план (`buys=9`), а у файл лягає вже зважений
        (`buys_target=8`), і читати лог означало вірити числу, яке не
        виконується. Я сам через це кілька хвилин гнався за фантомним багом.
        """
        sp, fu = self.spot.plan, self.futures.state
        before = (sp.buys_target, sp.sells_target, fu.orders_target)
        # Бази беруться з КОНФІГУ, а не прибиті числами. Були 10/10/3 — рівно
        # ті самі максимуми, що в конфігах; після підняття квоти вони мовчки
        # стали б стелею НИЖЧОЮ за розіграний план, тобто квота піднялась би
        # тільки на папері. Той самий клас, що й дубль формули ваги.
        scfg, fcfg = self.spot.cfg, self.futures.cfg
        sp.buys_target = min(
            sp.buys_target, self.campaign.scale_target(scfg.buys_per_day_max))
        sp.sells_target = min(
            sp.sells_target, self.campaign.scale_target(scfg.sells_per_day_max))
        fu.orders_target = min(
            fu.orders_target, self.campaign.scale_target(fcfg.orders_per_day_max))
        after = (sp.buys_target, sp.sells_target, fu.orders_target)
        if after != before and after != self._weighted_logged:
            logger.info(
                "soft-start slot %d: план дня після ваги %.2f — "
                "купівлі %d->%d, продажі %d->%d, фʼючерси %d->%d",
                self.slot_id, self.campaign.day_weight(),
                before[0], after[0], before[1], after[1], before[2], after[2])
        self._weighted_logged = after

    async def tick(self) -> None:
        if self.spot is None or self.futures is None:
            return                                  # start() has not run yet
        if self.draining:
            return                                  # OFF requested — stop() drains

        if self.campaign.expired():
            # РОЗПРОДАЖ ПЕРЕД ВИМКНЕННЯМ. `maybe_sell` ніколи не продає нижче
            # базового залишку — правильно під час прогріву, але через це в
            # кінці на балансі лишалось усе куплене (на клоні 29.08 це були
            # 4.36 і 7.90 USDT, замкнених назавжди). Тут базовий залишок
            # свідомо ігнорується: гріти більше нічого.
            #
            # ЧОМУ ДО `finish()`, А НЕ ПІСЛЯ: `finished()` вимикає кнопку в БД
            # і викидає warmer, тож після цього продавати вже нікому. Слот
            # лишається увімкненим, поки розпродаж не доведено до кінця.
            if self._spot_viable and not self._wound_down:
                try:
                    # УСІ кандидати, а не лише токени денного плану: монети
                    # накопичуються за всю історію слота, зокрема куплені під
                    # старим юніверсом (на клоні це був MX на 12.10 USDT).
                    _pool = list(dict.fromkeys(
                        list(self.spot.plan.tokens)
                        + list(self.campaign.state.tokens or [])
                        + list(SPOT_CANDIDATES)))
                    sent = await self.spot.wind_down(SPOT_WIND_DOWN_KEEP,
                                                     tokens=_pool)
                    if sent:
                        logger.info("soft-start slot %d: розпродаж — %d ордер(ів)",
                                    self.slot_id, sent)
                        return          # ще один тік на решту
                    self._wound_down = True
                    # ПЕРЕМІРЯТИ ПІСЛЯ РОЗПРОДАЖУ, ігноруючи тротл.
                    #
                    # `_refresh_held_market()` стоїть НИЖЧЕ по tick(), а ця
                    # гілка робить `return` — тобто у фінальному тіку вимір не
                    # оновлювався НІКОЛИ. У звіт ішло значення, зняте ДО
                    # продажу і до 30 хвилин давності: слот 2 показав «у
                    # монетах 9.49», коли на біржі лишалось ~6.19.
                    self._held_market_at = 0.0
                    await self._refresh_held_market()
                    # Без обіцянок: скільки саме лишилось — окремим рядком і
                    # з ВИМІРЯНОГО значення, а не з цілі. Раніше тут стояло
                    # «лишили ~20%» навіть тоді, коли не продалось нічого.
                    logger.info("soft-start slot %d: розпродаж завершено "
                                "(ціль %.0f%%), у монетах зараз ~%.2f USDT",
                                self.slot_id, SPOT_WIND_DOWN_KEEP * 100,
                                self._held_spot_value())
                except Exception:
                    logger.exception("soft-start slot %d: розпродаж упав — "
                                     "вимикаюсь, монети лишаються",
                                     self.slot_id)
                    self._wound_down = True
            # Finite job: stop acting the moment the campaign is over. The loop
            # flips the DB button off so the UI stops claiming it is warming.
            self.campaign.finish()
            return

        if False:   # стелі витрат немає — прогрів не зупиняється по бюджету
            return                                  # ceiling reached; stay quiet

        self._apply_day_weight()
        if self._spot_viable:
            await self._refresh_held_market()
        before = self._snapshot()
        if self._spot_viable:
            await self.spot.tick()
        if self._fut_viable:
            await self.futures.tick()
        elif self.futures.has_exposure():
            # This half is idle (balance too small, or the slot trades live) but
            # something is still open from before. Drain it — never open more.
            await self._drain_futures()
        await self._report_diff(before, self._snapshot())

    def finished(self) -> bool:
        """True when this slot has nothing left to do: campaign over or budget spent."""
        # Кампанія закінчується ЛИШЕ за часом (3 дні). Стелю витрат прибрано
        # свідомо: прогрів має гріти, а не впиратись у ліміт.
        return self.campaign.expired()

    async def _drain_futures(self) -> bool:
        """Resolve any open question, close any open position. True when clean."""
        f = self.futures
        if f is None:
            return True
        try:
            if f.state.needs_exchange_check:
                await f.sweep_exchange()
            if f.state.pending is not None:
                await f.reconcile_pending()
            if f.state.position is not None:
                await f.close_position(forced=True)
        except Exception:
            logger.exception("soft-start slot %d: draining futures failed",
                             self.slot_id)
        return not f.has_exposure()

    async def stop(self) -> bool:
        """Close anything still open before this slot stops being warmed.

        Returns True only when the slot is CLEAN. False means real money is
        still on the exchange — the caller must keep this warmer alive and
        retry, because dropping it here is what orphans a live position.
        """
        self.draining = True
        if self.futures is None or not self.futures.has_exposure():
            return True
        logger.warning("soft-start slot %d: switching OFF with exposure — "
                       "closing it first", self.slot_id)
        clean = await self._drain_futures()
        if not clean:
            self._stop_attempts += 1
            logger.error("soft-start slot %d: close FAILED on OFF (attempt %d) — "
                         "the position is STILL OPEN; keeping the warmer so the "
                         "next poll retries", self.slot_id, self._stop_attempts)
        return clean

    def stuck(self) -> bool:
        """OFF was requested but exposure remains."""
        return self.draining and self.futures is not None and self.futures.has_exposure()


# How many failed OFF-closes between operator alerts. The retry itself runs
# every poll; the alert is throttled so a stuck slot does not spam Telegram.
STUCK_ALERT_EVERY = 30


async def _alert_stuck(w, slot_id: int) -> None:
    """Tell the operator a warming position could not be closed.

    Reporting must never reach the trading loop, so every path is wrapped.
    """
    if w.reporter is None or w._stop_attempts % STUCK_ALERT_EVERY != 1:
        return
    try:
        await w.reporter.skipped(
            f"⚠️ position still OPEN after {w._stop_attempts} close attempt(s) "
            f"— retrying every poll; close it by hand if this persists",
            **w._status())
    except Exception:
        logger.debug("soft-start slot %d: stuck alert failed", slot_id)


async def _final_report(w, slot_id: int, reason: str, *, position_left: bool) -> None:
    """Closing summary — a message that STAYS, so the operator sees how it went."""
    w._final_sent = True
    if w.reporter is None:
        return
    try:
        await w.reporter.final_report(
            reason,
            # Підсумки і тривалість — З КАМПАНІЇ, не з репортера: той
            # створюється наново на кожному рестарті бота.
            stats=dict(w.campaign.state.stats or {}),
            elapsed_h=w.campaign.elapsed_hours(),
            spent=w.budget.spent,
            # Стелі немає — передаємо 0, щоб рядок про неї не зʼявлявся.
            ceiling=0.0,
            pnl=w.budget.pnl,
            futures_pnl=w.budget.futures_pnl,
            spot_pnl=w.budget.spot_pnl,
            held_value=w._held_spot_value(),
            held_measured=w.held_is_measured,
            position_left=position_left)
    except Exception:
        logger.exception("soft-start slot %d: final report failed", slot_id)


async def soft_start_loop(store, client_pool, universe_provider,
                          poll_sec: int = POLL_SEC, alerts=None) -> None:
    """Keep warming engines in sync with the per-slot button.

    `universe_provider()` returns the candidate symbols; the FeeGate narrows
    them to the 0%-fee ones at open time.
    """
    warmers: dict[int, SlotWarmer] = {}
    dry_run = not live_allowed()
    logger.info("soft-start runner: started (%s)",
                "DRY-RUN — set SOFT_START_LIVE=1 to arm" if dry_run else "LIVE")

    while True:
        try:
            slots = await store.list_all()
            wanted = {s.slot_id for s in slots
                      if getattr(s, "soft_start_enabled", False) and s.webkey}

            # Stop warmers whose button was switched off. A warmer is dropped
            # ONLY once it is clean — while a close keeps failing it stays here
            # (idle, never trading) so that every poll retries it.
            for slot_id in list(warmers):
                if slot_id not in wanted:
                    w = warmers[slot_id]
                    try:
                        clean = await w.stop()
                    except Exception:
                        logger.exception("soft-start slot %d: stop failed", slot_id)
                        clean = False
                    if not clean:
                        await _alert_stuck(w, slot_id)
                        continue
                    warmers.pop(slot_id, None)
                    logger.info("soft-start slot %d: OFF", slot_id)

            # Start newly enabled ones.
            for slot in slots:
                sid = slot.slot_id
                if sid not in wanted or sid in warmers:
                    continue
                try:
                    client = await client_pool.get(sid)
                    warmers[sid] = SlotWarmer(
                        sid, slot.webkey, client, universe_provider(),
                        dry_run=dry_run, alerts=alerts,
                        futures_allowed=not getattr(slot, "live_enabled", False))
                    await warmers[sid].start()
                    logger.info("soft-start slot %d: ON (%s)", sid,
                                "dry-run" if dry_run else "LIVE")
                except Exception:
                    logger.exception("soft-start slot %d: failed to start", sid)
                    # Drop it so the next poll retries. Left in place it would
                    # tick as a no-op forever and the slot would never warm.
                    w = warmers.pop(sid, None)
                    if w is not None and getattr(w, "futures", None) is not None:
                        warmers[sid] = w        # it may already hold a position

            # Tick each independently — one bad slot must not stop the others.
            for sid, w in list(warmers.items()):
                if w.draining:
                    continue          # OFF requested; the stop path retries it
                try:
                    await w.tick()
                except Exception:
                    logger.exception("soft-start slot %d: tick failed", sid)
                    continue

                # A finished campaign (or a spent budget) switches ITSELF off in
                # the DB, so the button reflects reality rather than claiming to
                # warm an account nothing is happening on.
                if w.finished():
                    reason = ("campaign finished" if w.campaign.expired()
                              else "spend ceiling reached")
                    logger.info("soft-start slot %d: %s — switching the slot off",
                                sid, reason)
                    try:
                        clean = await w.stop()
                    except Exception:
                        logger.exception("soft-start slot %d: auto-off failed", sid)
                        clean = False

                    # Tell the operator once, the moment we know a position was
                    # left behind — then keep retrying rather than walking away.
                    if not w._final_sent and (clean or w.stuck()):
                        await _final_report(w, sid, reason, position_left=not clean)
                    if not clean:
                        # Do NOT switch the button off and do NOT drop the
                        # warmer: the flag would claim the slot is idle while a
                        # position is still open, and nothing would retry.
                        await _alert_stuck(w, sid)
                        continue
                    try:
                        await store.set_soft_start(sid, False)
                    except Exception:
                        logger.exception("soft-start slot %d: auto-off failed", sid)
                    warmers.pop(sid, None)

        except asyncio.CancelledError:
            for sid, w in warmers.items():
                try:
                    if not await w.stop():
                        logger.error("soft-start slot %d: shutting down with a "
                                     "position STILL OPEN — close it by hand or "
                                     "restart the bot to let recover() do it", sid)
                except Exception:
                    logger.exception("soft-start: shutdown close failed")
            raise
        except Exception:
            logger.exception("soft-start runner: loop error (contained)")

        await asyncio.sleep(poll_sec)
