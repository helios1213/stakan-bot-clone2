"""Tests for reconciliation patch (May 2026).

Builds on the safety patch (orphan detection + market close fallback).
Tests three new behaviors:

  1. startup_reconcile: at bot start, close any MEXC position not in
     bot's state. Real scenario: bot crash mid-trade → restart fetches
     positions → market-closes them.

  2. periodic_reconcile_loop / reconcile_once:
     - Case A: MEXC position bot doesn't track → ORPHAN, close it
     - Case B: bot tracks position MEXC says is gone → STALE, mark closed
     - Race protection: skip positions younger than _RECONCILE_GRACE_SEC

  3. mark_position_closed_externally: helper for Case B. Records close
     in engine + DB without submitting close to MEXC.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.safety.reconciliation import (
    startup_reconcile,
    reconcile_once,
    periodic_reconcile_loop,
    _mexc_pos_to_close_args,
    _mexc_pos_age_sec,
)
from src.execution.live_executor import LiveClosePosResult


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _mexc_pos(symbol="PENGU_USDT", direction="short", qty=13194, lev=62,
              create_time_ms=None):
    """Build a fake MEXC position dict."""
    if create_time_ms is None:
        # Default: old enough to pass race protection
        create_time_ms = int(time.time() * 1000) - 60_000
    return {
        "symbol": symbol,
        "positionType": 1 if direction == "long" else 2,
        "holdVol": qty,
        "leverage": lev,
        "createTime": create_time_ms,
        "positionId": 99999,
    }


def _mk_executor_with_positions(positions: list[dict]):
    """Build a mocked LiveExecutor + client_pool that returns `positions`
    from get_open_positions(), and tracks market_close_position calls.
    """
    executor = MagicMock()
    client = MagicMock()
    client.get_open_positions = AsyncMock(return_value={"code": 0, "data": positions})
    pool = MagicMock()
    pool.get = AsyncMock(return_value=client)
    executor.client_pool = pool
    executor.market_close_position = AsyncMock(
        return_value=LiveClosePosResult(
            success=True,
            exit_price=0.009500,
            realized_pnl_usdt=-0.15,
            latency_ms=200,
        )
    )
    return executor


def _mk_live_pool(executors_by_slot: dict):
    """Build a fake live_pool with given executors."""
    pool = MagicMock()
    pool._executors = executors_by_slot
    pool.get_executor = MagicMock(side_effect=lambda sid: executors_by_slot.get(sid))
    return pool


# ──────────────────────────────────────────────────────────────────────
# Layer A — startup_reconcile
# ──────────────────────────────────────────────────────────────────────

class TestStartupReconcile:
    @pytest.mark.asyncio
    async def test_skips_when_no_live_pool(self):
        summary = await startup_reconcile(live_pool=None, alerts=None)
        assert summary["checked_slots"] == 0
        assert summary["found_positions"] == 0

    @pytest.mark.asyncio
    async def test_skips_when_no_executors(self):
        pool = _mk_live_pool({})
        summary = await startup_reconcile(pool, alerts=None)
        assert summary["checked_slots"] == 0

    @pytest.mark.asyncio
    async def test_no_positions_clean_startup(self):
        executor = _mk_executor_with_positions([])
        pool = _mk_live_pool({1: executor})
        summary = await startup_reconcile(pool, alerts=None)
        assert summary["checked_slots"] == 1
        assert summary["found_positions"] == 0
        # No close call should have happened
        executor.market_close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_closes_orphan_position_via_market(self):
        """Bot finds unmanaged position on MEXC at startup → market close."""
        orphan = _mexc_pos(symbol="PENGU_USDT", direction="short", qty=13194, lev=62)
        executor = _mk_executor_with_positions([orphan])
        pool = _mk_live_pool({1: executor})

        summary = await startup_reconcile(pool, alerts=None)

        assert summary["found_positions"] == 1
        assert summary["closed_successfully"] == 1
        assert summary["close_failures"] == 0

        # Verify market close was called with correct args
        executor.market_close_position.assert_called_once()
        call_kwargs = executor.market_close_position.call_args.kwargs
        assert call_kwargs["symbol"] == "PENGU_USDT"
        assert call_kwargs["direction"] == "short"
        assert call_kwargs["qty_contracts"] == 13194
        assert call_kwargs["leverage"] == 62

    @pytest.mark.asyncio
    async def test_handles_close_failure_gracefully(self):
        """Market close fails — increment failure counter, don't crash."""
        orphan = _mexc_pos()
        executor = _mk_executor_with_positions([orphan])
        executor.market_close_position = AsyncMock(
            return_value=LiveClosePosResult(
                success=False, error_msg="rate_limit",
            )
        )
        pool = _mk_live_pool({1: executor})

        summary = await startup_reconcile(pool, alerts=None)

        assert summary["found_positions"] == 1
        assert summary["closed_successfully"] == 0
        assert summary["close_failures"] == 1

    @pytest.mark.asyncio
    async def test_alerts_sent_on_orphan(self):
        """Telegram alert fires on orphan close (success or failure)."""
        orphan = _mexc_pos()
        executor = _mk_executor_with_positions([orphan])
        pool = _mk_live_pool({1: executor})

        alerts = MagicMock()
        alerts.send = AsyncMock()

        await startup_reconcile(pool, alerts=alerts)

        alerts.send.assert_called()
        call_kwargs = alerts.send.call_args.kwargs
        assert "RECONCILE" in call_kwargs["text"]
        assert "STARTUP" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_zero_qty_skipped(self):
        """Position with zero qty shouldn't trigger market close (would error)."""
        weird = _mexc_pos(qty=0)
        executor = _mk_executor_with_positions([weird])
        pool = _mk_live_pool({1: executor})

        summary = await startup_reconcile(pool, alerts=None)
        assert summary["found_positions"] == 1
        executor.market_close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_slots_all_checked(self):
        orphan1 = _mexc_pos(symbol="PENGU_USDT")
        orphan2 = _mexc_pos(symbol="ZEC_USDT", direction="long", qty=10)
        ex1 = _mk_executor_with_positions([orphan1])
        ex2 = _mk_executor_with_positions([orphan2])
        pool = _mk_live_pool({1: ex1, 2: ex2})

        summary = await startup_reconcile(pool, alerts=None)
        assert summary["checked_slots"] == 2
        assert summary["found_positions"] == 2
        assert summary["closed_successfully"] == 2


# ──────────────────────────────────────────────────────────────────────
# Layer B — reconcile_once (Case A: orphan, Case B: stale engine)
# ──────────────────────────────────────────────────────────────────────

class TestReconcileOnce:
    def _mk_shadow_engine(self, open_positions=None):
        """Build a fake shadow_engine with controllable _open_positions."""
        engine = MagicMock()
        engine._open_positions = open_positions or {}
        engine._stop = asyncio.Event()
        engine.mark_position_closed_externally = AsyncMock()
        return engine

    def _mk_position(self, symbol="PENGUUSDT", direction="short", age_sec=60):
        """Build a fake ShadowPosition that satisfies reconciliation predicates."""
        pos = MagicMock()
        pos.symbol = symbol  # Note: bot uses Binance-style symbol internally
        pos.direction = direction
        pos.mode = "live"
        pos.is_open = True
        pos.is_closing = False
        pos.elapsed_sec = age_sec
        pos.entry_price = 0.00942
        pos.qty = 13194
        return pos

    @pytest.mark.asyncio
    async def test_case_a_orphan_on_mexc_gets_closed(self):
        """MEXC has position, engine doesn't → market-close it."""
        orphan = _mexc_pos(symbol="PENGU_USDT")
        executor = _mk_executor_with_positions([orphan])
        pool = _mk_live_pool({1: executor})
        engine = self._mk_shadow_engine(open_positions={})

        with patch("src.exchanges.mexc_rest.to_mexc", side_effect=lambda s: s.replace("USDT", "_USDT")):
            summary = await reconcile_once(engine, pool, alerts=None)

        assert summary["mexc_positions"] == 1
        assert summary["engine_positions"] == 0
        assert summary["orphans_closed"] == 1
        executor.market_close_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_case_a_skips_just_opened_within_grace(self):
        """Race protection: skip MEXC position younger than grace window."""
        very_new = _mexc_pos(
            create_time_ms=int(time.time() * 1000) - 5_000,  # 5s old
        )
        executor = _mk_executor_with_positions([very_new])
        pool = _mk_live_pool({1: executor})
        engine = self._mk_shadow_engine(open_positions={})

        with patch("src.exchanges.mexc_rest.to_mexc", side_effect=lambda s: s.replace("USDT", "_USDT")):
            summary = await reconcile_once(engine, pool, alerts=None)

        # Found but not closed (race protection)
        assert summary["mexc_positions"] == 1
        assert summary["orphans_closed"] == 0
        executor.market_close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_case_b_stale_engine_marked_closed(self):
        """Engine tracks position MEXC says is gone → mark closed externally."""
        # Engine has PENGU position, MEXC returns empty
        pos = self._mk_position(symbol="PENGUUSDT", age_sec=60)
        engine = self._mk_shadow_engine(open_positions={"PENGUUSDT": [pos]})

        executor = _mk_executor_with_positions([])  # no MEXC positions
        pool = _mk_live_pool({1: executor})

        with patch("src.exchanges.mexc_rest.to_mexc", side_effect=lambda s: s.replace("USDT", "_USDT")):
            summary = await reconcile_once(engine, pool, alerts=None)

        assert summary["engine_positions"] == 1
        assert summary["stale_engine_marked"] == 1
        engine.mark_position_closed_externally.assert_called_once()
        call_kwargs = engine.mark_position_closed_externally.call_args.kwargs
        assert "reconciliation" in call_kwargs["reason"]

    @pytest.mark.asyncio
    async def test_case_b_skips_just_opened_engine_positions(self):
        """Race protection on engine side: skip positions <15s old."""
        new_pos = self._mk_position(age_sec=5)  # 5s old engine position
        engine = self._mk_shadow_engine(open_positions={"PENGUUSDT": [new_pos]})

        executor = _mk_executor_with_positions([])
        pool = _mk_live_pool({1: executor})

        with patch("src.exchanges.mexc_rest.to_mexc", side_effect=lambda s: s.replace("USDT", "_USDT")):
            summary = await reconcile_once(engine, pool, alerts=None)

        assert summary["engine_positions"] == 1
        assert summary["stale_engine_marked"] == 0
        engine.mark_position_closed_externally.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_action_when_states_match(self):
        """MEXC and engine both have same position → nothing to reconcile."""
        mexc_pos = _mexc_pos(symbol="PENGU_USDT")
        pos = self._mk_position(symbol="PENGUUSDT", age_sec=60)
        engine = self._mk_shadow_engine(open_positions={"PENGUUSDT": [pos]})

        executor = _mk_executor_with_positions([mexc_pos])
        pool = _mk_live_pool({1: executor})

        with patch("src.exchanges.mexc_rest.to_mexc", side_effect=lambda s: s.replace("USDT", "_USDT")):
            summary = await reconcile_once(engine, pool, alerts=None)

        assert summary["mexc_positions"] == 1
        assert summary["engine_positions"] == 1
        assert summary["orphans_closed"] == 0
        assert summary["stale_engine_marked"] == 0

    @pytest.mark.asyncio
    async def test_closing_positions_excluded_from_engine_state(self):
        """Positions in is_closing state shouldn't be counted as engine-tracked
        (they're mid-close, _close_position will handle them).
        """
        pos = self._mk_position(age_sec=60)
        pos.is_closing = True
        engine = self._mk_shadow_engine(open_positions={"PENGUUSDT": [pos]})

        executor = _mk_executor_with_positions([])
        pool = _mk_live_pool({1: executor})

        with patch("src.exchanges.mexc_rest.to_mexc", side_effect=lambda s: s.replace("USDT", "_USDT")):
            summary = await reconcile_once(engine, pool, alerts=None)

        # is_closing pos shouldn't appear as tracked, so no stale-state action
        assert summary["engine_positions"] == 0
        assert summary["stale_engine_marked"] == 0


# ──────────────────────────────────────────────────────────────────────
# Helper unit tests
# ──────────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_mexc_pos_to_close_args_long(self):
        p = {"symbol": "BTCUSDT", "positionType": 1, "holdVol": 100, "leverage": 25}
        sym, dir_, qty, lev = _mexc_pos_to_close_args(p)
        assert sym == "BTCUSDT"
        assert dir_ == "long"
        assert qty == 100
        assert lev == 25

    def test_mexc_pos_to_close_args_short(self):
        p = {"symbol": "PENGU_USDT", "positionType": 2, "holdVol": 13194, "leverage": 62}
        sym, dir_, qty, lev = _mexc_pos_to_close_args(p)
        assert dir_ == "short"

    def test_mexc_pos_age_zero_when_no_create_time(self):
        assert _mexc_pos_age_sec({}) == 0.0
        assert _mexc_pos_age_sec({"createTime": 0}) == 0.0

    def test_mexc_pos_age_positive_for_old_position(self):
        old_time = int(time.time() * 1000) - 120_000  # 2 min old
        age = _mexc_pos_age_sec({"createTime": old_time})
        assert 119 < age < 121


# ──────────────────────────────────────────────────────────────────────
# Periodic loop — verifies start/stop behavior
# ──────────────────────────────────────────────────────────────────────

class TestPeriodicLoop:
    @pytest.mark.asyncio
    async def test_loop_exits_on_stop_event(self):
        """Loop must exit cleanly when stop_event is set."""
        stop = asyncio.Event()
        engine = MagicMock()
        engine._stop = stop
        engine._open_positions = {}
        pool = _mk_live_pool({})

        # Use very short interval so we don't wait long
        task = asyncio.create_task(
            periodic_reconcile_loop(engine, pool, alerts=None, interval_sec=10.0)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert task.done()

    @pytest.mark.asyncio
    async def test_loop_continues_on_exception(self):
        """If reconcile_once throws, loop catches and continues."""
        stop = asyncio.Event()
        engine = MagicMock()
        engine._stop = stop
        # Make _open_positions access throw
        type(engine)._open_positions = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        pool = _mk_live_pool({})

        task = asyncio.create_task(
            periodic_reconcile_loop(engine, pool, alerts=None, interval_sec=0.05)
        )
        await asyncio.sleep(0.2)  # 4 iterations have happened, all errored
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done()
        # If it had crashed instead of catching, task would be in errored state
        assert task.exception() is None


# ──────────────────────────────────────────────────────────────────────
# mark_position_closed_externally (helper on ShadowEngine)
# ──────────────────────────────────────────────────────────────────────

class TestMarkClosedExternally:
    """Tests for the new ShadowEngine method.

    Uses a minimal ShadowEngine-like object since constructing a real one
    requires many dependencies. We test the method's logic with mocked
    collaborators.
    """
    def _setup_engine_and_pos(self):
        from src.strategy.shadow_engine import ShadowEngine
        from src.strategy.shadow_position import ShadowPosition

        # Manually construct minimal state. We don't call ShadowEngine.__init__
        # (it needs many deps); instead we attach fields it would set.
        engine = ShadowEngine.__new__(ShadowEngine)
        engine._open_positions = {"PENGUUSDT": []}
        engine.positions_closed = 0
        engine.ob_manager = MagicMock()
        engine.ob_manager.get = MagicMock(return_value=None)  # no orderbook
        engine.live_db = None
        engine.db = None
        engine._stop = asyncio.Event()

        # Build a position
        pos = ShadowPosition(
            symbol="PENGUUSDT",
            direction="short",
            detector_source="test",
            confidence=0.5,
            leverage=50,
            margin_usdt=5.0,
            notional_usdt=250.0,
            qty=2500.0,
            entry_target_price=0.00942,
            entry_price=0.00942,
            mode="live",
        )
        engine._open_positions["PENGUUSDT"].append(pos)
        return engine, pos

    @pytest.mark.asyncio
    async def test_marks_position_closed_with_reason(self):
        engine, pos = self._setup_engine_and_pos()
        # Patch _persist_trade to avoid touching DB
        engine._persist_trade = AsyncMock()

        await engine.mark_position_closed_externally(
            pos, reason="reconciliation_external_close",
        )

        assert pos.is_open is False  # exit_reason is set
        assert pos.exit_reason == "reconciliation_external_close"
        assert pos.closed_at_ms > 0
        # duration_ms could be 0 in fast-running tests — just verify it's set
        assert pos.duration_ms >= 0
        assert pos.duration_sec >= 0

    @pytest.mark.asyncio
    async def test_uses_entry_price_when_no_ob(self):
        """When no orderbook and no hint, falls back to entry_price (PnL ≈ 0)."""
        engine, pos = self._setup_engine_and_pos()
        engine._persist_trade = AsyncMock()

        await engine.mark_position_closed_externally(pos, reason="test")
        assert pos.exit_price == pos.entry_price

    @pytest.mark.asyncio
    async def test_uses_hint_when_provided(self):
        engine, pos = self._setup_engine_and_pos()
        engine._persist_trade = AsyncMock()

        await engine.mark_position_closed_externally(
            pos, reason="test", exit_price_hint=0.009500,
        )
        assert pos.exit_price == 0.009500
        # Short entry at 0.00942, exit at 0.0095 → loss
        assert pos.pnl_usdt < 0

    @pytest.mark.asyncio
    async def test_removes_from_open_positions(self):
        engine, pos = self._setup_engine_and_pos()
        engine._persist_trade = AsyncMock()
        assert pos in engine._open_positions["PENGUUSDT"]

        await engine.mark_position_closed_externally(pos, reason="test")
        assert pos not in engine._open_positions["PENGUUSDT"]

    @pytest.mark.asyncio
    async def test_idempotent_if_already_closing(self):
        engine, pos = self._setup_engine_and_pos()
        engine._persist_trade = AsyncMock()
        pos.is_closing = True

        await engine.mark_position_closed_externally(pos, reason="test")
        # Should not have modified anything
        assert pos.exit_reason is None
        engine._persist_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_if_already_closed(self):
        engine, pos = self._setup_engine_and_pos()
        engine._persist_trade = AsyncMock()
        pos.exit_reason = "previous_close"  # already closed

        await engine.mark_position_closed_externally(pos, reason="test")
        # Reason should not have been overwritten
        assert pos.exit_reason == "previous_close"

    @pytest.mark.asyncio
    async def test_uses_orderbook_mid_when_available(self):
        engine, pos = self._setup_engine_and_pos()
        engine._persist_trade = AsyncMock()

        # Mock orderbook with executable_exit_price
        mock_ob = MagicMock()
        mock_ob.is_synced = True
        mock_ob.executable_exit_price = MagicMock(return_value=0.009480)
        engine.ob_manager.get = MagicMock(return_value=mock_ob)

        await engine.mark_position_closed_externally(pos, reason="test")
        assert pos.exit_price == 0.009480


# ──────────────────────────────────────────────────────────────────────
# Two slots on one pair — the sweep must match per (slot, symbol)
# ──────────────────────────────────────────────────────────────────────

class TestReconcileIsSlotAware:
    """Regression for the slot-independence change (dfff235).

    Matching engine state by SYMBOL was equivalent to matching by slot only
    while one pair could hold one position across all slots. Once both slots
    trade the same pair, slot 1's untracked position hides behind slot 2's
    tracked one and the last-resort sweep skips it — leaving a real position
    with no watcher, no stop and no trail.
    """

    @staticmethod
    def _pos(symbol, slot_label, age_sec=60):
        pos = MagicMock()
        pos.symbol = symbol
        pos.direction = "short"
        pos.mode = "live"
        pos.is_open = True
        pos.is_closing = False
        pos.elapsed_sec = age_sec
        pos.entry_price = 0.00942
        pos.qty = 13194
        pos.account_label = slot_label
        return pos

    @staticmethod
    def _engine(open_positions):
        engine = MagicMock()
        engine._open_positions = open_positions
        engine._stop = asyncio.Event()
        engine.mark_position_closed_externally = AsyncMock()
        return engine

    @pytest.mark.asyncio
    async def test_orphan_on_one_slot_closed_while_sibling_holds_the_pair(self):
        ex1 = _mk_executor_with_positions([_mexc_pos(symbol="PENGU_USDT")])
        ex2 = _mk_executor_with_positions([_mexc_pos(symbol="PENGU_USDT")])
        pool = _mk_live_pool({1: ex1, 2: ex2})
        # Only slot 2's position is tracked; slot 1's is an orphan.
        engine = self._engine({"PENGUUSDT": [self._pos("PENGUUSDT", "slot2")]})

        with patch("src.exchanges.mexc_rest.to_mexc",
                   side_effect=lambda s: s.replace("USDT", "_USDT")):
            summary = await reconcile_once(engine, pool, alerts=None)

        assert summary["orphans_closed"] == 1, "slot 1's orphan was skipped"
        ex1.market_close_position.assert_called_once()
        ex2.market_close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_tracked_positions_on_both_slots_are_left_alone(self):
        ex1 = _mk_executor_with_positions([_mexc_pos(symbol="PENGU_USDT")])
        ex2 = _mk_executor_with_positions([_mexc_pos(symbol="PENGU_USDT")])
        pool = _mk_live_pool({1: ex1, 2: ex2})
        engine = self._engine({"PENGUUSDT": [self._pos("PENGUUSDT", "slot1"),
                                             self._pos("PENGUUSDT", "slot2")]})

        with patch("src.exchanges.mexc_rest.to_mexc",
                   side_effect=lambda s: s.replace("USDT", "_USDT")):
            summary = await reconcile_once(engine, pool, alerts=None)

        assert summary["orphans_closed"] == 0
        ex1.market_close_position.assert_not_called()
        ex2.market_close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_unreadable_slot_label_is_left_alone(self):
        """Fail safe: never close a position we cannot attribute to a slot."""
        ex1 = _mk_executor_with_positions([_mexc_pos(symbol="PENGU_USDT")])
        pool = _mk_live_pool({1: ex1})
        engine = self._engine({"PENGUUSDT": [self._pos("PENGUUSDT", None)]})

        with patch("src.exchanges.mexc_rest.to_mexc",
                   side_effect=lambda s: s.replace("USDT", "_USDT")):
            summary = await reconcile_once(engine, pool, alerts=None)

        assert summary["orphans_closed"] == 0
        ex1.market_close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_engine_position_found_though_sibling_slot_holds_the_pair(self):
        """Case B: slot 1's ghost must be cleared even while slot 2 is real."""
        ex1 = _mk_executor_with_positions([])            # slot 1 flat on MEXC
        ex2 = _mk_executor_with_positions([_mexc_pos(symbol="PENGU_USDT")])
        pool = _mk_live_pool({1: ex1, 2: ex2})
        ghost = self._pos("PENGUUSDT", "slot1")
        engine = self._engine({"PENGUUSDT": [ghost,
                                             self._pos("PENGUUSDT", "slot2")]})

        with patch("src.exchanges.mexc_rest.to_mexc",
                   side_effect=lambda s: s.replace("USDT", "_USDT")):
            summary = await reconcile_once(engine, pool, alerts=None)

        assert summary["stale_engine_marked"] == 1
        marked = [c.args[0] for c in engine.mark_position_closed_externally.await_args_list]
        assert marked == [ghost]


class TestOrphanCloseFeedsSafetyCorrectly:
    """record_open keys the per-symbol counter with the INTERNAL symbol.

    The orphan path passed the MEXC form, so the decrement missed and the pair
    stayed blocked on "max_concurrent_per_symbol reached" until a restart —
    silently, because the surrounding except swallowed everything.
    """

    @pytest.mark.asyncio
    async def test_record_close_gets_the_internal_symbol(self):
        ex = _mk_executor_with_positions([_mexc_pos(symbol="PENGU_USDT")])
        pool = _mk_live_pool({1: ex})
        safety = MagicMock()
        pool.get_safety = MagicMock(return_value=safety)
        engine = MagicMock()
        engine._open_positions = {}
        engine._stop = asyncio.Event()
        engine.mark_position_closed_externally = AsyncMock()
        engine.live_db = None

        with patch("src.exchanges.mexc_rest.to_mexc",
                   side_effect=lambda s: s.replace("USDT", "_USDT")):
            await reconcile_once(engine, pool, alerts=None)

        assert safety.record_close.called, "safety was never told about the orphan"
        got = safety.record_close.call_args.args[0]
        assert "_" not in got, f"MEXC format leaked into the counter: {got}"
        assert got == "PENGUUSDT"

    @pytest.mark.asyncio
    async def test_counter_actually_returns_to_zero(self):
        """End to end on the real controller, not a mock."""
        from src.execution.live_safety import LiveSafetyController
        c = LiveSafetyController()
        c.record_open("PENGUUSDT")
        assert c.state.open_live_positions["PENGUUSDT"] == 1
        allowed, why = c.can_open_live("PENGUUSDT", margin_usdt=5.0)
        assert allowed is False and "max_concurrent_per_symbol" in why
        c.record_close("PENGUUSDT", -0.10)
        allowed, _ = c.can_open_live("PENGUUSDT", margin_usdt=5.0)
        assert allowed is True, "the pair stayed blocked after its close"
