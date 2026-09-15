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


def test_a_REPASTED_webkey_does_NOT_arm_it(tmp_path):
    """Рішення оператора 2026-09-10: переклеювання ключа кампанію ПРОДОВЖУЄ,
    тож розчистка спота НЕ озброюється.

    Раніше тут пінилось протилежне («заміна ключа = інший акаунт»), і ціна
    помилки була в монетах: у ключа сплив термін, оператор перелогінився на
    ТОМУ САМОМУ акаунті — і перший же тік почав би зливати спот живої
    кампанії (на primary слот 1 це MX 6.25 + SUI 56.53 ≈ 57 USDT). Відрізнити
    перелогін від іншого акаунта за рядком ключа неможливо, тож намір задає
    ДІЯ оператора: видалення забуває кампанію, вставка — ні.
    """
    p = str(tmp_path / "c.json")
    c = SoftStartCampaign(p, 3, rng=random.Random(1))
    c.start_if_new("acct-1")
    c.mark_precleared()
    assert c.preclear_pending() is False

    again = SoftStartCampaign(p, 3, rng=random.Random(2))
    assert again.start_if_new("acct-2") is False     # переклеїли
    assert again.preclear_pending() is False, (
        "переклеювання ключа озброїло розчистку — це злило б спот живої "
        "кампанії")


def test_a_DELETED_key_arms_it_on_the_next_campaign(tmp_path):
    """Друга половина: видалення ключа забуває кампанію, і наступна вже
    озброює розчистку — монети попереднього акаунта нам не свої."""
    import os
    from src.execution.soft_start_campaign import (campaign_state_path,
                                                   forget_campaign)
    name = os.path.basename(campaign_state_path(1, str(tmp_path)))
    c = SoftStartCampaign(str(tmp_path / name), 3, rng=random.Random(1))
    c.start_if_new("acct-1")
    c.mark_precleared()
    assert c.preclear_pending() is False

    assert forget_campaign(1, str(tmp_path)) is True

    again = SoftStartCampaign(str(tmp_path / name), 3, rng=random.Random(2))
    assert again.start_if_new("acct-2") is True
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
    def __init__(self, per_pass=(0,), boom=False, held=("MX",), reports=None):
        self.plan = type("P", (), {"tokens": ["MX"]})()
        self.per_pass = list(per_pass)
        self.boom = boom
        self.calls = []          # keep_frac кожного виклику
        self.held = list(held)
        self.pools = []          # tokens кожного виклику
        self.reports = list(reports or [])
        self.wind_down_report = {"rejected": [], "unreadable": [], "unpriced": [], "dust": []}

    async def held_tokens(self):
        if self.boom:
            raise RuntimeError("біржа не віддала список активів")
        return list(self.held)

    async def wind_down(self, keep, tokens=None):
        self.calls.append(keep)
        self.pools.append(tokens)
        if self.reports:
            self.wind_down_report = self.reports.pop(0)
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
    w._preclear_stuck = 0
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


# ------------------------------------------------ 2026-09-14: ВСЕ в USDT, не мовчки

class _Rep:
    def __init__(self):
        self.msgs = []

    async def skipped(self, why, **st):
        self.msgs.append(why)


def _clean():
    return {"rejected": [], "unreadable": [], "unpriced": [], "dust": []}


@pytest.mark.asyncio
async def test_preclear_sells_every_held_coin_not_only_the_candidates(tmp_path):
    """Оператор: «щоб продавало все». BTC у списку кандидатів немає — раніше його не бачили взагалі."""
    c = _camp(tmp_path)
    spot = _Spot(per_pass=[1], held=["BTC", "SUI"])
    await _warmer(c, spot).tick()
    assert spot.pools[-1] == ["BTC", "SUI"], f"розчистка продає не весь гаманець: {spot.pools}"


@pytest.mark.asyncio
async def test_rejected_coin_past_deadline_is_retried_then_alerted_not_silently_done(tmp_path):
    c = _camp(tmp_path)
    c.state.preclear_until = time.time() - 1
    stuck = {**_clean(), "rejected": ["LINKUSDT"]}
    spot = _Spot(per_pass=[0] * 10, reports=[stuck] * 10)
    w = _warmer(c, spot)
    w.reporter = _Rep()
    w._status = lambda: {}
    from src.execution.soft_start_runner import MAX_PRECLEAR_STUCK
    for _ in range(MAX_PRECLEAR_STUCK):
        await w.tick()
        assert c.preclear_pending() is True, "розчистку оголошено завершеною з непроданою монетою"
    await w.tick()
    assert c.preclear_pending() is False
    assert w.reporter.msgs and "не продано: LINKUSDT" in w.reporter.msgs[-1], w.reporter.msgs


@pytest.mark.asyncio
async def test_coin_sold_on_retry_finishes_cleanly_without_alert(tmp_path):
    c = _camp(tmp_path)
    c.state.preclear_until = time.time() - 1
    spot = _Spot(per_pass=[0, 1, 0], reports=[{**_clean(), "rejected": ["LINKUSDT"]}, _clean(), _clean()])
    w = _warmer(c, spot)
    w.reporter = _Rep()
    await w.tick()                         # відмова -> чекаємо
    assert c.preclear_pending() is True
    await w.tick()                         # продалось
    await w.tick()                         # нічого не лишилось
    assert c.preclear_pending() is False and w.reporter.msgs == []


@pytest.mark.asyncio
async def test_dust_below_exchange_minimum_does_not_block_but_is_reported(tmp_path):
    c = _camp(tmp_path)
    c.state.preclear_until = time.time() - 1
    spot = _Spot(per_pass=[0], reports=[{**_clean(), "dust": [("TRXUSDT", 0.5)]}])
    w = _warmer(c, spot)
    w.reporter = _Rep()
    w._status = lambda: {}
    await w.tick()
    assert c.preclear_pending() is False, "пил, який неможливо продати, не має блокувати прогрів"
    assert w.reporter.msgs and "пил" in w.reporter.msgs[-1] and "TRXUSDT" in w.reporter.msgs[-1]

