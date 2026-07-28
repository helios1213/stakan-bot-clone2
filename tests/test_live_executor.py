"""
Tests for live_executor — symbol conversion, vol calculation, error handling.
Does NOT make real API calls (uses mocks).
"""
import pytest
from unittest.mock import AsyncMock

from src.execution.live_executor import (
    LiveExecutor,
    calculate_vol_contracts,
    signal_direction_to_side,
    is_slot_level_error,
    SIDE_OPEN_LONG,
    SIDE_OPEN_SHORT,
)
from src.execution.live_safety import LiveSafetyController


def _quiet_poll_mocks(client):
    """Give the fill-poll endpoints safe dict returns.

    A bare AsyncMock returns AsyncMock children, so production calls like
    `resp.get("code")` on an un-configured poll response yield never-awaited
    coroutines (RuntimeWarning). Configuring them keeps the suite warning-free.
    """
    empty = {"code": 0, "data": []}
    client.get_order_deals = AsyncMock(return_value=dict(empty))
    client.get_open_positions = AsyncMock(return_value=dict(empty))
    client.get_history_positions = AsyncMock(return_value=dict(empty))
    return client


# ============================================================
# Slot-level error detection
# ============================================================

class TestSlotLevelErrorDetection:
    def test_risk_control_message(self):
        msg = "Position opening is unavailable until risk control verification is completed"
        assert is_slot_level_error(msg) is True

    def test_face_verification(self):
        assert is_slot_level_error("Please complete face verification") is True

    def test_kyc(self):
        assert is_slot_level_error("KYC required") is True

    def test_account_frozen(self):
        assert is_slot_level_error("Your account is frozen") is True

    def test_compliance_review(self):
        assert is_slot_level_error("compliance review in progress") is True

    def test_normal_error_not_detected(self):
        # These are transient, not slot-level
        assert is_slot_level_error("Insufficient margin") is False
        assert is_slot_level_error("Order would liquidate") is False
        assert is_slot_level_error("Invalid price") is False
        assert is_slot_level_error("Symbol not found") is False

    def test_none_message(self):
        assert is_slot_level_error(None) is False
        assert is_slot_level_error("") is False

    def test_case_insensitive(self):
        assert is_slot_level_error("RISK CONTROL CHECK") is True
        assert is_slot_level_error("Risk Control Check") is True


# ============================================================
# vol calculation
# ============================================================

class TestVolCalculation:
    def test_zec_at_typical_price(self):
        # ZEC at $432, contract_size=0.01 → 1 contract = $4.32
        # $250 notional → 250 / 4.32 ≈ 58 contracts
        vol = calculate_vol_contracts("ZEC_USDT", 250.0, 432.0)
        assert 55 <= vol <= 60

    def test_pepe_with_scale(self):
        # PEPE at $0.000012, contract_size=10000 → 1 contract = $0.12
        # $250 notional → 250 / 0.12 ≈ 2083 contracts
        vol = calculate_vol_contracts("PEPE_USDT", 250.0, 0.000012)
        assert 2000 <= vol <= 2200

    def test_ena_at_typical_price(self):
        # ENA at $0.10, contract_size=10 → 1 contract = $1.00
        # $250 notional → 250 contracts
        vol = calculate_vol_contracts("ENA_USDT", 250.0, 0.10)
        assert vol == 250

    def test_minimum_one_contract(self):
        # Even tiny notional gives at least 1 contract
        vol = calculate_vol_contracts("ZEC_USDT", 0.5, 432.0)
        assert vol >= 1

    def test_unknown_symbol_refuses_with_zero(self):
        # Unknown symbol — REFUSE (return 0) rather than guess sizing; the
        # caller's `if vol < 1` guard aborts the entry. (The old 1.0/price
        # fallback mis-sized a real order, so it was changed to refuse.)
        vol = calculate_vol_contracts("UNKNOWN_USDT", 100.0, 50.0)
        assert vol == 0


# ============================================================
# direction mapping
# ============================================================

class TestDirectionMapping:
    def test_long_maps_to_open_long(self):
        assert signal_direction_to_side("long") == SIDE_OPEN_LONG

    def test_short_maps_to_open_short(self):
        assert signal_direction_to_side("short") == SIDE_OPEN_SHORT


# ============================================================
# LiveExecutor with mocked client pool
# ============================================================

class TestLiveExecutorOpen:
    @pytest.mark.asyncio
    async def test_successful_open(self):
        # Mock client returning valid response
        mock_client = AsyncMock()
        mock_client.submit_order.return_value = {
            "code": 0,
            "data": {"orderId": "12345"},
        }
        _quiet_poll_mocks(mock_client)

        mock_pool = AsyncMock()
        mock_pool.get.return_value = mock_client

        executor = LiveExecutor(client_pool=mock_pool, slot_id=1)
        result = await executor.place_market_open(
            symbol="ZEC_USDT",
            direction="long",
            notional_usdt=250.0,
            leverage=50,
            current_price=432.0,
        )

        assert result.success is True
        assert result.order_id == "12345"
        assert result.fill_qty_contracts > 0
        assert executor.opens_succeeded == 1
        assert executor.opens_failed == 0

    @pytest.mark.asyncio
    async def test_api_error_response(self):
        # Mock returns code != 0 (e.g. insufficient margin)
        mock_client = AsyncMock()
        mock_client.submit_order.return_value = {
            "code": 1001,
            "msg": "Insufficient margin",
        }

        mock_pool = AsyncMock()
        mock_pool.get.return_value = mock_client

        executor = LiveExecutor(client_pool=mock_pool, slot_id=1)
        result = await executor.place_market_open(
            symbol="ZEC_USDT",
            direction="long",
            notional_usdt=250.0,
            leverage=50,
            current_price=432.0,
        )

        assert result.success is False
        assert result.error_code == 1001
        assert "Insufficient margin" in (result.error_msg or "")
        assert executor.opens_failed == 1

    @pytest.mark.asyncio
    async def test_no_client_in_pool(self):
        # Pool has no client for slot
        mock_pool = AsyncMock()
        mock_pool.get.return_value = None

        executor = LiveExecutor(client_pool=mock_pool, slot_id=1)
        result = await executor.place_market_open(
            symbol="ZEC_USDT",
            direction="long",
            notional_usdt=250.0,
            leverage=50,
            current_price=432.0,
        )

        assert result.success is False
        assert "slot 1" in (result.error_msg or "")


class TestLiveExecutorClose:
    @pytest.mark.asyncio
    async def test_successful_close(self):
        mock_client = AsyncMock()
        mock_client.close_all_positions.return_value = {"code": 0}
        _quiet_poll_mocks(mock_client)

        mock_pool = AsyncMock()
        mock_pool.get.return_value = mock_client

        executor = LiveExecutor(client_pool=mock_pool, slot_id=1)
        result = await executor.close_position("ZEC_USDT")

        assert result.success is True
        assert executor.closes_succeeded == 1


# ============================================================
# Safety controller
# ============================================================

class TestLiveSafety:
    def test_default_allows_open(self):
        ctl = LiveSafetyController(max_margin_per_trade_usdt=10.0)
        allowed, reason = ctl.can_open_live("ZECUSDT", margin_usdt=5.0)
        assert allowed is True

    def test_blocks_excessive_margin(self):
        ctl = LiveSafetyController(max_margin_per_trade_usdt=5.0)
        allowed, reason = ctl.can_open_live("ZECUSDT", margin_usdt=10.0)
        assert allowed is False
        assert "exceeds max" in reason

    def test_kill_switch_blocks_all_opens(self):
        ctl = LiveSafetyController()
        ctl.engage_kill("test reason", duration_sec=3600)
        allowed, reason = ctl.can_open_live("ZECUSDT", margin_usdt=5.0)
        assert allowed is False
        assert "kill_active" in reason

    def test_consecutive_losses_no_longer_kill(self):
        """The 5-in-a-row pause fired on ordinary variance, not on a bleed."""
        ctl = LiveSafetyController()
        for _ in range(10):
            ctl.record_close("ZECUSDT", pnl_usdt=-0.01)
        assert ctl.state.consecutive_losses == 10   # still counted for the alert
        assert ctl.is_killed() is False

    def test_winning_trade_resets_consec_losses(self):
        ctl = LiveSafetyController()
        ctl.record_close("ZECUSDT", pnl_usdt=-1.0)
        ctl.record_close("ZECUSDT", pnl_usdt=-1.0)
        ctl.record_close("ZECUSDT", pnl_usdt=2.0)  # win — reset
        assert ctl.state.consecutive_losses == 0

    def test_cumulative_loss_alone_no_longer_kills(self):
        """Only a fall from the session high-water mark halts a slot now."""
        ctl = LiveSafetyController(max_drawdown_usdt=20.0)
        for _ in range(6):
            ctl.record_close("ZECUSDT", pnl_usdt=-2.0)   # -12 total, peak 0
        assert ctl.is_killed() is False
        ctl.record_close("ZECUSDT", pnl_usdt=-9.0)       # -21 below the peak
        assert ctl.is_killed() is True

    def test_per_symbol_concurrent_limit(self):
        ctl = LiveSafetyController(max_concurrent_per_symbol=1)
        ctl.record_open("ZECUSDT")
        allowed, reason = ctl.can_open_live("ZECUSDT", margin_usdt=5.0)
        assert allowed is False
        assert "max_concurrent_per_symbol" in reason

    def test_release_kill(self):
        ctl = LiveSafetyController()
        ctl.engage_kill("test")
        assert ctl.is_killed() is True
        ctl.release_kill()
        assert ctl.is_killed() is False
