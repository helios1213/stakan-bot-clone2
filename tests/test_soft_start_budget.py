"""Tests for the soft-start spend ceiling and balance-driven sizing.

The operator's rule is "warming must not cost more than N USDT". These pin the
parts that make that true:

  1. The ceiling is checked BEFORE an order and is a one-way ratchet — a
     profitable close never refunds room to spend more.
  2. It survives a restart. A budget that resets on a crash loop is no budget.
  3. Sizing scales with the balance: 25 USDT warms gently, 50 warms harder,
     without editing config by hand.
  4. A position that would tie up too much of the wallet is refused.
  5. An exhausted budget stops the warmer placing anything.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from src.execution.soft_start_budget import (
    MIN_VIABLE_BALANCE_USDT,
    SoftStartBudget,
    affordable,
    contracts_for_margin,
    futures_round_trip_cost,
    scale_spot_config,
    spot_order_cost,
)


# ---- the ceiling ---------------------------------------------------------

def test_charges_accumulate_and_exhaust(tmp_path):
    b = SoftStartBudget(str(tmp_path / "b.json"), max_usdt=5.0)
    assert b.remaining == 5.0 and not b.exhausted()
    b.charge(2.0, "spot buy")
    b.charge(2.5, "futures round-trip")
    assert b.remaining == pytest.approx(0.5)
    b.charge(0.6, "spot sell")
    assert b.exhausted() and b.remaining == 0.0


def test_can_afford_is_checked_before_spending(tmp_path):
    b = SoftStartBudget(str(tmp_path / "b.json"), max_usdt=1.0)
    assert b.can_afford(0.9) is True
    assert b.can_afford(1.5) is False
    b.charge(0.9, "x")
    assert b.can_afford(0.2) is False, "no room left for a 0.2 charge"


def test_profit_never_refunds_the_budget(tmp_path):
    """One-way ratchet: a winning trade must not buy more spending room."""
    b = SoftStartBudget(str(tmp_path / "b.json"), max_usdt=5.0)
    b.charge(3.0, "cost")
    b.charge(-10.0, "a profitable close")
    assert b.spent == pytest.approx(3.0)


def test_zero_charge_is_a_noop(tmp_path):
    b = SoftStartBudget(str(tmp_path / "b.json"), max_usdt=5.0)
    b.charge(0.0, "nothing")
    assert b.spent == 0.0 and b.state.entries == []


def test_budget_survives_restart(tmp_path):
    p = str(tmp_path / "b.json")
    b1 = SoftStartBudget(p, max_usdt=5.0)
    b1.charge(4.2, "spot")
    b2 = SoftStartBudget(p, max_usdt=5.0)          # process restarts
    assert b2.spent == pytest.approx(4.2)
    assert b2.remaining == pytest.approx(0.8)


def test_operator_can_raise_the_ceiling_without_losing_history(tmp_path):
    p = str(tmp_path / "b.json")
    SoftStartBudget(p, max_usdt=5.0).charge(5.0, "spent it all")
    b = SoftStartBudget(p, max_usdt=8.0)
    assert b.spent == pytest.approx(5.0)
    assert not b.exhausted() and b.remaining == pytest.approx(3.0)


def test_corrupt_state_starts_fresh_rather_than_crashing(tmp_path):
    p = tmp_path / "b.json"
    p.write_text("{not json")
    b = SoftStartBudget(str(p), max_usdt=5.0)
    assert b.spent == 0.0


def test_reset_clears_spending(tmp_path):
    b = SoftStartBudget(str(tmp_path / "b.json"), max_usdt=5.0)
    b.charge(4.0, "x")
    b.reset()
    assert b.spent == 0.0 and not b.exhausted()


# ---- cost estimation -----------------------------------------------------

def test_spot_cost_is_the_crossed_spread():
    assert spot_order_cost(100.0, 0.002) == pytest.approx(0.2)
    assert spot_order_cost(3.0, 0.002) == pytest.approx(0.006)


def test_futures_cost_covers_two_crossings_plus_funding():
    c = futures_round_trip_cost(100.0, spread_frac=0.0005, funding_frac=0.0001)
    assert c == pytest.approx(0.11)


def test_a_days_worth_of_warming_fits_a_5_usdt_ceiling():
    """Sanity-check the operator's number against the actual plan.

    Worst case at a 25 USDT balance: 10 buys + 10 sells at the max order size,
    plus 3 futures round trips on the priciest pair.
    """
    s = scale_spot_config(25.0)
    spot_day = 20 * spot_order_cost(s.order_usdt_max, 0.002)
    fut_day = 3 * futures_round_trip_cost(29.09)      # PEPE, 1 contract
    assert spot_day + fut_day < 5.0, (spot_day, fut_day)


# ---- balance-driven sizing ----------------------------------------------

def test_sizing_scales_with_balance():
    s25 = scale_spot_config(25.0)
    s50 = scale_spot_config(50.0)
    assert s50.order_usdt_max > s25.order_usdt_max
    assert s50.baseline_usdt_per_token > s25.baseline_usdt_per_token
    assert s25.order_usdt_max == pytest.approx(3.0)
    assert s25.baseline_usdt_per_token == pytest.approx(2.0)
    assert s50.order_usdt_max == pytest.approx(6.0)
    assert s50.baseline_usdt_per_token == pytest.approx(4.0)


def test_baselines_never_lock_up_the_whole_balance():
    """4 tokens on hold must leave room to actually trade."""
    for bal in (25.0, 50.0, 100.0, 500.0):
        s = scale_spot_config(bal)
        assert s.baseline_usdt_per_token * 4 < bal * 0.5


def test_order_size_never_drops_below_the_exchange_minimum():
    s = scale_spot_config(5.0)                 # tiny balance
    assert s.order_usdt_min >= 1.5
    assert s.order_usdt_max >= 1.5


def test_ten_max_orders_cannot_drain_the_balance():
    for bal in (25.0, 50.0, 200.0):
        s = scale_spot_config(bal)
        assert s.order_usdt_max * 10 <= bal * 1.25


# ---- futures position sizing --------------------------------------------

def test_contracts_scale_with_target_margin():
    # PEPE: contractSize 1e7, price ~2.909e-6 -> ~29.09 USDT per contract
    cs, px = 1e7, 2.909e-6
    assert contracts_for_margin(2.5, 5, cs, px) == 1      # 12.5 notional < 29
    assert contracts_for_margin(30.0, 5, cs, px) == 5     # 150 / 29.09


def test_contracts_never_zero():
    """A too-small target must return 1, so the caller decides affordability
    explicitly instead of an order silently never being sent."""
    assert contracts_for_margin(0.01, 1, 1e7, 2.909e-6) == 1
    assert contracts_for_margin(5.0, 5, 0, 0) == 1


def test_affordable_refuses_to_corner_the_wallet():
    assert affordable(5.82, 25.0) is True       # PEPE @5x on 25 USDT = 23%
    assert affordable(5.82, 10.0) is False      # 58% of the wallet
    assert affordable(1.0, 0.0) is False


def test_min_viable_balance_is_the_documented_floor():
    assert MIN_VIABLE_BALANCE_USDT == 25.0


# ---- integration with the spot warmer -----------------------------------

@pytest.mark.asyncio
async def test_spot_warmer_keeps_buying_regardless_of_spend(tmp_path, monkeypatch):
    from src.execution.spot_soft_start import DayPlan, SoftStartConfig, SpotSoftStart
    from src.execution.webkey.spot_client import OrderResult

    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 0.01)

    class FakeClient:
        def __init__(self):
            self.orders = []

        async def buy(self, ticker, *, usdt, price):
            self.orders.append(usdt)
            return OrderResult(True, False, ticker, "BUY", str(price),
                               str(usdt / price), {"code": 200})

    b = SoftStartBudget(str(tmp_path / "b.json"), max_usdt=0.001)
    b.charge(0.001, "pre-spent")
    cfg = SoftStartConfig(universe=("PENGU",), state_path=str(tmp_path / "s.json"))
    c = FakeClient()
    e = SpotSoftStart(c, cfg, rng=random.Random(1), budget=b)
    e.plan = DayPlan(date=e.plan.date, tokens=["PENGU"], buys_target=5, sells_target=5)

    # ПОВЕДІНКУ ЗМІНЕНО 2026-08-26 (рішення оператора): стелі витрат більше
    # немає — прогрів має гріти, а не впиратись у ліміт. Раніше тут пінилось
    # протилежне: «вичерпаний бюджет не має нічого відправляти».
    # Облік ЛИШИВСЯ: витрати далі рахуються і показуються у звіті.
    assert await e.maybe_buy() is True
    assert c.orders, "стеля витрат більше не має блокувати прогрів"
    assert b.spent > 0.001, "витрати перестали обліковуватись"
