"""Tests for latency-fix patches (May 2026).

Three independent fixes:
  1. webkey client: warmup lock fast-path (hot cookies skip the lock)
  2. shadow_engine: skip simulate_ioc_entry for live pairs
  3. binance_ws: bookTicker diagnostic stream + latency comparison
"""
import asyncio
import time
from dataclasses import dataclass
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


# ─── Fix 1: warmup lock fast-path ───────────────────────────────────────

@pytest.mark.asyncio
async def test_warmup_hot_path_skips_lock():
    """When cookies are fresh, warmup() returns without taking the lock."""
    from src.execution.webkey.client import MexcWebClient as WebkeyClient

    # Build a client without going through __init__ network logic.
    c = object.__new__(WebkeyClient)
    c._session = MagicMock()  # session already exists
    c._lock = asyncio.Lock()
    c._warmed_at = time.monotonic()  # fresh
    c.cookie_max_age_sec = 60.0
    # _ensure_session needs to return immediately
    c._ensure_session = AsyncMock(return_value=c._session)

    # Acquire the lock externally — if warmup() incorrectly took it,
    # it would hang. We give it a 100ms ceiling.
    await c._lock.acquire()
    try:
        await asyncio.wait_for(c.warmup(), timeout=0.1)
    finally:
        c._lock.release()


@pytest.mark.asyncio
async def test_stale_warmup_still_never_takes_the_lock_or_the_network():
    """warmup() is a NO-OP since the warmup-removal patch.

    This test used to assert the opposite: that a stale warmup falls through to
    a slow path and contends on the lock. That path is gone — the Akamai cookie
    fetch was proven unnecessary (probe 2026-08-12), so warmup() does nothing.

    What matters now is the guarantee the patch bought us, and it is worth
    pinning: even with cookies arbitrarily stale, warmup() must NOT touch the
    lock and must NOT hit the network. If either ever comes back, the cold-start
    latency it cost us comes back with it.
    """
    from src.execution.webkey.client import MexcWebClient as WebkeyClient

    c = object.__new__(WebkeyClient)
    c._session = MagicMock()
    c._lock = asyncio.Lock()
    c._warmed_at = time.monotonic() - 10_000     # arbitrarily stale
    c.cookie_max_age_sec = 60.0
    c._ensure_session = AsyncMock(return_value=c._session)
    c._session.get = AsyncMock(side_effect=AssertionError("warmup must not hit the network"))

    # Hold the lock: a warmup that wanted it would block here and time out.
    await c._lock.acquire()
    try:
        await asyncio.wait_for(c.warmup(), timeout=0.5)
    finally:
        c._lock.release()

    c._session.get.assert_not_called()

@pytest.mark.asyncio
async def test_simulate_skipped_for_live_pair():
    """For live pairs, simulate_ioc_entry must NOT be called.

    We verify by checking that the IOCExecutor's simulate_ioc_entry mock
    is never invoked when the pair is live, but IS invoked for shadow.
    """
    from src.strategy.shadow_engine import ShadowEngine

    eng = object.__new__(ShadowEngine)

    # Minimal stubs the live fast-path touches.
    eng.ioc_executor = MagicMock()
    eng.ioc_executor.simulate_ioc_entry = MagicMock()
    eng.entries_attempted = 0
    eng.entries_filled = 0
    eng.entries_partial = 0
    eng.entries_expired = 0
    eng.entries_rejected = 0
    eng.signals_skipped_no_book = 0
    eng.realism = MagicMock(signal_to_order_latency_ms=0,
                            ioc_retry_min_ms=10, ioc_retry_max_ms=20)
    eng.state_manager = MagicMock()
    eng.state_manager.is_in_live = MagicMock(return_value=True)
    eng._latency_enabled = False
    # T1.1/T1.3 knobs — 0 = off, matching the shipped defaults. __new__
    # skips __init__, so new engine attributes must be mirrored here.
    eng._mexc_feed_lag_ms = 0
    eng._max_book_age_ms = 0
    eng._queue_frac = 1.0          # T1.2; 1.0 = вимкнено
    eng.ob_manager = MagicMock()

    # Fake MEXC orderbook with best_bid / best_ask
    fake_ob = MagicMock()
    fake_ob.is_synced = True
    fake_ob.best_bid = MagicMock(return_value=MagicMock(price=1.0000))
    fake_ob.best_ask = MagicMock(return_value=MagicMock(price=1.0001))
    eng.ob_manager.get = MagicMock(return_value=fake_ob)

    # Fake live pool — used inside _open_position, which we'll stub out
    # via patching to verify just the simulate-skip logic.
    eng.live_pool = MagicMock()
    eng.live_pool.find_slots_for_pair = MagicMock(return_value=[])

    # Stub _open_position so test stays focused on _try_enter behaviour.
    eng._open_position = AsyncMock()

    # Build a fake signal + pair config.
    @dataclass
    class Sig:
        symbol: str = "SUIUSDT"
        direction: str = "long"
        mexc_price: float = 1.0001
        mexc_lag_pct: float = 0.05

    @dataclass
    class Cfg:
        margin_min_usdt: float = 4.0
        margin_max_usdt: float = 8.0
        leverage_min: int = 50
        leverage_max: int = 70
        ioc_max_attempts: int = 1
        ioc_offset_ticks: int = 0
        ioc_attempt_interval_ms: int = 50

    await eng._try_enter(Sig(), None, Cfg())

    # KEY ASSERTION: for live pair, simulate_ioc_entry never called.
    eng.ioc_executor.simulate_ioc_entry.assert_not_called()
    # And _open_position WAS called (live stub result drove through).
    assert eng._open_position.await_count == 1


@pytest.mark.asyncio
async def test_simulate_called_for_shadow_pair():
    """For shadow pairs, the legacy simulate_ioc_entry path must still run."""
    from src.strategy.shadow_engine import ShadowEngine
    from src.execution.ioc_executor import IOCAttemptResult

    eng = object.__new__(ShadowEngine)
    eng.ioc_executor = MagicMock()
    eng.ioc_executor.simulate_ioc_entry = MagicMock(
        return_value=IOCAttemptResult(
            status="filled", target_price=1.0001,
            avg_fill_price=1.0001, filled_qty=100.0,
            filled_notional_usdt=200.0, filled_pct=1.0,
        )
    )
    eng.entries_attempted = 0
    eng.entries_filled = 0
    eng.entries_partial = 0
    eng.entries_expired = 0
    eng.entries_rejected = 0
    eng.signals_skipped_no_book = 0
    eng.realism = MagicMock(signal_to_order_latency_ms=0,
                            ioc_retry_min_ms=10, ioc_retry_max_ms=20)
    eng.state_manager = MagicMock()
    eng.state_manager.is_in_live = MagicMock(return_value=False)  # SHADOW
    eng._latency_enabled = False
    # T1.1/T1.3 knobs — 0 = off, matching the shipped defaults. __new__
    # skips __init__, so new engine attributes must be mirrored here.
    eng._mexc_feed_lag_ms = 0
    eng._max_book_age_ms = 0
    eng._queue_frac = 1.0          # T1.2; 1.0 = вимкнено
    eng.ob_manager = MagicMock()

    fake_ob = MagicMock()
    fake_ob.is_synced = True
    fake_ob.best_bid = MagicMock(return_value=MagicMock(price=1.0000))
    fake_ob.best_ask = MagicMock(return_value=MagicMock(price=1.0001))
    eng.ob_manager.get = MagicMock(return_value=fake_ob)
    eng.live_pool = None
    eng._open_position = AsyncMock()

    @dataclass
    class Sig:
        symbol: str = "SUIUSDT"
        direction: str = "long"
        mexc_price: float = 1.0001
        mexc_lag_pct: float = 0.05

    @dataclass
    class Cfg:
        margin_min_usdt: float = 4.0
        margin_max_usdt: float = 8.0
        leverage_min: int = 50
        leverage_max: int = 70
        ioc_max_attempts: int = 1
        ioc_offset_ticks: int = 0
        ioc_attempt_interval_ms: int = 50

    # We need realism to short-circuit cleanly without should_reject_order
    # / simulate_server_error infra. Patch them out.
    with patch("src.strategy.shadow_engine.should_reject_order",
               return_value=(False, "")), \
         patch("src.strategy.shadow_engine.simulate_server_error",
               return_value=(False, 0)):
        await eng._try_enter(Sig(), None, Cfg())

    # KEY ASSERTION: for shadow pair, simulate_ioc_entry WAS called.
    assert eng.ioc_executor.simulate_ioc_entry.call_count == 1


# ─── Fix 3: bookTicker diagnostic stream ────────────────────────────────

@pytest.mark.asyncio
async def test_book_ticker_records_observation():
    """_handle_book_ticker_msg records (bid, ask, ts) per symbol."""
    from src.exchanges.binance_ws import BinanceWSClient

    c = object.__new__(BinanceWSClient)
    c._symbols = {"SUIUSDT"}
    c._bt_last = {}
    c.book_ticker_messages = 0

    msg = {
        "stream": "suiusdt@bookTicker",
        "data": {
            "e": "bookTicker", "u": 17, "s": "SUIUSDT",
            "b": "1.2345", "B": "100.0",
            "a": "1.2346", "A": "200.0",
            "T": 1700000000000, "E": 1700000000001,
        }
    }
    await c._handle_book_ticker_msg(msg)

    assert c.book_ticker_messages == 1
    assert "SUIUSDT" in c._bt_last
    bid, ask, ts = c._bt_last["SUIUSDT"]
    assert bid == 1.2345
    assert ask == 1.2346
    assert ts > 0


@pytest.mark.asyncio
async def test_book_ticker_records_latency_advantage_sample():
    """When depth catches up to a state bookTicker saw earlier, log the lag."""
    from src.exchanges.binance_ws import BinanceWSClient
    from collections import deque

    c = object.__new__(BinanceWSClient)
    c._symbols = {"SUIUSDT"}
    c._bt_last = {}
    c._bt_latency_advantage_ms = deque(maxlen=2000)
    c._book_ticker_enabled = True
    c.book_ticker_messages = 0
    c.book_ticker_matched = 0

    # Step 1: bookTicker observes a new top-of-book
    bt_seen_at = int(time.time() * 1000) - 50  # bookTicker saw it 50ms ago
    c._bt_last["SUIUSDT"] = (1.2345, 1.2346, bt_seen_at)

    # Step 2: depth diff arrives that moves top-of-book to that same level
    prev_top = (1.2340, 1.2341)  # before diff

    # Build a fake OrderBook with the NEW top after apply_diff
    fake_ob = MagicMock()
    fake_ob.best_bid = MagicMock(return_value=MagicMock(price=1.2345))
    fake_ob.best_ask = MagicMock(return_value=MagicMock(price=1.2346))

    c._record_book_ticker_latency_sample("SUIUSDT", prev_top, fake_ob)

    assert c.book_ticker_matched == 1
    assert len(c._bt_latency_advantage_ms) == 1
    advantage = c._bt_latency_advantage_ms[0]
    # Should be ~50ms (allow some scheduling slack)
    assert 30 <= advantage <= 200, f"unexpected advantage_ms={advantage}"


@pytest.mark.asyncio
async def test_book_ticker_skips_when_top_unchanged():
    """No sample recorded when depth diff didn't move top-of-book."""
    from src.exchanges.binance_ws import BinanceWSClient
    from collections import deque

    c = object.__new__(BinanceWSClient)
    c._symbols = {"SUIUSDT"}
    c._bt_last = {"SUIUSDT": (1.2345, 1.2346, int(time.time() * 1000))}
    c._bt_latency_advantage_ms = deque(maxlen=2000)
    c._book_ticker_enabled = True
    c.book_ticker_matched = 0

    # Top didn't change
    same_top = (1.2345, 1.2346)
    fake_ob = MagicMock()
    fake_ob.best_bid = MagicMock(return_value=MagicMock(price=1.2345))
    fake_ob.best_ask = MagicMock(return_value=MagicMock(price=1.2346))

    c._record_book_ticker_latency_sample("SUIUSDT", same_top, fake_ob)
    assert c.book_ticker_matched == 0
    assert len(c._bt_latency_advantage_ms) == 0


@pytest.mark.asyncio
async def test_book_ticker_skips_when_no_observation_yet():
    """No sample recorded when bookTicker hasn't observed this symbol yet."""
    from src.exchanges.binance_ws import BinanceWSClient
    from collections import deque

    c = object.__new__(BinanceWSClient)
    c._bt_last = {}  # no observations
    c._bt_latency_advantage_ms = deque(maxlen=2000)
    c._book_ticker_enabled = True
    c.book_ticker_matched = 0

    prev_top = (1.2340, 1.2341)
    fake_ob = MagicMock()
    fake_ob.best_bid = MagicMock(return_value=MagicMock(price=1.2345))
    fake_ob.best_ask = MagicMock(return_value=MagicMock(price=1.2346))

    c._record_book_ticker_latency_sample("SUIUSDT", prev_top, fake_ob)
    assert c.book_ticker_matched == 0
    assert len(c._bt_latency_advantage_ms) == 0


@pytest.mark.asyncio
async def test_book_ticker_skips_stale_observation():
    """bookTicker's last seen state doesn't match new depth top → skip."""
    from src.exchanges.binance_ws import BinanceWSClient
    from collections import deque

    c = object.__new__(BinanceWSClient)
    c._symbols = {"SUIUSDT"}
    # bookTicker last saw a DIFFERENT level than what depth now shows
    c._bt_last = {"SUIUSDT": (1.2300, 1.2301, int(time.time() * 1000))}
    c._bt_latency_advantage_ms = deque(maxlen=2000)
    c._book_ticker_enabled = True
    c.book_ticker_matched = 0

    prev_top = (1.2340, 1.2341)
    fake_ob = MagicMock()
    fake_ob.best_bid = MagicMock(return_value=MagicMock(price=1.2345))
    fake_ob.best_ask = MagicMock(return_value=MagicMock(price=1.2346))

    c._record_book_ticker_latency_sample("SUIUSDT", prev_top, fake_ob)
    assert c.book_ticker_matched == 0


@pytest.mark.asyncio
async def test_book_ticker_url_construction():
    """bookTicker streams URL format matches Binance spec."""
    from src.exchanges.binance_ws import BinanceWSClient

    c = object.__new__(BinanceWSClient)
    c.cfg = MagicMock(ws_base="wss://fstream.binance.com")
    c._symbols = {"SUIUSDT", "PENGUUSDT"}

    url = c._book_ticker_streams_for_url()
    assert "wss://fstream.binance.com/stream?streams=" in url
    assert "suiusdt@bookTicker" in url
    assert "penguusdt@bookTicker" in url


def test_book_ticker_disabled_by_default():
    """book_ticker_enabled defaults to False — no behaviour change on upgrade."""
    from src.config import BinanceConf

    c = BinanceConf(ws_base="wss://x", rest_base="https://y")
    assert c.book_ticker_enabled is False
    assert c.book_ticker_log_interval_sec == 60
