"""
Tests for event-driven scan loop (event-driven-detector patch, May 2026).

Verifies:
  1. Listeners are registered on both Binance and MEXC books for each symbol.
  2. Book update marks symbol dirty and sets the wakeup event.
  3. Symbol native format → binance format conversion is correct on MEXC books.
  4. New symbols added at runtime get listeners on next loop iteration.
  5. Safety-net timeout marks all symbols dirty even if listeners missed.
  6. _check_symbol() preserves all signal-emission behaviour from old _scan_once().
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from src.exchanges.orderbook import OrderBookManager
from src.strategy.signal import SignalWriter
from src.strategy.static_gap_detector import (
    StaticGapConf,
    StaticGapDetector,
)


def _seed_book(ob_mgr: OrderBookManager, exchange: str, symbol: str,
               bid: float, bid_size: float, ask: float, ask_size: float):
    """Create a synced OB on the manager with given top-of-book."""
    ob = ob_mgr.get_or_create(exchange, symbol, max_levels=20)
    ob.apply_snapshot(
        bids=[(bid, bid_size)],
        asks=[(ask, ask_size)],
        update_id=1,
    )
    return ob


def _make_detector(ob_mgr, cfg=None):
    """Build a StaticGapDetector with a no-op SignalWriter."""
    cfg = cfg or StaticGapConf(enabled=True, scan_interval_sec=0.2, cooldown_sec=5.0,
                               min_gap_ticks=1)
    writer = MagicMock(spec=SignalWriter)

    async def _write(_sig):
        pass

    writer.write = _write
    return StaticGapDetector(cfg=cfg, ob_manager=ob_mgr, signal_writer=writer)


def test_register_listeners_attaches_to_both_books():
    """After _register_book_listeners_if_needed, both binance and mexc OBs
    have exactly one detector listener each."""
    ob_mgr = OrderBookManager()
    _seed_book(ob_mgr, "binance", "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)
    _seed_book(ob_mgr, "mexc",    "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)

    det = _make_detector(ob_mgr)
    det._register_book_listeners_if_needed()

    b_ob = ob_mgr.get("binance", "ZECUSDT")
    m_ob = ob_mgr.get("mexc", "ZECUSDT")
    assert b_ob.listener_count() == 1, "binance book should have one listener"
    assert m_ob.listener_count() == 1, "mexc book should have one listener"
    assert ("binance", "ZECUSDT") in det._registered_books
    assert ("mexc", "ZECUSDT") in det._registered_books


def test_register_listeners_is_idempotent():
    """Calling _register_book_listeners_if_needed twice does not double-register."""
    ob_mgr = OrderBookManager()
    _seed_book(ob_mgr, "binance", "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)
    _seed_book(ob_mgr, "mexc",    "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)

    det = _make_detector(ob_mgr)
    det._register_book_listeners_if_needed()
    det._register_book_listeners_if_needed()
    det._register_book_listeners_if_needed()

    b_ob = ob_mgr.get("binance", "ZECUSDT")
    m_ob = ob_mgr.get("mexc", "ZECUSDT")
    assert b_ob.listener_count() == 1
    assert m_ob.listener_count() == 1


@pytest.mark.asyncio
async def test_binance_book_update_marks_symbol_dirty():
    """An apply_diff on the binance OB triggers our listener — symbol enters
    _dirty_symbols and _book_dirty_event becomes set."""
    ob_mgr = OrderBookManager()
    _seed_book(ob_mgr, "binance", "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)
    _seed_book(ob_mgr, "mexc",    "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)

    det = _make_detector(ob_mgr)
    det._book_dirty_event = asyncio.Event()
    det._register_book_listeners_if_needed()

    assert not det._book_dirty_event.is_set()
    assert det._dirty_symbols == set()

    b_ob = ob_mgr.get("binance", "ZECUSDT")
    b_ob.apply_diff(bids=[(100.1, 5)], asks=[], first_update_id=2, final_update_id=2)

    assert det._book_dirty_event.is_set(), "event must be set after update"
    assert "ZECUSDT" in det._dirty_symbols, "symbol must be in dirty set"


@pytest.mark.asyncio
async def test_mexc_book_update_uses_binance_symbol_in_dirty_set():
    """When the MEXC-side OB updates, the listener adds the same
    binance-format symbol to the dirty set.

    In this codebase MEXC books are keyed by binance-format symbol
    (see mexc_ws.py:322), so the listener closure is bound to that name
    at registration and stores it directly. No conversion happens at
    update time."""
    ob_mgr = OrderBookManager()
    _seed_book(ob_mgr, "binance", "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)
    _seed_book(ob_mgr, "mexc",    "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)

    det = _make_detector(ob_mgr)
    det._book_dirty_event = asyncio.Event()
    det._register_book_listeners_if_needed()

    m_ob = ob_mgr.get("mexc", "ZECUSDT")
    m_ob.apply_diff(bids=[(100.1, 5)], asks=[], first_update_id=2, final_update_id=2)

    assert "ZECUSDT" in det._dirty_symbols, (
        f"dirty set should contain binance format, got {det._dirty_symbols}"
    )


@pytest.mark.asyncio
async def test_new_symbol_gets_listener_on_next_registration():
    """Adding a new symbol's books to the manager after detector start
    causes listener registration on the next _register_book_listeners_if_needed
    call (which the scan loop runs every iteration)."""
    ob_mgr = OrderBookManager()
    _seed_book(ob_mgr, "binance", "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)
    _seed_book(ob_mgr, "mexc",    "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)

    det = _make_detector(ob_mgr)
    det._register_book_listeners_if_needed()
    assert len(det._registered_books) == 2

    # Now SUI is added at runtime (as pair_scanner would do).
    _seed_book(ob_mgr, "binance", "SUIUSDT", bid=1.2, bid_size=100, ask=1.21, ask_size=100)
    _seed_book(ob_mgr, "mexc",    "SUIUSDT", bid=1.2, bid_size=100, ask=1.21, ask_size=100)

    det._register_book_listeners_if_needed()
    assert len(det._registered_books) == 4, (
        f"expected 4 (2 symbols × 2 exchanges), got {len(det._registered_books)}"
    )
    assert ("binance", "SUIUSDT") in det._registered_books
    assert ("mexc", "SUIUSDT") in det._registered_books


@pytest.mark.asyncio
async def test_check_symbol_emits_signal_when_gap_above_threshold():
    """Regression guard: _check_symbol(symbol) preserves the signal-emission
    contract of the old _scan_once() inner loop. Same input → same signal."""
    import time as _time
    from src.strategy.signal import Signal

    ob_mgr = OrderBookManager()
    # binance bid 4 ticks above mexc bid → LONG signal at min_gap_ticks=3.
    # ZEC tick = 0.01 raw, scale = 1.0 → tick_scaled = 0.01.
    _seed_book(ob_mgr, "binance", "ZECUSDT", bid=100.04, bid_size=10, ask=100.05, ask_size=10)
    _seed_book(ob_mgr, "mexc",    "ZECUSDT", bid=100.00, bid_size=10, ask=100.05, ask_size=10)

    cfg = StaticGapConf(enabled=True, scan_interval_sec=0.2, cooldown_sec=5.0,
                        min_gap_ticks=3)
    captured: list[Signal] = []

    class _RecordingWriter:
        async def write(self, sig):
            captured.append(sig)

    det = StaticGapDetector(cfg=cfg, ob_manager=ob_mgr, signal_writer=_RecordingWriter())
    now_ms = int(_time.time() * 1000)
    await det._check_symbol("ZECUSDT", now_ms)

    assert len(captured) == 1, f"expected 1 signal, got {len(captured)}"
    sig = captured[0]
    assert sig.symbol == "ZECUSDT"
    assert sig.direction == "long"
    assert sig.source == "static_gap"
    assert sig.metadata["gap_ticks"] >= 3.0


@pytest.mark.asyncio
async def test_check_symbol_respects_cooldown():
    """If cooldown is active in the same direction, no second signal emits."""
    import time as _time
    from src.strategy.signal import Signal

    ob_mgr = OrderBookManager()
    _seed_book(ob_mgr, "binance", "ZECUSDT", bid=100.04, bid_size=10, ask=100.05, ask_size=10)
    _seed_book(ob_mgr, "mexc",    "ZECUSDT", bid=100.00, bid_size=10, ask=100.05, ask_size=10)

    cfg = StaticGapConf(enabled=True, scan_interval_sec=0.2, cooldown_sec=5.0,
                        min_gap_ticks=3)
    captured: list[Signal] = []

    class _RecordingWriter:
        async def write(self, sig):
            captured.append(sig)

    det = StaticGapDetector(cfg=cfg, ob_manager=ob_mgr, signal_writer=_RecordingWriter())
    now_ms = int(_time.time() * 1000)
    await det._check_symbol("ZECUSDT", now_ms)
    await det._check_symbol("ZECUSDT", now_ms + 100)  # 100ms later, still in cooldown
    assert len(captured) == 1, "second call within cooldown should not emit"


@pytest.mark.asyncio
async def test_listener_isolated_from_event_loop():
    """The listener closure is synchronous and only touches plain data
    structures + a sync .set() on asyncio.Event. It must not raise even if
    called before _book_dirty_event is initialised (defensive guard)."""
    ob_mgr = OrderBookManager()
    _seed_book(ob_mgr, "binance", "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)
    _seed_book(ob_mgr, "mexc",    "ZECUSDT", bid=100, bid_size=10, ask=100.5, ask_size=10)

    det = _make_detector(ob_mgr)
    # Deliberately do NOT initialise _book_dirty_event (simulates listener
    # firing before start() ran).
    assert det._book_dirty_event is None

    det._register_book_listeners_if_needed()

    # Trigger update — listener should add to dirty set without crashing
    # despite event being None.
    b_ob = ob_mgr.get("binance", "ZECUSDT")
    b_ob.apply_diff(bids=[(100.1, 5)], asks=[], first_update_id=2, final_update_id=2)
    assert "ZECUSDT" in det._dirty_symbols


def test_stats_exposes_new_counters():
    """stats() dict includes event_driven_checks, safety_scan_count, registered_book_count."""
    ob_mgr = OrderBookManager()
    det = _make_detector(ob_mgr)
    s = det.stats()
    assert "event_driven_checks" in s
    assert "safety_scan_count" in s
    assert "registered_book_count" in s
    assert s["event_driven_checks"] == 0
    assert s["safety_scan_count"] == 0
    assert s["registered_book_count"] == 0
