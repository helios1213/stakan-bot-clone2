"""T1 of the shadow-realism plan: stop shadow filling on liquidity it cannot have.

The mechanism being corrected: shadow anchors its IOC limit against our WS
reconstruction of the MEXC book AND judges the fill against that same
reconstruction. Whatever lag the feed has, both sides carry it, so it cancels
out — and shadow fills on levels the exchange had already taken. Measured
2026-08-21: 1.55-1.63x more fills per attempt than live on the same pairs.

Two knobs, BOTH defaulting to 0 (behaviour unchanged) because neither is
calibrated until [BOOKLAG] data exists:
  * mexc_feed_lag_ms — wait the feed lag out before walking the ladder (T1.1)
  * max_book_age_ms  — refuse to fill against a book that stopped ticking (T1.3)
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from src.exchanges.orderbook import OrderBook
from src.execution.ioc_executor import IOCExecutor

ENGINE = Path("src/strategy/shadow_engine.py").read_text()


def book(bid=100.0, ask=100.1, size=1000.0, age_ms=0):
    ob = OrderBook(symbol="TESTUSDT", exchange="mexc", max_levels=20)
    ob.apply_snapshot([(bid, size)], [(ask, size)], update_id=1)
    ob.last_update_ts_ms = int(time.time() * 1000) - age_ms
    return ob


# ---- T1.3: a synced book is not necessarily a fresh one ------------------

def test_stale_book_expires_instead_of_inventing_a_fill():
    ob = book(age_ms=5_000)
    r = IOCExecutor().simulate_ioc_entry(
        mexc_ob=ob, direction="long", notional_usdt=100.0,
        limit_price=100.5, max_book_age_ms=500,
    )
    assert r.status == "expired"
    assert r.expired_reason == "orderbook_stale"


def test_fresh_book_still_fills():
    ob = book(age_ms=50)
    r = IOCExecutor().simulate_ioc_entry(
        mexc_ob=ob, direction="long", notional_usdt=100.0,
        limit_price=100.5, max_book_age_ms=500,
    )
    assert r.status in ("filled", "partial"), r.expired_reason


def test_disabled_by_default_even_on_an_ancient_book():
    """The knob must be opt-in: shipping it must not silently change results."""
    ob = book(age_ms=60_000)
    r = IOCExecutor().simulate_ioc_entry(
        mexc_ob=ob, direction="long", notional_usdt=100.0, limit_price=100.5,
    )
    assert r.status in ("filled", "partial"), "default 0 must keep old behaviour"


def test_staleness_checked_for_both_directions():
    ob = book(age_ms=5_000)
    for direction, limit in (("long", 100.5), ("short", 99.5)):
        r = IOCExecutor().simulate_ioc_entry(
            mexc_ob=ob, direction=direction, notional_usdt=100.0,
            limit_price=limit, max_book_age_ms=500,
        )
        assert r.expired_reason == "orderbook_stale", direction


def test_unsynced_still_reports_its_own_reason():
    """Staleness must not swallow the pre-existing not-synced case."""
    ob = book()
    ob.is_synced = False
    r = IOCExecutor().simulate_ioc_entry(
        mexc_ob=ob, direction="long", notional_usdt=100.0,
        limit_price=100.5, max_book_age_ms=500,
    )
    assert r.expired_reason == "orderbook_not_synced"


def test_book_with_no_timestamp_is_not_treated_as_stale():
    ob = book()
    ob.last_update_ts_ms = 0          # never stamped
    r = IOCExecutor().simulate_ioc_entry(
        mexc_ob=ob, direction="long", notional_usdt=100.0,
        limit_price=100.5, max_book_age_ms=500,
    )
    assert r.status in ("filled", "partial"), "0 means unknown, not ancient"


def test_time_is_imported():
    """py_compile passes on a missing import — it fails at runtime, on a fill."""
    src = Path("src/execution/ioc_executor.py").read_text()
    assert re.search(r"^import time$", src, re.M)


# ---- T1.1: the lag wait is shadow-only and off by default ----------------

def test_feed_lag_defaults_to_zero():
    from src.config import ShadowConf
    assert ShadowConf().mexc_feed_lag_ms == 0
    assert ShadowConf().max_book_age_ms == 0


def test_feed_lag_wait_never_runs_for_live_pairs():
    """A sleep on the live path would delay real orders — the exact reason the
    shadow walk was removed from the live fast-path in the first place."""
    i = ENGINE.index("T1.1: undo the tautological")
    seg = ENGINE[i:i + 1200]
    assert "not _is_live_pair" in seg
    assert "asyncio.sleep(self._mexc_feed_lag_ms / 1000)" in seg


def test_feed_lag_refetches_the_book_after_waiting():
    """Waiting and then walking the pre-wait snapshot would be a no-op."""
    i = ENGINE.index("T1.1: undo the tautological")
    seg = ENGINE[i:i + 1200]
    assert 'mexc_ob = self.ob_manager.get("mexc", symbol)' in seg
    assert "_record_shadow_miss" in seg, "a book lost during the wait must be recorded"


def test_stale_guard_is_passed_only_on_the_shadow_walk():
    j = ENGINE.index("max_book_age_ms=self._max_book_age_ms")
    assert ENGINE.rfind("else:", 0, j) > ENGINE.rfind("if _is_live_pair:", 0, j)


# ---- T1.0: the measurement that has to come first ------------------------

def test_booklag_sample_is_emitted_on_live_fills():
    assert "[BOOKLAG]" in ENGINE
    i = ENGINE.index("[BOOKLAG]")
    seg = ENGINE[max(0, i - 1200):i + 500]
    assert "last_update_ts_ms" in seg, "age of our book is the point of the sample"
    assert "touch_gap_bps" in seg, "and how far the real fill sat from our touch"


def test_booklag_cannot_break_a_live_trade():
    i = ENGINE.index("[BOOKLAG]")
    seg = ENGINE[max(0, i - 900):i + 900]
    assert "try:" in seg and "except Exception:" in seg
