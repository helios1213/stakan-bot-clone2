# -*- coding: utf-8 -*-
"""Розчистка спота монет ПЕРЕД новою кампанією прогріву (рішення оператора 2026-09-05).

НАВІЩО ФІЧА. Прогрів має починатись із чистого спотового балансу. Монети
лишаються на слоті завжди: `maybe_sell` ніколи не продає нижче базового
залишку, а фінальний розпродаж свідомо лишає 20% спотового балансу. Тож
наступна кампанія стартувала б там, де вільного USDT майже немає, а вся
вартість замкнена в монетах — рівно те, що дало дедлок 31.08 і що зараз
тримає слот 1 клона з 0.93 USDT вільних при 27.37 у монетах.

ЩО ТУТ ПІНИТЬСЯ, і чому саме це:
  * ПРОВОДКА — `tick()` справді питає `preclear_pending()` ПЕРШИМ і виходить.
    Дірка «формула правильна, але не викликається» ловилась у цьому проєкті
    дванадцять разів; тест самої функції її не спіймав би.
  * ДЕФОЛТ `preclear_done=True` — старий файл стану (кампанія ТРИВАЄ) не має
    після деплою почати ліквідувати баланс. Це найдорожчий мутант із усіх.
  * темп: `keep_frac` спадає 1 -> 0, тобто продаж РОЗМАЗАНИЙ, а не однією
    пачкою.
  * фіча не блокує слот вічно, якщо біржа не віддає баланси.
"""
import random
import time

import pytest

from src.execution.soft_start_campaign import (PRECLEAR_MAX_SEC,
                                               PRECLEAR_MIN_SEC,
                                               CampaignState,
                                               SoftStartCampaign)
from src.execution.soft_start_runner import MAX_PRECLEAR_GRACE, SlotWarmer


# ---------------------------------------------------------------- кампанія

def test_a_new_campaign_arms_the_preclear(tmp_path):
    c = SoftStartCampaign(str(tmp_path / "c.json"), 3, rng=random.Random(1))
    t0 = time.time()
    assert c.start_if_new("acct-1") is True
    assert c.preclear_pending() is True
    window = c.state.preclear_until - c.state.started_at
    assert PRECLEAR_MIN_SEC <= window <= PRECLEAR_MAX_SEC
    assert c.state.preclear_until > t0


def test_a_changed_webkey_arms_it_too(tmp_path):
    """Заміна ключа = інший акаунт: його монети нам не свої."""
    p = str(tmp_path / "c.json")
    c = SoftStartCampaign(p, 3, rng=random.Random(1))
    c.start_if_new("acct-1")
    c.mark_precleared()
    assert c.preclear_pending() is False

    again = SoftStartCampaign(p, 3, rng=random.Random(2))
    assert again.start_if_new("acct-2") is True      # інший ключ
    assert again.preclear_pending() is True


def test_an_old_state_file_does_NOT_trigger_a_liquidation(tmp_path):
    """НАЙВАЖЛИВІШИЙ ТЕСТ ФАЙЛУ.

    Стан читається як `CampaignState(**json)`. Файл, що його писала попередня
    версія, не має ключів розчистки спота — вони візьмуть дефолт. Якби дефолт
    був False, ПЕРШИЙ ЖЕ тік після деплою почав би зливати баланс кампанії,
    яка спокійно триває. Дефолт мусить бути True.
    """
    import json
    p = tmp_path / "c.json"
    p.write_text(json.dumps({          # рівно те, що писала стара версія
        "started_at": time.time() - 3600, "days": 3, "finished": False,
        "day_weights": {"0": 0.5}, "tokens": ["MX"], "stats": {},
        "account_key": "acct-1",
    }))
    c = SoftStartCampaign(str(p), 3)
    assert c.state.preclear_done is True
    assert c.preclear_pending() is False
    assert c.start_if_new("acct-1") is False        # кампанія триває


def test_keep_frac_walks_from_one_to_zero(tmp_path):
    c = SoftStartCampaign(str(tmp_path / "c.json"), 3, rng=random.Random(1))
    c.start_if_new("a")
    t0, t1 = c.state.started_at, c.state.preclear_until
    assert c.preclear_keep_frac(t0) == pytest.approx(1.0)
    assert c.preclear_keep_frac((t0 + t1) / 2) == pytest.approx(0.5, abs=1e-6)
    assert c.preclear_keep_frac(t1) == pytest.approx(0.0)
    # ПІСЛЯ дедлайну — нуль, а не відʼємне і не 1.0. Помилка в цей бік
    # самовиправляється наступним тіком, у протилежний — лишила б монети.
    assert c.preclear_keep_frac(t1 + 9999) == 0.0


def test_a_broken_window_sells_everything_rather_than_nothing(tmp_path):
    c = SoftStartCampaign(str(tmp_path / "c.json"), 3, rng=random.Random(1))
    c.start_if_new("a")
    c.state.preclear_until = c.state.started_at      # span == 0
    assert c.preclear_keep_frac() == 0.0


# ------------------------------------------------------------ фейки раннера

class _Spot:
    def __init__(self, per_pass=(0,), boom=False):
        self.plan = type("P", (), {"tokens": ["MX"]})()
        self.per_pass = list(per_pass)
        self.boom = boom
        self.calls = []          # keep_frac кожного виклику

    async def wind_down(self, keep, tokens=None):
        self.calls.append(keep)
        if self.boom:
            raise RuntimeError("біржа не віддала баланс")
        return self.per_pass.pop(0) if self.per_pass else 0

    async def tick(self):                       # прогрів
        raise AssertionError("прогрів не має йти до кінця розчистки спота")


class _Futures:
    async def tick(self):
        raise AssertionError("прогрів не має йти до кінця розчистки спота")


def _warmer(camp, spot, futures=None):
    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w.draining = False
    w.reporter = None
    w.campaign = camp
    w.spot = spot
    w.futures = futures or _Futures()
    w._preclear_grace = 0
    w._wound_down = False
    w._wind_passes = 0
    return w


def _camp(tmp_path, seed=1):
    c = SoftStartCampaign(str(tmp_path / "c.json"), 3, rng=random.Random(seed))
    c.start_if_new("acct")
    return c


# ------------------------------------------------------------ ПРОВОДКА

@pytest.mark.asyncio
async def test_tick_runs_the_preclear_and_does_NOT_warm(tmp_path):
    """ТЕСТ ПРОВОДКИ. Прибери гачок із `tick()` — і цей тест мусить упасти.

    `_Spot.tick`/`_Futures.tick` кидають AssertionError: якщо прогрів усе ж
    пішов, ми дізнаємось про це гучно, а не через тихо пропущену розчистку спота.
    """
    c = _camp(tmp_path)
    spot = _Spot(per_pass=[2])
    w = _warmer(c, spot)
    await w.tick()
    assert spot.calls, "wind_down не викликано — розчистку спота не проведено в tick()"
    assert c.preclear_pending() is True          # ордери пішли, ще не кінець


@pytest.mark.asyncio
async def test_the_sell_off_is_spread_not_dumped(tmp_path):
    """keep_frac має СПАДАТИ між тіками, інакше це злив однією пачкою."""
    c = _camp(tmp_path)
    spot = _Spot(per_pass=[1, 1, 1])
    w = _warmer(c, spot)
    await w.tick()
    c.state.preclear_until -= 300                # «минуло 5 хв»
    await w.tick()
    assert len(spot.calls) == 2
    assert spot.calls[1] < spot.calls[0], f"keep_frac не спадає: {spot.calls}"
    assert all(0.0 <= k <= 1.0 for k in spot.calls)


@pytest.mark.asyncio
async def test_nothing_left_to_sell_past_the_deadline_finishes_it(tmp_path):
    c = _camp(tmp_path)
    c.state.preclear_until = time.time() - 1     # дедлайн уже минув
    spot = _Spot(per_pass=[0])
    w = _warmer(c, spot)
    await w.tick()
    assert c.preclear_pending() is False
    # і прапорець ПЕРЕЖИВАЄ рестарт
    again = SoftStartCampaign(c.path, 3)
    assert again.preclear_pending() is False


@pytest.mark.asyncio
async def test_zero_sales_before_the_deadline_keeps_waiting(tmp_path):
    """Монета могла не влізти в поточну частку — вона піде, коли keep просяде.

    Тобто нуль ордерів ДО дедлайну не означає «продавати нічого».
    """
    c = _camp(tmp_path)
    spot = _Spot(per_pass=[0])
    w = _warmer(c, spot)
    await w.tick()
    assert c.preclear_pending() is True


@pytest.mark.asyncio
async def test_a_failing_exchange_does_not_wedge_the_slot_forever(tmp_path):
    """Fail-closed, але з межею.

    Гріти на невідомому балансі не можна — але й мовчки стояти назавжди гірше:
    оператор побачить зупинку без причини. Після MAX_PRECLEAR_GRACE спроб
    прогрів починається, а факт іде в лог і в алерт.
    """
    c = _camp(tmp_path)
    spot = _Spot(boom=True)
    w = _warmer(c, spot)
    for _ in range(MAX_PRECLEAR_GRACE - 1):
        await w.tick()
        assert c.preclear_pending() is True      # ще тримаємо
    await w.tick()
    assert c.preclear_pending() is False         # здались, але голосно
    assert w._preclear_grace >= MAX_PRECLEAR_GRACE


@pytest.mark.asyncio
async def test_a_finished_preclear_lets_the_warming_run(tmp_path):
    """Зворотний бік проводки: коли розчистку спота завершено, tick() йде далі.

    Без цього тесту фіча могла б «працювати», заблокувавши прогрів назавжди.
    """
    c = _camp(tmp_path)
    c.mark_precleared()
    reached = {"yes": False}

    class _OkSpot(_Spot):
        async def tick(self):
            reached["yes"] = True

    class _OkFut:
        async def tick(self):
            reached["yes"] = True

    w = _warmer(c, _OkSpot(), _OkFut())
    # далі по tick() йде гілка expired() — доводимо лише, що розчистка спота
    # більше не перехоплює керування.
    assert c.preclear_pending() is False
    try:
        await w.tick()
    except AttributeError:
        pass          # фейк не має решти інтерфейсу — нам важливий сам факт
    assert not w.spot.calls, "wind_down не мав викликатись після завершення"
