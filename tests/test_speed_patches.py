"""Tests for OrderBook cached best_bid/ask, detector fast-path, and
background config reload in ShadowEngine."""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.exchanges.orderbook import OrderBook, OrderBookManager
from src.strategy.static_gap_detector import (
    StaticGapDetector,
    StaticGapConf,
    _SymbolFastPath,
)


# ════════════════════════════════════════════════════════════════════
# Cached best_bid / best_ask — O(1)
# ════════════════════════════════════════════════════════════════════


class TestCachedBestBidAsk:
    """Verify that best_bid/best_ask use cached values and are correct."""

    def test_best_bid_after_snapshot(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(100.0, 1.0), (99.0, 2.0), (98.0, 3.0)],
            asks=[(101.0, 1.0), (102.0, 2.0)],
            update_id=1,
        )
        bb = ob.best_bid()
        assert bb is not None
        assert bb.price == 100.0
        assert bb.size == 1.0

    def test_best_ask_after_snapshot(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(100.0, 1.0)],
            asks=[(101.0, 1.0), (102.0, 2.0), (103.0, 3.0)],
            update_id=1,
        )
        ba = ob.best_ask()
        assert ba is not None
        assert ba.price == 101.0
        assert ba.size == 1.0

    def test_best_bid_updates_after_diff(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(100.0, 1.0), (99.0, 2.0)],
            asks=[(101.0, 1.0)],
            update_id=1,
        )
        # New best bid at 100.5
        ob.apply_diff(
            bids=[(100.5, 0.5)],
            asks=[],
            first_update_id=2,
            final_update_id=2,
        )
        bb = ob.best_bid()
        assert bb.price == 100.5
        assert bb.size == 0.5

    def test_best_bid_updates_when_top_removed(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(100.0, 1.0), (99.0, 2.0)],
            asks=[(101.0, 1.0)],
            update_id=1,
        )
        # Remove top bid
        ob.apply_diff(
            bids=[(100.0, 0)],  # size=0 removes level
            asks=[],
            first_update_id=2,
            final_update_id=2,
        )
        bb = ob.best_bid()
        assert bb.price == 99.0
        assert bb.size == 2.0

    def test_best_ask_updates_when_top_removed(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(100.0, 1.0)],
            asks=[(101.0, 1.0), (102.0, 2.0)],
            update_id=1,
        )
        ob.apply_diff(
            bids=[],
            asks=[(101.0, 0)],
            first_update_id=2,
            final_update_id=2,
        )
        ba = ob.best_ask()
        assert ba.price == 102.0

    def test_empty_book_returns_none(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        assert ob.best_bid() is None
        assert ob.best_ask() is None

    def test_cache_fields_exist(self):
        """Verify the new cache fields are present on the dataclass."""
        ob = OrderBook(symbol="TEST", exchange="test")
        assert hasattr(ob, "_top_bid_price")
        assert hasattr(ob, "_top_ask_price")
        assert ob._top_bid_price == 0.0
        assert ob._top_ask_price == 0.0

    def test_cache_set_after_snapshot(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(50.0, 1.0)],
            asks=[(51.0, 1.0)],
            update_id=1,
        )
        assert ob._top_bid_price == 50.0
        assert ob._top_ask_price == 51.0

    def test_listeners_still_fire(self):
        """Ensure _recompute_top doesn't break listener notification."""
        ob = OrderBook(symbol="TEST", exchange="test")
        fired = []
        ob.add_listener(lambda _ob: fired.append(True))
        ob.apply_snapshot(
            bids=[(100.0, 1.0)],
            asks=[(101.0, 1.0)],
            update_id=1,
        )
        assert len(fired) == 1
        ob.apply_diff(
            bids=[(100.5, 0.5)],
            asks=[],
            first_update_id=2,
            final_update_id=2,
        )
        assert len(fired) == 2

    def test_mid_price_consistent(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(100.0, 1.0)],
            asks=[(102.0, 1.0)],
            update_id=1,
        )
        assert ob.mid_price() == 101.0

    def test_spread_pct_consistent(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(100.0, 1.0)],
            asks=[(101.0, 1.0)],
            update_id=1,
        )
        sp = ob.spread_pct()
        assert sp is not None
        assert abs(sp - 1.0) < 0.01


# ════════════════════════════════════════════════════════════════════
# Precomputed fast-path in detector
# ════════════════════════════════════════════════════════════════════


class TestDetectorFastPath:
    """Verify the _SymbolFastPath precomputation."""

    def test_fast_path_dataclass(self):
        fp = _SymbolFastPath(
            tick_scaled=0.0001,
            min_gap_ticks=3,
            cooldown_ms=5000,
        )
        assert fp.tick_scaled == 0.0001
        assert fp.min_gap_ticks == 3
        assert fp.cooldown_ms == 5000
        assert fp.gap_eps == 1e-9

    def test_rebuild_fast_paths(self):
        """Verify _rebuild_fast_paths populates cache correctly."""
        ob_manager = OrderBookManager()
        # Create a Binance book so all_symbols("binance") returns it.
        ob_manager.get_or_create("binance", "SUIUSDT")

        cfg = StaticGapConf(enabled=True, min_gap_ticks=3, cooldown_sec=5.0)
        signal_writer = MagicMock()

        detector = StaticGapDetector(
            cfg=cfg,
            ob_manager=ob_manager,
            signal_writer=signal_writer,
        )

        detector._rebuild_fast_paths()

        # SUI_USDT has tick=1e-4, scale=1.0 in TICK_SIZES/get_binance_scale
        fp = detector._get_fast_path("SUIUSDT")
        assert fp is not None
        assert fp.min_gap_ticks == 3
        assert fp.cooldown_ms == 5000
        assert fp.tick_scaled > 0

    def test_fast_path_returns_none_for_unknown(self):
        ob_manager = OrderBookManager()
        cfg = StaticGapConf(enabled=True)
        signal_writer = MagicMock()
        detector = StaticGapDetector(
            cfg=cfg,
            ob_manager=ob_manager,
            signal_writer=signal_writer,
        )
        assert detector._get_fast_path("NONEXISTENT") is None

    def test_fast_path_respects_overrides(self):
        """When pair overrides change, _rebuild_fast_paths picks them up."""
        ob_manager = OrderBookManager()
        ob_manager.get_or_create("binance", "SUIUSDT")

        cfg = StaticGapConf(enabled=True, min_gap_ticks=3, cooldown_sec=5.0)
        signal_writer = MagicMock()
        from src.strategy.static_gap_detector import PerPairDetectorOverride

        detector = StaticGapDetector(
            cfg=cfg,
            ob_manager=ob_manager,
            signal_writer=signal_writer,
        )

        # Set a per-pair override
        detector._pair_overrides["SUIUSDT"] = PerPairDetectorOverride(
            min_gap_ticks=7,
            cooldown_sec=10.0,
        )
        detector._rebuild_fast_paths()

        fp = detector._get_fast_path("SUIUSDT")
        assert fp is not None
        assert fp.min_gap_ticks == 7
        assert fp.cooldown_ms == 10000


# ════════════════════════════════════════════════════════════════════
# Background config reload (non-blocking)
# ════════════════════════════════════════════════════════════════════


class TestBackgroundConfigReload:
    """Verify that _get_pair_config doesn't await DB on the hot path."""

    @pytest.mark.asyncio
    async def test_get_pair_config_returns_immediately(self):
        """After TTL expires, _get_pair_config should return cached value
        without awaiting the DB reload."""
        from src.strategy.shadow_engine import ShadowEngine, PairExecConfig
        from src.config import ShadowConf

        # Mock everything ShadowEngine needs
        cfg = ShadowConf(enabled=True)
        db = AsyncMock()
        db.fetchall = AsyncMock(return_value=[])
        ob_manager = MagicMock()
        state_manager = MagicMock()
        funding_guard = MagicMock()

        engine = ShadowEngine(
            cfg=cfg,
            db=db,
            ob_manager=ob_manager,
            state_manager=state_manager,
            funding_guard=funding_guard,
        )

        # Pre-populate cache with a known config
        engine._pair_configs = {
            "SUIUSDT": PairExecConfig(margin_min_usdt=99.0),
        }
        # Force TTL to be expired
        engine._configs_loaded_at = 0

        # Call should return immediately with cached value
        t0 = time.monotonic()
        result = await engine._get_pair_config("SUIUSDT")
        elapsed_ms = (time.monotonic() - t0) * 1000

        # Should return the cached config (not default)
        assert result.margin_min_usdt == 99.0
        # Should be near-instant (< 5ms), NOT blocked by DB call
        assert elapsed_ms < 50, f"_get_pair_config took {elapsed_ms:.1f}ms — should be instant"

        # A background task should have been spawned
        assert engine._config_reload_task is not None

        # Give the background task a chance to run
        await asyncio.sleep(0.05)


# ════════════════════════════════════════════════════════════════════
# Regression: existing OrderBook tests still pass
# ════════════════════════════════════════════════════════════════════


class TestOrderBookRegression:
    """Ensure existing behavior is unchanged after cache patch."""

    def test_executable_exit_price_long(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(100.0, 1.0)],
            asks=[(101.0, 1.0)],
            update_id=1,
        )
        assert ob.executable_exit_price("long") == 100.0

    def test_executable_exit_price_short(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(100.0, 1.0)],
            asks=[(101.0, 1.0)],
            update_id=1,
        )
        assert ob.executable_exit_price("short") == 101.0

    def test_top_bids_sorted(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(100.0, 1.0), (99.0, 2.0), (98.0, 3.0)],
            asks=[(101.0, 1.0)],
            update_id=1,
        )
        bids = ob.top_bids(3)
        assert [b.price for b in bids] == [100.0, 99.0, 98.0]

    def test_top_asks_sorted(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        ob.apply_snapshot(
            bids=[(100.0, 1.0)],
            asks=[(101.0, 1.0), (102.0, 2.0), (103.0, 3.0)],
            update_id=1,
        )
        asks = ob.top_asks(3)
        assert [a.price for a in asks] == [101.0, 102.0, 103.0]

    def test_trim_doesnt_break_cache(self):
        """After a trim (>max_levels*4 entries), cache should still be correct."""
        ob = OrderBook(symbol="TEST", exchange="test", max_levels=5)
        # Create a book with many levels
        bids = [(100.0 - i * 0.01, 1.0) for i in range(25)]  # 25 > 5*4=20, triggers trim
        asks = [(101.0 + i * 0.01, 1.0) for i in range(25)]
        ob.apply_snapshot(bids=bids, asks=asks, update_id=1)

        # Apply diff to trigger trim
        new_bids = [(100.0 - i * 0.01, 1.0) for i in range(25)]
        ob.apply_diff(bids=new_bids, asks=[], first_update_id=2, final_update_id=2)

        bb = ob.best_bid()
        ba = ob.best_ask()
        assert bb is not None
        assert ba is not None
        assert bb.price == 100.0
        assert ba.price == 101.0


class TestPerformanceSanity:
    """Quick check that cached best_bid/ask is actually faster than O(n)."""

    def test_cached_is_fast(self):
        ob = OrderBook(symbol="TEST", exchange="test")
        # 1000 level book
        bids = [(1000.0 - i * 0.01, 1.0) for i in range(1000)]
        asks = [(1001.0 + i * 0.01, 1.0) for i in range(1000)]
        ob.apply_snapshot(bids=bids, asks=asks, update_id=1)

        # Time 100K best_bid calls
        t0 = time.perf_counter_ns()
        for _ in range(100_000):
            ob.best_bid()
        elapsed_ns = time.perf_counter_ns() - t0
        elapsed_us_per_call = elapsed_ns / 100_000 / 1000

        # With cache: should be <1µs per call.
        # Without cache (max over 1000 keys): ~5-20µs.
        assert elapsed_us_per_call < 3.0, (
            f"best_bid took {elapsed_us_per_call:.2f}µs/call — expected <3µs with cache"
        )
