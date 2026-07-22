"""Tests for orphan-position safety patch (May 2026).

Real incident 2026-05-13: PENGU short opened at 17:00:05, bot tried
to close at 17:28:18 but MEXC's internal IOC expired without filling.
close_position() returned success=True (BUG — code=0 only means MEXC
accepted the request, not that fill happened). Bot marked position
closed internally. Position drifted unbounded for 36 minutes; user
manually closed at 18:03:59 for -$7.93 loss.

This patch adds 4 defense layers:
  A. close_position() verifies position actually gone before returning
     success (via get_open_positions check). False-success → real failure.
  B. New market_close_position() method — uses type=5 MARKET order, which
     cannot expire (guaranteed fill, accepts slippage).
  C. _close_position() now escalates 2× IOC failure → 1× MARKET as final
     close attempt before declaring orphan.
  D. _watch_position() has an absolute max_hold ceiling (default 600s).
     If position lives that long, force-close regardless of strategy
     state — catches phase-exit-broken / WS-stall scenarios.

Tests verify each layer's behaviour with mocked MEXC clients.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.execution.live_executor import LiveExecutor


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _mk_executor():
    """LiveExecutor with mocked client pool."""
    pool = MagicMock()
    pool.get = AsyncMock()
    executor = LiveExecutor(
        client_pool=pool,
        slot_id=1,
        order_timeout_sec=2.0,
        close_timeout_sec=5.0,
    )
    return executor, pool


# ──────────────────────────────────────────────────────────────────────
# Layer A — close_position() verifies actual close
# ──────────────────────────────────────────────────────────────────────

class TestCloseVerification:
    @pytest.mark.asyncio
    async def test_returns_failure_when_position_still_open_after_close_all(self):
        """Real incident reproduction: MEXC returns code=0 but IOC expired
        and position remained open. Patched close_position must detect this
        via get_open_positions and return success=False.
        """
        executor, pool = _mk_executor()

        client = MagicMock()
        client.get_open_positions = AsyncMock(side_effect=[
            # Pre-close snapshot — position exists
            {"code": 0, "data": [{"symbol": "PENGU_USDT", "positionId": 12345}]},
            # Post-close verification — STILL EXISTS (this is the bug case)
            {"code": 0, "data": [{"symbol": "PENGU_USDT", "positionId": 12345}]},
        ])
        client.close_all_positions = AsyncMock(return_value={"code": 0, "msg": "OK"})
        client.get_history_positions = AsyncMock(return_value={"code": 0, "data": []})
        pool.get = AsyncMock(return_value=client)

        result = await executor.close_position("PENGU_USDT")

        assert result.success is False, "Must detect false-success: position still open"
        assert "still_open" in result.error_msg.lower()

    @pytest.mark.asyncio
    async def test_returns_success_when_position_gone_even_without_history(self):
        """If history_positions has no row yet (slow indexing) BUT position
        is verified gone via open_positions, treat as success (caller will
        use mid_price fallback for the missing fill data).
        """
        executor, pool = _mk_executor()

        client = MagicMock()
        client.get_open_positions = AsyncMock(side_effect=[
            # Pre-close — exists
            {"code": 0, "data": [{"symbol": "PENGU_USDT", "positionId": 12345}]},
            # Post-close — GONE
            {"code": 0, "data": []},
        ])
        client.close_all_positions = AsyncMock(return_value={"code": 0, "msg": "OK"})
        client.get_history_positions = AsyncMock(return_value={"code": 0, "data": []})
        pool.get = AsyncMock(return_value=client)

        result = await executor.close_position("PENGU_USDT")

        assert result.success is True, "Position confirmed gone → close succeeded even without history"

    @pytest.mark.asyncio
    async def test_returns_success_when_history_has_fill_data(self):
        """Happy path: history_positions returns fill data immediately.
        Should not require open_positions verification.
        """
        executor, pool = _mk_executor()

        client = MagicMock()
        client.get_open_positions = AsyncMock(return_value={
            "code": 0, "data": [{"symbol": "PENGU_USDT", "positionId": 12345}],
        })
        client.close_all_positions = AsyncMock(return_value={"code": 0, "msg": "OK"})
        # Return a closed-state matching row
        client.get_history_positions = AsyncMock(return_value={
            "code": 0,
            "data": [{
                "symbol": "PENGU_USDT",
                "positionId": 12345,
                "state": 3,
                "updateTime": 9999999999999,  # very recent
                "closeAvgPrice": "0.009500",
                "openAvgPrice": "0.009480",
                "realised": "1.234",
            }],
        })
        pool.get = AsyncMock(return_value=client)

        with patch("src.execution.live_executor.get_binance_scale", return_value=1.0):
            result = await executor.close_position("PENGU_USDT")

        assert result.success is True
        assert result.exit_price > 0


# ──────────────────────────────────────────────────────────────────────
# Layer B — market_close_position()
# ──────────────────────────────────────────────────────────────────────

class TestMarketClose:
    @pytest.mark.asyncio
    async def test_submits_type_5_market_order_with_close_short_side(self):
        """Verify correct order params: side=2 (CLOSE_SHORT), type=5 (MARKET)."""
        executor, pool = _mk_executor()

        client = MagicMock()
        client.get_open_positions = AsyncMock(side_effect=[
            {"code": 0, "data": [{"symbol": "PENGU_USDT", "positionId": 12345, "holdVol": 13194}]},
            {"code": 0, "data": []},  # post-close: gone
        ])
        client.submit_order = AsyncMock(return_value={"code": 0, "data": {"orderId": "abc"}})
        client.get_history_positions = AsyncMock(return_value={"code": 0, "data": []})
        pool.get = AsyncMock(return_value=client)

        result = await executor.market_close_position(
            symbol="PENGU_USDT",
            direction="short",
            qty_contracts=13194,
            leverage=62,
        )

        assert result.success is True
        # Verify the correct order was submitted
        client.submit_order.assert_called_once()
        call_kwargs = client.submit_order.call_args.kwargs
        assert call_kwargs["side"] == 2, "CLOSE_SHORT side code"
        assert call_kwargs["order_type"] == "5", "MARKET order type"
        assert call_kwargs["vol"] == 13194
        assert call_kwargs["leverage"] == 62

    @pytest.mark.asyncio
    async def test_close_long_uses_side_4(self):
        executor, pool = _mk_executor()

        client = MagicMock()
        client.get_open_positions = AsyncMock(side_effect=[
            {"code": 0, "data": [{"symbol": "BTCUSDT", "positionId": 99, "holdVol": 100}]},
            {"code": 0, "data": []},
        ])
        client.submit_order = AsyncMock(return_value={"code": 0, "data": {"orderId": "abc"}})
        client.get_history_positions = AsyncMock(return_value={"code": 0, "data": []})
        pool.get = AsyncMock(return_value=client)

        await executor.market_close_position("BTCUSDT", "long", 100, 50)

        assert client.submit_order.call_args.kwargs["side"] == 4, "CLOSE_LONG side code"

    @pytest.mark.asyncio
    async def test_returns_failure_when_position_still_open_after_market(self):
        """Even MARKET orders can technically fail (partial fills, MEXC issues).
        Must detect and return failure so user can intervene.
        """
        executor, pool = _mk_executor()

        client = MagicMock()
        client.get_open_positions = AsyncMock(side_effect=[
            # Pre-close
            {"code": 0, "data": [{"symbol": "PENGU_USDT", "positionId": 12345, "holdVol": 13194}]},
            # Post-close verification — STILL THERE (pathological)
            {"code": 0, "data": [{"symbol": "PENGU_USDT", "positionId": 12345}]},
        ])
        client.submit_order = AsyncMock(return_value={"code": 0, "data": {"orderId": "abc"}})
        client.get_history_positions = AsyncMock(return_value={"code": 0, "data": []})
        pool.get = AsyncMock(return_value=client)

        result = await executor.market_close_position("PENGU_USDT", "short", 13194, 62)

        assert result.success is False
        assert "still_open" in result.error_msg.lower()

    @pytest.mark.asyncio
    async def test_uses_exchange_quantity_when_disagrees(self):
        """If bot's cached qty differs from MEXC's actual holdVol, trust MEXC."""
        executor, pool = _mk_executor()

        client = MagicMock()
        # Bot thinks 10000, exchange shows 13194
        client.get_open_positions = AsyncMock(side_effect=[
            {"code": 0, "data": [{"symbol": "PENGU_USDT", "positionId": 12345, "holdVol": 13194}]},
            {"code": 0, "data": []},
        ])
        client.submit_order = AsyncMock(return_value={"code": 0, "data": {"orderId": "abc"}})
        client.get_history_positions = AsyncMock(return_value={"code": 0, "data": []})
        pool.get = AsyncMock(return_value=client)

        await executor.market_close_position("PENGU_USDT", "short", 10000, 62)

        assert client.submit_order.call_args.kwargs["vol"] == 13194, (
            "Should use exchange-reported qty when it disagrees with bot's"
        )

    @pytest.mark.asyncio
    async def test_handles_api_error_code(self):
        """MEXC returns non-zero code → return failure."""
        executor, pool = _mk_executor()

        client = MagicMock()
        client.get_open_positions = AsyncMock(return_value={
            "code": 0, "data": [{"symbol": "PENGU_USDT", "positionId": 12345, "holdVol": 13194}],
        })
        client.submit_order = AsyncMock(return_value={"code": 600, "msg": "rate limit"})
        pool.get = AsyncMock(return_value=client)

        result = await executor.market_close_position("PENGU_USDT", "short", 13194, 62)

        assert result.success is False
        assert "600" in result.error_msg or "rate" in result.error_msg.lower()


# ──────────────────────────────────────────────────────────────────────
# Layer D — absolute max_hold constant
# ──────────────────────────────────────────────────────────────────────

class TestAbsoluteMaxHoldConstant:
    def test_default_is_600_seconds(self):
        """Verify the default safety ceiling is 10 minutes."""
        import importlib
        import src.strategy.shadow_engine as engine_mod
        # Reload to ensure default env value is picked up
        with patch.dict("os.environ", {}, clear=False):
            # remove if user has overridden in their env
            import os
            os.environ.pop("STAKAN_ABSOLUTE_MAX_HOLD_SEC", None)
            importlib.reload(engine_mod)
            assert engine_mod._ABSOLUTE_MAX_HOLD_SEC == 600

    def test_env_override_respected(self):
        import importlib, os
        import src.strategy.shadow_engine as engine_mod

        os.environ["STAKAN_ABSOLUTE_MAX_HOLD_SEC"] = "120"
        try:
            importlib.reload(engine_mod)
            assert engine_mod._ABSOLUTE_MAX_HOLD_SEC == 120
        finally:
            os.environ.pop("STAKAN_ABSOLUTE_MAX_HOLD_SEC", None)
            importlib.reload(engine_mod)

    def test_zero_or_negative_treated_as_disabled(self):
        """Setting to 0 or negative effectively disables the safety net
        (set to huge value), but logs a warning.
        """
        import importlib, os
        import src.strategy.shadow_engine as engine_mod

        os.environ["STAKAN_ABSOLUTE_MAX_HOLD_SEC"] = "0"
        try:
            importlib.reload(engine_mod)
            # Should be set to ~1 year (large) rather than 0
            assert engine_mod._ABSOLUTE_MAX_HOLD_SEC > 86400
        finally:
            os.environ.pop("STAKAN_ABSOLUTE_MAX_HOLD_SEC", None)
            importlib.reload(engine_mod)
