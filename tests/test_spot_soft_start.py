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

    def __init__(self, held=0.0, ok=True, usdt=1000.0):
        self.orders = []
        self.held = held
        self.ok = ok
        # Вільний USDT: рушій перечитує його ПЕРЕД кожною купівлею (розмір
        # виводиться з балансу на старті, а той за добу витрачається). Фейк
        # без цього поля давав free=0 і всі купівлі мовчки скіпались.
        self.usdt = usdt

    async def currency(self, ticker):
        return PENGU

    async def balances(self, ids):
        from src.execution.spot_soft_start import USDT_CURRENCY_ID
        if ids and ids[0] == USDT_CURRENCY_ID:
            return {"USDT": {"available": self.usdt,
                             "currency_id": USDT_CURRENCY_ID}}
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


@pytest.mark.asyncio
async def test_wind_down_sells_coins_bought_under_an_older_universe(
        tmp_path, monkeypatch):
    """ЖИВИЙ ВИПАДОК 29.08, знайдений оператором.

    Розпродаж ішов по `plan.tokens` — сьогоднішньому денному плану. Але монети
    накопичуються за ВСЮ історію слота: на клоні план був
    ['LINK','PENGU','SUI','TRX'], а найбільший залишок — MX на 12.10 USDT,
    куплений тоді, коли юніверс складався з одного MX. Він не потрапив би в
    розпродаж НІКОЛИ.
    """
    from src.execution.spot_soft_start import DayPlan, SpotSoftStart
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)
    held = {"MX": 12.10, "LINK": 1.83}
    sold = []

    class _Cl:
        async def currency(self, t):
            return type("C", (), {"currency_id": "id-" + t})()
        async def balances(self, ids):
            t = ids[0].replace("id-", "")
            return {t: {"available": held.get(t, 0.0)}}
        async def sell(self, ticker, *, quantity, price):
            sold.append(ticker)
            from src.execution.webkey.spot_client import OrderResult
            return OrderResult(True, False, ticker, "SELL", str(price),
                               str(quantity), {"code": 200})

    e = SpotSoftStart(_Cl(), cfg(tmp_path, universe=("LINK",),
                                 order_usdt_min=0.5), rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["LINK"], buys_target=0,
                     sells_target=0)

    # Тільки план -> MX не бачимо.
    assert await e.wind_down(0.20) == 1
    assert sold == ["LINK"], sold

    # З явним ширшим списком -> MX продається.
    sold.clear()
    assert await e.wind_down(0.20, tokens=["LINK", "MX", "PENGU"]) == 2
    assert "MX" in sold, f"MX знову не потрапив у розпродаж: {sold}"


@pytest.mark.asyncio
async def test_wind_down_asks_for_one_coin_at_a_time(tmp_path, monkeypatch):
    """ВИМІРЯНО 29.08: `balances()` із кількома coinId одразу віддає ПОРОЖНІЙ
    словник — без помилки, тобто «нічого не тримаємо» замість реального
    балансу. Тиха неправда: я сам на ній спіймався, роблячи перевірку
    залишків, і побачив 0.00 там, де було 17.30 USDT.
    """
    from src.execution.spot_soft_start import DayPlan, SpotSoftStart
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)
    sizes = []

    class _Cl:
        async def currency(self, t):
            return type("C", (), {"currency_id": "id-" + t})()
        async def balances(self, ids):
            sizes.append(len(ids))
            return {ids[0].replace("id-", ""): {"available": 10.0}}
        async def sell(self, ticker, *, quantity, price):
            from src.execution.webkey.spot_client import OrderResult
            return OrderResult(True, False, ticker, "SELL", str(price),
                               str(quantity), {"code": 200})

    e = SpotSoftStart(_Cl(), cfg(tmp_path, universe=("MX",), order_usdt_min=0.5),
                      rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["MX"], buys_target=0, sells_target=0)
    await e.wind_down(0.20, tokens=["MX", "LINK", "TRX"])
    assert sizes and all(n == 1 for n in sizes), (
        f"баланси питаються пачкою — вона віддає порожньо: {sizes}")


@pytest.mark.asyncio
async def test_wind_down_keeps_a_share_of_the_WHOLE_spot_balance(
        tmp_path, monkeypatch):
    """ФОРМУЛЮВАННЯ ОПЕРАТОРА: «20% від спотового балансу».

    Перша версія рахувала 20% для КОЖНОЇ монети окремо, і USDT у знаменник не
    входив узагалі. На живих числах слота 1 (монети 17.31 + USDT 8.12) це
    лишало 3.46 — тобто 14% балансу, а не 20. Правильна ціль: 5.09.
    """
    from src.execution.spot_soft_start import (DayPlan, SpotSoftStart,
                                               USDT_CURRENCY_ID)
    prices = {"MXUSDT": 1.0, "TRXUSDT": 1.0}
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: prices.get(s))
    held = {"MX": 16.0, "TRX": 4.0}          # монет на 20.0
    sold = {}

    class _Cl:
        async def currency(self, t):
            return type("C", (), {"currency_id": "id-" + t})()
        async def balances(self, ids):
            if ids[0] == USDT_CURRENCY_ID:
                return {"USDT": {"available": 5.0}}   # спот разом = 25.0
            t = ids[0].replace("id-", "")
            return {t: {"available": held.get(t, 0.0)}}
        async def sell(self, ticker, *, quantity, price):
            sold[ticker] = quantity
            from src.execution.webkey.spot_client import OrderResult
            return OrderResult(True, False, ticker, "SELL", str(price),
                               str(quantity), {"code": 200})

    e = SpotSoftStart(_Cl(), cfg(tmp_path, universe=("MX",), order_usdt_min=0.5),
                      rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["MX"], buys_target=0, sells_target=0)

    await e.wind_down(0.20, tokens=["MX", "TRX"])
    left = sum(held[t] - sold.get(t, 0.0) for t in held)
    # ціль: 20% від 25.0 = 5.0 у монетах
    assert abs(left - 5.0) < 1e-6, f"лишилось {left}, а мало 5.0"
    # і ріжеться ПРОПОРЦІЙНО, а не одна монета в нуль
    assert sold["MX"] > 0 and sold["TRX"] > 0, sold
    assert abs(sold["MX"] / 16.0 - sold["TRX"] / 4.0) < 1e-6, "непропорційно"


@pytest.mark.asyncio
async def test_wind_down_does_nothing_when_already_below_target(
        tmp_path, monkeypatch):
    """Монет уже менше за ціль — продавати нічого, а не «продати ще трохи»."""
    from src.execution.spot_soft_start import (DayPlan, SpotSoftStart,
                                               USDT_CURRENCY_ID)
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)

    class _Cl:
        async def currency(self, t):
            return type("C", (), {"currency_id": "id-" + t})()
        async def balances(self, ids):
            if ids[0] == USDT_CURRENCY_ID:
                return {"USDT": {"available": 90.0}}
            return {"MX": {"available": 2.0}}      # 2 з 92 = 2.2%
        async def sell(self, *a, **k):
            raise AssertionError("продавати не мало")

    e = SpotSoftStart(_Cl(), cfg(tmp_path, universe=("MX",), order_usdt_min=0.5),
                      rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["MX"], buys_target=0, sells_target=0)
    assert await e.wind_down(0.20, tokens=["MX"]) == 0


@pytest.mark.asyncio
async def test_unreadable_usdt_is_flagged_not_silently_ignored(
        tmp_path, monkeypatch, caplog):
    """Без вільного USDT знаменник менший -> ціль нижча -> продамо БІЛЬШЕ, ніж
    треба. Це має бути видно, а не мовчки змінене правило."""
    import logging
    from src.execution.spot_soft_start import (DayPlan, SpotSoftStart,
                                               USDT_CURRENCY_ID)
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)

    class _Cl:
        async def currency(self, t):
            return type("C", (), {"currency_id": "id-" + t})()
        async def balances(self, ids):
            if ids[0] == USDT_CURRENCY_ID:
                raise RuntimeError("нема звʼязку")
            return {"MX": {"available": 10.0}}
        async def sell(self, ticker, *, quantity, price):
            from src.execution.webkey.spot_client import OrderResult
            return OrderResult(True, False, ticker, "SELL", str(price),
                               str(quantity), {"code": 200})

    e = SpotSoftStart(_Cl(), cfg(tmp_path, universe=("MX",), order_usdt_min=0.5),
                      rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["MX"], buys_target=0, sells_target=0)
    with caplog.at_level(logging.WARNING):
        await e.wind_down(0.20, tokens=["MX"])
    assert any("вільний USDT не прочитано" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_wind_down_uses_the_exchange_minimum_not_the_order_size(
        tmp_path, monkeypatch):
    """ЖИВИЙ БАГ 30.08: розпродаж порахував правильно і НЕ ПРОДАВ НІЧОГО.

        монети 58.58 + USDT 146.87 = 205.45; лишаємо 41.09, продаємо 17.49
        XRPUSDT: частка ~5.45 нижча за мінімальний ноціонал — пропускаю
        MXUSDT / PENGUUSDT / AVAXUSDT — так само
        -> «розпродаж завершено, лишили ~20%», а на балансі 58.56

    Причина: як «межу пилу» брався `cfg.order_usdt_min`, який МАСШТАБУЄТЬСЯ
    від балансу (4% від 146.87 = 5.87). Одна змінна виконувала дві різні
    ролі — мінімальний розмір ордера ПРОГРІВУ і мінімальний ноціонал БІРЖІ.
    Що більший гаманець, то сильніше вона блокувала розпродаж — тобто саме
    там, де продати треба найбільше.
    """
    from src.execution.spot_soft_start import (DayPlan, SpotSoftStart,
                                               USDT_CURRENCY_ID)
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)
    held = {"MX": 20.0, "XRP": 20.0, "PENGU": 18.58}
    sold = []

    class _Cl:
        async def currency(self, t):
            return type("C", (), {"currency_id": "id-" + t})()
        async def balances(self, ids):
            if ids[0] == USDT_CURRENCY_ID:
                return {"USDT": {"available": 146.87}}
            t = ids[0].replace("id-", "")
            return {t: {"available": held.get(t, 0.0)}}
        async def sell(self, ticker, *, quantity, price):
            sold.append((ticker, quantity))
            from src.execution.webkey.spot_client import OrderResult
            return OrderResult(True, False, ticker, "SELL", str(price),
                               str(quantity), {"code": 200})

    # order_usdt_min як на живому гаманці — 5.87, вище за кожну з часток
    c = cfg(tmp_path, universe=("MX",), order_usdt_min=5.87)
    e = SpotSoftStart(_Cl(), c, rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["MX"], buys_target=0, sells_target=0)

    sent = await e.wind_down(0.20, tokens=["MX", "XRP", "PENGU"])
    assert sent == 3, f"розпродаж знову впав у межу розміру ордера: {sold}"
    # ціль: 20% від (58.58 монет + 146.87 USDT) = 41.09 -> продати 17.49
    total_sold = sum(q for _, q in sold)
    assert abs(total_sold - 17.49) < 0.05, total_sold


@pytest.mark.asyncio
async def test_real_dust_is_still_skipped(tmp_path, monkeypatch):
    """Біржовий мінімум лишається: ордер на 0.3 USDT це відмова біржі."""
    from src.execution.spot_soft_start import (DayPlan, SpotSoftStart,
                                               USDT_CURRENCY_ID)
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)

    class _Cl:
        async def currency(self, t):
            return type("C", (), {"currency_id": "id-" + t})()
        async def balances(self, ids):
            if ids[0] == USDT_CURRENCY_ID:
                return {"USDT": {"available": 0.0}}
            return {"MX": {"available": 1.0}}
        async def sell(self, *a, **k):
            raise AssertionError("пил не має відправлятись")

    e = SpotSoftStart(_Cl(), cfg(tmp_path, universe=("MX",), order_usdt_min=0.1),
                      rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["MX"], buys_target=0, sells_target=0)
    # монет на 1.0, лишаємо 20% -> продати 0.8, це нижче біржового мінімуму
    assert await e.wind_down(0.20, tokens=["MX"]) == 0


@pytest.mark.asyncio
async def test_a_wind_down_that_sold_nothing_says_so(tmp_path, monkeypatch,
                                                     caplog):
    """Лог рапортував «лишили ~20%» навіть коли не пішов ЖОДЕН ордер."""
    import logging
    from src.execution.spot_soft_start import (DayPlan, SpotSoftStart,
                                               USDT_CURRENCY_ID)
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)

    class _Cl:
        async def currency(self, t):
            return type("C", (), {"currency_id": "id-" + t})()
        async def balances(self, ids):
            if ids[0] == USDT_CURRENCY_ID:
                return {"USDT": {"available": 0.0}}
            return {"MX": {"available": 1.2}}
        async def sell(self, *a, **k):
            raise AssertionError("не мало")

    e = SpotSoftStart(_Cl(), cfg(tmp_path, universe=("MX",), order_usdt_min=0.1),
                      rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["MX"], buys_target=0, sells_target=0)
    with caplog.at_level(logging.WARNING):
        assert await e.wind_down(0.20, tokens=["MX"]) == 0
    assert any("жодного ордера" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_buy_is_skipped_when_free_usdt_ran_out(tmp_path, monkeypatch):
    """Розмір виводиться з балансу, ЗНЯТОГО НА СТАРТІ, а він за добу
    витрачається на монети. Поки спотова половина вимикалась за порогом, це
    не проявлялось; тепер вона лишається живою при малому USDT (щоб МОГТИ
    ПРОДАВАТИ), і без цієї перевірки кожна купівля йшла б у гарантовану
    відмову біржі — серія «insufficient funds» замість тиші.
    """
    from src.execution.spot_soft_start import DayPlan, SpotSoftStart
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)
    c = FakeClient(usdt=0.50)          # вільного майже немає
    e = SpotSoftStart(c, cfg(tmp_path, universe=("PENGU",), order_usdt_min=1.5,
                             order_usdt_max=2.0), rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["PENGU"], buys_target=5,
                     sells_target=0)
    assert await e.maybe_buy() is False
    assert not [o for o in c.orders if o[0] == "BUY"], c.orders


@pytest.mark.asyncio
async def test_unreadable_free_balance_does_not_stop_buying(tmp_path,
                                                            monkeypatch):
    """Не прочитали — це НЕ «коштів немає»: інакше блимання мережі зупиняло б
    прогрів. Гейт пропускається, а не спрацьовує."""
    from src.execution.spot_soft_start import DayPlan, SpotSoftStart
    monkeypatch.setattr("src.execution.spot_soft_start.public_last_price",
                        lambda s: 1.0)

    class _Cl(FakeClient):
        async def balances(self, ids):
            raise RuntimeError("мережа")

    c = _Cl()
    e = SpotSoftStart(c, cfg(tmp_path, universe=("PENGU",), order_usdt_min=1.5,
                             order_usdt_max=2.0), rng=random.Random(1))
    e.plan = DayPlan(date=e.plan.date, tokens=["PENGU"], buys_target=5,
                     sells_target=0)
    assert await e.maybe_buy() is True
    assert [o for o in c.orders if o[0] == "BUY"]
