"""Two live slots are two MEXC accounts and must trade a signal independently.

Before this, a signal produced at most ONE position across all slots even on the
same pair: the cooldown, the max-positions check and the in-flight guard were all
keyed by SYMBOL, and the slot loop broke after the first attempt. Measured
consequence on 27.07: slot 2 only ever traded what slot 1 happened to skip, and
went to zero the moment slot 1 stopped being held by its soft start.

These tests pin the guarantee: every live slot gets its own pass, and one
account's state never gates another's.
"""
from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.strategy.shadow_engine import ShadowEngine
from src.strategy.shadow_position import ShadowPosition

SYM = "1000PEPEUSDT"


def _engine(slots: list[int] | None, *, in_live: bool = True) -> ShadowEngine:
    """Minimal engine wired for the entry-gate path only."""
    eng = ShadowEngine.__new__(ShadowEngine)
    eng.cfg = SimpleNamespace(enabled=True)
    eng.signals_received = 0
    eng.signals_fanned_out = 0
    eng.signals_skipped_not_tradeable = 0
    eng.signals_skipped_momentum = 0
    eng.signals_skipped_lag_out_of_range = 0
    eng.signals_skipped_cooldown = 0
    eng.signals_skipped_funding = 0
    eng.signals_skipped_max_positions = 0
    eng.signals_skipped_pending_submit = 0
    eng._cooldown_until = {}
    eng._pending_submissions = set()
    eng._open_positions = defaultdict(list)
    eng.max_positions_per_symbol = 1
    eng.funding_guard = SimpleNamespace(is_too_close_for_entry=lambda: False)
    eng.state_manager = SimpleNamespace(
        is_tradeable=lambda s: True,
        is_in_live=lambda s: in_live,
    )
    eng.live_pool = (None if slots is None
                     else SimpleNamespace(find_slots_for_pair=lambda s: list(slots)))
    eng._momentum_ok = lambda *a, **k: True
    eng._get_pair_config = AsyncMock(return_value=SimpleNamespace(min_mexc_lag_pct=0))
    eng._try_enter = AsyncMock()
    return eng


def _sig():
    return SimpleNamespace(symbol=SYM, direction="long")


def _pos(label: str | None) -> ShadowPosition:
    p = ShadowPosition(
        symbol=SYM, direction="long", detector_source="static_gap",
        confidence=0.5, leverage=50, margin_usdt=5.0, notional_usdt=250.0,
        entry_price=0.003, entry_target_price=0.003,
    )
    p.account_label = label
    return p


# ── fan-out ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_signal_reaches_every_live_slot():
    eng = _engine([1, 2])
    eng._enter_for_slot = AsyncMock()
    await eng.on_signal(_sig())
    assert sorted(c.args[3] for c in eng._enter_for_slot.await_args_list) == [1, 2]


@pytest.mark.asyncio
async def test_one_slot_raising_does_not_cancel_the_other():
    eng = _engine([1, 2])

    async def boom(signal, signal_id, cfg, pin_slot):
        if pin_slot == 1:
            raise RuntimeError("slot 1 exploded")

    eng._enter_for_slot = AsyncMock(side_effect=boom)
    await eng.on_signal(_sig())          # must not propagate
    assert sorted(c.args[3] for c in eng._enter_for_slot.await_args_list) == [1, 2]


def test_shadow_pair_stays_a_single_unpinned_pass():
    assert _engine(None)._entry_slots(SYM) == [None]
    assert _engine([1, 2], in_live=False)._entry_slots(SYM) == [None]
    assert _engine([])._entry_slots(SYM) == [None]


# ── the three gates are per account ───────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_of_one_account_leaves_the_other_free():
    eng = _engine([1, 2])
    eng._cooldown_until[(1, SYM)] = 2 ** 31          # slot 1 paused for ever
    await eng.on_signal(_sig())
    assert [c.args[3] for c in eng._try_enter.await_args_list] == [2]
    assert eng.signals_skipped_cooldown == 1


@pytest.mark.asyncio
async def test_open_position_on_one_account_leaves_the_other_free():
    eng = _engine([1, 2])
    eng._open_positions[SYM].append(_pos("slot1"))    # slot 1 already in a trade
    await eng.on_signal(_sig())
    assert [c.args[3] for c in eng._try_enter.await_args_list] == [2]
    assert eng.signals_skipped_max_positions == 1


@pytest.mark.asyncio
async def test_in_flight_submit_of_one_account_leaves_the_other_free():
    eng = _engine([1, 2])
    eng._pending_submissions.add((1, SYM))            # slot 1 mid-submit
    await eng.on_signal(_sig())
    assert [c.args[3] for c in eng._try_enter.await_args_list] == [2]
    assert eng.signals_skipped_pending_submit == 1


@pytest.mark.asyncio
async def test_each_account_still_holds_at_most_one_position():
    """Independence must not become "unlimited positions per account"."""
    eng = _engine([1, 2])
    eng._open_positions[SYM].extend([_pos("slot1"), _pos("slot2")])
    await eng.on_signal(_sig())
    assert eng._try_enter.await_count == 0
    assert eng.signals_skipped_max_positions == 2


@pytest.mark.asyncio
async def test_in_flight_key_is_released_for_its_own_slot():
    eng = _engine([1, 2])
    await eng.on_signal(_sig())
    assert eng._pending_submissions == set()


# ── the funnel counts slot passes, not signals ────────────────────────

@pytest.mark.asyncio
async def test_slot_passes_counted_once_per_slot():
    """passed_pre went negative because per-slot drops were subtracted from a
    per-signal total. The gates now have their own denominator."""
    eng = _engine([1, 2])
    await eng.on_signal(_sig())
    assert eng.signals_received == 1
    assert eng.signals_fanned_out == 2


@pytest.mark.asyncio
async def test_slot_passes_counted_for_a_shadow_pair_too():
    eng = _engine(None)
    await eng.on_signal(_sig())
    assert eng.signals_fanned_out == 1


@pytest.mark.asyncio
async def test_gate_skips_never_exceed_slot_passes():
    eng = _engine([1, 2])
    eng._cooldown_until[(1, SYM)] = 2 ** 31
    eng._pending_submissions.add((2, SYM))
    await eng.on_signal(_sig())
    drops = (eng.signals_skipped_cooldown + eng.signals_skipped_funding
             + eng.signals_skipped_max_positions
             + eng.signals_skipped_pending_submit)
    assert drops <= eng.signals_fanned_out


# ── one open alert per position actually opened ───────────────────────

@pytest.mark.asyncio
async def test_open_position_returns_what_it_created():
    """The alert wrapper announces the RETURNED position.

    Reading _open_positions[symbol][-1] announced the previous, still-open
    position whenever a call opened nothing (clone, 28.07 08:20: two identical
    [LIVE OPEN] messages for one trade).
    """
    from src.strategy.shadow_engine import ShadowEngine
    import inspect

    src = inspect.getsource(ShadowEngine._open_position)
    assert "return pos" in src, "_open_position must return the position it created"
    # Every early exit must yield None so the wrapper stays silent.
    assert src.count("return pos") == 1
