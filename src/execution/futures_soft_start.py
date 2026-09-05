"""Futures account warming: open a position, hold it, close it. Slowly, rarely.

Spec (operator's, verbatim intent):
  * open ONLY on pairs with 0% fee                  -> enforced by FeeGate
  * a position stays open 10-300 minutes, never less than 10
  * 3-10 hours of pause BETWEEN orders
  * 1-3 orders per day

This is the module that puts real money at risk, so the safety design is
deliberately heavier than the spot one:

  * **Two independent live switches.** `dry_run=False` AND env `SOFT_START_LIVE=1`.
    Either one missing means nothing is sent.
  * **Fee is re-checked immediately before EVERY open**, not once at planning
    time. A tier can change between the plan and the order.
  * **0% — це ПЕРЕВАГА, а не умова** (змінено 2026-08-26). Пара з `0/0`
    береться першою. Якщо таких немає — прогрів НЕ зупиняється: береться
    найдешевша платна, а її комісія списується у бюджет як витрата. Раніше
    правило `zero_both` вимикало прогрів цілком на акаунті без промо — тобто
    саме там, де він найпотрібніший. `allow_paid_fees=False` повертає старе.
    Обидві ноги — market (type 5), тобто ТЕЙКЕР; у вартість іде `takerFee`,
    помножений на 2. `max_fee_frac` (10 bps/нога) відсікає безглузді пари.
  * **Невідома ставка ≠ безкоштовна і ≠ дешева.** Пара, чию ставку не вдалось
    прочитати, не береться взагалі: без числа не порахувати ані бюджет, ані
    рішення. Це НЕ те саме, що «ставка відома і ненульова».
  * **The open position is persisted the instant it is opened**, with its close
    deadline. If the bot restarts mid-hold, `recover()` finds the position and
    closes it on time instead of leaving it to sit.
  * **One position at a time.** No stacking.
  * **An open that gets no answer is a question, not a failure.** The intent is
    persisted BEFORE the request leaves; a timeout then asks the exchange
    whether it filled, and adopts the position if it did. An exchange that
    cannot be read keeps the question open rather than assuming "nothing
    happened".
  * **A dry-run process never erases a live record.** Unsetting SOFT_START_LIVE
    and restarting cannot make the bot forget a position it really opened.
  * Every order is wrapped: a failure logs and skips, never cascades.

State lives next to the spot state, in data/, so a rebuild does not lose it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LIVE_ENV = "SOFT_START_LIVE"

# MEXC order sides. 1 = open long, 3 = open short (the bot's own convention,
# matching submit_order(side=...) elsewhere in this package).
SIDE_LONG = 1
SIDE_SHORT = 3

ORDER_TYPE_MARKET = "5"

# Крок очікування між спробами прочитати історію закритої позиції. Константа,
# а не літерал, щоб сюїта могла її стиснути: інакше кожен тест на цьому шляху
# чесно спить секундами (файл виріс з 1.6с до 15.5с на першому ж прогоні).
HISTORY_RETRY_DELAY_SEC = 1.5

# Скільки разів перепитати біржу, чи відкрилась позиція, коли відповідь на
# ордер загубилась. Видимість філу на MEXC відстає на ~50-200мс, а швидка
# мережева відмова повертається за ~150мс — тобто перше читання цілком може
# застати «нічого немає» там, де позиція вже є.
PENDING_RECHECKS = 4
PENDING_RECHECK_DELAY_SEC = 1.5


@dataclass
class FuturesSoftStartConfig:
    orders_per_day_min: int = 1
    # 3 -> 6 (рішення оператора 2026-08-26). Пауза 3-10 год між відкриттями
    # лишається, тож 6 ордерів фізично влазять лише при коротких паузах —
    # це стеля, а не ціль.
    orders_per_day_max: int = 6
    hold_minutes_min: int = 10           # spec: never less than 10
    hold_minutes_max: int = 300
    pause_hours_min: float = 3.0
    pause_hours_max: float = 10.0
    margin_usdt_min: float = 5.0
    margin_usdt_max: float = 20.0
    leverage_min: int = 5
    leverage_max: int = 20
    long_ratio: float = 0.5              # share of opens that go long
    # Було: прогрів ВІДМОВЛЯВСЯ від пари, якщо тейкер не нуль. Тобто на акаунті
    # без промо прогрів не йшов узагалі — а це саме той акаунт, якому прогрів
    # потрібен найбільше. Тепер нуль — це ПЕРЕВАГА, а не умова: платні пари
    # беруться теж, а комісія списується у бюджет як витрата.
    # Пріоритет 0/0 лишається, але він БЕЗУМОВНИЙ: `pick_pair` повертає першу
    # ж пару з `zero_both`, а прапорця `require_zero_taker` не читає ніхто
    # (греп по src/ і tests/: лише оголошення нижче). `allow_paid_fees=False`
    # повертає стару жорстку поведінку.
    # Години, у які дозволено ВІДКРИВАТИ. Спотова половина такий гейт мала з
    # самого початку, фʼючерсна — ні, і 26.08 вона відкрила позицію о 01:35
    # ночі, поки спот законно спав. Одна половина суворо тримається людських
    # годин, друга торгує о третій ночі — це внутрішня неузгодженість, а саме
    # неузгодженості й шукають системи, що ловлять автоматизацію.
    # ЗАКРИТТЯ ЦИМ НЕ ГЕЙТИТЬСЯ І НЕ МОЖЕ БУТИ: позиція, відкрита о 22:30 з
    # триманням 300 хв, мусить закритись о 03:30, інакше ми її осиротимо.
    active_hour_start: int = 6
    active_hour_end: int = 23
    require_zero_taker: bool = True      # спершу пробувати пари з 0/0
    allow_paid_fees: bool = True         # якщо 0% немає — гріти платно і рахувати
    max_fee_frac: float = 0.001          # стеля: 10 bps за ногу, вище — не гріти
    state_path: str = "/app/data/futures_soft_start_state.json"

    def validate(self) -> None:
        if self.hold_minutes_min < 10:
            raise ValueError("hold_minutes_min < 10 violates the spec")
        if self.hold_minutes_max < self.hold_minutes_min:
            raise ValueError("hold window inverted")
        if self.pause_hours_min < 0 or self.pause_hours_max < self.pause_hours_min:
            raise ValueError("pause window invalid")
        if not 1 <= self.orders_per_day_min <= self.orders_per_day_max:
            raise ValueError("orders per day invalid")
        if not 0 <= self.active_hour_start < self.active_hour_end <= 24:
            raise ValueError("active hours invalid")
        if self.margin_usdt_min <= 0 or self.margin_usdt_max < self.margin_usdt_min:
            raise ValueError("margin range invalid")
        if not 1 <= self.leverage_min <= self.leverage_max:
            raise ValueError("leverage range invalid")


@dataclass
class OpenPosition:
    symbol: str
    side: int
    vol: int
    leverage: int
    opened_at: float
    close_after: float          # epoch seconds — the hold deadline
    # Was this position actually SENT to the exchange? The dry path never
    # persists a position, so any record loaded from disk is a real one —
    # hence the default. A dry-run process must never erase such a record.
    opened_live: bool = True

    def due(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.close_after

    def held_minutes(self, now: float | None = None) -> float:
        return ((now or time.time()) - self.opened_at) / 60.0


@dataclass
class FuturesState:
    date: str = ""
    orders_target: int = 0
    orders_done: int = 0
    next_open_at: float = 0.0            # epoch seconds; the 3-10h pause
    position: dict | None = None         # asdict(OpenPosition) while holding
    # An order we are about to send, or have sent without hearing back. Written
    # BEFORE the request leaves the process, so a timeout or a crash mid-flight
    # still leaves a breadcrumb telling us which symbol to ask the exchange
    # about. An unresolved `pending` is treated as possible live exposure.
    pending: dict | None = None
    # Set when the state file could not be read: we no longer know what the
    # account holds, so the exchange has to be asked before anything is opened.
    needs_exchange_check: bool = False

    def is_today(self) -> bool:
        return self.date == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state(path: str) -> FuturesState | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return FuturesState(**json.loads(p.read_text()))
    except Exception as e:
        # An unreadable file is NOT "no position" — that is exactly how a live
        # position gets forgotten. Keep the bad file for forensics and hand back
        # a state that forces an exchange check before anything is opened.
        logger.error("futures soft-start: STATE UNREADABLE (%s) — the exchange "
                     "will be asked what this account actually holds", e)
        try:
            p.rename(p.with_suffix(p.suffix + f".corrupt.{int(time.time())}"))
        except Exception as e2:                       # pragma: no cover - fs edge
            logger.warning("could not preserve the corrupt state file: %s", e2)
        return FuturesState(needs_exchange_check=True)


def save_state(path: str, st: FuturesState) -> None:
    """Write the state ATOMICALLY.

    A half-written file reads back as corrupt, and corrupt used to mean "no
    position". tmp + os.replace means a reader sees either the old state or the
    new one, never a truncated one.
    """
    try:
        p = Path(path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(st), indent=2))
        os.replace(tmp, p)
    except Exception as e:
        # A lost state file means a forgotten open position, so this is loud.
        logger.error("futures soft-start: STATE SAVE FAILED (%s) — "
                     "an open position may not be recovered after a restart", e)


def live_allowed() -> bool:
    return os.environ.get(LIVE_ENV, "") in ("1", "true", "yes")


class FuturesSoftStart:
    """Open -> hold 10-300min -> close, 1-6 times a day, 3-10h apart.

        ss = FuturesSoftStart(client, fee_gate, universe=[...])
        await ss.recover()          # close anything left open by a restart
        while True:
            await ss.tick()
            await asyncio.sleep(60)
    """

    def __init__(self, client, fee_gate, universe: list[str],
                 cfg: FuturesSoftStartConfig | None = None,
                 *, dry_run: bool = True, rng: random.Random | None = None,
                 budget=None, balance_usdt: float | None = None, on_action=None) -> None:
        # `budget`: shared spend ceiling (None = uncapped). `balance_usdt`: the
        # futures wallet, used to size the position and to refuse one that would
        # tie up too much of the account. Both optional so existing callers and
        # tests keep working unchanged.
        self.budget = budget
        self.balance_usdt = balance_usdt
        self._meta_cache: dict[str, tuple[float, float]] = {}
        self.cfg = cfg or FuturesSoftStartConfig()
        self.cfg.validate()
        self.client = client
        self.fee_gate = fee_gate
        # Деталі останнього закриття — читає раннер для звіту (тримання, PnL).
        # Раніше звіт друкував «after 0min», бо тривалості не було звідки взяти.
        self.last_closed: dict | None = None
        # Лічильник дій кампанії — див. коментар у spot_soft_start: диф у
        # раннері стояв за перевіркою репортера і губив короткі цикли.
        self.on_action = on_action
        # Наша ОЦІНКА комісії за round-trip останнього відкриття — щоб на
        # закритті звірити її з тим, що біржа взяла насправді.
        self._last_fee_estimate: float = 0.0
        self.universe = list(universe)
        self.rng = rng or random.Random()
        self.dry_run = bool(dry_run)
        self.state = load_state(self.cfg.state_path) or FuturesState()
        if not self.state.is_today():
            self._roll_day()
        if self.dry_run:
            logger.info("FuturesSoftStart: DRY-RUN — nothing will be sent")
        elif not live_allowed():
            logger.warning("FuturesSoftStart: dry_run=False but %s is not set — "
                           "staying inert", LIVE_ENV)
        else:
            logger.warning("FuturesSoftStart: LIVE — real positions will be opened")

    @property
    def sending(self) -> bool:
        """Both switches must agree before anything leaves the process."""
        return (not self.dry_run) and live_allowed()

    # ---- planning -------------------------------------------------------

    def _roll_day(self) -> None:
        c = self.cfg
        self.state = FuturesState(
            date=_today(),
            orders_target=self.rng.randint(c.orders_per_day_min, c.orders_per_day_max),
            orders_done=0,
            next_open_at=0.0,
            # A new day never clears exposure or an open question about it.
            position=self.state.position if self.state else None,
            pending=self.state.pending if self.state else None,
            needs_exchange_check=(self.state.needs_exchange_check
                                  if self.state else False),
        )
        save_state(c.state_path, self.state)
        logger.info("futures soft-start: %s — %d order(s) planned today",
                    self.state.date, self.state.orders_target)

    def _schedule_pause(self) -> None:
        c = self.cfg
        hours = self.rng.uniform(c.pause_hours_min, c.pause_hours_max)
        self.state.next_open_at = time.time() + hours * 3600
        save_state(c.state_path, self.state)
        logger.info("futures soft-start: next open in %.1fh", hours)

    # ---- pair choice ----------------------------------------------------

    def _size_position(self, sym: str, leverage: int, fee_frac: float = 0.0):
        """(contracts, margin_usdt, notional_usdt) or (None, ..) to skip.

        Skips when: the contract metadata is unreadable, or a single contract
        would tie up too much of the wallet. Стелі витрат більше немає (див.
        коментар нижче в цій же функції), тож вартість round-trip тут нічого
        не блокує. Every skip is a refusal to trade — never a silent fallback
        to some other size.
        """
        from .soft_start_budget import (affordable, contracts_for_margin,
                                        futures_round_trip_cost, futures_target_margin)
        from src.exchanges.mexc_rest import to_mexc

        cs, px = self.contract_meta(to_mexc(sym))
        if cs <= 0 or px <= 0:
            logger.warning("futures soft-start: no contract meta for %s — skipping", sym)
            return None, 0.0, 0.0

        if self.balance_usdt is None:
            # No wallet reading available: keep the historical minimal behaviour.
            vol = 1
            target = self.rng.uniform(self.cfg.margin_usdt_min, self.cfg.margin_usdt_max)
        else:
            # Свій генератор -> маржа гуляє 6-14%, а не завжди рівно 10%.
            target = futures_target_margin(self.balance_usdt, self.rng)
            vol = contracts_for_margin(target, leverage, cs, px)

        notional = vol * cs * px
        margin = notional / leverage

        if self.balance_usdt is not None and not affordable(margin, self.balance_usdt):
            logger.info("futures soft-start: %s needs %.2f margin of a %.2f wallet "
                        "— too big, skipping", sym, margin, self.balance_usdt)
            return None, 0.0, 0.0

        # СТЕЛІ ВИТРАТ БІЛЬШЕ НЕМАЄ (рішення оператора 2026-08-26): прогрів
        # має гріти, а не впиратись у ліміт. Облік ЛИШИВСЯ — витрати далі
        # рахуються і показуються у звіті, просто нічого не блокують.
        # `SoftStartBudget.can_afford()` вміє це сама (стеля <= 0 = без межі),
        # тож тут виклик просто прибрано.

        return vol, margin, notional

    async def pick_pair(self) -> tuple[str | None, float]:
        """Пара для прогріву + ставка ТЕЙКЕРА за одну ногу.

        ПОРЯДОК ВІДБОРУ:
          1. пара з `0/0` — безкоштовна, беремо одразу (як і раніше);
          2. якщо таких немає і `allow_paid_fees` — НАЙДЕШЕВША платна, а її
             комісія йде у вартість і списується з бюджету.

        ЩО ЛИШАЄТЬСЯ FAIL-CLOSED і чому. Ставка, яку **не вдалось прочитати**
        (`fee is None` — таймаут, рейт-ліміт), НЕ вважається нулем і НЕ
        вважається дешевою: ми просто не знаємо, скільки заплатимо, тож не
        можемо ані порахувати бюджет, ані вирішити. Це не те саме, що «ставка
        відома і вона ненульова» — саме цю різницю плутати не можна.

        `max_fee_frac` — стеля здорового глузду: платити 50 bps за ногу заради
        прогріву безглуздо, бюджет вигорить за кілька ордерів.

        Повертає `(symbol, taker_fee_frac)`; `(None, 0.0)`, якщо гріти нічим.
        """
        pool = list(self.universe)
        self.rng.shuffle(pool)
        paid: list[tuple[float, str]] = []
        unknown = 0
        for sym in pool:
            try:
                fee = await self.fee_gate.fee(sym)
            except Exception as e:
                logger.warning("futures soft-start: fee lookup %s failed (%s)", sym, e)
                unknown += 1
                continue
            if fee is None:
                unknown += 1
                continue                       # fail-closed: невідоме ≠ дешеве
            if fee.zero_both:
                return sym, 0.0                # безкоштовно — найкращий випадок
            if not self.cfg.allow_paid_fees:
                continue
            # Обидві ноги прогріву — market (type 5), тобто тейкер. Мейкерська
            # ставка тут не використовується взагалі.
            if fee.taker > self.cfg.max_fee_frac:
                logger.debug("skip %s — тейкер %.4f вище стелі %.4f",
                             sym, fee.taker, self.cfg.max_fee_frac)
                continue
            paid.append((float(fee.taker), sym))

        if paid:
            # ВИПАДКОВО СЕРЕД НАЙДЕШЕВШИХ, а не «перша після сортування».
            #
            # ДЕФЕКТ, ЯКИЙ ЦЕ ЛІКУЄ (мій власний, від 2026-08-26): було
            # `paid.sort(); paid[0]`. Коли ставки РІВНІ — а на акаунті без
            # промо всі 24 пари мають однаковий taker 0.0004 — сортування
            # кортежів ламає нічию за ДРУГИМ елементом, тобто за назвою. У
            # результаті вибиралась завжди алфавітно перша пара
            # (`1000PEPEUSDT`), і випадковість вибору пари зникала повністю.
            # Помітно це не з коду, а з логів: усі прогріви на одній парі.
            cheapest = min(f for f, _ in paid)
            # Допуск, бо ставки — float: 0.0004 і 0.00040000000000000002 мають
            # вважатись однаковими, інакше нічия розпадеться на рівному місці.
            best = [sym for f, sym in paid if f <= cheapest + 1e-9]
            sym = self.rng.choice(sorted(best))
            fee_frac = cheapest
            logger.info("futures soft-start: пар із 0%% немає — гріємо %s за "
                        "тейкер %.4f (%.1f bps/нога) з %d найдешевших, "
                        "комісія піде у витрати",
                        sym, fee_frac, fee_frac * 10000, len(best))
            return sym, fee_frac

        logger.warning("futures soft-start: немає придатної пари "
                       "(ставку не прочитано у %d із %d) — пропускаю",
                       unknown, len(pool))
        return None, 0.0

    # ---- contract metadata ----------------------------------------------

    def contract_meta(self, contract_symbol: str) -> tuple[float, float]:
        """(contract_size, last_price) for a contract, from the PUBLIC API.

        Needed to turn "I want ~N USDT of margin" into a contract count. Cached
        per process: contract size never changes and the price only needs to be
        roughly right for sizing.
        """
        hit = self._meta_cache.get(contract_symbol)
        if hit:
            return hit
        import json
        import urllib.request

        def _get(url):
            req = urllib.request.Request(url, headers={"accept": "*/*"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())

        size = price = 0.0
        try:
            d = _get("https://contract.mexc.com/api/v1/contract/detail"
                     f"?symbol={contract_symbol}").get("data") or {}
            size = float(d.get("contractSize") or 0)
        except Exception as e:
            logger.warning("contract detail %s failed: %s", contract_symbol, e)
        try:
            t = _get("https://contract.mexc.com/api/v1/contract/ticker"
                     f"?symbol={contract_symbol}").get("data") or {}
            price = float(t.get("lastPrice") or 0)
        except Exception as e:
            logger.warning("contract ticker %s failed: %s", contract_symbol, e)

        self._meta_cache[contract_symbol] = (size, price)
        return size, price

    # ---- open / close ---------------------------------------------------

    async def open_position(self) -> bool:
        c = self.cfg
        if self.state.position is not None:
            return False                        # one at a time
        if self.state.pending is not None:
            # An earlier open never got an answer. Until we know whether it
            # landed, opening again risks stacking two live positions.
            logger.info("futures soft-start: an open is still unresolved — "
                        "not opening another")
            return False
        if self.state.needs_exchange_check:
            logger.error("futures soft-start: account state unknown (unreadable "
                         "state file) — refusing to open until it is reconciled")
            return False
        # Стелі немає — вичерпатись нічому (див. _size_position).
        sym, fee_frac = await self.pick_pair()
        if sym is None:
            return False

        leverage = self.rng.randint(c.leverage_min, c.leverage_max)
        side = SIDE_LONG if self.rng.random() < c.long_ratio else SIDE_SHORT
        hold_min = self.rng.randint(c.hold_minutes_min, c.hold_minutes_max)

        # Size from the WALLET, not from a hardcoded 1 contract. Previously
        # `margin_usdt_*` was computed and then ignored while vol was pinned to
        # 1 — the config looked like it controlled size but did not.
        # ПРОГРІТИ КЕШ У ПОТОЦІ. `contract_meta` робить два синхронні
        # `urlopen(timeout=15)`, а ми в тому самому event loop, що й арбітраж.
        # Кеш per-process, тож мережа тут буває один раз на символ — але цей
        # один раз коштував би до 30с замороженого loop, якби публічний API
        # підвис. Після прогріву `_size_position` бере з кеша, без мережі.
        try:
            from src.exchanges.mexc_rest import to_mexc as _to_mexc
            await asyncio.to_thread(self.contract_meta, _to_mexc(sym))
        except Exception:
            logger.debug("futures soft-start: прогрів мети не вдався",
                         exc_info=True)
        vol, margin, notional = self._size_position(sym, leverage, fee_frac)
        if vol is None:
            return False

        if not self.sending:
            logger.info("[DRY futures] OPEN %s side=%d vol=%d lev=%dx margin~%.2f "
                        "hold=%dmin (nothing sent)", sym, side, vol, leverage,
                        margin, hold_min)
            self.state.orders_done += 1
            self._schedule_pause()
            return True

        # Breadcrumb FIRST. A market order that fills and then loses its
        # response (timeout, non-JSON body, process killed) is a real position
        # nothing would otherwise know about. Persisting the intent before the
        # send means there is always a symbol to ask the exchange about.
        # `fee_frac` теж персиститься: якщо відповідь загубилась і позицію
        # доведеться АДОПТУВАТИ вже після рестарту, ставку заново прочитати
        # нізвідки, і комісія списалась би як нуль — тобто стеля витрат тихо
        # брехала б саме на платному акаунті.
        self.state.pending = {"symbol": sym, "side": side, "vol": vol,
                              "leverage": leverage, "hold_min": hold_min,
                              "notional": notional, "fee_frac": fee_frac,
                              "sent_at": time.time()}
        save_state(c.state_path, self.state)

        try:
            from src.exchanges.mexc_rest import to_mexc
            resp = await self.client.submit_order(
                symbol=to_mexc(sym), side=side, vol=vol,
                leverage=leverage, order_type=ORDER_TYPE_MARKET)
        except Exception as e:
            # NOT a failure — an UNKNOWN. The order may well be filled.
            logger.error("[futures] OPEN %s got no answer (%s) — asking the "
                         "exchange whether it landed", sym, e)
            return await self.reconcile_pending()

        if str((resp or {}).get("code")) != "0":
            # A definitive refusal from the exchange: nothing was opened.
            logger.warning("[futures] OPEN %s rejected: %s", sym,
                           json.dumps(resp or {})[:200])
            self.state.pending = None
            save_state(c.state_path, self.state)
            return False

        pos = OpenPosition(symbol=sym, side=side, vol=vol, leverage=leverage,
                           opened_at=time.time(),
                           close_after=time.time() + hold_min * 60,
                           opened_live=True)
        # Persist BEFORE anything else can fail: an unrecorded open position is
        # the worst outcome this module can produce.
        self.state.position = asdict(pos)
        self.state.pending = None
        self.state.orders_done += 1
        self._count("futures_opens")
        save_state(c.state_path, self.state)
        # Charge the round trip up front: both crossings and a possible funding
        # settlement are known now, and booking them at open means the ceiling
        # cannot be blown by a position we have already committed to.
        if self.budget is not None:
            from .soft_start_budget import futures_round_trip_cost
            _fee_cost = 2 * notional * fee_frac
            self._last_fee_estimate = _fee_cost
            self.budget.charge(
                futures_round_trip_cost(notional, fee_frac=fee_frac),
                f"futures round-trip {sym}"
                + (f" (комісія {_fee_cost:.4f} @ {fee_frac*10000:.1f}bps/нога)"
                   if fee_frac else " (0% комісія)"))
        logger.info("[futures] OPENED %s side=%d lev=%dx vol=%d "
                    "(margin~%.2f, notional~%.2f) — closing in %dmin",
                    sym, side, leverage, vol, margin, notional, hold_min)
        self._schedule_pause()
        return True

    async def close_position(self, *, forced: bool = False) -> bool:
        if self.state.position is None:
            return False
        pos = OpenPosition(**self.state.position)

        if not self.sending:
            if pos.opened_live:
                # The record describes a REAL position. Clearing it here would
                # send no order and orphan the position permanently — which is
                # what happens on the documented "unset SOFT_START_LIVE and
                # restart" way of disarming.
                logger.error("[futures] %s was opened LIVE but this process is "
                             "dry-run — NOT touching the record. Close it by "
                             "hand, or restart with %s=1 to let the bot do it.",
                             pos.symbol, LIVE_ENV)
                return False
            logger.info("[DRY futures] CLOSE %s after %.1fmin (nothing sent)",
                        pos.symbol, pos.held_minutes())
            self.state.position = None
            save_state(self.cfg.state_path, self.state)
            return True

        try:
            from src.exchanges.mexc_rest import to_mexc
            resp = await self.client.close_all_positions(to_mexc(pos.symbol))
        except Exception as e:
            # Keep the state so the next tick retries — never drop an open position.
            logger.error("[futures] CLOSE %s FAILED: %s — will retry next tick",
                         pos.symbol, e)
            return False

        if str((resp or {}).get("code")) != "0":
            logger.error("[futures] CLOSE %s rejected: %s — will retry",
                         pos.symbol, json.dumps(resp or {})[:200])
            return False

        held = pos.held_minutes()

        # ЗАПИС ПРО ЗАКРИТТЯ — НЕГАЙНО, ЩЕ ДО ЧИТАННЯ PnL.
        #
        # Біржа щойно підтвердила закриття (`code == 0`), тобто позиції вже
        # НЕМАЄ. Стан на диску мусить це відображати ОДРАЗУ, бо читання PnL
        # нижче ретраїть до 4 разів і спить 1.5+3.0+4.5 = 9.0 секунд — а
        # докерський грейс на зупинку контейнера всього 10с (`stop_grace_period`
        # тепер 60с, але покладатись лише на нього не можна: SIGKILL посеред
        # цього вікна лишав на диску ФАНТОМНУ позицію, якої на біржі немає).
        # PnL — це ЗВІТНІСТЬ; факт закриття — це СТАН. Плутати їх не можна.
        self._count("futures_closes")
        self.state.position = None
        save_state(self.cfg.state_path, self.state)

        # РЕАЛІЗОВАНИЙ PnL із біржі, а не з наших припущень. Модель вартості
        # рахувала лише спред+фандинг+комісію і свідомо ігнорувала рух ринку —
        # але за 53 хвилини тримання рух і є основною частиною вартості: на
        # першому ж живому циклі фʼючерсний баланс змінився на -0.40 USDT при
        # комісії 0.03. Тепер це видно у звіті, а не лишається за кадром.
        realised = await self._realised_pnl(pos)
        self.last_closed = {"symbol": pos.symbol, "held_min": held,
                            "realised": realised}
        logger.info("[futures] CLOSED %s after %.1fmin%s%s",
                    pos.symbol, held, " (forced)" if forced else "",
                    f" | realised={realised:+.4f} USDT"
                    if realised is not None else " | realised=невідомо")
        if realised is not None and self.budget is not None:
            try:
                self.budget.record_pnl(realised, f"futures {pos.symbol}")
            except Exception:
                logger.debug("futures soft-start: PnL не записано", exc_info=True)
        return True

    def _count(self, kind: str, n: int = 1) -> None:
        if not self.on_action:
            return
        try:
            self.on_action(kind, n)
        except Exception:
            logger.debug("[futures] лічильник %s не оновлено", kind,
                         exc_info=True)

    async def _realised_pnl(self, pos) -> float | None:
        """Реалізований PnL щойно закритої позиції, або None якщо не прочитали.

        None — це «невідомо», НЕ нуль: приписати нуль означало б показати
        збиткову угоду як безкоштовну. Пошук за символом і часом відкриття,
        бо на акаунті можуть бути й інші закриті позиції.
        """
        try:
            from src.exchanges.mexc_rest import to_mexc
            cs = to_mexc(pos.symbol)
            opened_ms = int(pos.opened_at * 1000)
            best = None
            # РЕТРАЙ, БО ЦЕ ГОНКА, А НЕ ВІДСУТНІСТЬ ДАНИХ. Виміряно на живому
            # закритті 26.08 (slot 2, LINKUSDT): у мить закриття історія
            # порожня і лог писав «realised=невідомо», а через кілька хвилин
            # той самий запит віддавав рядок (realised -0.0898, closePL
            # -0.0594, fee -0.0304). Тобто біржа пише історію з затримкою.
            # Тут не гарячий шлях — кілька секунд очікування нічого не коштують.
            for attempt in range(4):
                if attempt:
                    await asyncio.sleep(HISTORY_RETRY_DELAY_SEC * attempt)
                r = await self.client.get_history_positions(symbol=cs,
                                                            page_size=10)
                for row in (r or {}).get("data") or []:
                    if row.get("symbol") != cs:
                        continue
                    # Допуск 60с: MEXC округлює час, а позиція одна за раз.
                    if int(row.get("createTime", 0) or 0) < opened_ms - 60_000:
                        continue
                    best = row
                    break
                if best is not None:
                    break
            if best is None:
                logger.info("[futures] %s: історія позиції ще не зʼявилась за "
                            "4 спроби — PnL лишається невідомим", pos.symbol)
                return None

            # ТІЛЬКИ РУХ РИНКУ, БЕЗ КОМІСІЇ — інакше вона рахується ДВІЧІ.
            #
            # ВИМІРЯНО на нашій першій живій позиції (PEPE_USDT, 26.08):
            #     realised = -0.4007
            #     closeProfitLoss = -0.3710   (рух ринку)
            #     fee = -0.0297               (комісія біржі)
            # тобто realised = closeProfitLoss + fee. Комісія вже лежить у
            # `spent` (ми заряджаємо її ДО відправки), тож брати `realised`
            # означало б показати її і у витратах, і в PnL.
            close_pl = best.get("closeProfitLoss")
            fee = float(best.get("fee") or 0.0)
            if close_pl is not None:
                pnl = float(close_pl)
            else:
                # Запасний шлях: віднімаємо комісію самі. `fee` віддається
                # відʼємним, тому мінус.
                pnl = float(best.get("realised") or 0.0) - fee

            # ФАКТИЧНА комісія проти нашої оцінки. Оцінка йде з
            # `tiered_fee_rate`, і якщо біржа раптом почала брати інакше, ми
            # дізнаємось про це ТУТ, а не через тиждень по балансу.
            est = self._last_fee_estimate
            if est and abs(abs(fee) - est) > max(0.01 * est, 0.005):
                logger.warning(
                    "[futures] %s: комісія біржі %.4f проти оцінки %.4f — "
                    "розбіжність %.0f%%. Перевір тариф акаунта.",
                    pos.symbol, abs(fee), est,
                    100 * (abs(fee) - est) / est if est else 0.0)
            return pnl
        except Exception:
            logger.debug("futures soft-start: історію позицій не прочитано",
                         exc_info=True)
            return None

    # ---- asking the exchange --------------------------------------------

    def _universe_contracts(self) -> dict:
        """{contract symbol -> our symbol} for everything we are allowed to warm."""
        from src.exchanges.mexc_rest import to_mexc
        return {to_mexc(s): s for s in self.universe}

    async def _exchange_positions(self) -> list | None:
        """Open positions per the EXCHANGE, or None when the read failed.

        None means "unknown" and must never be read as "nothing is open" —
        that confusion is precisely how a live position gets orphaned.
        """
        try:
            resp = await self.client.get_open_positions()
        except Exception as e:
            logger.error("futures soft-start: open-positions read failed (%s)", e)
            return None
        if str((resp or {}).get("code")) != "0":
            logger.error("futures soft-start: open-positions rejected: %s",
                         json.dumps(resp or {})[:200])
            return None
        return list((resp or {}).get("data") or [])

    @staticmethod
    def _held(row: dict) -> bool:
        try:
            return float(row.get("holdVol") or 0) > 0
        except (TypeError, ValueError):
            return False

    async def reconcile_pending(self) -> bool:
        """Resolve an open we never got an answer for, by asking the exchange.

        Returns True when a position was found and ADOPTED (so the caller can
        treat it as an open that happened). The pending record is dropped only
        on a definitive answer — an unreadable exchange keeps the question open
        rather than guessing "nothing happened".
        """
        pend = self.state.pending
        if not pend:
            return False
        sym = str(pend.get("symbol") or "")
        from src.exchanges.mexc_rest import to_mexc
        contract = to_mexc(sym)

        # РІШЕННЯ ПРО РЕАЛЬНУ ПОЗИЦІЮ НЕ МОЖНА БРАТИ З ОДНОГО СЕМПЛА.
        #
        # Тут стояло рівно одне читання з нульовою затримкою, і порожня
        # відповідь НЕЗВОРОТНО стирала pending. А цей самий проєкт уже виміряв
        # затримку видимості філу: `live_executor.py` — «MEXC's fill-visibility
        # lag is ~50-200ms after submit», і схему «одна перевірка» там уже
        # відкидали після реальної ліквідації (-$25.82, 49 хв голого шорта).
        # Ризикова саме ШВИДКА відмова (reset / не-JSON при RTT ~150мс) —
        # рівно в ту смугу.
        #
        # Поруч, у цьому ж файлі, `_realised_pnl` ретраїть 4 рази заради суто
        # ЗВІТНОГО числа. Шлях, що вирішує, чи є експозиція, мусить бути не
        # менш обережним.
        rows = None
        for attempt in range(1, PENDING_RECHECKS + 1):
            rows = await self._exchange_positions()
            if rows is None:
                logger.error("futures soft-start: %s is UNRESOLVED — keeping the "
                             "pending record and re-checking next tick", sym)
                return False
            match = next((r for r in rows
                          if str(r.get("symbol")) == contract
                          and self._held(r)), None)
            if match is not None:
                break
            if attempt < PENDING_RECHECKS:
                logger.info("futures soft-start: %s ще не видно на біржі "
                            "(спроба %d/%d) — перепитую", sym, attempt,
                            PENDING_RECHECKS)
                await asyncio.sleep(PENDING_RECHECK_DELAY_SEC * attempt)
        else:
            match = None

        if match is None:
            logger.info("futures soft-start: %s did not open after all — "
                        "clearing the pending record", sym)
            self.state.pending = None
            save_state(self.cfg.state_path, self.state)
            return False

        self._adopt(match, sym, hold_min=int(pend.get("hold_min")
                                             or self.cfg.hold_minutes_min),
                    opened_at=float(pend.get("sent_at") or time.time()))
        self.state.pending = None
        self.state.orders_done += 1
        save_state(self.cfg.state_path, self.state)
        # An adopted position costs exactly what a normal one costs — charging
        # it keeps the ceiling honest whichever way the open was learned about.
        if self.budget is not None and pend.get("notional"):
            from .soft_start_budget import futures_round_trip_cost
            _ff = float(pend.get("fee_frac") or 0.0)
            self.budget.charge(
                futures_round_trip_cost(float(pend["notional"]), fee_frac=_ff),
                f"futures round-trip {sym} (adopted)"
                + (f" (комісія @ {_ff*10000:.1f}bps/нога)" if _ff else ""))
        logger.error("futures soft-start: %s DID open despite the error — "
                     "adopted and scheduled to close", sym)
        self._schedule_pause()
        return True

    def _adopt(self, row: dict, sym: str, *, hold_min: int,
               opened_at: float) -> None:
        """Turn an exchange position row into our own tracked position."""
        pos = OpenPosition(
            symbol=sym,
            side=SIDE_LONG if int(row.get("positionType") or 1) == 1 else SIDE_SHORT,
            vol=int(float(row.get("holdVol") or 0)),
            leverage=int(row.get("leverage") or 0),
            opened_at=opened_at,
            close_after=opened_at + hold_min * 60,
            opened_live=True,
        )
        self.state.position = asdict(pos)

    async def sweep_exchange(self) -> None:
        """After an unreadable state file: find out what the account holds.

        Only positions on symbols WE warm are adopted. Anything else on the
        account belongs to something other than soft-start and is left strictly
        alone — closing another system's position would be worse than the
        problem this is solving.
        """
        if not self.state.needs_exchange_check:
            return
        rows = await self._exchange_positions()
        if rows is None:
            logger.error("futures soft-start: state unreadable AND the exchange "
                         "could not be read — nothing will be opened until this "
                         "resolves")
            return                                # keep the flag, re-check later

        ours = self._universe_contracts()
        mine = [r for r in rows if str(r.get("symbol")) in ours and self._held(r)]
        foreign = [r for r in rows if self._held(r) and r not in mine]
        if foreign:
            logger.warning("futures soft-start: account holds %d position(s) "
                           "outside the warming universe — leaving them alone",
                           len(foreign))
        if mine:
            row = mine[0]                          # one at a time, by design
            sym = ours[str(row.get("symbol"))]
            logger.error("futures soft-start: found an untracked %s position "
                         "after an unreadable state file — adopting it and "
                         "closing it now", sym)
            # hold_min=0: we do not know when this opened, and money we lost
            # track of is not something to sit on for another warming window.
            self._adopt(row, sym, hold_min=0, opened_at=time.time())
            if len(mine) > 1:
                logger.error("futures soft-start: %d warming positions are open "
                             "at once — only %s is tracked, close the rest by "
                             "hand", len(mine), sym)
        self.state.needs_exchange_check = False
        save_state(self.cfg.state_path, self.state)

    def has_exposure(self) -> bool:
        """True when this engine may have real money on the exchange.

        Deliberately pessimistic: an unresolved pending order counts, because
        we do not yet know that it did NOT fill.
        """
        return (self.state.position is not None
                or self.state.pending is not None
                or self.state.needs_exchange_check)

    async def recover(self) -> None:
        """Called at startup: adopt or close whatever a restart left behind."""
        if self.state.needs_exchange_check:
            await self.sweep_exchange()
        if self.state.pending is not None:
            await self.reconcile_pending()
        if self.state.position is None:
            return
        pos = OpenPosition(**self.state.position)
        if pos.due():
            logger.warning("futures soft-start: position %s was left open past its "
                           "deadline (%.1fmin) — closing now", pos.symbol,
                           pos.held_minutes())
            await self.close_position(forced=True)
        else:
            remaining = (pos.close_after - time.time()) / 60
            logger.info("futures soft-start: resuming hold on %s, %.1fmin left",
                        pos.symbol, remaining)

    # ---- loop -----------------------------------------------------------

    def active_now(self, now=None) -> bool:
        """Чи можна ВІДКРИВАТИ зараз. Закриття не питає цього ніколи."""
        from datetime import datetime
        h = (now or datetime.now()).hour
        return self.cfg.active_hour_start <= h < self.cfg.active_hour_end

    async def tick(self) -> None:
        try:
            if not self.state.is_today():
                self._roll_day()

            # Answer open questions before acting on anything else.
            if self.state.needs_exchange_check:
                await self.sweep_exchange()
            if self.state.pending is not None:
                await self.reconcile_pending()

            if self.state.position is not None:
                if OpenPosition(**self.state.position).due():
                    await self.close_position()
                return                           # never open while holding

            if self.state.orders_done >= self.state.orders_target:
                return
            if time.time() < self.state.next_open_at:
                return
            if not self.active_now():
                return                           # поза людськими годинами
            await self.open_position()
        except Exception as e:
            logger.warning("futures soft-start tick error (contained): %s", e)

    async def run(self, poll_sec: int = 60) -> None:
        await self.recover()
        while True:
            await self.tick()
            await asyncio.sleep(poll_sec)
