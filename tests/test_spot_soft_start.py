"""Tests for spot soft-start. No network, no orders, deterministic RNG.

What these pin — every one of them is a money-safety property:
  1. The daily spend ceiling is enforced BEFORE the order, against money
     already spent today.
  2. A token is never sold below its baseline hold.
  3. A sell clamped by the baseline that falls under the min notional is
     skipped rather than sent as dust.
  4. Counters only advance on a SUCCESSFUL order — a rejection must not eat
     the day's budget.
  5. A nonsense config is rejected at construction, not at 3am on a live run.
"""
from __future__ import annotations

import random
from datetime import datetime

import pytest

from src.execution.spot_soft_start import (
    DayPlan,
    SoftStartConfig,
    SpotSoftStart,
    new_day_plan,
)
from src.execution.webkey.spot_client import OrderResult
from src.execution.webkey.spot_currency import SpotCurrency

PENGU = SpotCurrency(
    ticker="PENGU", quote="USDT",
    currency_id="1c2cf4f4531b404d9bb5622f3881f32c",
    market_currency_id="128f589271cb4951b03e71e6323eb7be",
    price_scale=6, qty_scale=2, full_name="Pudgy Penguins",
    order_types=("LIMIT_ORDER",),
)


class FakeClient:
    """Records what would be ordered; never touches the network."""

    def __init__(self, held=0.0, ok=True):
        self.orders = []
        self.held = held
        self.ok = ok

    async def currency(self, ticker):
        return PENGU

    async def balances(self, ids):
        return {"PENGU": {"available": self.held, "currency_id": PENGU.currency_id}}

    async def buy(self, ticker, *, usdt, price):
        self.orders.append(("BUY", ticker, usdt, price))
        return OrderResult(self.ok, True, ticker, "BUY", str(price), str(usdt / price),
                           {"code": 200 if self.ok else 400})

    async def sell(self, ticker, *, quantity, price):
        self.orders.append(("SELL", ticker, quantity, price))
        return OrderResult(self.ok, True, ticker, "SELL", str(price), str(quantity),
                           {"code": 200 if self.ok else 400})


def cfg(tmp_path, **kw) -> SoftStartConfig:
    kw.setdefault("universe", ("PENGU",))
    kw.setdefault("state_path", str(tmp_path / "state.json"))
    return SoftStartConfig(**kw)


def engine(tmp_path, client, seed=7, **kw) -> SpotSoftStart:
    e = SpotSoftStart(client, cfg(tmp_path, **kw), rng=random.Random(seed))
    e.plan = DayPlan(date=e.plan.date, tokens=["PENGU"],
                     buys_target=10, sells_target=10)
    return e


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda symbol: 0.01)


def test_invalid_config_rejected_at_construction(tmp_path):
    with pytest.raises(ValueError):
        SpotSoftStart(FakeClient(), cfg(tmp_path, order_usdt_min=0))
    with pytest.raises(ValueError):
        SpotSoftStart(FakeClient(), cfg(tmp_path, universe=()))
    with pytest.raises(ValueError, match="daily ceiling"):
        # every buy would silently skip — better to fail loudly at startup
        SpotSoftStart(FakeClient(), cfg(tmp_path, order_usdt_max=150,
                                        daily_buy_usdt_ceiling=100))


@pytest.mark.asyncio
async def test_daily_ceiling_blocks_the_buy(tmp_path):
    c = FakeClient()
    e = engine(tmp_path, c, order_usdt_min=100, order_usdt_max=150,
               daily_buy_usdt_ceiling=150)
    e.plan.spent_usdt = 140.0            # only 10 USDT of room left
    assert await e.maybe_buy() is False
    assert c.orders == []


@pytest.mark.asyncio
async def test_never_sells_below_baseline(tmp_path):
    """Held value sits at the baseline -> nothing may be sold."""
    c = FakeClient(held=1000.0)          # 1000 * 0.01 = 10 USDT held
    e = engine(tmp_path, c, baseline_usdt_per_token=10.0)
    assert await e.maybe_sell() is False
    assert c.orders == []


@pytest.mark.asyncio
async def test_sell_leaves_at_least_the_baseline(tmp_path):
    c = FakeClient(held=10000.0)         # 100 USDT held, baseline 10
    e = engine(tmp_path, c, baseline_usdt_per_token=10.0)
    assert await e.maybe_sell() is True
    _, _, qty, px = c.orders[0]
    remaining_value = (c.held - qty) * 0.01
    assert remaining_value >= 10.0 - 1e-6, f"breached baseline: {remaining_value}"


@pytest.mark.asyncio
async def test_dust_sell_is_skipped(tmp_path):
    """Just above baseline: the clamp would leave a sub-minimum order."""
    c = FakeClient(held=1050.0)          # 10.5 USDT held, baseline 10, min 1.5
    e = engine(tmp_path, c, baseline_usdt_per_token=10.0, order_usdt_min=1.5)
    assert await e.maybe_sell() is False
    assert c.orders == []


@pytest.mark.asyncio
async def test_rejected_order_does_not_consume_budget(tmp_path):
    c = FakeClient(ok=False)
    e = engine(tmp_path, c)
    before = (e.plan.buys_done, e.plan.spent_usdt)
    assert await e.maybe_buy() is False
    assert (e.plan.buys_done, e.plan.spent_usdt) == before


@pytest.mark.asyncio
async def test_targets_cap_the_day(tmp_path):
    c = FakeClient()
    e = engine(tmp_path, c)
    e.plan.buys_target = 2
    for _ in range(5):
        await e.maybe_buy()
    assert e.plan.buys_done == 2
    assert len(c.orders) == 2


def test_day_plan_respects_spec_bounds(tmp_path):
    """1-4 токени/день, купівлі й продажі — В МЕЖАХ КОНФІГУ.

    Межі беруться з `c`, а не прибиті числами: квоту підняли 2026-08-26
    (10/10 -> 25/20), і прибите число ламало б цей тест на кожній зміні
    квоти, нічого при цьому не перевіряючи по суті.
    """
    c = cfg(tmp_path, universe=("A", "B", "C", "D", "E", "F"))
    for seed in range(60):
        p = new_day_plan(c, random.Random(seed))
        assert 1 <= len(p.tokens) <= 4
        assert len(set(p.tokens)) == len(p.tokens), "no duplicate tokens"
        assert c.buys_per_day_min <= p.buys_target <= c.buys_per_day_max
        assert c.sells_per_day_min <= p.sells_target <= c.sells_per_day_max


def test_active_hours_window(tmp_path):
    e = SpotSoftStart(FakeClient(), cfg(tmp_path, active_hour_start=6,
                                        active_hour_end=23))
    assert e.active_now(datetime(2026, 8, 20, 6, 0)) is True
    assert e.active_now(datetime(2026, 8, 20, 22, 59)) is True
    assert e.active_now(datetime(2026, 8, 20, 23, 0)) is False
    assert e.active_now(datetime(2026, 8, 20, 3, 0)) is False


@pytest.mark.asyncio
async def test_tick_is_inert_outside_active_hours(tmp_path, monkeypatch):
    c = FakeClient()
    e = engine(tmp_path, c)
    monkeypatch.setattr(e, "active_now", lambda now=None: False)
    await e.tick()
    assert c.orders == []


# ---- розпродаж наприкінці кампанії (2026-08-29) ----------------------------

@pytest.mark.asyncio
async def test_wind_down_ignores_the_baseline_that_blocks_normal_sells(
        tmp_path, monkeypatch):
    """ЩО ЦЕ ЛІКУЄ. `maybe_sell` НІКОЛИ не продає нижче базового залишку —
    правильно під час прогріву (акаунт має виглядати як такий, що ТРИМАЄ
    монети), але через це в кінці кампанії все куплене лишалось замкненим:
    на клоні 29.08 це були 4.36 і 7.90 USDT.

    Розпродаж базовий залишок свідомо ігнорує: гріти більше нічого.
    """
    from src.execution.spot_soft_start import DayPlan, SpotSoftStart
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)

    sold = []

    class _Cl:
        async def currency(self, t):
            return type("C", (), {"currency_id": "cid"})()

        async def balances(self, ids):
            return {"MX": {"available": 10.0}}

        async def sell(self, ticker, *, quantity, price):
            sold.append((ticker, quantity))
            from src.execution.webkey.spot_client import OrderResult
            return OrderResult(True, False, ticker, "SELL", str(price),
                               str(quantity), {"code": 200})

    # baseline 5.0 — звичайний продаж не зміг би опустити нижче нього
    c = cfg(tmp_path, universe=("MX",), baseline_usdt_per_token=5.0,
            order_usdt_min=0.5)
    e = SpotSoftStart(_Cl(), c, rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["MX"], buys_target=0,
                     sells_target=0)

    sent = await e.wind_down(0.20)
    assert sent == 1, "розпродаж нічого не відправив"
    _t, qty = sold[0]
    # тримали 10.0 за ціною 1.0 -> лишити 20% = 2.0, продати 8.0
    assert abs(qty - 8.0) < 1e-6, f"продано {qty}, а мало 8.0 (лишити 20%)"


@pytest.mark.asyncio
async def test_wind_down_leaves_a_remainder_on_purpose(tmp_path, monkeypatch):
    """Рахунок, вичищений у НУЛЬ рівно в мить завершення прогріву, — це теж
    патерн, і помітніший за невеликий залишок."""
    from src.execution.spot_soft_start import DayPlan, SpotSoftStart
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)
    sold = []

    class _Cl:
        async def currency(self, t):
            return type("C", (), {"currency_id": "cid"})()
        async def balances(self, ids):
            return {"MX": {"available": 10.0}}
        async def sell(self, ticker, *, quantity, price):
            sold.append(quantity)
            from src.execution.webkey.spot_client import OrderResult
            return OrderResult(True, False, ticker, "SELL", str(price),
                               str(quantity), {"code": 200})

    e = SpotSoftStart(_Cl(), cfg(tmp_path, universe=("MX",), order_usdt_min=0.5),
                      rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["MX"], buys_target=0, sells_target=0)
    await e.wind_down(0.20)
    assert sold and sold[0] < 10.0, "продали ВСЕ — залишку не лишилось"


@pytest.mark.asyncio
async def test_wind_down_skips_dust(tmp_path, monkeypatch):
    """Залишок нижче мінімального ноціоналу — не ордер, а відмова біржі."""
    from src.execution.spot_soft_start import DayPlan, SpotSoftStart
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)

    class _Cl:
        async def currency(self, t):
            return type("C", (), {"currency_id": "cid"})()
        async def balances(self, ids):
            return {"MX": {"available": 0.4}}
        async def sell(self, *a, **k):
            raise AssertionError("пил не має відправлятись")

    e = SpotSoftStart(_Cl(), cfg(tmp_path, universe=("MX",), order_usdt_min=1.5),
                      rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["MX"], buys_target=0, sells_target=0)
    assert await e.wind_down(0.20) == 0


@pytest.mark.asyncio
async def test_wind_down_survives_an_unreadable_balance(tmp_path, monkeypatch):
    """Один токен не прочитався — решта мусить продатись."""
    from src.execution.spot_soft_start import DayPlan, SpotSoftStart
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)
    sold = []

    class _Cl:
        async def currency(self, t):
            if t == "BAD":
                raise RuntimeError("нема такого")
            return type("C", (), {"currency_id": "cid"})()
        async def balances(self, ids):
            return {"MX": {"available": 10.0}}
        async def sell(self, ticker, *, quantity, price):
            sold.append(ticker)
            from src.execution.webkey.spot_client import OrderResult
            return OrderResult(True, False, ticker, "SELL", str(price),
                               str(quantity), {"code": 200})

    e = SpotSoftStart(_Cl(), cfg(tmp_path, universe=("MX", "BAD"),
                                 order_usdt_min=0.5), rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["BAD", "MX"],
                     buys_target=0, sells_target=0)
    assert await e.wind_down(0.20) == 1
    assert sold == ["MX"]
