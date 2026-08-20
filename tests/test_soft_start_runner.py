"""Tests for the soft-start runner — the thing the Telegram button drives.

The button is the only user-facing control, so these pin what pressing it does:

  1. ON  starts a warmer for that slot; OFF stops it.
  2. Switching OFF with an open futures position CLOSES it first. Without this
     the button would orphan a live position on the exchange.
  3. A slot with no webkey is never warmed.
  4. One slot blowing up does not stop the others.
  5. The runner is DRY-RUN unless SOFT_START_LIVE is set — the button alone
     can never start placing real orders.
"""
from __future__ import annotations

import asyncio

import pytest

from src.execution import soft_start_runner as ssr
# Bound BEFORE the autouse fixture patches ssr.SlotWarmer, so the two stop()
# tests below exercise the REAL class rather than the fake the loop tests use.
from src.execution.soft_start_runner import SlotWarmer as RealSlotWarmer


class FakeSlot:
    def __init__(self, slot_id, webkey="WEB" + "0" * 64, soft_start_enabled=False):
        self.slot_id = slot_id
        self.webkey = webkey
        self.soft_start_enabled = soft_start_enabled


class FakeStore:
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


class FakePool:
    def __init__(self):
        self.gets = []

    async def get(self, slot_id):
        self.gets.append(slot_id)
        return object()


class FakeCampaign:
    def __init__(self, done=False):
        self.done = done

    def expired(self):
        return self.done


class FakeBudget:
    def __init__(self, done=False):
        self.done = done

    def exhausted(self):
        return self.done


class FakeWarmer:
    """Stands in for SlotWarmer so the loop is tested without real engines.

    Mirrors the real interface, including `finished()` — the loop calls it to
    decide whether a slot should switch ITSELF off.
    """
    made: list["FakeWarmer"] = []

    def __init__(self, slot_id, webkey, client, universe, *, dry_run, **kw):
        self.slot_id = slot_id
        self.dry_run = dry_run
        self.universe = universe
        self.ticks = 0
        self.started = False
        self.stopped = False
        self.explode = False
        self.campaign = FakeCampaign()
        self.budget = FakeBudget()
        FakeWarmer.made.append(self)

    async def start(self):
        self.started = True

    async def tick(self):
        if self.explode:
            raise RuntimeError("slot on fire")
        self.ticks += 1

    async def stop(self):
        self.stopped = True

    def finished(self):
        return self.campaign.expired() or self.budget.exhausted()


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    FakeWarmer.made = []
    monkeypatch.setattr(ssr, "SlotWarmer", FakeWarmer)


async def run_one_pass(store, pool, universe=None, sleeps=1):
    """Drive the loop for `sleeps` iterations, then cancel it."""
    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] >= sleeps:
            raise asyncio.CancelledError
        return None

    import src.execution.soft_start_runner as mod
    orig = mod.asyncio.sleep
    mod.asyncio.sleep = fake_sleep
    try:
        with pytest.raises(asyncio.CancelledError):
            await ssr.soft_start_loop(store, pool, lambda: universe or ["HYPEUSDT"])
    finally:
        mod.asyncio.sleep = orig


@pytest.mark.asyncio
async def test_button_on_starts_a_warmer(monkeypatch):
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    store = FakeStore([FakeSlot(1, soft_start_enabled=True)])
    await run_one_pass(store, FakePool())
    assert len(FakeWarmer.made) == 1
    w = FakeWarmer.made[0]
    assert w.slot_id == 1 and w.started and w.ticks == 1


@pytest.mark.asyncio
async def test_disabled_slot_is_never_warmed(monkeypatch):
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    store = FakeStore([FakeSlot(1, soft_start_enabled=False)])
    await run_one_pass(store, FakePool())
    assert FakeWarmer.made == []


@pytest.mark.asyncio
async def test_slot_without_webkey_is_never_warmed(monkeypatch):
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    store = FakeStore([FakeSlot(1, webkey=None, soft_start_enabled=True)])
    await run_one_pass(store, FakePool())
    assert FakeWarmer.made == []


@pytest.mark.asyncio
async def test_button_off_stops_the_warmer(monkeypatch):
    """Pass 1 the slot is on, pass 2 it is off -> warmer must be stopped."""
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    slot = FakeSlot(1, soft_start_enabled=True)
    store = FakeStore([slot])

    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] == 1:
            slot.soft_start_enabled = False     # operator presses OFF
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    import src.execution.soft_start_runner as mod
    orig = mod.asyncio.sleep
    mod.asyncio.sleep = fake_sleep
    try:
        with pytest.raises(asyncio.CancelledError):
            await ssr.soft_start_loop(store, FakePool(), lambda: ["HYPEUSDT"])
    finally:
        mod.asyncio.sleep = orig

    assert len(FakeWarmer.made) == 1
    assert FakeWarmer.made[0].stopped is True


@pytest.mark.asyncio
async def test_one_bad_slot_does_not_stop_the_others(monkeypatch):
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    store = FakeStore([FakeSlot(1, soft_start_enabled=True),
                       FakeSlot(2, soft_start_enabled=True)])

    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] == 1:
            FakeWarmer.made[0].explode = True   # slot 1 starts failing
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    import src.execution.soft_start_runner as mod
    orig = mod.asyncio.sleep
    mod.asyncio.sleep = fake_sleep
    try:
        with pytest.raises(asyncio.CancelledError):
            await ssr.soft_start_loop(store, FakePool(), lambda: ["HYPEUSDT"])
    finally:
        mod.asyncio.sleep = orig

    assert FakeWarmer.made[1].ticks == 2, "healthy slot must keep ticking"


@pytest.mark.asyncio
async def test_dry_run_unless_env_is_set(monkeypatch):
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    await run_one_pass(FakeStore([FakeSlot(1, soft_start_enabled=True)]), FakePool())
    assert FakeWarmer.made[0].dry_run is True

    FakeWarmer.made = []
    monkeypatch.setenv("SOFT_START_LIVE", "1")
    await run_one_pass(FakeStore([FakeSlot(2, soft_start_enabled=True)]), FakePool())
    assert FakeWarmer.made[0].dry_run is False


@pytest.mark.asyncio
async def test_warmer_stop_closes_an_open_position():
    """SlotWarmer.stop() must close a held position — the real class, not a fake.

    This is the property that keeps the OFF button from orphaning a position.
    """
    class FakeFutures:
        def __init__(self):
            self.state = type("S", (), {"position": {"symbol": "HYPEUSDT"}})()
            self.closed = False

        async def close_position(self, *, forced=False):
            self.closed = True
            self.state.position = None
            return True

    w = RealSlotWarmer.__new__(RealSlotWarmer)   # bypass __init__ (needs a client)
    w.slot_id = 1
    w.futures = FakeFutures()
    await w.stop()
    assert w.futures.closed is True


@pytest.mark.asyncio
async def test_warmer_stop_is_noop_without_a_position():
    class FakeFutures:
        def __init__(self):
            self.state = type("S", (), {"position": None})()
            self.closed = False

        async def close_position(self, *, forced=False):
            self.closed = True
            return True

    w = RealSlotWarmer.__new__(RealSlotWarmer)
    w.slot_id = 1
    w.futures = FakeFutures()
    await w.stop()
    assert w.futures.closed is False
