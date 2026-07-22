"""Tests for peak-listener patch (May 2026).

Background:
  ShadowPosition.peak_price_favorable was previously updated only from
  the watch-loop polling path (20ms tick). Peaks living <20ms were
  silently missed, biasing phase_2 dead_impulse and phase_3 stall
  detection toward "no progress" verdicts in cases where a brief peak
  actually occurred.

  This patch makes OrderBook expose an add_listener/remove_listener API.
  ShadowEngine registers a synchronous listener that calls
  pos.update_peak_only(executable_exit_price) on every WS depth update.

Tests verify:
  1. OrderBook.add_listener fires the callback on every apply_diff and
     apply_snapshot.
  2. Listeners are isolated — an exception in one doesn't break others
     and doesn't break the WS apply path.
  3. remove_listener is idempotent.
  4. ShadowPosition.update_peak_only updates peak for long correctly.
  5. update_peak_only updates peak for short correctly (lower = better).
  6. update_peak_only is a no-op when position is closed / closing.
  7. update_peak_only does not touch ROI / MFE / MAE / current_price.
  8. last_progress_ms only advances on actual improvement, not equal price.
"""
from __future__ import annotations

import time


from src.exchanges.orderbook import OrderBook
from src.strategy.shadow_position import ShadowPosition


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _new_book() -> OrderBook:
    return OrderBook(symbol="PENGUUSDT", exchange="mexc", max_levels=20)


def _new_pos(direction: str = "long", entry: float = 0.10000) -> ShadowPosition:
    return ShadowPosition(
        symbol="PENGUUSDT",
        direction=direction,
        detector_source="static_gap",
        confidence=0.5,
        leverage=50,
        margin_usdt=5.0,
        notional_usdt=250.0,
        qty=2500.0,
        entry_target_price=entry,
        entry_price=entry,
    )


# ──────────────────────────────────────────────────────────────────────
# OrderBook listener API
# ──────────────────────────────────────────────────────────────────────

class TestOrderBookListenerAPI:
    def test_listener_fires_on_snapshot(self) -> None:
        ob = _new_book()
        calls: list[float | None] = []

        def cb(book: OrderBook) -> None:
            ba = book.best_ask()
            calls.append(ba.price if ba else None)

        ob.add_listener(cb)
        ob.apply_snapshot(
            bids=[(0.0998, 1000), (0.0997, 2000)],
            asks=[(0.1002, 1000), (0.1003, 2000)],
            update_id=1,
        )

        assert len(calls) == 1
        assert calls[0] == 0.1002

    def test_listener_fires_on_diff(self) -> None:
        ob = _new_book()
        ob.apply_snapshot(
            bids=[(0.0998, 1000)],
            asks=[(0.1002, 1000)],
            update_id=1,
        )

        calls: list[float | None] = []

        def cb(book: OrderBook) -> None:
            bb = book.best_bid()
            calls.append(bb.price if bb else None)

        ob.add_listener(cb)
        # Three sequential diffs — each should fire the listener once.
        ob.apply_diff(bids=[(0.0999, 500)], asks=[], first_update_id=2, final_update_id=2)
        ob.apply_diff(bids=[(0.1000, 500)], asks=[], first_update_id=3, final_update_id=3)
        ob.apply_diff(bids=[(0.0999, 0)], asks=[], first_update_id=4, final_update_id=4)

        assert len(calls) == 3
        assert calls == [0.0999, 0.1000, 0.1000]

    def test_add_listener_is_idempotent(self) -> None:
        ob = _new_book()
        calls: list[int] = []

        def cb(book: OrderBook) -> None:
            calls.append(1)

        ob.add_listener(cb)
        ob.add_listener(cb)  # same callable — should not double-register
        ob.add_listener(cb)

        assert ob.listener_count() == 1

        ob.apply_snapshot(bids=[(1.0, 1)], asks=[(2.0, 1)], update_id=1)
        assert len(calls) == 1  # fired once, not three times

    def test_remove_listener_is_idempotent(self) -> None:
        ob = _new_book()

        def cb(book: OrderBook) -> None:
            pass

        ob.remove_listener(cb)  # not registered — should not raise
        ob.add_listener(cb)
        ob.remove_listener(cb)
        ob.remove_listener(cb)  # already removed — should not raise

        assert ob.listener_count() == 0

    def test_remove_listener_stops_callbacks(self) -> None:
        ob = _new_book()
        calls: list[int] = []

        def cb(book: OrderBook) -> None:
            calls.append(1)

        ob.add_listener(cb)
        ob.apply_snapshot(bids=[(1.0, 1)], asks=[(2.0, 1)], update_id=1)
        assert len(calls) == 1

        ob.remove_listener(cb)
        ob.apply_diff(bids=[(1.0, 2)], asks=[], first_update_id=2, final_update_id=2)
        assert len(calls) == 1  # no more fires

    def test_listener_exception_does_not_break_apply(self) -> None:
        """A buggy listener must not break the WS apply path."""
        ob = _new_book()

        def bad(book: OrderBook) -> None:
            raise RuntimeError("boom")

        good_calls: list[int] = []

        def good(book: OrderBook) -> None:
            good_calls.append(1)

        ob.add_listener(bad)
        ob.add_listener(good)

        # Should not raise; orderbook state should still update normally.
        ob.apply_snapshot(bids=[(0.99, 1)], asks=[(1.01, 1)], update_id=1)

        assert ob.best_bid().price == 0.99
        assert ob.best_ask().price == 1.01
        assert len(good_calls) == 1  # good listener still fired

    def test_listener_can_remove_self_during_callback(self) -> None:
        """Listener removing itself during iteration must not break others."""
        ob = _new_book()
        fired: list[str] = []

        def self_removing(book: OrderBook) -> None:
            fired.append("A")
            book.remove_listener(self_removing)

        def other(book: OrderBook) -> None:
            fired.append("B")

        ob.add_listener(self_removing)
        ob.add_listener(other)

        ob.apply_snapshot(bids=[(1.0, 1)], asks=[(2.0, 1)], update_id=1)
        assert fired == ["A", "B"]

        # Second apply — only "other" should fire.
        fired.clear()
        ob.apply_diff(bids=[(1.0, 2)], asks=[], first_update_id=2, final_update_id=2)
        assert fired == ["B"]


# ──────────────────────────────────────────────────────────────────────
# ShadowPosition.update_peak_only semantics
# ──────────────────────────────────────────────────────────────────────

class TestUpdatePeakOnly:
    def test_long_first_call_seeds_peak(self) -> None:
        pos = _new_pos(direction="long", entry=0.10000)
        assert pos.peak_price_favorable == 0.0
        assert pos.last_progress_ms == 0

        before = int(time.time() * 1000)
        pos.update_peak_only(0.10005)
        after = int(time.time() * 1000)

        assert pos.peak_price_favorable == 0.10005
        assert before <= pos.last_progress_ms <= after

    def test_long_peak_advances_only_on_improvement(self) -> None:
        pos = _new_pos(direction="long", entry=0.10000)
        pos.update_peak_only(0.10005)
        first_progress = pos.last_progress_ms

        time.sleep(0.005)
        pos.update_peak_only(0.10003)  # lower — no improvement
        assert pos.peak_price_favorable == 0.10005
        assert pos.last_progress_ms == first_progress  # unchanged

        time.sleep(0.005)
        pos.update_peak_only(0.10010)  # higher — improvement
        assert pos.peak_price_favorable == 0.10010
        assert pos.last_progress_ms > first_progress

    def test_short_peak_advances_on_lower_price(self) -> None:
        pos = _new_pos(direction="short", entry=0.10000)
        pos.update_peak_only(0.09995)
        assert pos.peak_price_favorable == 0.09995

        pos.update_peak_only(0.09998)  # higher — no improvement for short
        assert pos.peak_price_favorable == 0.09995

        pos.update_peak_only(0.09990)  # lower — improvement
        assert pos.peak_price_favorable == 0.09990

    def test_noop_when_closed(self) -> None:
        pos = _new_pos(direction="long", entry=0.10000)
        pos.exit_reason = "manual"  # closed
        assert not pos.is_open

        pos.update_peak_only(0.10100)
        assert pos.peak_price_favorable == 0.0  # unchanged

    def test_noop_when_closing(self) -> None:
        pos = _new_pos(direction="long", entry=0.10000)
        pos.is_closing = True
        assert pos.is_open  # exit_reason still None
        pos.update_peak_only(0.10100)
        assert pos.peak_price_favorable == 0.0  # unchanged

    def test_noop_when_no_entry_price(self) -> None:
        pos = _new_pos(direction="long", entry=0.0)
        pos.entry_price = 0.0  # not entered yet
        pos.update_peak_only(0.10100)
        assert pos.peak_price_favorable == 0.0

    def test_noop_when_price_invalid(self) -> None:
        pos = _new_pos(direction="long", entry=0.10000)
        pos.update_peak_only(0.0)
        assert pos.peak_price_favorable == 0.0
        pos.update_peak_only(-1.0)
        assert pos.peak_price_favorable == 0.0

    def test_does_not_touch_roi_mfe_mae(self) -> None:
        """update_peak_only must NOT modify ROI/MFE/MAE/current_price.
        Those remain the responsibility of update_price() in the poll loop.
        """
        pos = _new_pos(direction="long", entry=0.10000)
        pos.update_peak_only(0.10500)

        assert pos.current_price == 0.0       # untouched
        assert pos.current_roi_pct == 0.0     # untouched
        assert pos.peak_roi_pct == 0.0        # untouched
        assert pos.trough_roi_pct == 0.0      # untouched
        assert pos.mfe_pct == 0.0             # untouched
        assert pos.mae_pct == 0.0             # untouched

        # update_price should still work and now compute ROI/MFE/MAE
        # from the current sample. peak_price_favorable should be preserved
        # at the higher value seeded by update_peak_only.
        pos.update_price(0.10300)
        assert pos.current_price == 0.10300
        assert pos.current_roi_pct > 0       # 3% × 50 leverage = 150% — positive
        assert pos.mfe_pct > 0
        # peak_price_favorable stays at the higher value (0.10500)
        # because update_price only improves (never lowers) the peak.
        assert pos.peak_price_favorable == 0.10500


# ──────────────────────────────────────────────────────────────────────
# Integration: listener → position
# ──────────────────────────────────────────────────────────────────────

class TestListenerIntegration:
    def test_high_resolution_peak_capture(self) -> None:
        """Simulate a short-lived peak between two polls.

        Scenario:
          - long position, entry 0.10000
          - WS pushes: 0.10005, 0.10020 (PEAK), 0.10003
          - Polling-only would see 0.10003 (current after burst) — peak missed.
          - With listener: peak_price_favorable = 0.10020.
        """
        ob = _new_book()
        ob.apply_snapshot(
            bids=[(0.09998, 1000)],
            asks=[(0.10002, 1000)],
            update_id=1,
        )

        pos = _new_pos(direction="long", entry=0.10000)

        # Wire listener the same way ShadowEngine does.
        def listener(book: OrderBook) -> None:
            if not pos.is_open or pos.is_closing:
                return
            price = book.executable_exit_price(pos.direction)
            if price is not None and price > 0:
                pos.update_peak_only(price)

        ob.add_listener(listener)

        # Burst of WS updates (best_bid for long = exit price).
        ob.apply_diff(bids=[(0.10005, 500)], asks=[], first_update_id=2, final_update_id=2)
        ob.apply_diff(bids=[(0.10020, 500)], asks=[], first_update_id=3, final_update_id=3)
        ob.apply_diff(bids=[(0.10005, 0), (0.10020, 0), (0.10003, 500)],
                      asks=[], first_update_id=4, final_update_id=4)

        # If polling missed the burst entirely, peak would equal whatever
        # the next poll observed (0.10003). With the listener active,
        # the 0.10020 spike was captured.
        assert pos.peak_price_favorable == 0.10020

    def test_listener_silenced_after_close(self) -> None:
        ob = _new_book()
        ob.apply_snapshot(
            bids=[(0.09998, 1000)],
            asks=[(0.10002, 1000)],
            update_id=1,
        )
        pos = _new_pos(direction="long", entry=0.10000)

        def listener(book: OrderBook) -> None:
            if not pos.is_open or pos.is_closing:
                return
            price = book.executable_exit_price(pos.direction)
            if price is not None and price > 0:
                pos.update_peak_only(price)

        ob.add_listener(listener)
        ob.apply_diff(bids=[(0.10010, 500)], asks=[], first_update_id=2, final_update_id=2)
        assert pos.peak_price_favorable == 0.10010

        # Close the position — listener should no-op even if still registered.
        pos.exit_reason = "manual"
        ob.apply_diff(bids=[(0.10050, 500)], asks=[], first_update_id=3, final_update_id=3)
        assert pos.peak_price_favorable == 0.10010  # unchanged

        # And after explicit remove_listener — totally silent.
        ob.remove_listener(listener)
        assert ob.listener_count() == 0
