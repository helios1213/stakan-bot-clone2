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

# USDT on MEXC spot. Known constant — the balances endpoint is addressed by
# currencyId, not by ticker.
USDT_CURRENCY_ID = "128f589271cb4951b03e71e6323eb7be"


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
                                  rng=random.Random(), budget=self.budget)
        self.futures = FuturesSoftStart(
            self.client, self.fee_gate, self.universe,
            FuturesSoftStartConfig(
                state_path=f"{self.data_dir}/futures_soft_start_slot{self.slot_id}.json"),
            dry_run=self.dry_run, rng=random.Random(),
            budget=self.budget, balance_usdt=fut_bal,
        )
        self._spot_viable = spot_bal >= MIN_VIABLE_BALANCE_USDT
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

        if self.campaign.start_if_new() and self.reporter is not None:
            try:
                await self.reporter.campaign_started(self.campaign.state.days,
                                                     **self._status())
            except Exception as e:
                logger.debug("soft-start reporter: start notice failed: %s", e)
        logger.info("soft-start slot %d: campaign day %d/%d, %.2f day(s) left",
                    self.slot_id, self.campaign.state.day_index() + 1,
                    self.campaign.state.days, self.campaign.state.remaining_days())
        await self.futures.recover()

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
            "held_value": self._held_spot_value(),
            "position": pos,
        }

    def _held_spot_value(self) -> float:
        """Скільки USDT зараз лежить у куплених монетах, приблизно.

        Потрібне САМЕ поруч зі спотовим кеш-фло: купівля йде в облік мінусом, і
        поки монету не продано, вона виглядає як збиток, яким не є. Рахуємо з
        плану (витрачено мінус повернуто) — це не ринкова переоцінка, а
        вкладена сума, і саме так вона й підписана у звіті.
        """
        try:
            p = self.spot.plan
            spent = float(getattr(p, "spent_usdt", 0.0) or 0.0)
            back = sum(float(e.get("usdt") or 0.0)
                       for e in (self.budget.state.entries or [])
                       if str(e.get("reason", "")).startswith("PnL spot sell"))
            return max(0.0, spent - back)
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
        sp.buys_target = min(sp.buys_target, self.campaign.scale_target(10))
        sp.sells_target = min(sp.sells_target, self.campaign.scale_target(10))
        fu.orders_target = min(fu.orders_target, self.campaign.scale_target(3))
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
            # Finite job: stop acting the moment the campaign is over. The loop
            # flips the DB button off so the UI stops claiming it is warming.
            self.campaign.finish()
            return

        if False:   # стелі витрат немає — прогрів не зупиняється по бюджету
            return                                  # ceiling reached; stay quiet

        self._apply_day_weight()
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
        await w.reporter.final_report(reason, spent=w.budget.spent,
                                      ceiling=w.budget.state.max_usdt,
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
