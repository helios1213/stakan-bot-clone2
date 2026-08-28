"""A hard spend ceiling for account warming, and balance-driven sizing.

Two problems this solves.

1. HOW MUCH WARMING IS ALLOWED TO COST
   Warming is not supposed to make money — but it must not quietly bleed
   either. The operator's rule: **no more than N USDT of cost, total**.

   "Cost" here deliberately does NOT mean market movement. If a token drops 10%
   while held, warming did not spend that — the market moved. What warming
   actually burns is:

     * spread it crosses to make an order fill (`marketable_buffer` per side),
     * funding on a futures position held across a settlement,
     * a futures close that came back negative.

   The first two are known BEFORE the order is sent, so they are charged
   up-front and an order that would breach the ceiling is never placed. The
   third is charged after the fact. Profitable closes do NOT refund the budget —
   the ceiling is a one-way ratchet, which is the conservative reading.

2. HOW BIG THE ORDERS SHOULD BE
   The same config must work on a 25 USDT balance and on a 50 USDT one, without
   the operator hand-editing numbers. `scale_spot_config` derives the sizes from
   the balance, so a bigger account simply warms harder, and a small one still
   places orders that clear the exchange minimum.

State is persisted so the ceiling survives restarts — otherwise a crash loop
would silently reset the budget and spend forever.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

logger = logging.getLogger(__name__)

# Below this a spot account cannot place a compliant order at all: MEXC's
# practical spot minimum is ~1.5 USDT and we keep a baseline hold per token.
# Нижче цього балансу половина прогріву простоює. 25 -> 10 (рішення оператора
# 2026-08-26): на живому дні спот просів 25.00 -> 13.21, бо покупки
# перетворили USDT на монети, і половина стала — тобто поріг зупиняв фарм
# рівно так само, як стеля витрат, яку прибрали.
#
# ЧОМУ САМЕ 10, А НЕ 5. Розмір ордера рахується як 12% балансу, а мінімальний
# ноціонал на споті MEXC ~1 USDT. При балансі 5 стеля розміру була б 0.60 —
# нижче мінімуму, і КОЖЕН ордер відхилявся б біржею. 10 лишає робочий діапазон
# (див. scale_spot_config, який тепер теж масштабує НИЖНЮ межу).
MIN_VIABLE_BALANCE_USDT = 10.0

DEFAULT_MAX_COST_USDT = 5.0


@dataclass
class BudgetState:
    max_usdt: float = DEFAULT_MAX_COST_USDT
    spent_usdt: float = 0.0
    entries: list = field(default_factory=list)   # recent charges, for the report
    # Реалізований PnL прогріву (може бути ±). ОКРЕМО від `spent_usdt`, бо це
    # різні за природою числа: `spent` — те, що ми свідомо платимо (спред,
    # комісія, фандинг) і що відоме ДО відправки; `pnl` — рух ринку, відомий
    # лише постфактум і здатний бути додатним. Змішавши їх в одне поле, ми б
    # втратили можливість сказати, скільки прогрів коштує САМ ПО СОБІ.
    pnl_usdt: float = 0.0
    # РЕАЛІЗОВАНИЙ фʼючерсний PnL окремо від спотового кеш-фло. Разом вони
    # дають `pnl_usdt`, але для читання це різні речі: фʼючерсний PnL —
    # справжній прибуток/збиток, а спотовий кеш-фло — здебільшого USDT, що
    # змінили форму на монети. Змішані в одному полі вони давали «PnL -4.358»
    # при реальному результаті -0.34, і звіт читався як катастрофа.
    futures_pnl_usdt: float = 0.0
    # Чистий спотовий кеш-фло за ВСЮ кампанію: купівлі мінусом, продажі
    # плюсом. Відʼємне значення = стільки USDT зараз лежить у монетах.
    #
    # ЧОМУ ОКРЕМИМ ЛІЧИЛЬНИКОМ. Раніше «скільки в монетах» виводилось із
    # `plan.spent_usdt` (ДЕННИЙ план, обнуляється щодоби) мінус усі продажі
    # за кампанію — дві РІЗНІ ЧАСОВІ БАЗИ. Уже на другий день різниця ставала
    # відʼємною, обрізалась у нуль, і «разом» показувало +46.57 замість 0.26.
    # Тут обидва боки з одного джерела й одного періоду.
    spot_flow_usdt: float = 0.0

    def remaining(self) -> float:
        return max(0.0, self.max_usdt - self.spent_usdt)

    def exhausted(self) -> bool:
        return self.spent_usdt >= self.max_usdt


class SoftStartBudget:
    """One-way spend ceiling shared by the spot and futures warmers of a slot."""

    def __init__(self, path: str, max_usdt: float = DEFAULT_MAX_COST_USDT) -> None:
        self.path = path
        self.state = self._load(path, max_usdt)

    @staticmethod
    def _load(path: str, max_usdt: float) -> BudgetState:
        p = Path(path)
        if p.exists():
            try:
                raw = json.loads(p.read_text())
                # Невідомі поля з новішої версії не мають ламати завантаження,
                # а відсутні — беруть дефолт.
                known = {f.name for f in fields(BudgetState)}
                st = BudgetState(**{k: v for k, v in raw.items() if k in known})
                st.max_usdt = max_usdt          # operator may have raised/lowered it

                # ЗАСІВ `spot_flow_usdt` ДЛЯ ФАЙЛІВ, ЗАПИСАНИХ ДО ЙОГО ПОЯВИ.
                # Без цього поле стартує з нуля, тоді як `pnl_usdt` збережений,
                # і «у монетах» стає 0 при живому кеш-фло — рівно той баг, що
                # показував «разом +46.57» замість 0.26, тільки після деплою
                # фіксу. Відновлення ТОЧНЕ і не залежить від обрізаного списку
                # записів: pnl = фʼючерси + спот, отже спот = pnl - фʼючерси.
                if "spot_flow_usdt" not in raw and st.pnl_usdt:
                    st.spot_flow_usdt = st.pnl_usdt - st.futures_pnl_usdt
                    logger.info("soft-start budget: спотовий потік відновлено "
                                "зі старого файла — %.4f USDT",
                                st.spot_flow_usdt)
                return st
            except Exception as e:
                logger.warning("soft-start budget unreadable (%s) — starting fresh", e)
        return BudgetState(max_usdt=max_usdt)

    def _save(self) -> None:
        try:
            Path(self.path).write_text(json.dumps(asdict(self.state), indent=2))
        except Exception as e:
            # Losing this file means losing the ceiling — say so loudly.
            logger.error("soft-start budget SAVE FAILED (%s) — ceiling may reset "
                         "on restart", e)

    # ---- queries --------------------------------------------------------

    @property
    def remaining(self) -> float:
        return self.state.remaining()

    @property
    def spent(self) -> float:
        return self.state.spent_usdt

    def exhausted(self) -> bool:
        return self.state.exhausted()

    def can_afford(self, cost_usdt: float) -> bool:
        """Would this charge stay inside the ceiling? Checked BEFORE sending."""
        return (self.state.spent_usdt + max(0.0, cost_usdt)) <= self.state.max_usdt

    # ---- charging -------------------------------------------------------

    def record_pnl(self, usdt: float, reason: str) -> None:
        """Записати реалізований PnL (±). Стелю не чіпає — її більше немає.

        Двосторонній, на відміну від `charge`: прибуткове закриття справді
        зменшує вартість прогріву, і ховати це було б брехнею в наш бік.
        """
        try:
            amount = float(usdt)
        except (TypeError, ValueError):
            return
        self.state.pnl_usdt += amount
        if str(reason).startswith("futures"):
            self.state.futures_pnl_usdt += amount
        elif str(reason).startswith("spot"):
            self.state.spot_flow_usdt += amount
        self.state.entries.append(
            {"ts": int(time.time()), "usdt": round(amount, 6),
             "reason": f"PnL {reason}"})
        if len(self.state.entries) > 200:
            self.state.entries = self.state.entries[-200:]
        self._save()
        logger.info("soft-start PnL: %+.4f USDT (%s) — сумарно %+.4f",
                    amount, reason, self.state.pnl_usdt)

    @property
    def pnl(self) -> float:
        return self.state.pnl_usdt

    @property
    def held_spot_value(self) -> float:
        """Скільки USDT зараз лежить у куплених монетах, ЗА ЦІНОЮ КУПІВЛІ.

        Не ринкова переоцінка: ми знаємо, скільки вклали й скільки повернули,
        але не переоцінюємо залишок за курсом — інакше число мінялось би
        щохвилини, а підсумок здавався б точнішим, ніж він є.
        """
        return max(0.0, -self.state.spot_flow_usdt)

    @property
    def futures_pnl(self) -> float:
        """Тільки реалізований фʼючерсний результат — без спотового кеш-фло."""
        return self.state.futures_pnl_usdt

    @property
    def net_cost(self) -> float:
        """Скільки прогрів коштував РАЗОМ: свідомі витрати мінус зароблене.

        Додатне = прогрів у мінус; від'ємне = вийшли в плюс попри витрати.
        """
        return self.state.spent_usdt - self.state.pnl_usdt

    def charge(self, cost_usdt: float, reason: str) -> None:
        """Book a cost. Negative amounts are ignored: a profit never refunds."""
        amount = max(0.0, float(cost_usdt))
        if amount == 0:
            return
        self.state.spent_usdt += amount
        self.state.entries.append(
            {"ts": int(time.time()), "usdt": round(amount, 6), "reason": reason})
        # Keep the log bounded; it exists for the report, not for accounting.
        if len(self.state.entries) > 200:
            self.state.entries = self.state.entries[-200:]
        self._save()
        # Без «/стеля» і без попередження про вичерпання: стелю прибрано,
        # і рядок, що обіцяє зупинку якої не буде, — це брехливий лог.
        logger.info("soft-start витрати: -%.4f USDT (%s) — разом %.4f",
                    amount, reason, self.state.spent_usdt)

    def reset(self) -> None:
        self.state = BudgetState(max_usdt=self.state.max_usdt)
        self._save()


# ---- cost estimation ----------------------------------------------------

def spot_order_cost(notional_usdt: float, marketable_buffer: float,
                    fee_frac: float = 0.0) -> float:
    """What ONE spot order costs us.

    Два доданки, обидва відомі ДО відправки:
      * `marketable_buffer` — ми свідомо ставимо ціну трохи крізь дотик, щоб
        ордер налився; цей офсет і є вартістю перетину;
      * `fee_frac` — комісія біржі за цей ордер. Була відсутня в моделі, бо
        прогрів починався з припущення «у нас 0%». Коли нуля немає, прогрів
        усе одно йде — просто комісія ЗАПИСУЄТЬСЯ у витрати, а не ігнорується.

    Ордери прогріву — marketable limit, тобто ТЕЙКЕР. Передавати сюди
    мейкерську ставку означало б занизити витрати.
    """
    return abs(notional_usdt) * (abs(marketable_buffer) + abs(fee_frac))


def futures_round_trip_cost(notional_usdt: float, spread_frac: float = 0.0005,
                            funding_frac: float = 0.0001,
                            fee_frac: float = 0.0) -> float:
    """Estimated cost of open+close on a futures position.

    Two spread crossings plus, conservatively, one funding settlement — a hold
    of up to 300 minutes can span one. Market PnL is NOT included here; a losing
    close is charged separately, when it is known.

    `fee_frac` — комісія за ОДНУ ногу; множиться на 2, бо позицію прогріву і
    відкривають, і закривають. Обидві ноги йдуть `ORDER_TYPE_MARKET` (type 5),
    тобто ТЕЙКЕРСЬКІ — сюди має приходити takerFee, не makerFee.
    Нуль означає «комісії немає», а не «невідомо»: невідому ставку прогрів
    трактує окремо (див. `FuturesSoftStart.pick_pair`).
    """
    return abs(notional_usdt) * (2 * abs(spread_frac) + abs(funding_frac)
                                 + 2 * abs(fee_frac))


# ---- balance-driven sizing ----------------------------------------------

@dataclass
class SpotSizing:
    order_usdt_min: float
    order_usdt_max: float
    baseline_usdt_per_token: float
    daily_buy_usdt_ceiling: float


def scale_spot_config(balance_usdt: float, *, max_tokens: int = 4) -> SpotSizing:
    """Derive spot order sizes from the actual balance.

    Shape of the rule:
      * a per-token baseline hold of ~8% of the balance, so `max_tokens` of them
        lock up roughly a third and the rest stays liquid to trade with;
      * a max order of ~12% of the balance, so ten of them cannot drain it;
      * a daily ceiling of the whole balance — buys and sells cycle, they do not
        accumulate.

    At 25 USDT that gives 1.5-3.0 per order and a 2.0 baseline; at 50 it gives
    1.5-6.0 and 4.0 — bigger account, harder warming, same config object.
    """
    bal = max(0.0, float(balance_usdt))
    # НИЖНЯ МЕЖА ТЕЖ МАСШТАБУЄТЬСЯ. Була прибита 1.5, і на малому балансі це
    # ламало сайзинг: при 13 USDT стеля 12% = 1.59, тобто діапазон 1.5-1.59 —
    # усі ордери фактично однакові, а нижче 12.5 нижня межа взагалі
    # перевищувала верхню. 1.1 — запас над мінімальним ноціоналом MEXC (~1).
    order_min = max(1.1, round(bal * 0.04, 2))
    # Стеля не менша за 1.6x від низу, інакше «діапазон» вироджується в точку.
    order_max = max(round(order_min * 1.6, 2), round(bal * 0.12, 2))
    baseline = max(1.0, round(bal * 0.08, 2))
    return SpotSizing(
        order_usdt_min=order_min,
        order_usdt_max=order_max,
        baseline_usdt_per_token=baseline,
        daily_buy_usdt_ceiling=round(bal, 2),
    )


# Частка балансу під маржу однієї позиції прогріву. РОЗКИД, а не константа:
# рівно 10% щоразу — це прибитий підпис, помітний навіть без аналізу, бо всі
# наші прогрівні позиції мали б однакову маржу з точністю до копійок.
MARGIN_FRAC_MIN = 0.06
MARGIN_FRAC_MAX = 0.14


def futures_target_margin(balance_usdt: float, rng=None) -> float:
    """How much margin one warming position may use: 6-14% of the balance.

    One position at a time, so this is the whole futures exposure. At 25 USDT
    that is 1.5-3.5 — enough for a 1-contract position on every pair in the
    universe at 5x except the most expensive, which is handled by the
    affordability check at open time.

    `rng=None` дає детермінований центр діапазону (10%) — так поводяться старі
    виклики й тести. Прогрів завжди передає свій генератор.
    """
    bal = max(0.0, float(balance_usdt))
    frac = 0.10 if rng is None else rng.uniform(MARGIN_FRAC_MIN, MARGIN_FRAC_MAX)
    return max(0.5, round(bal * frac, 2))


def human_order_usdt(rng, lo: float, hi: float, price: float | None = None,
                     qty_decimals: int = 4) -> float:
    """Сума одного спот-ордера — так, щоб серія не виглядала машинною.

    ТРИ ПРОБЛЕМИ РІВНОМІРНОГО `uniform(lo, hi)`, яким це було:

    1. **Діапазон вузький** (при балансі 25 це 1.5-3.0, тобто рівно вдвічі),
       тож на око кожна покупка «десь два бакси». Формально випадково —
       практично однаково.
    2. **Розподіл рівномірний**, а це сам по собі підпис: сума НІКОЛИ не буває
       ані біля нижньої межі частіше, ані зрідка великою. У людини хвіст
       важкий — багато дрібних і час від часу одна помітна.
    3. **Кількість виходить «машинною».** Біржа бачить не долари, а КІЛЬКІСТЬ:
       $1.78 при ціні 2.66 дає 0.6692 монети. Людина частіше купує 1, 2, 5, 10
       монет — рівне число.

    Тому: логарифмічно-рівномірний розіграш (важкий хвіст) + у частині випадків
    прив'язка до РІВНОЇ КІЛЬКОСТІ монет, якщо ціна відома і така кількість
    влазить у діапазон. Повертає суму в USDT.
    """
    lo = max(0.01, float(lo))
    hi = max(lo, float(hi))
    # log-uniform: рівна ймовірність на кожен ПОРЯДОК, а не на кожен долар.
    import math
    u = rng.uniform(math.log(lo), math.log(hi))
    usdt = math.exp(u)

    # У ~35% випадків цілимось у рівну кількість монет. Не завжди: суцільно
    # рівні кількості були б таким самим підписом, як суцільно нерівні.
    if price and price > 0 and rng.random() < 0.35:
        # ЗБИРАЄМО ВСІ придатні варіанти, а не беремо перший-ліпший.
        #
        # ЧОМУ ЦЕ ВАЖЛИВО, і я на цьому спіймався: при вузькому діапазоні у
        # нього влазить РІВНО ОДНА рівна кількість. На MX ($2.66) у смугу
        # 1.5-3.0 проходить тільки «1 монета» — і прив'язка почала видавати
        # 2.66 у 8 випадках із 20. Повторюване однакове число — підпис
        # ЯСКРАВІШИЙ за будь-який нерівний розкид, тобто «фікс» робив гірше.
        cands = set()
        for step in (1000, 100, 50, 10, 5, 1, 0.5, 0.1):
            for k in (0, 1):                       # вниз і вгору від розіграшу
                snapped = (int(usdt / price / step) + k) * step
                if snapped <= 0:
                    continue
                cand = round(snapped * price, 2)
                if lo <= cand <= hi:
                    cands.add(cand)
        # Менше двох варіантів — прив'язка вироджується в константу; краще
        # лишити «нерівну» суму, ніж повторювати одне й те саме число.
        if len(cands) >= 2:
            return rng.choice(sorted(cands))
    return round(usdt, 2)


def contracts_for_margin(target_margin_usdt: float, leverage: int,
                         contract_size: float, price: float) -> int:
    """How many contracts approximate the target margin. Always >= 1.

    Returns 1 when even a single contract exceeds the target — the caller must
    then decide affordability explicitly (see `affordable`), rather than this
    function silently returning 0 and the order never being sent.
    """
    if contract_size <= 0 or price <= 0 or leverage <= 0:
        return 1
    one_contract_notional = contract_size * price
    target_notional = target_margin_usdt * leverage
    return max(1, int(target_notional // one_contract_notional))


def affordable(margin_needed_usdt: float, balance_usdt: float,
               max_fraction: float = 0.35) -> bool:
    """Refuse a position that would tie up more than a third of the balance.

    A 1-contract PEPE position needs ~5.8 USDT of margin at 5x; on a 25 USDT
    account that is 23% — fine. On a 10 USDT account it would be 58%, and
    warming must not corner the account like that.
    """
    if balance_usdt <= 0:
        return False
    return margin_needed_usdt <= balance_usdt * max_fraction
