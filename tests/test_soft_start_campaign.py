"""Tests for the 3-day warming campaign and its randomisation.

Warming is a finite job. These pin that it:
  1. runs for exactly the configured number of days and then expires,
  2. switches ITSELF off in the DB when it finishes,
  3. randomises every observable dimension — how much happens on a day, which
     actions happen, and in what ORDER,
  4. does not reroll a day's plan on restart (that would double the activity).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path

import pytest

from src.execution import soft_start_runner as ssr
from src.execution.soft_start_campaign import (
    DAY_SEC,
    DEFAULT_CAMPAIGN_DAYS,
    SoftStartCampaign,
    shuffled_actions,
)


# ---- length ---------------------------------------------------------------

def test_default_is_three_days():
    assert DEFAULT_CAMPAIGN_DAYS == 3


def test_fresh_campaign_is_not_expired(tmp_path):
    c = SoftStartCampaign(str(tmp_path / "c.json"))
    assert c.start_if_new() is True
    assert c.expired() is False
    assert c.state.remaining_days() == pytest.approx(3.0, abs=0.01)


def test_start_is_idempotent(tmp_path):
    c = SoftStartCampaign(str(tmp_path / "c.json"))
    assert c.start_if_new() is True
    t0 = c.state.started_at
    assert c.start_if_new() is False, "must not restart a running campaign"
    assert c.state.started_at == t0


def test_expires_after_the_configured_days(tmp_path):
    c = SoftStartCampaign(str(tmp_path / "c.json"), days=3)
    c.start_if_new()
    c.state.started_at = time.time() - 2.9 * DAY_SEC
    assert c.expired() is False
    c.state.started_at = time.time() - 3.01 * DAY_SEC
    assert c.expired() is True


def test_day_index_advances(tmp_path):
    c = SoftStartCampaign(str(tmp_path / "c.json"))
    c.start_if_new()
    for day in (0, 1, 2):
        c.state.started_at = time.time() - (day + 0.5) * DAY_SEC
        assert c.state.day_index() == day


def test_campaign_survives_restart(tmp_path):
    p = str(tmp_path / "c.json")
    c1 = SoftStartCampaign(p)
    c1.start_if_new()
    started = c1.state.started_at
    c2 = SoftStartCampaign(p)
    assert c2.state.started_at == started, "restart must not restart the campaign"


def test_finish_is_sticky(tmp_path):
    p = str(tmp_path / "c.json")
    c = SoftStartCampaign(p)
    c.start_if_new()
    c.finish()
    assert c.expired() is True
    assert SoftStartCampaign(p).expired() is True


def test_reset_starts_over(tmp_path):
    c = SoftStartCampaign(str(tmp_path / "c.json"))
    c.start_if_new()
    c.finish()
    c.reset()
    assert c.state.started_at == 0.0 and not c.state.finished


def test_corrupt_state_does_NOT_start_a_fresh_campaign(tmp_path):
    """Раніше називалось `..._starts_fresh` і пінило саме те, що виявилось
    дефектом: битий файл давав `started_at=0` -> `start_if_new()` True ->
    ще 3 доби ЖИВОЇ торгівлі через збій файлової системи. Тепер fail-closed."""
    p = tmp_path / "c.json"
    p.write_text("{broken")
    c = SoftStartCampaign(str(p))
    assert c.state.started_at == 0.0
    assert c._unreadable is True
    assert c.start_if_new("acct") is False
    assert c.expired() is True


# ---- randomisation --------------------------------------------------------

def test_day_weight_is_stable_within_a_day(tmp_path):
    """Rolled once, not per tick — a day has a shape."""
    c = SoftStartCampaign(str(tmp_path / "c.json"), rng=random.Random(1))
    c.start_if_new()
    first = c.day_weight()
    assert all(c.day_weight() == first for _ in range(20))


def test_day_weight_persists_across_restart(tmp_path):
    """A restart must not reroll into a busier day and double the activity."""
    p = str(tmp_path / "c.json")
    c1 = SoftStartCampaign(p, rng=random.Random(1))
    c1.start_if_new()
    w = c1.day_weight()
    c2 = SoftStartCampaign(p, rng=random.Random(999))
    assert c2.day_weight() == w


def test_day_weights_differ_across_days(tmp_path):
    c = SoftStartCampaign(str(tmp_path / "c.json"), rng=random.Random(5))
    c.start_if_new()
    seen = []
    for day in range(3):
        c.state.started_at = time.time() - (day + 0.5) * DAY_SEC
        seen.append(c.day_weight())
    assert len(set(seen)) > 1, "every day the same weight is not randomisation"
    assert all(0.15 <= w <= 1.0 for w in seen)


def test_scale_target_makes_quiet_and_busy_days(tmp_path):
    c = SoftStartCampaign(str(tmp_path / "c.json"), rng=random.Random(3))
    c.start_if_new()
    targets = []
    for day in range(30):
        c.state.started_at = time.time() - (day + 0.5) * DAY_SEC
        targets.append(c.scale_target(10))
    assert min(targets) < max(targets), "targets must vary day to day"
    assert all(0 <= t <= 10 for t in targets)


def test_shuffled_actions_only_returns_available_ones():
    rng = random.Random(0)
    assert shuffled_actions(rng, buy=True, sell=False) == ["buy"]
    assert shuffled_actions(rng, buy=False, sell=False) == []
    assert sorted(shuffled_actions(rng, buy=True, sell=True)) == ["buy", "sell"]


def test_shuffled_actions_actually_varies_the_order():
    """A fixed buy-then-sell rhythm is a pattern; this is what breaks it."""
    seen = set()
    for seed in range(40):
        seen.add(tuple(shuffled_actions(random.Random(seed), buy=True, sell=True)))
    assert seen == {("buy", "sell"), ("sell", "buy")}


# ---- the loop switches a finished campaign off ---------------------------

class _Slot:
    def __init__(self, slot_id):
        self.slot_id = slot_id
        self.webkey = "WEB" + "0" * 64
        self.soft_start_enabled = True


class _Store:
    def __init__(self, slots):
        self.slots = slots
        self.switched_off = []

    async def list_all(self):
        return self.slots

    async def set_soft_start(self, slot_id, enabled):
        if not enabled:
            self.switched_off.append(slot_id)
        for s in self.slots:
            if s.slot_id == slot_id:
                s.soft_start_enabled = enabled


class _Pool:
    async def get(self, slot_id):
        return object()


@pytest.mark.asyncio
async def test_finished_campaign_switches_the_slot_off(monkeypatch):
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)

    made = []

    class DoneWarmer:
        """Mirrors SlotWarmer's interface — including `futures` and `reporter`,
        which the loop reads when posting the closing report."""

        # сторож дрейфу: цикл читає це у SlotWarmer
        held_is_measured = False

        def __init__(self, slot_id, webkey, client, universe, *, dry_run, **kw):
            made.append(self)
            self.slot_id = slot_id
            self.stopped = False
            self.draining = False
            self._stop_attempts = 0
            self._final_sent = False
            # state.stats і elapsed_hours() — фінальний звіт бере підсумки з
            # КАМПАНІЇ, а не з репортера (той обнуляється на кожному рестарті).
            self.campaign = type("C", (), {
                "expired": lambda self: True,
                "elapsed_hours": lambda self: 72.0,
                "state": type("S", (), {"stats": {}, "tokens": []})(),
            })()
            # pnl і entries — фінальний звіт тепер показує рух ринку і те,
            # що лишилось у монетах. Фейк мусить це вміти, інакше звіт падає
            # у своєму ж try/except і тест бачить лише «звіту не було».
            self.budget = type("B", (), {"exhausted": lambda self: False,
                                         "spent": 0.0, "pnl": 0.0, "futures_pnl": 0.0, "spot_pnl": 0.0,
                                         "state": type("S", (), {
                                             "max_usdt": 5.0,
                                             "entries": []})()})()
            self._held_spot_value = lambda: 0.0
            self.futures = type("F", (), {
                "state": type("St", (), {"position": None, "pending": None,
                                         "needs_exchange_check": False})(),
                "has_exposure": lambda self: False,
            })()
            self.reported = []
            reported = self.reported

            class _Rep:
                async def final_report(self, reason, **kw):
                    reported.append((reason, kw))

            self.reporter = _Rep()

        async def start(self):
            pass

        async def tick(self):
            pass

        async def stop(self):
            self.stopped = True
            self.draining = True
            return True                      # clean: nothing left open

        def stuck(self):
            return False

        def _status(self):
            return {"day": 3, "days": 3, "spent": 0.0, "ceiling": 5.0,
                    "position": None}

        def finished(self):
            return True

    monkeypatch.setattr(ssr, "SlotWarmer", DoneWarmer)
    store = _Store([_Slot(1)])

    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] >= 1:
            raise asyncio.CancelledError

    orig = ssr.asyncio.sleep
    ssr.asyncio.sleep = fake_sleep
    try:
        with pytest.raises(asyncio.CancelledError):
            await ssr.soft_start_loop(store, _Pool(), lambda: ["HYPEUSDT"])
    finally:
        ssr.asyncio.sleep = orig

    assert store.switched_off == [1], "a finished campaign must switch itself off"
    assert store.slots[0].soft_start_enabled is False
    # and the operator gets a closing summary, not just silence
    assert made, "the warmer was never constructed"
    assert made[0].reported, "a finished campaign must post a closing report"
    reason, kw = made[0].reported[0]
    assert "campaign" in reason
    assert kw["position_left"] is False


# ---- вага дня: одна формула, і лог не бреше (2026-08-26) -------------------

def test_scale_target_is_actually_used_by_the_runner():
    """`scale_target` існував, але його не викликав НІХТО — ту саму формулу
    переписали вбудовано в раннері. Дубль нічого не ламав, але читаючи метод
    можна було зробити хибний висновок про поведінку.

    Тест ВИКОНАВЧИЙ: підміняємо scale_target і дивимось, чи змінився результат.
    Грепом по джерелу такий дубль не ловиться.
    """
    from src.execution.soft_start_runner import SlotWarmer

    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w._weighted_logged = None

    calls = []

    class _Camp:
        def scale_target(self, base):
            calls.append(base)
            return 2

        def day_weight(self):
            return 0.5

    class _Plan:
        buys_target = 9
        sells_target = 9

    class _State:
        orders_target = 3

    # cfg із максимумами: раннер бере бази ваги з КОНФІГУ, а не з прибитих
    # чисел — інакше після підняття квоти стеля лишилась би старою і квота
    # піднялась би тільки на папері.
    _scfg = type("SC", (), {"buys_per_day_max": 25, "sells_per_day_max": 20})()
    _fcfg = type("FC", (), {"orders_per_day_max": 6})()
    w.campaign = _Camp()
    w.spot = type("S", (), {"plan": _Plan(), "cfg": _scfg})()
    w.futures = type("F", (), {"state": _State(), "cfg": _fcfg})()

    SlotWarmer._apply_day_weight(w)

    assert calls == [25, 20, 6], (
        f"раннер не кличе campaign.scale_target — формула знову дублюється: "
        f"{calls}")
    assert w.spot.plan.buys_target == 2
    assert w.futures.state.orders_target == 2


def test_weight_never_raises_a_target_only_lowers_it():
    """`min(поточне, стеля)` — вага РІЗАЄ активність, а не роздуває її.
    Інакше тихий день міг би стати бурхливішим за розіграний."""
    from src.execution.soft_start_runner import SlotWarmer

    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w._weighted_logged = None
    w.campaign = type("C", (), {"scale_target": lambda self, b: 99,
                                "day_weight": lambda self: 1.0})()
    _scfg = type("SC", (), {"buys_per_day_max": 25, "sells_per_day_max": 20})()
    _fcfg = type("FC", (), {"orders_per_day_max": 6})()
    w.spot = type("S", (), {"plan": type("P", (), {"buys_target": 3,
                                                   "sells_target": 1})(),
                            "cfg": _scfg})()
    w.futures = type("F", (), {"state": type("St", (), {"orders_target": 1})(),
                               "cfg": _fcfg})()

    SlotWarmer._apply_day_weight(w)
    assert (w.spot.plan.buys_target, w.spot.plan.sells_target,
            w.futures.state.orders_target) == (3, 1, 1)


def test_the_rolled_plan_log_does_not_claim_to_be_final():
    """Рядок друкувався як «soft-start plan …» ще ДО ваги, тож число в лозі
    (buys=9) не збігалось із тим, що виконується (8). Тепер він явно каже, що
    це розіграш, а не остаточний план."""
    import inspect
    from src.execution.spot_soft_start import SpotSoftStart
    src = inspect.getsource(SpotSoftStart.__init__)
    assert "РОЗІГРАНО" in src and "вага дня застосується далі" in src
    assert '"soft-start plan %s' not in src


# ---- вибір ПАР: спот і фʼючерси (2026-08-26) -------------------------------

def test_campaign_token_pool_is_rolled_once_and_remembered(tmp_path):
    """Рестарт не має міняти те, чим акаунт торгує — це виглядало б як інша
    людина за тим самим ключем."""
    import random
    from src.execution.soft_start_campaign import SoftStartCampaign
    path = str(tmp_path / "c.json")
    c1 = SoftStartCampaign(path, 3, rng=random.Random(1))
    first = c1.token_pool(["MX", "DOGE", "XRP", "SOL", "TRX", "ADA"])
    assert 1 <= len(first) <= 5

    c2 = SoftStartCampaign(path, 3, rng=random.Random(999))
    assert c2.token_pool(["MX", "DOGE", "XRP", "SOL", "TRX", "ADA"]) == first


def test_token_pool_is_not_always_the_same_across_campaigns(tmp_path):
    """Різні кампанії (різні слоти/машини) мусять отримувати різні набори —
    інакше «випадковий вибір» вироджується в один список для всіх."""
    import random
    from src.execution.soft_start_campaign import SoftStartCampaign
    cands = ["MX", "DOGE", "XRP", "SOL", "TRX", "ADA", "LTC", "SHIB", "PEPE"]
    pools = set()
    for seed in range(12):
        c = SoftStartCampaign(str(tmp_path / f"c{seed}.json"), 3,
                              rng=random.Random(seed))
        pools.add(tuple(c.token_pool(cands)))
    assert len(pools) >= 6, f"набори майже не різняться: {pools}"


def test_a_delisted_token_drops_out_of_a_saved_pool(tmp_path):
    import random
    from src.execution.soft_start_campaign import SoftStartCampaign
    path = str(tmp_path / "c.json")
    c = SoftStartCampaign(path, 3, rng=random.Random(1))
    c.state.tokens = ["MX", "GONE", "DOGE"]
    kept = c.token_pool(["MX", "DOGE", "XRP"])
    assert "GONE" not in kept and set(kept) <= {"MX", "DOGE"}


@pytest.mark.asyncio
async def test_futures_pair_is_random_among_equally_priced_ones():
    """ДЕФЕКТ, ЯКИЙ ЦЕ ЛІКУЄ (мій власний): було `paid.sort(); paid[0]`.
    Ставки рівні (на акаунті без промо всі пари 0.0004), тож нічия ламалась
    за НАЗВОЮ — і завжди вигравала алфавітно перша (`1000PEPEUSDT`).
    Випадковість вибору пари зникала повністю, і помітно це лише з логів.
    """
    import random
    from src.execution.futures_soft_start import (FuturesSoftStart,
                                                  FuturesSoftStartConfig)

    class _Fee:
        def __init__(self, t): self.taker = t; self.maker = 0.0
        @property
        def zero_both(self): return self.taker == 0.0

    class _Gate:
        async def fee(self, sym): return _Fee(0.0004)

    syms = ["1000PEPEUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ZECUSDT"]
    picked = set()
    for seed in range(30):
        ss = FuturesSoftStart.__new__(FuturesSoftStart)
        ss.universe = list(syms)
        ss.fee_gate = _Gate()
        ss.cfg = FuturesSoftStartConfig(state_path="/tmp/x.json")
        ss.rng = random.Random(seed)
        sym, fee = await ss.pick_pair()
        assert fee == 0.0004
        picked.add(sym)
    assert len(picked) >= 4, f"пара майже не змінюється: {picked}"


@pytest.mark.asyncio
async def test_a_genuinely_cheaper_pair_still_wins():
    """Випадковість — тільки СЕРЕД НАЙДЕШЕВШИХ, а не замість вибору."""
    import random
    from src.execution.futures_soft_start import (FuturesSoftStart,
                                                  FuturesSoftStartConfig)

    fees = {"AUSDT": 0.0004, "BUSDT": 0.0001, "CUSDT": 0.0004}

    class _Fee:
        def __init__(self, t): self.taker = t; self.maker = 0.0
        @property
        def zero_both(self): return self.taker == 0.0

    class _Gate:
        async def fee(self, sym): return _Fee(fees[sym])

    for seed in range(15):
        ss = FuturesSoftStart.__new__(FuturesSoftStart)
        ss.universe = list(fees)
        ss.fee_gate = _Gate()
        ss.cfg = FuturesSoftStartConfig(state_path="/tmp/x.json")
        ss.rng = random.Random(seed)
        sym, fee = await ss.pick_pair()
        assert sym == "BUSDT" and fee == 0.0001


# ---- темп спота: день не має вигорати за 20 хвилин (2026-08-26) ------------

def _pace_engine(buys_done=0, sells_done=0):
    import random
    from src.execution.spot_soft_start import (DayPlan, SoftStartConfig,
                                               SpotSoftStart)
    e = SpotSoftStart.__new__(SpotSoftStart)
    e.cfg = SoftStartConfig()
    e.rng = random.Random(1)
    e.plan = DayPlan(date="x", tokens=["MX"], buys_target=8, sells_target=2)
    e.plan.buys_done = buys_done
    e.plan.sells_done = sells_done
    return e


def test_pace_is_low_early_in_the_day_and_rises_near_the_end():
    """Прибите 0.5 давало рівно те, на що скаржився оператор: план дня
    (8 покупок + 2 продажі) виконувався за 22 ХВИЛИНИ, далі 23 години тиші.
    Для прогріву це найгірший профіль — сплеск помітніший за рівну активність.
    """
    import datetime
    e = _pace_engine()
    early = e._tick_probability(datetime.datetime(2026, 8, 26, 7, 0))
    late = e._tick_probability(datetime.datetime(2026, 8, 26, 22, 30))
    assert early < 0.05, f"на початку дня темп завеликий: {early}"
    assert late > early * 3, f"під кінець темп не піднявся: {early} -> {late}"


def test_pace_is_zero_once_the_plan_is_done():
    import datetime
    e = _pace_engine(buys_done=8, sells_done=2)
    assert e._tick_probability(datetime.datetime(2026, 8, 26, 12, 0)) == 0.0


def test_pace_never_exceeds_the_old_maximum():
    """Стеля 0.5 обмежує сплеск, якщо часу лишилось мало."""
    import datetime
    e = _pace_engine()
    for hh, mm in ((22, 59), (23, 0), (6, 0)):
        p = e._tick_probability(datetime.datetime(2026, 8, 26, hh, mm))
        assert 0.0 <= p <= 0.5, (hh, mm, p)


def test_a_whole_simulated_day_is_spread_not_bursty():
    """НАЙВАЖЛИВІШИЙ ТУТ: міряємо РОЗМАХ дня, а не окрему ймовірність.

    Виміряно на симуляції повного вікна: старий темп давав усі 10 дій за 16
    хвилин, новий — з 07:32 до 22:17.
    """
    import datetime
    e = _pace_engine()
    t = datetime.datetime(2026, 8, 26, 6, 0)
    times = []
    for _ in range(17 * 60):
        p = e._tick_probability(t)
        for kind in ("buy", "sell"):
            done = e.plan.buys_done if kind == "buy" else e.plan.sells_done
            targ = e.plan.buys_target if kind == "buy" else e.plan.sells_target
            if done >= targ:
                continue
            if e.rng.random() < p:
                if kind == "buy":
                    e.plan.buys_done += 1
                else:
                    e.plan.sells_done += 1
                times.append(t)
        t += datetime.timedelta(minutes=1)

    assert len(times) >= 8, f"план майже не виконався: {len(times)}"
    span_min = (times[-1] - times[0]).total_seconds() / 60
    assert span_min > 240, (
        f"день вигорів за {span_min:.0f} хв — сплеск повернувся")


@pytest.mark.asyncio
async def test_tick_actually_uses_the_pacing_not_a_constant(monkeypatch):
    """ПРОВОДКА, а не лише формула.

    Тести вище перевіряють `_tick_probability` напряму і мутанта в `tick()`
    НЕ ловлять: там могло лишитись прибите `pace = 0.5`, формула була б
    правильна й невживана, а день і далі вигорав би за 20 хвилин. Це вже
    третій випадок цієї діри за сесію (гейт бюджету, розмір у звіті).
    """
    import datetime
    e = _pace_engine()
    calls = []

    def _spy(now=None):
        calls.append(now)
        return 0.0                     # 0 -> жодної дії, тік має бути тихим

    monkeypatch.setattr(e, "_tick_probability", _spy)
    monkeypatch.setattr(e, "active_now", lambda now=None: True)
    monkeypatch.setattr(e, "_roll_day", lambda: None)

    async def _fail(*a, **k):
        raise AssertionError("дія пішла попри нульовий темп")

    monkeypatch.setattr(e, "maybe_buy", _fail)
    monkeypatch.setattr(e, "maybe_sell", _fail)

    await e.tick()
    assert calls, "tick() не питає _tick_probability — темп прибитий у коді"


# ---- розпродаж на завершенні кампанії: ПРОВОДКА (2026-08-29) ---------------

@pytest.mark.asyncio
async def test_expired_campaign_winds_down_before_switching_off():
    """ПРОВОДКА, а не формула. `wind_down()` може бути ідеальним і невживаним —
    це вже шостий випадок такої діри за сесію.

    Порядок критичний: розпродаж мусить статись ДО `finish()`, бо той вимикає
    кнопку в БД і викидає warmer — після цього продавати вже нікому.
    """
    from src.execution.soft_start_runner import SlotWarmer

    order = []

    class _Camp:
        # ПЕРЕДПРОДАЖ (2026-09-05): tick() питає це ПЕРШИМ. Фейк мусить
        # вміти, інакше він розійдеться з реальним SoftStartCampaign і
        # тест мовчки перевірятиме не той шлях.
        def preclear_pending(self): return False
        # Частка залишку тепер розігрується кампанією (2026-09-05).
        def wind_down_keep(self): return 0.20
        # state.tokens — раннер бере набір кампанії, щоб розпродати й монети
        # зі старого юніверсу. Фейк мусить це вміти, інакше виняток тихо
        # проковтнеться і тест побачить лише «finish».
        state = type("S", (), {"tokens": ["LINK"]})()
        def expired(self): return True
        def finish(self): order.append("finish")

    class _Spot:
        plan = type("P", (), {"tokens": ["LINK"]})()
        async def wind_down(self, keep, tokens=None):
            order.append(("wind_down", keep))
            return 0                      # нічого продавати — завершено одразу

    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w.draining = False
    w._wound_down = False
    w._wind_passes = 0   # мусить дзеркалити __init__
    w._spot_viable = True
    w.campaign = _Camp()
    w.spot = _Spot()
    w.futures = object()

    await SlotWarmer.tick(w)
    assert order and order[0][0] == "wind_down", (
        f"розпродаж не викликано або не першим: {order}")
    assert order[-1] == "finish", "кнопку вимкнено до завершення розпродажу"


@pytest.mark.asyncio
async def test_wind_down_gets_another_tick_while_it_still_sells():
    """Поки розпродаж відправляє ордери, слот НЕ вимикається: інакше половина
    монет лишилась би непроданою."""
    from src.execution.soft_start_runner import SlotWarmer

    finished = []

    class _Camp:
        # ПЕРЕДПРОДАЖ (2026-09-05): tick() питає це ПЕРШИМ. Фейк мусить
        # вміти, інакше він розійдеться з реальним SoftStartCampaign і
        # тест мовчки перевірятиме не той шлях.
        def preclear_pending(self): return False
        # Частка залишку тепер розігрується кампанією (2026-09-05).
        def wind_down_keep(self): return 0.20
        state = type("S", (), {"tokens": ["LINK"]})()
        def expired(self): return True
        def finish(self): finished.append(1)

    class _Spot:
        plan = type("P", (), {"tokens": ["LINK"]})()
        async def wind_down(self, keep, tokens=None):
            return 2                      # ще продає

    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w.draining = False
    w._wound_down = False
    w._wind_passes = 0   # мусить дзеркалити __init__
    w._spot_viable = True
    w.campaign = _Camp()
    w.spot = _Spot()
    w.futures = object()

    await SlotWarmer.tick(w)
    assert not finished, "слот вимкнувся посеред розпродажу"


@pytest.mark.asyncio
async def test_a_failing_wind_down_does_not_wedge_the_slot_forever():
    """Розпродаж упав — слот усе одно мусить завершитись, інакше кампанія
    висітиме вічно, а монети однаково лишаться."""
    from src.execution.soft_start_runner import SlotWarmer

    finished = []

    class _Camp:
        # ПЕРЕДПРОДАЖ (2026-09-05): tick() питає це ПЕРШИМ. Фейк мусить
        # вміти, інакше він розійдеться з реальним SoftStartCampaign і
        # тест мовчки перевірятиме не той шлях.
        def preclear_pending(self): return False
        # Частка залишку тепер розігрується кампанією (2026-09-05).
        def wind_down_keep(self): return 0.20
        state = type("S", (), {"tokens": ["LINK"]})()
        def expired(self): return True
        def finish(self): finished.append(1)

    class _Spot:
        plan = type("P", (), {"tokens": ["LINK"]})()
        async def wind_down(self, keep, tokens=None):
            raise RuntimeError("біржа мовчить")

    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w.draining = False
    w._wound_down = False
    w._wind_passes = 0   # мусить дзеркалити __init__
    w._spot_viable = True
    w.campaign = _Camp()
    w.spot = _Spot()
    w.futures = object()

    await SlotWarmer.tick(w)
    assert finished, "слот завис після падіння розпродажу"


@pytest.mark.asyncio
async def test_runner_passes_the_full_token_pool_to_wind_down():
    """ПРОВОДКА. `wind_down` уміє ширший список, але якщо раннер і далі шле
    лише денний план, монети зі старого юніверсу (MX на 12.10 USDT) знову
    лишаться замкненими. Сьомий за сесію тест саме на виклик."""
    from src.execution.soft_start_runner import SlotWarmer, SPOT_CANDIDATES

    got = {}

    class _Camp:
        # ПЕРЕДПРОДАЖ (2026-09-05): tick() питає це ПЕРШИМ. Фейк мусить
        # вміти, інакше він розійдеться з реальним SoftStartCampaign і
        # тест мовчки перевірятиме не той шлях.
        def preclear_pending(self): return False
        # Частка залишку тепер розігрується кампанією (2026-09-05).
        def wind_down_keep(self): return 0.20
        state = type("S", (), {"tokens": ["LINK", "PENGU"]})()
        def expired(self): return True
        def finish(self): pass

    class _Spot:
        plan = type("P", (), {"tokens": ["LINK"]})()
        async def wind_down(self, keep, tokens=None):
            got["tokens"] = tokens
            return 0

    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w.draining = False
    w._wound_down = False
    w._wind_passes = 0   # мусить дзеркалити __init__
    w._spot_viable = True
    w.campaign = _Camp()
    w.spot = _Spot()
    w.futures = object()

    await SlotWarmer.tick(w)
    toks = got.get("tokens") or []
    assert "MX" in toks, "кандидати не передані — MX знову поза розпродажем"
    assert "PENGU" in toks, "набір кампанії не переданий"
    assert len(set(toks)) == len(toks), "дублі в списку"
    assert set(SPOT_CANDIDATES) <= set(toks)


@pytest.mark.asyncio
async def test_engines_count_actions_without_a_reporter():
    """ЛІЧИЛЬНИКИ ПЕРЕЇХАЛИ В РУШІЇ, і ось чому.

    Раніше вони рахувались диференціюванням знімків у `_report_diff`, а той
    починається з `if self.reporter is None: return` — тобто **без Telegram
    не рахувалось нічого**. Плюс диф порівнює знімки між тіками і губить
    дію, що почалась і скінчилась між ними.

    Тепер рахує сам рушій, рівно один раз на успішний ордер.
    """
    import random
    from src.execution.spot_soft_start import (DayPlan, SoftStartConfig,
                                               SpotSoftStart)
    seen = []
    e = SpotSoftStart.__new__(SpotSoftStart)
    e.on_action = lambda kind, n=1: seen.append((kind, n))
    e.cfg = SoftStartConfig()
    e.rng = random.Random(1)
    e._count("spot_buys")
    e._count("spot_sells", 2)
    assert seen == [("spot_buys", 1), ("spot_sells", 2)]


def test_counting_never_breaks_trading():
    """Облік не має ламати торгівлю: збій лічильника ковтається."""
    import random
    from src.execution.spot_soft_start import SoftStartConfig, SpotSoftStart
    e = SpotSoftStart.__new__(SpotSoftStart)
    e.cfg = SoftStartConfig()
    e.rng = random.Random(1)
    e.on_action = lambda kind, n=1: (_ for _ in ()).throw(OSError("диск"))
    e._count("spot_buys")          # не має кинути
    e.on_action = None
    e._count("spot_buys")          # і без колбека теж


@pytest.mark.asyncio
async def test_runner_wires_the_counter_into_both_engines(monkeypatch,
                                                          tmp_path):
    """ПРОВОДКА, ВИКОНАННЯМ. Рушії мусять ОТРИМАТИ `campaign.bump` — інакше
    вони рахують у порожнечу, і підсумок знову покаже занижені числа.

    Перевіряємо не текст, а те, що прийшло в конструктори і що виклик
    колбека справді доходить до кампанії.
    """
    from src.execution import soft_start_runner as ssr
    from src.execution.soft_start_runner import SlotWarmer

    got = {}
    bumped = []

    def _spot(*a, **kw):
        got["spot"] = kw.get("on_action")
        return object()

    def _fut(*a, **kw):
        got["fut"] = kw.get("on_action")
        return type("F", (), {
            "state": type("S", (), {"position": None, "pending": None})(),
            "recover": lambda self: _noop()})()

    async def _noop():
        return None

    monkeypatch.setattr(ssr, "SpotSoftStart", _spot)
    monkeypatch.setattr(ssr, "FuturesSoftStart", _fut)

    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w.data_dir = str(tmp_path)
    w.dry_run = True
    w.universe = ["HYPEUSDT"]
    w.client = object(); w.spot_client = object(); w.fee_gate = object()
    w.futures_allowed = True; w.reporter = None; w._account_key = "acc"
    w.budget = type("B", (), {"reset": lambda self: None})()
    w.campaign = type("C", (), {
        "start_if_new": lambda self, k: False,
        "bump": lambda self, kind, n=1: bumped.append((kind, n)),
        "token_pool": lambda self, c: ["MX"],
        "state": type("S", (), {"days": 3, "tokens": ["MX"],
                                "day_index": lambda self: 0,
                                "remaining_days": lambda self: 1.0})(),
    })()

    async def _bal(): return (50.0, 50.0)
    async def _uni(): return ["MX"]
    async def _coins(t): return 0.0
    w._read_balances = _bal; w._spot_universe = _uni
    w.spot_client_coins_value = _coins

    await SlotWarmer.start(w)

    assert got.get("spot") is not None, "спотовий рушій без лічильника"
    assert got.get("fut") is not None, "фʼючерсний рушій без лічильника"
    # і колбек справді веде до кампанії, а не в порожнечу
    got["spot"]("spot_buys", 1)
    got["fut"]("futures_opens", 1)
    assert bumped == [("spot_buys", 1), ("futures_opens", 1)], bumped


@pytest.mark.asyncio
async def test_counter_failure_does_not_stop_the_telegram_report():
    """Лічильники — це облік, звіт — це видимість. Падіння першого не має
    глушити друге."""
    from src.execution.soft_start_runner import SlotWarmer

    sent = []

    class _Camp:
        state = type("S", (), {"stats": {}, "day_index": lambda self: 0,
                               "days": 3})()
        def bump(self, key, n=1): raise OSError("диск повний")
        def day_weight(self): return 1.0

    class _Rep:
        async def spot_buy(self, *a, **k): sent.append("buy")

    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w._held_market = None
    w._held_market_at = 0.0
    w.reporter = _Rep()
    w.campaign = _Camp()
    w.budget = type("B", (), {"spent": 0.0, "pnl": 0.0, "futures_pnl": 0.0, "spot_pnl": 0.0,
                              "held_spot_value": 0.0,
                              "state": type("S2", (), {"max_usdt": 0.0,
                                                       "entries": []})()})()
    w.spot = type("S", (), {"cfg": type("C", (), {"order_usdt_max": 3.0})(),
                            "last_action": None})()
    w.futures = type("F", (), {"state": type("St", (), {"position": None})(),
                               "last_closed": None})()

    await SlotWarmer._report_diff(w, (0, 0, 0, None), (1, 0, 0, None))
    assert sent == ["buy"], "звіт заглушено падінням лічильника"


# ---- повторний прогрів і заміна акаунта (2026-08-30) -----------------------

def _camp(tmp_path, name="c.json", days=3, seed=1):
    import random
    from src.execution.soft_start_campaign import SoftStartCampaign
    return SoftStartCampaign(str(tmp_path / name), days, rng=random.Random(seed))


def test_a_finished_campaign_can_be_started_again(tmp_path):
    """ЖИВА ПРОБЛЕМА. Прогрів можна було запустити рівно ОДИН раз на слот,
    назавжди: `start_if_new` дивився лише на `started_at`, а завершена
    кампанія його має. Повторне натискання 🌱 мовчки нічого не робило, і
    `expired()` одразу гасив слот назад.
    """
    import time
    c = _camp(tmp_path)
    assert c.start_if_new("acc1") is True
    assert c.start_if_new("acc1") is False, "кампанія що ТРИВАЄ не має рестартувати"

    # доводимо її до кінця
    c.state.started_at = time.time() - 4 * 86400
    assert c.expired()
    assert c.start_if_new("acc1") is True, "завершену кампанію не можна почати знову"
    assert not c.state.finished and not c.expired()


def test_a_repasted_key_CONTINUES_the_campaign(tmp_path):
    """Рішення оператора 2026-09-10: ПЕРЕКЛЕЮВАННЯ вебкея продовжує поточну
    кампанію, а нову починає лише ВИДАЛЕННЯ + вставка.

    Раніше тут пінилось протилежне (зміна ключа = чистий аркуш), і це ламало
    єдиний реальний сценарій: у ключа сплив термін, оператор перелогінився на
    ТОМУ САМОМУ акаунті — і кампанія на 1.84/3 доби починалась з нуля, а
    розчистка спота ще й зливала монети. Відрізнити «той самий акаунт» від
    «іншого» за самим ключем НЕМОЖЛИВО: відбиток рахується з рядка ключа, і
    перелогін його міняє так само, як інший акаунт. Тож намір бере не код, а
    оператор — тим, ЯКОЮ дією він поклав ключ.
    """
    c = _camp(tmp_path)
    c.start_if_new("acc-OLD")
    c.bump("spot_buys", 28)
    c.token_pool(["MX", "DOGE", "XRP", "SOL", "TRX"])
    started = c.state.started_at

    assert c.start_if_new("acc-NEW") is False, "переклеювання почало НОВУ кампанію"
    assert c.state.started_at == started, "кампанію перезапущено"
    assert c.state.stats["spot_buys"] == 28, "лічильники втрачено при переклеюванні"
    assert c.state.tokens, "набір токенів втрачено при переклеюванні"
    assert c.state.account_key == "acc-NEW", "новий відбиток не записано"


def test_forgetting_the_campaign_is_what_makes_the_next_start_fresh(tmp_path):
    """Друга половина того самого рішення: ВИДАЛЕННЯ ключа забуває кампанію,
    і аж тоді наступна вставка починає нову з чистим обліком.

    Файл не стирається, а перейменовується в `.forgotten` — слід лишається,
    бо в ньому облік завершеної кампанії.
    """
    from src.execution.soft_start_campaign import (campaign_state_path,
                                                   forget_campaign)
    name = os.path.basename(campaign_state_path(1, str(tmp_path)))
    c = _camp(tmp_path, name)
    c.start_if_new("acc-OLD")
    c.bump("spot_buys", 28)
    c.token_pool(["MX", "DOGE", "XRP", "SOL", "TRX"])

    assert forget_campaign(1, str(tmp_path)) is True
    assert not os.path.exists(campaign_state_path(1, str(tmp_path)))
    assert os.path.exists(campaign_state_path(1, str(tmp_path)) + ".forgotten")

    c2 = _camp(tmp_path, name)
    assert c2.start_if_new("acc-NEW") is True, "після забуття не почалась нова"
    assert c2.state.stats == {}, "лічильники старого акаунта перейшли на новий"
    assert c2.state.tokens == [], "набір токенів старого акаунта перейшов"


def test_forgetting_a_slot_with_no_campaign_is_a_no_op(tmp_path):
    """Видалення ключа зі слота, який ніколи не грівся, не має падати."""
    from src.execution.soft_start_campaign import forget_campaign
    assert forget_campaign(7, str(tmp_path)) is False


def test_the_same_account_does_not_restart_the_campaign(tmp_path):
    """Рестарт бота не має перекидати кампанію: той самий ключ — та сама
    кампанія."""
    c = _camp(tmp_path)
    c.start_if_new("acc1")
    started = c.state.started_at
    c.bump("spot_buys", 5)
    assert c.start_if_new("acc1") is False
    assert c.state.started_at == started
    assert c.state.stats["spot_buys"] == 5


def test_a_file_from_before_the_account_key_gets_it_written(tmp_path):
    """Старі файли не мають `account_key`. Вони НЕ повинні виглядати як
    «інший акаунт» — інакше перший же старт після деплою скинув би живу
    кампанію."""
    c = _camp(tmp_path)
    c.start_if_new("")            # без ключа, як у старому коді
    c.bump("spot_buys", 3)
    assert c.start_if_new("acc1") is False, "порожній ключ прочитано як зміну акаунта"
    assert c.state.account_key == "acc1", "ключ не дописано"
    assert c.state.stats["spot_buys"] == 3, "лічильники втрачено на рівному місці"
