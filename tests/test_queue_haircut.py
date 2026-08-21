"""T1.2 — the simulator must not take the whole level as if we were alone.

It walked the ladder assuming every displayed contract was ours. In reality a
queue sits on that level and an IOC only sweeps the tail. Measured 2026-08-21:
live PEPE fills are 46% partial, shadow's are 5.7%.

The important property, and the reason this is the right knob after
`mexc_feed_lag_ms` failed: the haircut changes the SIZE of the fill, never the
FACT of it — the fact is decided by `min(_asks) <= limit`. So it degrades
smoothly, while the feed-lag knob turned out to be an on/off switch (48ms and
116ms produced the same result).

Ships at 1.0 = disabled. Calibrate from `shadow_twin` rows, not by guessing.
"""
from __future__ import annotations

import pytest

from src.exchanges.orderbook import OrderBook
from src.execution.ioc_executor import IOCExecutor


def book(levels_bid, levels_ask):
    ob = OrderBook(symbol="TESTUSDT", exchange="mexc", max_levels=20)
    ob.apply_snapshot(levels_bid, levels_ask, update_id=1)
    return ob


def buy(queue_frac, notional=1000.0, size=100.0):
    """One ask level of `size` contracts at price 10 → $1000 of displayed depth."""
    ob = book([(9.9, size)], [(10.0, size)])
    return IOCExecutor().simulate_ioc_entry(
        mexc_ob=ob, direction="long", notional_usdt=notional,
        limit_price=10.0, queue_frac=queue_frac,
    )


def sell(queue_frac, notional=1000.0, size=100.0):
    ob = book([(10.0, size)], [(10.1, size)])
    return IOCExecutor().simulate_ioc_entry(
        mexc_ob=ob, direction="short", notional_usdt=notional,
        limit_price=10.0, queue_frac=queue_frac,
    )


# ---- disabled by default -------------------------------------------------

def test_default_takes_the_whole_level_unchanged():
    r = buy(1.0)
    assert r.status == "filled"
    assert r.filled_pct == pytest.approx(1.0)


def test_omitting_the_argument_is_the_same_as_1_0():
    """Shipping this must not move a single existing number."""
    ob = book([(9.9, 100.0)], [(10.0, 100.0)])
    a = IOCExecutor().simulate_ioc_entry(mexc_ob=ob, direction="long",
                                         notional_usdt=1000.0, limit_price=10.0)
    b = buy(1.0)
    assert a.filled_pct == b.filled_pct and a.avg_fill_price == b.avg_fill_price


# ---- the haircut cuts SIZE, not the fact of a fill -----------------------

@pytest.mark.parametrize("frac,expect", [(1.0, 1.0), (0.5, 0.5), (0.25, 0.25)])
def test_fill_share_scales_with_queue_frac(frac, expect):
    r = buy(frac)
    assert r.filled_pct == pytest.approx(expect, abs=0.01)


def test_a_haircut_turns_a_full_fill_into_a_partial():
    """This is the whole point: live is 46% partial, shadow 5.7%."""
    assert buy(1.0).status == "filled"
    assert buy(0.5).status == "partial"


def test_it_never_turns_a_fill_into_an_expiry():
    """The FACT of a fill is decided by the limit, not by the queue. If a
    haircut could expire an order it would be another on/off switch, which is
    exactly how mexc_feed_lag_ms failed."""
    for frac in (1.0, 0.5, 0.1, 0.01):
        assert buy(frac).status in ("filled", "partial"), frac


def test_price_is_unaffected_when_one_level_is_enough():
    assert buy(0.5).avg_fill_price == pytest.approx(buy(1.0).avg_fill_price)


def test_deeper_levels_are_reached_when_the_top_is_thinned():
    """Less available on top → the walk goes deeper → worse average price.
    That is the realistic consequence of standing behind a queue."""
    ob = book([(9.9, 50.0)], [(10.0, 50.0), (10.1, 50.0)])
    ex = IOCExecutor()
    full = ex.simulate_ioc_entry(mexc_ob=ob, direction="long",
                                 notional_usdt=600.0, limit_price=10.1,
                                 queue_frac=1.0)
    thin = ex.simulate_ioc_entry(mexc_ob=ob, direction="long",
                                 notional_usdt=600.0, limit_price=10.1,
                                 queue_frac=0.5)
    assert thin.avg_fill_price >= full.avg_fill_price
    assert thin.filled_pct < full.filled_pct


def test_short_side_is_thinned_too():
    """The sell walk is a separate function — it drifted apart before."""
    assert sell(1.0).filled_pct == pytest.approx(1.0)
    assert sell(0.5).filled_pct == pytest.approx(0.5, abs=0.01)


# ---- wiring --------------------------------------------------------------

def test_config_default_is_one():
    from src.config import ShadowConf
    assert ShadowConf().queue_frac == 1.0


def test_engine_clamps_the_value():
    """0 would mean 'no liquidity at all' and would silently stop shadow
    trading; >1 would invent depth that was never displayed."""
    from pathlib import Path
    src = Path("src/strategy/shadow_engine.py").read_text()
    assert 'min(1.0, max(0.01, float(getattr(cfg, "queue_frac", 1.0))))' in src


def test_engine_passes_it_to_both_the_simulator_and_the_twin():
    """If the twin used a different model, the pairwise comparison would be
    measuring our own inconsistency instead of the exchange."""
    from pathlib import Path
    src = Path("src/strategy/shadow_engine.py").read_text()
    assert src.count("queue_frac=self._queue_frac") == 2
