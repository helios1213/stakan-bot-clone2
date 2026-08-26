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


def test_corrupt_state_starts_fresh(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{broken")
    c = SoftStartCampaign(str(p))
    assert c.state.started_at == 0.0


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

        def __init__(self, slot_id, webkey, client, universe, *, dry_run, **kw):
            made.append(self)
            self.slot_id = slot_id
            self.stopped = False
            self.draining = False
            self._stop_attempts = 0
            self._final_sent = False
            self.campaign = type("C", (), {"expired": lambda self: True})()
            self.budget = type("B", (), {"exhausted": lambda self: False,
                                         "spent": 0.0,
                                         "state": type("S", (), {"max_usdt": 5.0})()})()
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

    w.campaign = _Camp()
    w.spot = type("S", (), {"plan": _Plan()})()
    w.futures = type("F", (), {"state": _State()})()

    SlotWarmer._apply_day_weight(w)

    assert calls == [10, 10, 3], (
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
    w.spot = type("S", (), {"plan": type("P", (), {"buys_target": 3,
                                                   "sells_target": 1})()})()
    w.futures = type("F", (), {"state": type("St", (), {"orders_target": 1})()})()

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
