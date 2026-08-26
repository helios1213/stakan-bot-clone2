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
    def __init__(self, open_code="0", close_code="0", positions=None,
                 positions_code="0"):
        self.opened = []
        self.closed = []
        self.open_code = open_code
        self.close_code = close_code
        # What the EXCHANGE says is open. `positions_code != "0"` (or "raise")
        # means the read failed — which must never be read as "nothing open".
        self.positions = positions if positions is not None else []
        self.positions_code = positions_code
        self.position_reads = 0

    async def get_open_positions(self):
        self.position_reads += 1
        if self.positions_code == "raise":
            raise ConnectionError("down")
        return {"code": self.positions_code, "data": list(self.positions)}

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


def mk(tmp_path, fees=None, *, dry_run=True, client=None, seed=3, budget=None,
       **cfgkw):
    cfg = FuturesSoftStartConfig(state_path=str(tmp_path / "fs.json"), **cfgkw)
    gate = FakeGate(fees if fees is not None else {"HYPEUSDT": (0, 0)})
    cl = client or FakeClient()
    ss = FuturesSoftStart(cl, gate, list(gate.fees) or ["HYPEUSDT"], cfg,
                          dry_run=dry_run, rng=random.Random(seed),
                          budget=budget)
    # No network in tests: contract_meta otherwise calls contract.mexc.com, and
    # a rate limit there turned into a spurious "no contract meta — skipping".
    ss.contract_meta = lambda contract: (0.1, 40.0)
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
async def test_paid_taker_now_warms_and_is_charged(tmp_path, live):
    """ПРАВИЛО ЗМІНЕНО 2026-08-26. Раніше `BTC_USDT` (maker=0, taker=0.0002)
    ВІДКИДАВСЯ, і на акаунті без промо прогрів не йшов узагалі — тобто саме
    там, де він найпотрібніший.

    Тепер платна пара гріється, а комісія ЛЯГАЄ У ВИТРАТИ. Перевіряємо обидва
    боки: ордер пішов І бюджет побачив комісію."""
    from src.execution.soft_start_budget import SoftStartBudget
    bud = SoftStartBudget(str(tmp_path / "b.json"), max_usdt=50.0)
    ss, cl, _ = mk(tmp_path, {"BTCUSDT": (0, 0.0002)}, dry_run=False, budget=bud)
    assert await ss.open_position() is True
    assert cl.opened, "платна пара мусить грітись, а не блокуватись"

    assert bud.spent > 0, "витрати не списались узагалі"
    entries = " ".join(str(e) for e in bud.state.entries)
    assert "bps/нога" in entries, f"комісії не видно у витратах: {entries}"

    # І кількісно: комісія має бути САМЕ 2 ноги × ставку × ноціонал.
    notional = float(ss.state.position["vol"]) * 0.1 * 40.0   # cs × px із mk()
    from src.execution.soft_start_budget import futures_round_trip_cost
    assert abs(bud.spent
               - futures_round_trip_cost(notional, fee_frac=0.0002)) < 1e-9


@pytest.mark.asyncio
async def test_paid_pair_can_still_be_refused_by_config(tmp_path, live):
    """Стара жорстка поведінка лишається доступною однією змінною."""
    ss, cl, _ = mk(tmp_path, {"BTCUSDT": (0, 0.0002)}, dry_run=False,
                   allow_paid_fees=False)
    assert await ss.open_position() is False
    assert cl.opened == []


@pytest.mark.asyncio
async def test_zero_fee_pair_is_preferred_over_a_paid_one(tmp_path, live):
    """0% має вигравати завжди, коли він доступний — платне це запасний шлях."""
    ss, cl, _ = mk(tmp_path, {"BTCUSDT": (0, 0.0002), "HYPEUSDT": (0, 0)},
                   dry_run=False)
    sym, fee = await ss.pick_pair()
    assert sym == "HYPEUSDT" and fee == 0.0


@pytest.mark.asyncio
async def test_the_cheapest_paid_pair_wins(tmp_path, live):
    ss, _, _ = mk(tmp_path, {"BTCUSDT": (0, 0.0004), "HYPEUSDT": (0, 0.0001)},
                  dry_run=False)
    sym, fee = await ss.pick_pair()
    assert sym == "HYPEUSDT" and fee == 0.0001


@pytest.mark.asyncio
async def test_an_absurdly_expensive_pair_is_still_refused(tmp_path, live):
    """`max_fee_frac` — стеля здорового глузду: 50 bps/нога вигорить бюджет
    за кілька ордерів, і прогрів перестане бути прогрівом."""
    ss, cl, _ = mk(tmp_path, {"BTCUSDT": (0, 0.005)}, dry_run=False)
    sym, _ = await ss.pick_pair()
    assert sym is None
    assert await ss.open_position() is False


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
async def test_open_exception_asks_the_exchange_and_adopts_a_real_fill(tmp_path, live):
    """A lost response is a QUESTION, not a failure.

    A market order can fill and still raise on the way back (timeout, non-JSON
    body). The old code logged "skipped" and returned before writing any state,
    leaving a real leveraged position nothing in the process knew about.
    """
    from src.execution.soft_start_budget import SoftStartBudget
    cl = FakeClient(open_code="raise",
                    positions=[{"symbol": "HYPE_USDT", "holdVol": "3",
                                "positionType": 1, "leverage": 10}])
    ss, _, _ = mk(tmp_path, dry_run=False, client=cl,
                  budget=SoftStartBudget(str(tmp_path / "b.json"), 5.0))
    assert await ss.open_position() is True, "the position is real — adopt it"
    assert ss.state.position is not None
    assert ss.state.position["symbol"] == "HYPEUSDT"
    assert ss.state.position["vol"] == 3
    assert ss.state.pending is None, "the question is answered"
    assert ss.budget.spent > 0, "an adopted position costs what any other does"
    # and it survives a restart
    assert load_state(ss.cfg.state_path).position is not None


@pytest.mark.asyncio
async def test_open_exception_with_nothing_open_clears_cleanly(tmp_path, live):
    cl = FakeClient(open_code="raise", positions=[])
    ss, _, _ = mk(tmp_path, dry_run=False, client=cl)
    assert await ss.open_position() is False
    assert ss.state.position is None
    assert ss.state.pending is None


@pytest.mark.asyncio
async def test_unreadable_exchange_keeps_the_question_open(tmp_path, live):
    """"Could not read" must never collapse into "nothing is open"."""
    cl = FakeClient(open_code="raise", positions_code="raise")
    ss, _, _ = mk(tmp_path, dry_run=False, client=cl)
    assert await ss.open_position() is False
    assert ss.state.pending is not None, "the pending record must survive"
    assert ss.has_exposure() is True
    assert load_state(ss.cfg.state_path).pending is not None

    # ...and the next tick re-asks, adopting once the exchange answers.
    cl.positions_code = "0"
    cl.positions = [{"symbol": "HYPE_USDT", "holdVol": "2",
                     "positionType": 2, "leverage": 5}]
    await ss.tick()
    assert ss.state.position is not None
    assert ss.state.position["side"] == SIDE_SHORT
    assert ss.state.pending is None


@pytest.mark.asyncio
async def test_an_unresolved_open_blocks_a_second_one(tmp_path, live):
    """Never stack: opening again while a fill is unconfirmed risks two positions."""
    cl = FakeClient(open_code="raise", positions_code="raise")
    ss, _, _ = mk(tmp_path, dry_run=False, client=cl)
    await ss.open_position()
    sent = len(cl.opened)
    assert await ss.open_position() is False
    assert len(cl.opened) == sent, "nothing new may be sent"


@pytest.mark.asyncio
async def test_rejected_open_clears_the_breadcrumb(tmp_path, live):
    """A definitive rejection is not ambiguity — no exchange round-trip needed."""
    cl = FakeClient(open_code="9999")
    ss, _, _ = mk(tmp_path, dry_run=False, client=cl)
    assert await ss.open_position() is False
    assert ss.state.pending is None
    assert cl.position_reads == 0


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


# ---- disarming must not lose a position -----------------------------------

@pytest.mark.asyncio
async def test_dry_run_close_refuses_to_erase_a_live_position(tmp_path, live,
                                                              monkeypatch):
    """Unsetting SOFT_START_LIVE and restarting must not orphan a position.

    The dry branch sent no order but cleared the record and persisted that —
    so the bot forgot a real position, permanently. This is reachable from
    recover(), tick() and stop().
    """
    ss, cl, _ = mk(tmp_path, dry_run=False)
    assert await ss.open_position() is True
    saved = dict(ss.state.position)

    monkeypatch.delenv("SOFT_START_LIVE", raising=False)   # operator disarms
    ss2, cl2, _ = mk(tmp_path, dry_run=False)              # restart
    assert ss2.state.position is not None, "loaded from disk"
    assert await ss2.close_position(forced=True) is False, "refuse, do not erase"
    assert ss2.state.position == saved, "the record must survive untouched"
    assert cl2.closed == [], "and nothing was sent"
    assert load_state(ss.cfg.state_path).position is not None

    await ss2.recover()
    assert ss2.state.position is not None, "recover must not erase it either"


@pytest.mark.asyncio
async def test_a_dry_run_position_is_still_disposable(tmp_path):
    """The refusal is about LIVE records only — a dry one may be cleared."""
    ss, _, _ = mk(tmp_path, dry_run=True)
    pos = OpenPosition(symbol="HYPEUSDT", side=SIDE_LONG, vol=1, leverage=5,
                       opened_at=time.time(), close_after=time.time() - 1,
                       opened_live=False)
    ss.state.position = json.loads(json.dumps(pos.__dict__))
    assert await ss.close_position() is True
    assert ss.state.position is None


# ---- an unreadable state file ---------------------------------------------

@pytest.mark.asyncio
async def test_corrupt_state_file_asks_the_exchange(tmp_path, live):
    """A truncated state file used to read as "no position"."""
    path = tmp_path / "fs.json"
    path.write_text('{"date": "2026-08-20", "orders_do')      # torn write
    cl = FakeClient(positions=[{"symbol": "HYPE_USDT", "holdVol": "4",
                                "positionType": 1, "leverage": 7}])
    ss, _, _ = mk(tmp_path, dry_run=False, client=cl)
    assert ss.state.needs_exchange_check is True
    assert ss.has_exposure() is True, "unknown is not the same as empty"

    await ss.recover()
    # Adopted AND closed straight away: we do not know when it opened, and
    # money we lost track of is not something to sit on.
    assert cl.closed == ["HYPE_USDT"]
    assert ss.state.position is None
    assert ss.state.needs_exchange_check is False
    assert ss.has_exposure() is False
    assert list(tmp_path.glob("fs.json.corrupt.*")), "the bad file is kept"


@pytest.mark.asyncio
async def test_an_adopted_orphan_is_not_erased_by_a_dry_run_process(tmp_path):
    """Same refusal as any live record — disarming must not lose it."""
    (tmp_path / "fs.json").write_text("torn")
    cl = FakeClient(positions=[{"symbol": "HYPE_USDT", "holdVol": "4",
                                "positionType": 1, "leverage": 7}])
    ss, _, _ = mk(tmp_path, dry_run=False, client=cl)     # no SOFT_START_LIVE
    await ss.recover()
    assert cl.closed == [], "dry-run sends nothing"
    assert ss.state.position is not None, "and forgets nothing"
    assert ss.has_exposure() is True


@pytest.mark.asyncio
async def test_corrupt_state_leaves_foreign_positions_alone(tmp_path, live):
    """Only symbols WE warm are adopted — never another system's position."""
    path = tmp_path / "fs.json"
    path.write_text("not json at all")
    cl = FakeClient(positions=[{"symbol": "ONDO_USDT", "holdVol": "50",
                                "positionType": 1, "leverage": 50}])
    ss, _, _ = mk(tmp_path, dry_run=False, client=cl)
    await ss.recover()
    assert ss.state.position is None, "not ours — do not touch it"
    assert cl.closed == []


@pytest.mark.asyncio
async def test_corrupt_state_and_unreadable_exchange_refuses_to_open(tmp_path, live):
    (tmp_path / "fs.json").write_text("{{{")
    cl = FakeClient(positions_code="raise")
    ss, _, _ = mk(tmp_path, dry_run=False, client=cl)
    await ss.recover()
    assert ss.state.needs_exchange_check is True, "still unknown — keep asking"
    assert await ss.open_position() is False
    assert cl.opened == [], "never open into an unknown account state"


def test_state_is_written_atomically(tmp_path):
    """A reader must see the old state or the new one, never a torn one."""
    from src.execution.futures_soft_start import FuturesState, save_state
    path = str(tmp_path / "fs.json")
    save_state(path, FuturesState(date="2026-08-20", orders_target=2))
    save_state(path, FuturesState(date="2026-08-21", orders_target=3))
    assert load_state(path).orders_target == 3
    assert not list(tmp_path.glob("*.tmp")), "no temp file left behind"


# ---- комісія у моделі витрат (зміна 2026-08-26) ---------------------------

def test_round_trip_cost_charges_both_legs_of_the_fee():
    """Позицію прогріву і відкривають, і закривають — обидві ноги market,
    тобто тейкерські. Одна нога в моделі занизила б витрати вдвічі."""
    from src.execution.soft_start_budget import futures_round_trip_cost
    free = futures_round_trip_cost(1000.0, fee_frac=0.0)
    paid = futures_round_trip_cost(1000.0, fee_frac=0.0002)
    assert abs((paid - free) - 2 * 1000.0 * 0.0002) < 1e-9


def test_zero_fee_costs_exactly_what_it_did_before():
    """Акаунт із промо не має подорожчати від цієї зміни ані на цент."""
    from src.execution.soft_start_budget import futures_round_trip_cost
    assert (futures_round_trip_cost(1000.0)
            == futures_round_trip_cost(1000.0, fee_frac=0.0))


def test_spot_order_cost_adds_the_fee_to_the_crossing():
    from src.execution.soft_start_budget import spot_order_cost
    assert abs(spot_order_cost(100.0, 0.002, 0.0005) - (0.2 + 0.05)) < 1e-9
    # Нуль комісії = стара поведінка.
    assert spot_order_cost(100.0, 0.002) == spot_order_cost(100.0, 0.002, 0.0)


@pytest.mark.asyncio
async def test_an_unreadable_fee_is_never_treated_as_cheap(tmp_path, live):
    """НЕВІДОМА ставка ≠ безкоштовна і ≠ дешева.

    Це не те саме, що «ставка відома і ненульова»: без числа не порахувати
    бюджет. Гілка лишається fail-closed навіть після того, як платні пари
    дозволили."""
    ss, cl, _ = mk(tmp_path, {"BTCUSDT": None}, dry_run=False)
    sym, fee = await ss.pick_pair()
    assert sym is None and fee == 0.0
    assert await ss.open_position() is False
    assert cl.opened == []


@pytest.mark.asyncio
async def test_fee_survives_a_restart_before_adoption(tmp_path, live):
    """Якщо відповідь загубилась і позицію адоптують уже після рестарту,
    ставку заново прочитати нізвідки — тому вона персиститься в `pending`.
    Без цього комісія списалась би як НУЛЬ саме на платному акаунті."""
    import src.execution.futures_soft_start as fss

    seen = []
    orig = fss.save_state

    def _spy(path, state):
        if getattr(state, "pending", None):
            seen.append(dict(state.pending))
        return orig(path, state)

    ss, cl, _ = mk(tmp_path, {"BTCUSDT": (0, 0.0002)}, dry_run=False)
    monkey = fss.save_state
    fss.save_state = _spy
    try:
        assert await ss.open_position() is True
    finally:
        fss.save_state = monkey

    assert seen, "pending не персистився ПЕРЕД відправкою"
    assert seen[0].get("fee_frac") == 0.0002, (
        f"ставка не збережена в pending: {seen[0]} — після рестарту адопція "
        f"спише комісію як НУЛЬ саме на платному акаунті")


@pytest.mark.asyncio
async def test_the_budget_gate_prices_the_fee_too(tmp_path, live):
    """Стеля перевіряється ДО відправки — і мусить бачити комісію.

    Мутант, що прибрав `fee_frac` саме з `can_afford` (а не з списання),
    проходив зеленим: ордер, який пробиває стелю, усе одно йшов, а стеля
    дізнавалась про це заднім числом. Тест ставить бюджет РІВНО між
    безкоштовною і платною вартістю.
    """
    from src.execution.soft_start_budget import (SoftStartBudget,
                                                 futures_round_trip_cost)
    # Ноціонал беремо з самого сайзера, а не з відкритої позиції: у dry-run
    # позиція не персиститься, тож `state.position` там None.
    probe, _, _ = mk(tmp_path, {"BTCUSDT": (0, 0.0002)}, dry_run=True)
    _v, _m, notional = probe._size_position("BTCUSDT", 10, 0.0002)
    assert notional and notional > 0

    free = futures_round_trip_cost(notional, fee_frac=0.0)
    paid = futures_round_trip_cost(notional, fee_frac=0.0002)
    assert paid > free, "фікстура безглузда — комісія нічого не змінює"

    # Стеля вміщає безкоштовний round-trip, але НЕ платний.
    ceiling = (free + paid) / 2
    bud = SoftStartBudget(str(tmp_path / "tight.json"), max_usdt=ceiling)
    ss, cl, _ = mk(tmp_path, {"BTCUSDT": (0, 0.0002)}, dry_run=False, budget=bud)
    assert await ss.open_position() is False, \
        "гейт не побачив комісію — ордер пробив стелю витрат"
    assert cl.opened == []

    # Контроль: та сама стеля з БЕЗКОШТОВНОЮ парою пропускає.
    bud2 = SoftStartBudget(str(tmp_path / "tight2.json"), max_usdt=ceiling)
    ss2, cl2, _ = mk(tmp_path, {"HYPEUSDT": (0, 0)}, dry_run=False, budget=bud2)
    assert await ss2.open_position() is True
    assert cl2.opened


# ---- рандомізація РОЗМІРУ (2026-08-26) ------------------------------------

def test_margin_is_a_spread_not_a_constant():
    """Було рівно 10% балансу ЩОРАЗУ — тобто всі прогрівні позиції мали
    однакову маржу з точністю до копійок. Це прибитий підпис."""
    import random
    from src.execution.soft_start_budget import (futures_target_margin,
                                                 MARGIN_FRAC_MIN, MARGIN_FRAC_MAX)
    r = random.Random(5)
    vals = {futures_target_margin(26.13, r) for _ in range(50)}
    assert len(vals) > 10, f"маржа майже не гуляє: {sorted(vals)}"
    assert all(26.13 * MARGIN_FRAC_MIN - 0.01 <= v <= 26.13 * MARGIN_FRAC_MAX + 0.01
               for v in vals), sorted(vals)


def test_margin_without_rng_stays_the_old_deterministic_value():
    """Старі виклики й тести не мають поїхати."""
    from src.execution.soft_start_budget import futures_target_margin
    assert futures_target_margin(25.0) == 2.5


def test_order_size_never_leaves_the_band():
    import random
    from src.execution.soft_start_budget import human_order_usdt
    r = random.Random(3)
    for price in (2.66, 3.7e-6, 180.0, None):
        for _ in range(300):
            v = human_order_usdt(r, 1.5, 3.0, price)
            assert 1.5 <= v <= 3.0, (price, v)


def test_order_size_has_a_heavy_tail_not_a_flat_one():
    """Рівномірний розподіл сам по собі підпис: сума ніколи не буває ані
    частіше дрібною, ані зрідка помітною. Логарифмічний дає важкий хвіст."""
    import random
    from src.execution.soft_start_budget import human_order_usdt
    r = random.Random(3)
    vals = [human_order_usdt(r, 1.5, 6.0, None) for _ in range(2000)]
    low = sum(1 for v in vals if v < 2.5)
    high = sum(1 for v in vals if v > 4.5)
    assert low > high * 1.5, f"хвіст не важкий: low={low} high={high}"


def test_a_narrow_band_does_not_collapse_the_size_to_one_number():
    """ПАСТКА, В ЯКУ Я ВЛІЗ І ВИЛІЗ.

    Перша версія прив'язки до рівної кількості монет брала ПЕРШИЙ крок, що
    влазить у діапазон. На MX ($2.66) у смугу 1.5-3.0 проходить рівно одна
    рівна кількість — «1 монета», — і сума почала повторюватись у 8 випадках
    із 20. Повторюване однакове число ЯСКРАВІШЕ за будь-який нерівний розкид,
    тобто «фікс» робив гірше, ніж було.
    """
    import random
    from collections import Counter
    from src.execution.soft_start_budget import human_order_usdt

    # КІЛЬКА ПОСІВІВ, не один: на одному посіві межа стояла за 3 спостереження
    # від провалу (63 проти 60), тобто тест був би флакучим і «зеленів» би на
    # зламаному коді від зміни посіву. Міряємо СЕРЕДНЮ частку модального
    # значення. Виміряно: полагоджена версія ~16%, зламана ~32%.
    shares, uniques = [], []
    for seed in (11, 12, 13, 14, 15):
        r = random.Random(seed)
        vals = [human_order_usdt(r, 1.5, 3.0, 2.66) for _ in range(200)]
        shares.append(Counter(vals).most_common(1)[0][1] / len(vals))
        uniques.append(len(set(vals)))
    avg = sum(shares) / len(shares)
    assert avg <= 0.24, (
        f"модальне значення займає {avg:.0%} — прив'язка вироджується в "
        f"константу (частки по посівах: {[f'{x:.0%}' for x in shares]})")
    assert min(uniques) >= 40, f"мало унікальних сум: {uniques}"


def test_round_quantities_do_appear_when_the_band_allows():
    """Біржа бачить КІЛЬКІСТЬ, не долари. Людина частіше купує 1/5/10 монет."""
    import random
    from src.execution.soft_start_budget import human_order_usdt
    r = random.Random(2)
    price = 0.25
    qtys = [round(human_order_usdt(r, 1.5, 6.0, price) / price, 6)
            for _ in range(200)]
    round_ones = sum(1 for q in qtys if abs(q - round(q)) < 1e-6)
    assert round_ones >= 10, f"рівних кількостей майже немає: {round_ones}/200"
    assert round_ones <= 150, "рівні кількості СУЦІЛЬНО — це такий самий підпис"
