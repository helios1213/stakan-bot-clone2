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
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Below this a spot account cannot place a compliant order at all: MEXC's
# practical spot minimum is ~1.5 USDT and we keep a baseline hold per token.
MIN_VIABLE_BALANCE_USDT = 25.0

DEFAULT_MAX_COST_USDT = 5.0


@dataclass
class BudgetState:
    max_usdt: float = DEFAULT_MAX_COST_USDT
    spent_usdt: float = 0.0
    entries: list = field(default_factory=list)   # recent charges, for the report

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
                st = BudgetState(**raw)
                st.max_usdt = max_usdt          # operator may have raised/lowered it
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
        logger.info("soft-start budget: -%.4f USDT (%s) — spent %.4f / %.2f",
                    amount, reason, self.state.spent_usdt, self.state.max_usdt)
        if self.state.exhausted():
            logger.warning("soft-start budget EXHAUSTED (%.4f / %.2f USDT) — "
                           "no further warming orders will be placed",
                           self.state.spent_usdt, self.state.max_usdt)

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
    order_max = max(1.5, round(bal * 0.12, 2))
    baseline = max(1.0, round(bal * 0.08, 2))
    return SpotSizing(
        order_usdt_min=1.5,
        order_usdt_max=order_max,
        baseline_usdt_per_token=baseline,
        daily_buy_usdt_ceiling=round(bal, 2),
    )


def futures_target_margin(balance_usdt: float) -> float:
    """How much margin one warming position may use: ~10% of the balance.

    One position at a time, so this is the whole futures exposure. At 25 USDT
    that is 2.5 — enough for a 1-contract position on every pair in the universe
    at 5x except the most expensive, which is handled by the affordability check
    at open time.
    """
    return max(0.5, round(max(0.0, float(balance_usdt)) * 0.10, 2))


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
