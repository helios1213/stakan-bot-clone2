"""Spot account warming over the MEXC web (webkey) path.

Ported into the bot from the standalone `spot_soft_start_web.py`, with the two
things that made the standalone version awkward removed:

  * **No hardcoded CURRENCY_IDS.** Ids and pair precision come from
    `spot_currency.py`, which reads them off the public pair page. Adding a
    token to the universe is now just adding its ticker — no "buy 1-2 USDT of
    it first so we can learn its id" step.
  * **Precision from one source.** The old version took ids from the balances
    endpoint and decimals from `api.mexc.com/exchangeInfo`; they could disagree.
    Both now come from the same `info` blob.

Behaviour (unchanged from the spec): each day pick 1-4 tokens; 0-10 buys and
0-10 sells spread across them, each 1-150 USDT; never sell a token below
`baseline_usdt_per_token`; a hard daily spend ceiling; randomised timing inside
an active-hours window.

SAFETY:
  * DRY-RUN by default. Live requires BOTH an explicit `dry_run=False` and the
    `SOFT_START_LIVE=1` environment variable — two independent switches, because
    this is the module that spends real money.
  * Every order is wrapped by `SpotWebClient`: a failure logs and skips.
  * The daily ceiling is checked BEFORE each buy, against money already spent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .webkey.spot_client import SpotWebClient

logger = logging.getLogger(__name__)

PUBLIC_API = "https://api.mexc.com"
LIVE_ENV = "SOFT_START_LIVE"


@dataclass
class SoftStartConfig:
    quote: str = "USDT"
    universe: tuple[str, ...] = ("MX",)
    tokens_per_day_min: int = 1
    tokens_per_day_max: int = 4
    # Денна квота піднята 2026-08-26 (рішення оператора): 10/10 давали ~10 дій
    # на добу, і після розмазування по 17-годинному вікну акаунт виглядав
    # майже мертвим. Продажів більше, ніж було, бо саме вони повертають USDT —
    # без них покупки впираються в денну стелю (= баланс) і цикл глухне.
    buys_per_day_min: int = 0
    buys_per_day_max: int = 25
    sells_per_day_min: int = 0
    sells_per_day_max: int = 20
    order_usdt_min: float = 1.5          # keep >= the observed spot min notional
    order_usdt_max: float = 150.0
    baseline_usdt_per_token: float = 10.0   # never sell a token below this value
    daily_buy_usdt_ceiling: float = 400.0   # hard cap on daily spend
    sell_fraction_min: float = 0.2
    sell_fraction_max: float = 0.6
    marketable_buffer: float = 0.002     # cross the book slightly so orders fill
    # Комісія спота за ОДИН ордер, часткою від ноціоналу.
    #
    # ЦЕ ПРИПУЩЕННЯ, А НЕ ВИМІР — і його треба знати. `FeeGate` читає
    # `/account/tiered_fee_rate`, тобто ФʼЮЧЕРСНУ сітку; спотова інша, і
    # перевіреного приватного ендпоінта для неї в нас немає. Тому число тут
    # береться з конфігу, а не «вимірюється».
    #
    # Дефолт 0.0005 (5 bps) — стандартний тейкер MEXC на споті. Він СВІДОМО
    # завищений для акаунта з промо: бюджет — це стеля витрат, і завищення
    # витрачає її швидше, тобто помиляється в БЕЗПЕЧНИЙ бік. Якщо оператор
    # знає свою реальну ставку — виставити тут; 0.0 = «комісії немає».
    #
    # Ордери прогріву — marketable limit, тобто ТЕЙКЕР.
    spot_fee_frac: float = 0.0005
    active_hour_start: int = 6
    active_hour_end: int = 23
    tick_min_sec: int = 180
    tick_max_sec: int = 1500
    state_path: str = "/app/data/spot_soft_start_state.json"

    def validate(self) -> None:
        if self.order_usdt_min <= 0 or self.order_usdt_max < self.order_usdt_min:
            raise ValueError("order_usdt range invalid")
        if self.daily_buy_usdt_ceiling < self.order_usdt_max:
            raise ValueError(
                "daily ceiling is below a single max order — every buy would be skipped")
        if not self.universe:
            raise ValueError("empty universe")
        if not 0 <= self.active_hour_start < self.active_hour_end <= 24:
            raise ValueError("active hours invalid")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _order_cost(notional_usdt: float, marketable_buffer: float,
                fee_frac: float = 0.0) -> float:
    """Cost of ONE order: перетин книги + комісія біржі.

    Комісії тут раніше не було взагалі — модель мовчки припускала 0%. На
    акаунті без промо це занижувало витрати рівно на розмір комісії, тобто
    стеля бюджету не спрацьовувала б там, де мала.
    """
    from .soft_start_budget import spot_order_cost
    return spot_order_cost(notional_usdt, marketable_buffer, fee_frac)


def public_last_price(symbol: str) -> float | None:
    """Public price feed — no auth. Only prices come from here; ids and decimals
    come from the pair page (see spot_currency.py)."""
    try:
        url = f"{PUBLIC_API}/api/v3/ticker/price?symbol={symbol}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return float(json.loads(r.read())["price"])
    except Exception as e:
        logger.warning("price fetch failed for %s: %s", symbol, e)
        return None


@dataclass
class DayPlan:
    date: str
    tokens: list[str]
    buys_target: int
    sells_target: int
    buys_done: int = 0
    sells_done: int = 0
    spent_usdt: float = 0.0

    def is_today(self) -> bool:
        return self.date == _today()


def new_day_plan(cfg: SoftStartConfig, rng: random.Random) -> DayPlan:
    pool = list(cfg.universe)
    n = rng.randint(cfg.tokens_per_day_min, min(cfg.tokens_per_day_max, len(pool)))
    return DayPlan(
        date=_today(),
        tokens=rng.sample(pool, n),
        buys_target=rng.randint(cfg.buys_per_day_min, cfg.buys_per_day_max),
        sells_target=rng.randint(cfg.sells_per_day_min, cfg.sells_per_day_max),
    )


def load_plan(cfg: SoftStartConfig) -> DayPlan | None:
    p = Path(cfg.state_path)
    if not p.exists():
        return None
    try:
        return DayPlan(**json.loads(p.read_text()))
    except Exception as e:
        logger.warning("state unreadable (%s) — starting a fresh plan", e)
        return None


def save_plan(cfg: SoftStartConfig, plan: DayPlan) -> None:
    try:
        Path(cfg.state_path).write_text(json.dumps(asdict(plan), indent=2))
    except Exception as e:
        logger.warning("state save failed: %s", e)


class SpotSoftStart:
    def __init__(self, client: SpotWebClient, cfg: SoftStartConfig | None = None,
                 rng: random.Random | None = None, budget=None) -> None:
        self.cfg = cfg or SoftStartConfig()
        self.cfg.validate()
        self.client = client
        self.rng = rng or random.Random()
        # Optional hard spend ceiling shared with the futures warmer. None means
        # "no ceiling" — kept optional so existing callers and tests are unchanged.
        self.budget = budget
        # Слід останньої УСПІШНОЇ дії — читає раннер, щоб звіт показував
        # справжні суму й кількість, а не стелю конфігу.
        self.last_action: dict | None = None
        self.plan = load_plan(self.cfg)
        if self.plan is None or not self.plan.is_today():
            self.plan = new_day_plan(self.cfg, self.rng)
            save_plan(self.cfg, self.plan)
        # «РОЗІГРАНО», а не «план». Вага дня застосовується ПІЗНІШЕ, у
        # `SlotWarmer._apply_day_weight()`, тож ці числа — ще не те, що
        # виконуватиметься: розіграні 9 покупок при вазі 0.771 стають 8.
        # Рядок називався «soft-start plan …» і читався як остаточний план —
        # через це я сам кілька хвилин гнався за фантомним багом, звіряючи лог
        # із файлом стану. Ефективний план друкує раннер, після ваги.
        logger.info("soft-start РОЗІГРАНО %s: tokens=%s buys=%d sells=%d "
                    "(вага дня застосується далі)",
                    self.plan.date, self.plan.tokens,
                    self.plan.buys_target, self.plan.sells_target)

    def _tick_probability(self, now: datetime | None = None) -> float:
        """Імовірність зробити дію на ЦЬОМУ тіку — щоб день не вигорав одразу.

        БУЛО ПРИБИТЕ 0.5, і це давало рівно те, на що скаржиться оператор:
        план дня (8 покупок + 2 продажі) виконувався за 22 ХВИЛИНИ, після чого
        бот мовчав до наступної доби. Для прогріву це найгірший можливий
        профіль: сплеск і 23 години тиші помітніші за рівну активність, заради
        якої все й робиться. Активне вікно 6:00-23:00 не використовувалось.

        Тепер темп виводиться з того, скільки дій лишилось і скільки тіків
        лишилось у вікні: `лишилось / тіків_до_кінця`. Це самокоригується —
        пропущений тік трохи піднімає ймовірність наступного, а до кінця вікна
        решта дій дотискається.

        Стеля 0.5 лишена свідомо: вона обмежує сплеск, якщо часу лишилось мало,
        але не дає темпу підскочити вище за старий максимум. Підлога 0.001 —
        щоб при довгому вікні дії все ж траплялись, а не відкладались на кінець.
        """
        p = self.plan
        left = max(0, p.buys_target - p.buys_done) + \
            max(0, p.sells_target - p.sells_done)
        if left <= 0:
            return 0.0
        n = now or datetime.now()
        # Тік раз на хвилину, тож хвилини до кінця вікна = кількість спроб.
        mins_left = (self.cfg.active_hour_end - n.hour) * 60 - n.minute
        if mins_left <= 1:
            return 0.5
        return max(0.001, min(0.5, left / float(mins_left)))

    def active_now(self, now: datetime | None = None) -> bool:
        h = (now or datetime.now()).hour
        return self.cfg.active_hour_start <= h < self.cfg.active_hour_end

    def _roll_day(self) -> None:
        if not self.plan.is_today():
            self.plan = new_day_plan(self.cfg, self.rng)
            save_plan(self.cfg, self.plan)
            logger.info("new day: tokens=%s buys=%d sells=%d", self.plan.tokens,
                        self.plan.buys_target, self.plan.sells_target)

    async def maybe_buy(self) -> bool:
        p, cfg = self.plan, self.cfg
        if p.buys_done >= p.buys_target or not p.tokens:
            return False
        token = self.rng.choice(p.tokens)
        symbol = f"{token}{cfg.quote}"

        # ЦІНА ПЕРШОЮ, а сума після неї — саме заради розміру. Біржа бачить не
        # долари, а КІЛЬКІСТЬ монет, тож щоб частину ордерів зробити «рівними»
        # (1, 5, 10 монет — як у людини), сайзер має знати ціну. Раніше сума
        # рахувалась до запиту ціни, і прив'язатись до кількості було нічим.
        px = public_last_price(symbol)
        if not px:
            return False

        from .soft_start_budget import human_order_usdt
        usdt = human_order_usdt(self.rng, cfg.order_usdt_min,
                                cfg.order_usdt_max, px)

        if p.spent_usdt + usdt > cfg.daily_buy_usdt_ceiling:
            logger.info("[buy] skip %s %.2f — daily ceiling %.0f (spent %.2f)",
                        symbol, usdt, cfg.daily_buy_usdt_ceiling, p.spent_usdt)
            return False

        # The buffer we cross to get filled IS the cost of this order, and it is
        # known before sending — so an order that would breach the ceiling is
        # never placed rather than being noticed afterwards.
        cost = _order_cost(usdt, cfg.marketable_buffer, cfg.spot_fee_frac)
        res = await self.client.buy(token, usdt=usdt,
                                    price=px * (1 + cfg.marketable_buffer))
        if res.ok:
            p.buys_done += 1
            p.spent_usdt += usdt
            # ФАКТИЧНІ числа для звіту оператору. Раніше репортер отримував
            # `order_usdt_max` і літерал "~" — тобто показував СТЕЛЮ розміру
            # замість реальної суми (звідси «~3.00 USDT» у кожному рядку, хоч
            # насправді йшло 1.5-2.2) і порожню кількість. Рушій і далі нічого
            # не знає про Telegram: він просто лишає слід.
            self.last_action = {
                "kind": "buy", "symbol": symbol, "usdt": usdt,
                "qty": res.quantity, "price": res.price,
            }
            if self.budget is not None and not res.dry_run:
                self.budget.record_pnl(-usdt, f"spot buy {symbol}")
            save_plan(cfg, p)
            if self.budget is not None and not res.dry_run:
                self.budget.charge(cost, f"spot buy {symbol}"
                                   + (f" (комісія @ {cfg.spot_fee_frac*10000:.1f}bps)"
                                      if cfg.spot_fee_frac else ""))
            return True
        logger.warning("[buy] %s rejected: %s", symbol, res.error)
        return False

    async def wind_down(self, keep_frac: float = 0.20,
                        tokens: list | None = None) -> int:
        """Розпродати монети наприкінці кампанії, лишивши ~`keep_frac` вартості.

        НАВІЩО ОКРЕМИЙ РЕЖИМ, А НЕ ЗВИЧАЙНІ ПРОДАЖІ. `maybe_sell` НІКОЛИ не
        продає нижче `baseline_usdt_per_token` — це правильно під час прогріву
        (акаунт має виглядати як такий, що ТРИМАЄ монети, а не як пилосос), але
        рівно через це в кінці кампанії на балансі лишається все куплене.
        На клоні 29.08 це було 4.36 і 7.90 USDT, замкнених назавжди.

        Тут базовий залишок свідомо ІГНОРУЄТЬСЯ: прогрів завершився, тримати
        більше нічого не треба. Лишаємо `keep_frac` від поточної вартості монет
        — повний нуль виглядав би як «вийшов і забув», а це теж патерн.

        Продажі йдуть по одному на виклик і НЕ рахуються в денний план: план
        уже вичерпано, а розпродаж — окрема дія завершення.
        Повертає кількість відправлених ордерів.
        """
        cfg = self.cfg
        sent = 0
        # НЕ `plan.tokens`, А ВСЕ, ЩО МОЖЕ ЛЕЖАТИ НА БАЛАНСІ.
        #
        # Денний план містить лише сьогоднішні токени, а монети накопичуються
        # за всю історію слота — зокрема куплені під СТАРИМ юніверсом. На
        # клоні 29.08 план був ['LINK','PENGU','SUI','TRX'], а найбільший
        # залишок — MX на 12.10 USDT, куплений тоді, коли юніверс складався
        # з одного MX. Він не потрапив би в розпродаж ніколи.
        #
        # Порожній баланс токена коштує один запит і нічого не ламає, тож
        # дешевше перевірити зайве, ніж лишити гроші замкненими.
        pool = tokens if tokens is not None else list(self.plan.tokens)
        for token in list(dict.fromkeys(pool)):
            symbol = f"{token}{cfg.quote}"
            try:
                cur = await self.client.currency(token)
                # ПО ОДНОМУ coinId. Виміряно 2026-08-29: запит із кількома
                # id одразу віддає ПОРОЖНІЙ словник, без помилки — тобто
                # «нічого не тримаємо» замість реального балансу. Тиха
                # неправда, на якій я сам спіймався, роблячи цю перевірку.
                bals = await self.client.balances([cur.currency_id])
                held = float(bals.get(token, {}).get("available", 0) or 0)
            except Exception as e:
                logger.warning("[wind-down] %s: баланс не прочитано (%s)",
                               symbol, e)
                continue
            px = public_last_price(symbol)
            if not px or held <= 0:
                continue
            value = held * px
            target_value = value * max(0.0, min(1.0, keep_frac))
            sell_value = value - target_value
            if sell_value < cfg.order_usdt_min:
                logger.info("[wind-down] %s: лишок ~%.2f USDT — продавати нічого",
                            symbol, value)
                continue
            qty = min(held, sell_value / px)
            cost = _order_cost(qty * px, cfg.marketable_buffer, cfg.spot_fee_frac)
            res = await self.client.sell(token, quantity=qty,
                                         price=px * (1 - cfg.marketable_buffer))
            if not res.ok:
                logger.warning("[wind-down] %s відхилено: %s", symbol, res.error)
                continue
            sent += 1
            proceeds = qty * px
            self.last_action = {"kind": "sell", "symbol": symbol,
                                "usdt": proceeds, "qty": res.quantity,
                                "price": res.price}
            logger.info("[wind-down] %s продано ~%.2f USDT, лишається ~%.2f",
                        symbol, proceeds, target_value)
            if self.budget is not None and not res.dry_run:
                self.budget.record_pnl(proceeds, f"spot sell {symbol}")
                self.budget.charge(cost, f"spot wind-down {symbol}")
        return sent

    async def maybe_sell(self) -> bool:
        p, cfg = self.plan, self.cfg
        if p.sells_done >= p.sells_target or not p.tokens:
            return False
        token = self.rng.choice(p.tokens)
        symbol = f"{token}{cfg.quote}"

        try:
            cur = await self.client.currency(token)
            bals = await self.client.balances([cur.currency_id, cur.market_currency_id])
        except Exception as e:
            logger.warning("[sell] %s: %s — skipped", symbol, e)
            return False

        held = float(bals.get(token, {}).get("available", 0) or 0)
        px = public_last_price(symbol)
        if held <= 0 or not px:
            logger.info("[sell] skip %s — nothing held / no price", symbol)
            return False

        held_value = held * px
        surplus = held_value - cfg.baseline_usdt_per_token
        if surplus <= cfg.order_usdt_min:
            logger.info("[sell] skip %s — held ~%.2f USDT, at/below baseline %.2f",
                        symbol, held_value, cfg.baseline_usdt_per_token)
            return False

        sell_value = max(cfg.order_usdt_min,
                         surplus * self.rng.uniform(cfg.sell_fraction_min,
                                                    cfg.sell_fraction_max))
        qty = sell_value / px
        # Never breach the baseline: clamp, then re-check it is still worth sending.
        max_qty = held - (cfg.baseline_usdt_per_token / px)
        qty = min(qty, max_qty)
        if qty <= 0 or qty * px < cfg.order_usdt_min:
            logger.info("[sell] skip %s — clamped below min notional", symbol)
            return False

        cost = _order_cost(qty * px, cfg.marketable_buffer, cfg.spot_fee_frac)
        res = await self.client.sell(token, quantity=qty,
                                     price=px * (1 - cfg.marketable_buffer))
        if res.ok:
            p.sells_done += 1
            proceeds = qty * px
            self.last_action = {
                "kind": "sell", "symbol": symbol, "usdt": proceeds,
                "qty": res.quantity, "price": res.price,
            }
            # СПОТОВИЙ КЕШ-ФЛО у той самий облік, що й фʼючерсний PnL.
            # Покупка — це не витрата: USDT перетворились на монету, і вартість
            # нікуди не зникла. Витратою є РІЗНИЦЯ між тим, що вклали, і тим,
            # що повернули, і вона стає відомою лише на продажі. Тому продаж
            # записується як +proceeds, а купівля як -usdt — сума по кампанії
            # і є реалізованим спотовим результатом (решта лишається в монетах).
            if self.budget is not None and not res.dry_run:
                self.budget.record_pnl(proceeds, f"spot sell {symbol}")
            save_plan(cfg, p)
            if self.budget is not None and not res.dry_run:
                self.budget.charge(cost, f"spot sell {symbol}"
                                   + (f" (комісія @ {cfg.spot_fee_frac*10000:.1f}bps)"
                                      if cfg.spot_fee_frac else ""))
            return True
        logger.warning("[sell] %s rejected: %s", symbol, res.error)
        return False

    async def tick(self) -> None:
        self._roll_day()
        if not self.active_now():
            return
        try:
            # Shuffle the order rather than always considering a buy first: a
            # fixed buy-then-sell rhythm is a pattern, and warming exists to not
            # look like one. Each action still fires only ~half the time, so a
            # tick can also do nothing at all.
            from .soft_start_campaign import shuffled_actions
            p = self.plan
            pace = self._tick_probability()
            for action in shuffled_actions(
                    self.rng,
                    buy=p.buys_done < p.buys_target,
                    sell=p.sells_done < p.sells_target):
                if self.rng.random() >= pace:
                    continue
                if action == "buy":
                    await self.maybe_buy()
                else:
                    await self.maybe_sell()
        except Exception as e:                      # last-resort containment
            logger.warning("soft-start tick error (contained): %s", e)

    async def run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self.rng.randint(self.cfg.tick_min_sec,
                                                 self.cfg.tick_max_sec))


def live_allowed() -> bool:
    """Live needs the env switch as well as dry_run=False — two independent gates."""
    return os.environ.get(LIVE_ENV, "") in ("1", "true", "yes")
