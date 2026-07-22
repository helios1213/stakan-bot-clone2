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
async def test_warmup_stale_path_takes_lock():
    """When cookies are stale, warmup() falls through to slow path.

    We verify that the slow path is taken by checking that the lock IS
    contended — if we hold it externally, the stale warmup blocks until
    we release. (A fresh warmup would skip the lock entirely.)
    """
    from src.execution.webkey.client import MexcWebClient as WebkeyClient

    c = object.__new__(WebkeyClient)
    c._session = MagicMock()
    c._lock = asyncio.Lock()
    c._warmed_at = time.monotonic() - 1000  # very stale → must take lock
    c.cookie_max_age_sec = 60.0
    c._ensure_session = AsyncMock(return_value=c._session)

    # Pre-acquire the lock. The stale-path warmup must wait on it.
    await c._lock.acquire()

    warmup_completed = [False]

    async def runner():
        # Mock session.get so we don't try to fetch from network after
        # the lock is released. We only care about contention, not result.
        c._session.get = AsyncMock(side_effect=Exception("stop-test"))
        try:
            await c.warmup()
        except Exception:
            pass
        warmup_completed[0] = True

    task = asyncio.create_task(runner())
    # Wait a beat — if warmup were on fast path it would already be done.
    await asyncio.sleep(0.05)
    assert not warmup_completed[0], "stale warmup should be blocked on lock"

    c._lock.release()
    await asyncio.wait_for(task, timeout=1.0)
    assert warmup_completed[0], "warmup should proceed after lock released"


# ─── Fix 2: skip simulate_ioc_entry for live pairs ──────────────────────

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
