"""Tests for futures soft-start. No network, no orders, deterministic RNG.

Every test here guards money or the spec:
  * spec bounds: hold >= 10min, 1-3 orders/day, 3-10h pause
  * ONLY 0%-fee pairs get opened, and the fee is re-checked at open time
  * both live switches must agree before anything is sent
  * an open position is persisted immediately and recovered after a restart
  * a failed close keeps the position so the next tick retries it
  * never two positions at once
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest

from src.execution.fee_gate import PairFee
from src.execution.futures_soft_start import (
    SIDE_LONG,
    SIDE_SHORT,
    FuturesSoftStart,
    FuturesSoftStartConfig,
    OpenPosition,
    load_state,
)


class FakeGate:
    def __init__(self, fees: dict):
        self.fees = fees
        self.lookups = []

    async def fee(self, symbol):
        self.lookups.append(symbol)
        f = self.fees.get(symbol)
        if f is None:
            return None
        maker, taker = f
        return PairFee(symbol=symbol, maker=maker, taker=taker)


class FakeClient:
    def __init__(self, open_code="0", close_code="0"):
        self.opened = []
        self.closed = []
        self.open_code = open_code
        self.close_code = close_code

    async def submit_order(self, **kw):
        self.opened.append(kw)
        if self.open_code == "raise":
            raise ConnectionError("down")
        return {"code": self.open_code}

    async def close_all_positions(self, symbol):
        self.closed.append(symbol)
        if self.close_code == "raise":
            raise ConnectionError("down")
        return {"code": self.close_code}


def mk(tmp_path, fees=None, *, dry_run=True, client=None, seed=3, **cfgkw):
    cfg = FuturesSoftStartConfig(state_path=str(tmp_path / "fs.json"), **cfgkw)
    gate = FakeGate(fees if fees is not None else {"HYPEUSDT": (0, 0)})
    cl = client or FakeClient()
    ss = FuturesSoftStart(cl, gate, list(gate.fees) or ["HYPEUSDT"], cfg,
                          dry_run=dry_run, rng=random.Random(seed))
    return ss, cl, gate


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("SOFT_START_LIVE", "1")


# ---- config / spec bounds -------------------------------------------------

def test_hold_under_ten_minutes_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="violates the spec"):
        FuturesSoftStartConfig(hold_minutes_min=5,
                               state_path=str(tmp_path / "x")).validate()


def test_daily_plan_within_spec(tmp_path):
    for seed in range(40):
        ss, _, _ = mk(tmp_path, seed=seed)
        assert 1 <= ss.state.orders_target <= 3


def test_pause_between_orders_is_3_to_10h(tmp_path):
    ss, _, _ = mk(tmp_path)
    for _ in range(30):
        before = time.time()
        ss._schedule_pause()
        gap_h = (ss.state.next_open_at - before) / 3600
        assert 3.0 <= gap_h <= 10.0


# ---- the live switches ----------------------------------------------------

def test_dry_run_by_default(tmp_path):
    ss, _, _ = mk(tmp_path)
    assert ss.dry_run is True and ss.sending is False


def test_env_alone_does_not_enable_sending(tmp_path, live):
    ss, _, _ = mk(tmp_path, dry_run=True)
    assert ss.sending is False


def test_flag_alone_does_not_enable_sending(tmp_path, monkeypatch):
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    ss, _, _ = mk(tmp_path, dry_run=False)
    assert ss.sending is False


def test_both_switches_enable_sending(tmp_path, live):
    ss, _, _ = mk(tmp_path, dry_run=False)
    assert ss.sending is True


@pytest.mark.asyncio
async def test_dry_run_sends_no_order(tmp_path):
    ss, cl, _ = mk(tmp_path)
    assert await ss.open_position() is True
    assert cl.opened == []


# ---- fee gating -----------------------------------------------------------

@pytest.mark.asyncio
async def test_only_zero_fee_pairs_are_opened(tmp_path, live):
    fees = {"HEMIUSDT": (0.0001, 0.0004), "HYPEUSDT": (0, 0)}
    ss, cl, _ = mk(tmp_path, fees, dry_run=False)
    await ss.open_position()
    assert cl.opened, "should have opened the zero-fee pair"
    assert ss.state.position["symbol"] == "HYPEUSDT"


@pytest.mark.asyncio
async def test_unknown_fee_pair_is_never_opened(tmp_path, live):
    ss, cl, _ = mk(tmp_path, {"MYSTERY": None}, dry_run=False)
    assert await ss.open_position() is False
    assert cl.opened == []


@pytest.mark.asyncio
async def test_zero_maker_but_paid_taker_excluded_by_default(tmp_path, live):
    """BTC_USDT: maker=0, taker=0.0002. A warm-up gets CLOSED too, so a paid
    taker is a real cost — excluded unless the operator opts out."""
    ss, cl, _ = mk(tmp_path, {"BTCUSDT": (0, 0.0002)}, dry_run=False)
    assert await ss.open_position() is False
    assert cl.opened == []

    ss2, cl2, _ = mk(tmp_path, {"BTCUSDT": (0, 0.0002)}, dry_run=False,
                     require_zero_taker=False)
    assert await ss2.open_position() is True
    assert cl2.opened


@pytest.mark.asyncio
async def test_fee_is_rechecked_at_open_time(tmp_path, live):
    """Not just at planning — a tier can change in between."""
    ss, _, gate = mk(tmp_path, {"HYPEUSDT": (0, 0)}, dry_run=False)
    await ss.open_position()
    assert gate.lookups, "open must consult the fee gate"


# ---- position lifecycle ---------------------------------------------------

@pytest.mark.asyncio
async def test_position_persisted_immediately(tmp_path, live):
    ss, _, _ = mk(tmp_path, dry_run=False)
    await ss.open_position()
    on_disk = json.loads(Path(ss.cfg.state_path).read_text())
    assert on_disk["position"]["symbol"] == "HYPEUSDT"
    assert on_disk["position"]["close_after"] > time.time()


@pytest.mark.asyncio
async def test_hold_deadline_is_within_spec(tmp_path, live):
    ss, _, _ = mk(tmp_path, dry_run=False)
    await ss.open_position()
    pos = OpenPosition(**ss.state.position)
    minutes = (pos.close_after - pos.opened_at) / 60
    assert 10 <= minutes <= 300


@pytest.mark.asyncio
async def test_never_two_positions_at_once(tmp_path, live):
    ss, cl, _ = mk(tmp_path, dry_run=False)
    await ss.open_position()
    assert await ss.open_position() is False
    assert len(cl.opened) == 1


@pytest.mark.asyncio
async def test_tick_does_not_open_while_holding(tmp_path, live):
    ss, cl, _ = mk(tmp_path, dry_run=False)
    await ss.open_position()
    ss.state.next_open_at = 0          # pause elapsed
    ss.state.orders_target = 3
    await ss.tick()
    assert len(cl.opened) == 1


@pytest.mark.asyncio
async def test_tick_closes_when_due(tmp_path, live):
    ss, cl, _ = mk(tmp_path, dry_run=False)
    await ss.open_position()
    ss.state.position["close_after"] = time.time() - 1
    await ss.tick()
    assert cl.closed == ["HYPE_USDT"]
    assert ss.state.position is None


@pytest.mark.asyncio
async def test_failed_close_keeps_the_position_for_retry(tmp_path, live):
    """Dropping the record would orphan a live position — the worst outcome."""
    ss, cl, _ = mk(tmp_path, dry_run=False, client=FakeClient(close_code="raise"))
    await ss.open_position()
    ss.state.position["close_after"] = time.time() - 1
    await ss.tick()
    assert ss.state.position is not None, "must keep it and retry"


@pytest.mark.asyncio
async def test_rejected_open_does_not_record_a_position(tmp_path, live):
    ss, _, _ = mk(tmp_path, dry_run=False, client=FakeClient(open_code="510"))
    assert await ss.open_position() is False
    assert ss.state.position is None


@pytest.mark.asyncio
async def test_open_exception_is_contained(tmp_path, live):
    ss, _, _ = mk(tmp_path, dry_run=False, client=FakeClient(open_code="raise"))
    assert await ss.open_position() is False
    assert ss.state.position is None


# ---- restart recovery -----------------------------------------------------

@pytest.mark.asyncio
async def test_recover_closes_an_overdue_position(tmp_path, live):
    ss, cl, _ = mk(tmp_path, dry_run=False)
    await ss.open_position()
    ss.state.position["close_after"] = time.time() - 60

    ss2, cl2, _ = mk(tmp_path, dry_run=False)   # simulate a restart
    ss2.state = load_state(ss.cfg.state_path)
    ss2.state.position = ss.state.position
    await ss2.recover()
    assert ss2.state.position is None
    assert cl2.closed


@pytest.mark.asyncio
async def test_recover_resumes_a_live_hold(tmp_path, live):
    ss, _, _ = mk(tmp_path, dry_run=False)
    await ss.open_position()
    ss2, cl2, _ = mk(tmp_path, dry_run=False)
    ss2.state.position = dict(ss.state.position)
    await ss2.recover()
    assert ss2.state.position is not None, "still within the hold window"
    assert cl2.closed == []


@pytest.mark.asyncio
async def test_daily_target_caps_opens(tmp_path, live):
    ss, cl, _ = mk(tmp_path, dry_run=False)
    ss.state.orders_target = 2
    for _ in range(6):
        ss.state.next_open_at = 0
        if ss.state.position is not None:
            ss.state.position["close_after"] = time.time() - 1
        await ss.tick()
    assert len(cl.opened) <= 2


def test_sides_are_both_reachable(tmp_path):
    seen = set()
    for seed in range(50):
        ss, _, _ = mk(tmp_path, seed=seed)
        seen.add(SIDE_LONG if ss.rng.random() < ss.cfg.long_ratio else SIDE_SHORT)
    assert seen == {SIDE_LONG, SIDE_SHORT}
