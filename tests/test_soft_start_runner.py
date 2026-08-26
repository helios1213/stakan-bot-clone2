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
    def __init__(self, slot_id, webkey="WEB" + "0" * 64, soft_start_enabled=False,
                 live_enabled=False):
        self.slot_id = slot_id
        self.webkey = webkey
        self.soft_start_enabled = soft_start_enabled
        # The loop refuses a futures warmer while the arb strategy is live here.
        self.live_enabled = live_enabled


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
    # What stop() reports on a freshly built warmer. The loop builds its own
    # warmers, so a test that needs a stuck slot sets this before the pass.
    clean_default: bool = True

    def __init__(self, slot_id, webkey, client, universe, *, dry_run,
                 futures_allowed=True, **kw):
        self.slot_id = slot_id
        self.dry_run = dry_run
        self.universe = universe
        self.futures_allowed = futures_allowed
        self.ticks = 0
        self.started = False
        self.stopped = False
        self.stop_calls = 0
        self.explode = False
        # What stop() reports. False = a position is STILL OPEN, which the loop
        # must treat as "keep this warmer and retry", never as "done".
        self.clean = FakeWarmer.clean_default
        self.draining = False
        self.campaign = FakeCampaign()
        self.budget = FakeBudget()
        self.reporter = None
        self.futures = None
        self._stop_attempts = 0
        self._final_sent = False
        FakeWarmer.made.append(self)

    async def start(self):
        self.started = True

    async def tick(self):
        if self.explode:
            raise RuntimeError("slot on fire")
        self.ticks += 1

    async def stop(self):
        self.stopped = True
        self.stop_calls += 1
        self.draining = True
        if not self.clean:
            self._stop_attempts += 1
        return self.clean

    def stuck(self) -> bool:
        return self.draining and not self.clean

    def _status(self) -> dict:
        return {"day": 1, "days": 3, "spent": 0.0, "ceiling": 5.0, "position": None}

    def finished(self):
        return self.campaign.expired() or self.budget.exhausted()


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    FakeWarmer.made = []
    FakeWarmer.clean_default = True
    monkeypatch.setattr(ssr, "SlotWarmer", FakeWarmer)


async def run_one_pass(store, pool, universe=None, sleeps=1, on_sleep=None):
    """Drive the loop for `sleeps` iterations, then cancel it.

    `on_sleep(n)` runs BETWEEN iterations. The loop keeps its warmers in a local
    dict, so a second call to this helper gets brand-new warmers — anything a
    test needs to change mid-life has to happen here.
    """
    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if on_sleep is not None:
            on_sleep(calls["n"])
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
    w = _real_warmer(_FakeFutures())
    assert await w.stop() is True, "a clean close reports the slot as clean"
    assert w.futures.closed is True


@pytest.mark.asyncio
async def test_warmer_stop_is_noop_without_a_position():
    w = _real_warmer(_FakeFutures(position=None))
    assert await w.stop() is True
    assert w.futures.closed is False


@pytest.mark.asyncio
async def test_stop_reports_NOT_clean_when_the_close_fails():
    """The money property: a refused close must not look like a finished job.

    close_all_positions returning a non-zero code is not an exception — the old
    stop() ignored the bool and the loop dropped the warmer, orphaning a live
    leveraged position with nothing left to retry it.
    """
    w = _real_warmer(_FakeFutures(close_ok=False))
    assert await w.stop() is False
    assert w.futures.state.position is not None, "the record must survive"
    assert w.stuck() is True


@pytest.mark.asyncio
async def test_stop_is_not_clean_while_an_open_is_unresolved():
    """An open we never got an answer for is possible exposure, not 'nothing'."""
    w = _real_warmer(_FakeFutures(position=None, pending={"symbol": "HYPEUSDT"}))
    assert await w.stop() is False


@pytest.mark.asyncio
async def test_button_off_keeps_the_warmer_until_the_close_succeeds():
    """OFF with a stuck position: the warmer STAYS so every poll retries it.

    Dropping it here is the money bug — a live leveraged position with nothing
    left in the process that knows about it.
    """
    FakeWarmer.clean_default = False              # the close keeps failing
    store = FakeStore([FakeSlot(1, soft_start_enabled=True)])

    def on_sleep(n):
        if n == 1:
            store.slots[0].soft_start_enabled = False

    await run_one_pass(store, FakePool(), sleeps=4, on_sleep=on_sleep)
    w = FakeWarmer.made[0]
    assert w.stop_calls >= 2, "every poll must retry the close"
    assert w.ticks == 1, "a draining warmer must never trade again"


@pytest.mark.asyncio
async def test_button_off_drops_the_warmer_once_it_closes_cleanly():
    store = FakeStore([FakeSlot(1, soft_start_enabled=True)])

    def on_sleep(n):
        if n == 1:
            store.slots[0].soft_start_enabled = False

    await run_one_pass(store, FakePool(), sleeps=3, on_sleep=on_sleep)
    w = FakeWarmer.made[0]
    assert w.stop_calls == 1, "a clean close is not retried"


@pytest.mark.asyncio
async def test_auto_off_does_not_flip_the_db_flag_while_a_position_is_open():
    """A finished campaign that cannot close must not claim the slot is idle.

    Flipping soft_start_enabled=0 with a position still open makes the UI lie
    AND drops the slot out of `wanted`, so nothing ever retries the close.
    """
    FakeWarmer.clean_default = False
    store = FakeStore([FakeSlot(1, soft_start_enabled=True)])

    def on_sleep(n):
        if n == 1:
            FakeWarmer.made[0].campaign.done = True

    await run_one_pass(store, FakePool(), sleeps=3, on_sleep=on_sleep)
    assert store.switched_off == [], "the button must stay ON while exposed"
    assert FakeWarmer.made[0].stop_calls >= 1


@pytest.mark.asyncio
async def test_auto_off_switches_off_once_the_slot_is_clean():
    store = FakeStore([FakeSlot(1, soft_start_enabled=True)])

    def on_sleep(n):
        if n == 1:
            FakeWarmer.made[0].campaign.done = True

    await run_one_pass(store, FakePool(), sleeps=3, on_sleep=on_sleep)
    assert store.switched_off == [1]


@pytest.mark.asyncio
async def test_live_slot_gets_no_futures_warmer(monkeypatch):
    """Two systems on one account close each other's positions — refuse.

    The reconciler treats an untracked position as an orphan and market-closes
    it; soft-start's own close is symbol-wide and would take the arb position
    down with it.
    """
    store = FakeStore([FakeSlot(1, soft_start_enabled=True, live_enabled=True),
                       FakeSlot(2, soft_start_enabled=True)])
    await run_one_pass(store, FakePool())
    by_slot = {w.slot_id: w for w in FakeWarmer.made}
    assert by_slot[1].futures_allowed is False
    assert by_slot[2].futures_allowed is True


def test_fake_warmer_still_matches_the_real_one():
    """Guard against the drift that has broken these tests twice.

    Every attribute the loop reads off a warmer must exist on BOTH the real
    class and the fake — otherwise the loop tests pass against an interface
    that no longer exists.
    """
    import inspect
    import re

    real = (set(re.findall(r"self\.(\w+)\s*=",
                           inspect.getsource(RealSlotWarmer.__init__)))
            | {n for n in dir(RealSlotWarmer) if not n.startswith("__")})
    fake = FakeWarmer(1, "k", None, [], dry_run=True)

    # Everything the loop reads off a warmer, taken from the loop's own source
    # rather than from a list someone has to remember to update.
    src = inspect.getsource(ssr) 
    reads = set(re.findall(r"\bw\.(\w+)", src)) | set(re.findall(r"\bwarmers\[\w+\]\.(\w+)", src))
    assert reads, "could not parse what the loop reads — fix this guard"
    for name in sorted(reads):
        assert name in real, f"the loop reads w.{name}, which SlotWarmer lacks"
        assert hasattr(fake, name), f"FakeWarmer is missing {name} — it is lying"


class _FakeFutures:
    """Stands in for FuturesSoftStart in the stop()/drain tests."""

    _DEFAULT = object()

    def __init__(self, position=_DEFAULT, pending=None, close_ok=True,
                 needs_check=False):
        if position is _FakeFutures._DEFAULT:
            position = {"symbol": "HYPEUSDT"}
        self.state = type("S", (), {})()
        # cfg дзеркалить реальний FuturesSoftStart: раннер бере базу ваги дня
        # з конфігу, а не з прибитого числа.
        self.cfg = type("FC", (), {"orders_per_day_max": 6})()
        self.state.position = position
        self.state.pending = pending
        self.state.needs_exchange_check = needs_check
        self.close_ok = close_ok
        self.closed = False

    def has_exposure(self):
        return (self.state.position is not None
                or self.state.pending is not None
                or self.state.needs_exchange_check)

    async def sweep_exchange(self):
        self.state.needs_exchange_check = False

    async def reconcile_pending(self):
        return False

    async def close_position(self, *, forced=False):
        self.closed = True
        if not self.close_ok:
            return False                  # rejected: the record must survive
        self.state.position = None
        return True


def _real_warmer(futures) -> "RealSlotWarmer":
    """A real SlotWarmer with only the fields stop() touches (no client needed)."""
    w = RealSlotWarmer.__new__(RealSlotWarmer)
    w.slot_id = 1
    w.draining = False
    w._stop_attempts = 0
    w.futures = futures
    return w


@pytest.mark.asyncio
async def test_an_idle_futures_half_still_drains_a_leftover_position():
    """A half that stops trading must not stop CLOSING.

    `_fut_viable` goes False when the balance is too small or when the arb
    strategy is live on the slot. Skipping futures.tick() then also skipped the
    close, so a position opened before the switch would sit there forever.
    """
    w = RealSlotWarmer.__new__(RealSlotWarmer)
    w.slot_id = 1
    w.draining = False
    w._stop_attempts = 0
    w.reporter = None
    w.futures = _FakeFutures()
    w.spot = type("S", (), {"plan": type("P", (), {"buys_done": 0, "sells_done": 0,
                                                   "buys_target": 0, "sells_target": 0})(),
                            "cfg": type("SC", (), {"buys_per_day_max": 25,
                                                   "sells_per_day_max": 20})(),
                            "tick": _noop})()
    # scale_target — той самий дрейф фейків, що вже двічі ламав цю сюїту:
    # раннер тепер бере стелю з кампанії (одна формула замість дубля), тож
    # фейк мусить це вміти. Якщо додаси раннеру ще виклик до campaign —
    # дзеркаль його ТУТ у тому ж коміті.
    w.campaign = type("C", (), {"expired": lambda self: False,
                                "day_weight": lambda self: 1.0,
                                "scale_target": lambda self, base: base,
                                "finish": lambda self: None,
                                "state": type("St", (), {"days": 3,
                                                         "day_index": lambda self: 0})()})()
    w.budget = type("B", (), {"exhausted": lambda self: False, "spent": 0.0,
                              "state": type("S2", (), {"max_usdt": 5.0})()})()
    w._weighted_logged = None
    w._spot_viable = False
    w._fut_viable = False                       # this half is switched off
    w.futures.state.orders_done = 0
    w.futures.state.orders_target = 0

    await w.tick()
    assert w.futures.closed is True, "the leftover position must be closed"


async def _noop(*a, **kw):
    return None


# ---- проводка звіту: раннер має віддавати ФАКТИЧНІ числа (2026-08-26) ------

@pytest.mark.asyncio
async def test_runner_reports_the_real_order_size_not_the_config_ceiling():
    """НАСКРІЗЬ, бо саме тут була діра.

    Тести репортера перевіряють рендер і мутанта в раннері НЕ ловлять: раннер
    міг передавати `cfg.order_usdt_max` і літерал «~», і кожен рядок звіту
    показував би 3.00 USDT незалежно від реального розміру (1.5-2.2). Звіт, що
    показує константу замість виміру, гірший за відсутній — за ним неможливо
    помітити, що розмір не змінюється.
    """
    # RealSlotWarmer, не SlotWarmer: autouse-фікстура підміняє ssr.SlotWarmer
    # фейком, і `from ... import SlotWarmer` віддав би саме його.
    w = RealSlotWarmer.__new__(RealSlotWarmer)
    w.slot_id = 1
    seen = []

    class _Rep:
        async def spot_buy(self, symbol, usdt, qty, **st):
            seen.append(("buy", symbol, usdt, qty))

        async def spot_sell(self, symbol, qty, usdt=0.0, **st):
            seen.append(("sell", symbol, usdt, qty))

        async def futures_open(self, *a, **kw):
            seen.append(("open", a, kw))

        async def futures_close(self, *a, **kw):
            seen.append(("close", a, kw))

    w.reporter = _Rep()
    w.spot = type("S", (), {
        "cfg": type("C", (), {"order_usdt_max": 3.0})(),
        "last_action": {"kind": "buy", "symbol": "MXUSDT",
                        "usdt": 1.78, "qty": "0.6692"},
    })()
    w.futures = type("F", (), {"state": type("St", (), {"position": None})(),
                               "last_closed": None})()
    w.campaign = type("C", (), {"state": type("S2", (), {
        "day_index": lambda self: 0, "days": 3})()})()
    # Фейк бюджету мусить дзеркалити реальний: додався pnl (рух ринку) і
    # entries (з них рахується, скільки лежить у монетах).
    w.budget = type("B", (), {"spent": 0.1, "pnl": 0.0,
                              "state": type("S3", (), {"max_usdt": 5.0,
                                                       "entries": []})()})()

    await RealSlotWarmer._report_diff(w, (0, 0, 0, None), (1, 0, 0, None))

    assert seen, "звіт не відправлено взагалі"
    kind, sym, usdt, qty = seen[0]
    assert kind == "buy"
    assert sym == "MXUSDT", f"символ не з реальної дії: {sym}"
    assert abs(usdt - 1.78) < 1e-9, f"сума зі стелі конфігу, а не з ордера: {usdt}"
    assert qty == "0.6692", f"кількість не передана: {qty!r}"


@pytest.mark.asyncio
async def test_runner_reports_futures_open_with_its_size():
    w = RealSlotWarmer.__new__(RealSlotWarmer)
    w.slot_id = 1
    seen = {}

    class _Rep:
        async def futures_open(self, symbol, side, leverage, hold_min, **kw):
            seen.update(symbol=symbol, side=side, lev=leverage,
                        hold=hold_min, **kw)

    w.reporter = _Rep()
    w.spot = type("S", (), {"cfg": type("C", (), {"order_usdt_max": 3.0})(),
                            "last_action": None})()
    pos = {"symbol": "1000PEPEUSDT", "side": 1, "leverage": 9, "vol": 1,
           "notional": 37.44, "opened_at": 0, "close_after": 53 * 60}
    w.futures = type("F", (), {"state": type("St", (), {"position": pos})(),
                               "last_closed": None})()
    w.campaign = type("C", (), {"state": type("S2", (), {
        "day_index": lambda self: 0, "days": 3})()})()
    # Фейк бюджету мусить дзеркалити реальний: додався pnl (рух ринку) і
    # entries (з них рахується, скільки лежить у монетах).
    w.budget = type("B", (), {"spent": 0.1, "pnl": 0.0,
                              "state": type("S3", (), {"max_usdt": 5.0,
                                                       "entries": []})()})()

    await RealSlotWarmer._report_diff(w, (0, 0, 0, None), (0, 0, 1, "1000PEPEUSDT"))
    assert seen.get("symbol") == "1000PEPEUSDT"
    assert seen.get("lev") == 9 and seen.get("hold") == 53
    assert seen.get("vol") == 1, "розмір позиції не передано у звіт"
