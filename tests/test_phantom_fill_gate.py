"""Don't stack orders on top of an IOC whose fate the exchange never told us.

2026-08-21 12:03 — FIVE phantom fills on SOXL_USDT in 34 seconds. Each time we
declared `ioc_expired_no_fill`, kept firing new orders, and the phantom guard
had to flatten a real position we did not know we held. Collateral damage:
`api_error_2021 leverage is inconsistent with the existing position`.

The log separates two outcomes cleanly, and only one of them is dangerous:

  * `via=ws_expired` — MEXC answered: terminal state, dealVol=0. 211 of these,
    ZERO phantoms. Fast (~150ms). Must keep its full speed.
  * no `[FILL SRC]` at all — WS silent, REST poll timed out (~950ms). 28 of
    these, and 5 were REAL fills. This is the entire phantom population.

So the gate applies to the second class only: block new opens on that symbol
until the phantom re-check answers. Bounded by the check's last window (~12s).
"""
from __future__ import annotations

import asyncio

import pytest

from src.execution.live_executor import LiveExecutor


def ex(unknown=()):
    e = object.__new__(LiveExecutor)
    e._halted = False
    e._phantom_unknown = set(unknown)
    e._phantom_tasks = set()
    e.opens_attempted = 0
    e.opens_failed = 0
    e.last_error = ""
    e.slot_id = 2
    return e


def _open(e, symbol="SOXL_USDT", direction="long"):
    return asyncio.run(e.place_ioc_open(
        symbol=symbol, direction=direction, notional_usdt=100.0,
        leverage=50, mexc_ob=None,
    ))


def test_blocked_while_the_previous_outcome_is_unknown():
    e = ex(unknown={"SOXL_USDT"})
    r = _open(e)
    assert r.success is False
    assert "phantom_check_pending" in (r.error_msg or "")
    assert e.last_error == "phantom_check_pending"


def test_a_different_symbol_is_not_blocked():
    """One symbol's unknown outcome must not halt the whole slot."""
    e = ex(unknown={"SOXL_USDT"})
    r = _open(e, symbol="PEPE_USDT")
    # gets past the phantom gate and fails later, on the missing orderbook
    assert "phantom_check_pending" not in (r.error_msg or "")


def test_clean_symbol_passes_the_gate():
    e = ex()
    r = _open(e)
    assert "phantom_check_pending" not in (r.error_msg or "")


def test_the_gate_counts_as_a_failed_open():
    e = ex(unknown={"SOXL_USDT"})
    _open(e)
    assert e.opens_failed == 1, "a refused open must be visible in the stats"


def test_block_is_released_when_the_check_finishes():
    """Whatever the check concludes, the symbol must trade again."""
    e = ex()

    async def scenario():
        async def noop(*_a, **_kw):
            return None
        e._phantom_open_check = noop
        e._phantom_unknown.add("SOXL_USDT")
        e._schedule_phantom_check("1", "SOXL_USDT", "long", 50, unknown=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return "SOXL_USDT" in e._phantom_unknown

    assert asyncio.run(scenario()) is False


def test_block_is_released_even_if_the_check_itself_raises():
    """A crash inside the check must not park the symbol forever."""
    e = ex()

    async def scenario():
        async def boom(*_a, **_kw):
            raise RuntimeError("deal-check exploded")
        e._phantom_open_check = boom
        e._phantom_unknown.add("SOXL_USDT")
        e._schedule_phantom_check("1", "SOXL_USDT", "long", 50, unknown=True)
        for _ in range(5):
            await asyncio.sleep(0)
        return "SOXL_USDT" in e._phantom_unknown

    assert asyncio.run(scenario()) is False


def test_block_is_released_if_scheduling_fails_outright():
    e = ex()
    e._phantom_unknown.add("SOXL_USDT")
    e._phantom_open_check = None          # create_task will raise
    e._schedule_phantom_check("1", "SOXL_USDT", "long", 50, unknown=True)
    assert "SOXL_USDT" not in e._phantom_unknown


def test_confirmed_expiry_does_not_arm_the_gate():
    """88% of expiries are exchange-confirmed; gating them would gut throughput."""
    e = ex()

    async def scenario():
        async def noop(*_a, **_kw):
            return None
        e._phantom_open_check = noop
        e._schedule_phantom_check("1", "SOXL_USDT", "long", 50, unknown=False)
        await asyncio.sleep(0)
        return "SOXL_USDT" in e._phantom_unknown

    assert asyncio.run(scenario()) is False


def test_unknown_classification_requires_the_rest_path_and_no_fill():
    """`_outcome_unknown` must be true ONLY for rest-poll-with-no-fill."""
    from pathlib import Path
    src = Path("src/execution/live_executor.py").read_text()
    assert '_outcome_unknown = (_fill_via == "rest"' in src
    assert "not (fill_price_scaled > 0 and real_filled_vol > 0)" in src
