# -*- coding: utf-8 -*-
"""Залишок у монетах після кампанії — ДІАПАЗОН [0.15; 0.25], а не рівно 20%.

НАВІЩО. Рахунок, який після КОЖНОЇ кампанії і на КОЖНОМУ акаунті лишається
з точно тією самою часткою в монетах, — це підпис. Решта прогріву давно
рандомізована між акаунтами (набір токенів, денні квоти, вага дня, розміри
ордерів, тривалість тримання), і саме ця частка лишалась єдиною константою.

ЩО ПІНИТЬСЯ:
  * число в діапазоні і РОЗІГРУЄТЬСЯ РАЗ, а не щоразу — інакше кожен прохід
    розпродажу (їх до MAX_WIND_DOWN_PASSES) цілив би в іншу частку і
    послідовність проходів перестала б сходитись;
  * воно ПЕРЕЖИВАЄ рестарт — тими самими міркуваннями, що й `day_weights`;
  * ПРОВОДКА: раннер бере число З КАМПАНІЇ, а не з літерала. Без цього тесту
    можна було б лишити в `tick()` стару константу, і рандомізація виявилась
    би написаною, але невживаною — дірка, яку в цьому проєкті ловили 12 разів;
  * старий файл стану (кампанія ТРИВАЄ) отримує своє число ліниво, а не
    лишається на 0.20 назавжди.
"""
import random

import pytest

from src.execution.soft_start_campaign import (WIND_DOWN_KEEP_MAX,
                                               WIND_DOWN_KEEP_MIN,
                                               SoftStartCampaign)
from src.execution.soft_start_runner import SlotWarmer


def _camp(tmp_path, seed=1, name="c.json"):
    c = SoftStartCampaign(str(tmp_path / name), 3, rng=random.Random(seed))
    c.start_if_new("acct")
    return c


def test_the_share_lands_inside_the_range(tmp_path):
    c = _camp(tmp_path)
    k = c.wind_down_keep()
    assert WIND_DOWN_KEEP_MIN <= k <= WIND_DOWN_KEEP_MAX
    assert (WIND_DOWN_KEEP_MIN, WIND_DOWN_KEEP_MAX) == (0.15, 0.25)


def test_it_is_rolled_once_not_per_call(tmp_path):
    """Кожен виклик мусить давати ТЕ САМЕ число.

    Розпродаж робить до MAX_WIND_DOWN_PASSES проходів; якби ціль плавала між
    проходами, послідовність не сходилась би до жодної частки.
    """
    c = _camp(tmp_path)
    assert len({c.wind_down_keep() for _ in range(20)}) == 1


def test_it_survives_a_restart(tmp_path):
    c = _camp(tmp_path)
    k = c.wind_down_keep()
    again = SoftStartCampaign(c.path, 3, rng=random.Random(999))
    assert again.wind_down_keep() == k, "рестарт перерозіграв частку"


def test_two_campaigns_do_not_share_one_number(tmp_path):
    """Інакше рандомізація нічого не дає: усі акаунти знову однакові."""
    a = _camp(tmp_path, seed=1, name="a.json").wind_down_keep()
    b = _camp(tmp_path, seed=7, name="b.json").wind_down_keep()
    assert a != b


def test_an_old_state_file_lazily_draws_instead_of_staying_at_20(tmp_path):
    """Кампанія, що ВЖЕ ТРИВАЄ, теж має отримати своє число.

    У файлі попередньої версії поля немає -> дефолт 0.0 -> ліниво розігруємо
    при першому розпродажі. Інакше кампанії, що йдуть просто зараз,
    достоялися б на старій константі.
    """
    import json
    import time
    p = tmp_path / "old.json"
    p.write_text(json.dumps({
        "started_at": time.time() - 3600, "days": 3, "finished": False,
        "day_weights": {}, "tokens": ["MX"], "stats": {}, "account_key": "a",
    }))
    c = SoftStartCampaign(str(p), 3, rng=random.Random(3))
    assert c.state.wind_down_keep == 0.0          # ще не розіграно
    k = c.wind_down_keep()
    assert WIND_DOWN_KEEP_MIN <= k <= WIND_DOWN_KEEP_MAX
    # і одразу лягло на диск
    assert SoftStartCampaign(str(p), 3).wind_down_keep() == k


# ------------------------------------------------------------------ ПРОВОДКА

class _Spot:
    def __init__(self):
        self.plan = type("P", (), {"tokens": ["MX"]})()
        self.keeps = []

    async def wind_down(self, keep, tokens=None):
        self.keeps.append(keep)
        return 0                      # продавати нічого -> гілка завершується


@pytest.mark.asyncio
async def test_the_runner_passes_the_CAMPAIGN_number_not_a_literal(tmp_path):
    """ТЕСТ ПРОВОДКИ. Поверни в `tick()` літерал 0.20 — і цей тест упаде."""
    c = _camp(tmp_path, seed=11)
    c.state.preclear_done = True          # розчистку спота вже зроблено
    c.state.started_at -= 4 * 86400       # кампанія протухла
    expected = c.wind_down_keep()
    assert expected != 0.20, "сід дав рівно 0.20 — тест нічого не довів би"

    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w.draining = False
    w.reporter = None
    w.campaign = c
    w.spot = _Spot()
    w.futures = object()
    w._preclear_grace = 0
    w._wound_down = False
    w._wind_passes = 0
    w._spot_viable = True
    w._held_market = None
    w._held_market_at = 0.0
    try:
        await w.tick()
    except Exception:
        pass          # далі по гілці фейк не має решти інтерфейсу — байдуже
    assert w.spot.keeps, "wind_down не викликано"
    assert w.spot.keeps[0] == expected, (
        f"раннер передав {w.spot.keeps[0]}, а кампанія розіграла {expected}")
