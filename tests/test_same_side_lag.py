"""Tests for the same-side-lag formula (May 2026 rewrite).

The new formula compares same side of book across exchanges:
  LONG  fires when (binance_bid - mexc_bid) >= min_gap
        → MEXC bid is behind Binance bid → MEXC will catch up → buy MEXC
  SHORT fires when (mexc_ask - binance_ask) >= min_gap
        → MEXC ask is behind Binance ask → MEXC will fall to Binance level → sell MEXC

Key differences from prior mid-based formula:
  - Both directions use POSITIVE gap_ticks (magnitude of lag)
  - The formula compares bid-to-bid (LONG) and ask-to-ask (SHORT) — not mid
  - Asymmetric spreads can trigger when mid-based wouldn't
  - No imbalance, no CVD, no exec_edge filter — only cooldown
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy.static_gap_detector import StaticGapConf, StaticGapDetector


# ───── Helpers (mirror test_static_gap_midgap.py pattern) ─────────────

@dataclass
class _BookEntry:
    price: float
    qty: float = 100.0


class _FakeOB:
    def __init__(self, bid: float, ask: float, synced: bool = True):
        self._bid = _BookEntry(bid)
        self._ask = _BookEntry(ask)
        self.is_synced = synced

    def best_bid(self): return self._bid
    def best_ask(self): return self._ask
    # Alloc-free accessors used by the detector hot path. Return 0.0 to
    # signal "empty book" — matches the production OrderBook contract.
    def best_bid_price(self) -> float: return self._bid.price if self._bid is not None else 0.0
    def best_ask_price(self) -> float: return self._ask.price if self._ask is not None else 0.0


class _FakeOBManager:
    def __init__(self, books):
        self._books = books

    def all_symbols(self, exchange):
        return [sym for (ex, sym) in self._books if ex == exchange]

    def get(self, exchange, symbol):
        return self._books.get((exchange, symbol))


def _make_det(b_bid, b_ask, m_bid, m_ask, min_gap_ticks=3, cooldown_sec=5.0):
    """Build detector with explicit bid/ask values for both exchanges."""
    sym = "PENGUUSDT"
    books = {
        ("binance", sym): _FakeOB(b_bid, b_ask),
        ("mexc",    sym): _FakeOB(m_bid, m_ask),
    }
    cfg = StaticGapConf(
        enabled=True,
        scan_interval_sec=0.05,
        min_gap_ticks=min_gap_ticks,
        cooldown_sec=cooldown_sec,
    )
    writer = MagicMock()
    writer.write = AsyncMock()
    det = StaticGapDetector(cfg, _FakeOBManager(books), writer)
    return det, writer, sym


@pytest.fixture(autouse=True)
def _patch_tick():
    with patch(
        "src.strategy.static_gap_detector.get_tick_size",
        return_value=0.000001,
    ), patch(
        "src.exchanges.mexc_rest.get_binance_scale",
        return_value=1.0,
    ), patch(
        "src.strategy.static_gap_detector.to_mexc",
        side_effect=lambda s: s.replace("USDT", "_USDT"),
    ):
        yield


# ───── Direction triggers ─────────────────────────────────────────────

class TestSameSideLagFormula:
    @pytest.mark.asyncio
    async def test_long_fires_when_mexc_bid_lags(self):
        """MEXC bid is 3 ticks BELOW Binance bid → LONG."""
        # b_bid 0.010360, m_bid 0.010357 → diff = +3 ticks → LONG
        det, w, _ = _make_det(
            b_bid=0.010360, b_ask=0.010361,
            m_bid=0.010357, m_ask=0.010358,
        )
        await det._scan_once()

        assert w.write.await_count == 1
        sig = w.write.await_args.args[0]
        assert sig.direction == "long"
        assert sig.metadata["long_gap_ticks"] == pytest.approx(3.0, abs=0.01)
        assert sig.metadata["gap_ticks"] == pytest.approx(3.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_short_fires_when_mexc_ask_lags(self):
        """MEXC ask is 3 ticks ABOVE Binance ask → SHORT."""
        # m_ask 0.010361, b_ask 0.010358 → diff = +3 ticks → SHORT
        det, w, _ = _make_det(
            b_bid=0.010357, b_ask=0.010358,
            m_bid=0.010360, m_ask=0.010361,
        )
        await det._scan_once()

        assert w.write.await_count == 1
        sig = w.write.await_args.args[0]
        assert sig.direction == "short"
        assert sig.metadata["short_gap_ticks"] == pytest.approx(3.0, abs=0.01)
        assert sig.metadata["gap_ticks"] == pytest.approx(3.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_no_signal_when_lag_below_threshold(self):
        """Lag of 2 ticks (below min_gap=3) → no signal."""
        det, w, _ = _make_det(
            b_bid=0.010360, b_ask=0.010361,
            m_bid=0.010358, m_ask=0.010359,  # bid lag = 2
        )
        await det._scan_once()
        assert w.write.await_count == 0

    @pytest.mark.asyncio
    async def test_negative_lag_is_not_signal(self):
        """If MEXC bid is ABOVE Binance bid (negative lag), no LONG."""
        # b_bid 0.010357, m_bid 0.010360 → diff = -3 (MEXC ahead, not lagging)
        det, w, _ = _make_det(
            b_bid=0.010357, b_ask=0.010358,
            m_bid=0.010360, m_ask=0.010361,
            # But note m_ask > b_ask by 3 → SHORT will fire (different formula)
        )
        await det._scan_once()

        # Should fire SHORT (m_ask lags), not LONG
        sig = w.write.await_args.args[0]
        assert sig.direction == "short", \
            "Should be SHORT (m_ask higher) not LONG (m_bid higher)"

    @pytest.mark.asyncio
    async def test_both_qualify_picks_larger(self):
        """If both LONG and SHORT thresholds met, pick the larger lag."""
        # Scenario: MEXC has wider spread on both sides simultaneously
        # b_bid 0.010360, m_bid 0.010355 → long_gap = 5 ticks
        # m_ask 0.010370, b_ask 0.010361 → short_gap = 9 ticks
        det, w, _ = _make_det(
            b_bid=0.010360, b_ask=0.010361,
            m_bid=0.010355, m_ask=0.010370,
        )
        await det._scan_once()

        assert w.write.await_count == 1
        sig = w.write.await_args.args[0]
        # short_gap (9) > long_gap (5), so SHORT wins
        assert sig.direction == "short"
        assert sig.metadata["gap_ticks"] == pytest.approx(9.0, abs=0.01)


# ───── Confidence formula ─────────────────────────────────────────────

class TestConfidenceScaling:
    @pytest.mark.asyncio
    async def test_confidence_at_threshold(self):
        """At exactly min_gap, confidence should be 0.5."""
        det, w, _ = _make_det(
            b_bid=0.010360, b_ask=0.010361,
            m_bid=0.010357, m_ask=0.010358,
            min_gap_ticks=3,
        )
        await det._scan_once()
        sig = w.write.await_args.args[0]
        assert sig.confidence == pytest.approx(0.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_confidence_at_double_threshold(self):
        """At 2x min_gap, confidence should be 1.0."""
        det, w, _ = _make_det(
            b_bid=0.010366, b_ask=0.010367,
            m_bid=0.010360, m_ask=0.010361,  # bid lag = 6 ticks (2 * min_gap)
            min_gap_ticks=3,
        )
        await det._scan_once()
        sig = w.write.await_args.args[0]
        assert sig.confidence == pytest.approx(1.0, abs=0.01)


# ───── Filters absent ─────────────────────────────────────────────────

class TestFiltersRemoved:
    """Verify all non-cooldown filters are gone — signal fires regardless
    of imbalance / CVD / exec_edge in isolation."""

    @pytest.mark.asyncio
    async def test_signal_fires_with_zero_exec_edge(self):
        """Exec edge filter is removed: signal fires even when
        cross-exchange arb edge is zero or negative.

        Setup: bid lag = 3 ticks, but b_bid < m_ask (no arb edge for LONG).
        """
        # b_bid 0.010360, m_ask 0.010362 → exec_buy_ticks = -2 (no arb edge)
        # but b_bid - m_bid = 3 → LONG fires by new formula
        det, w, _ = _make_det(
            b_bid=0.010360, b_ask=0.010363,
            m_bid=0.010357, m_ask=0.010362,
        )
        await det._scan_once()

        # Despite no exec edge, signal must fire (filter removed)
        assert w.write.await_count == 1
        sig = w.write.await_args.args[0]
        assert sig.direction == "long"
        # Verify exec_buy_ticks logged but didn't block
        assert sig.metadata["exec_buy_ticks"] < 0


# ───── Cooldown still works ───────────────────────────────────────────

class TestCooldownSurvives:
    @pytest.mark.asyncio
    async def test_cooldown_blocks_same_direction(self):
        """Cooldown is the only filter retained. Same-direction signal
        within cooldown window is blocked."""
        det, w, _ = _make_det(
            b_bid=0.010360, b_ask=0.010361,
            m_bid=0.010357, m_ask=0.010358,
            cooldown_sec=5.0,
        )
        await det._scan_once()
        assert w.write.await_count == 1

        # Same conditions, immediately again — should be blocked
        await det._scan_once()
        assert w.write.await_count == 1, "Cooldown should block second signal"
