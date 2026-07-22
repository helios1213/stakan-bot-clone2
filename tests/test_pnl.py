"""Tests for src/utils/pnl.py"""
import pytest

from src.utils.pnl import calc_pnl_usdt, calc_roi_pct


class TestCalcPnlUsdt:
    def test_long_profit(self):
        # LONG, price went up: (101 - 100) * 10 = 10
        assert calc_pnl_usdt("long", entry=100.0, exit=101.0, qty=10.0) == 10.0

    def test_long_loss(self):
        # LONG, price went down: (99 - 100) * 10 = -10
        assert calc_pnl_usdt("long", entry=100.0, exit=99.0, qty=10.0) == -10.0

    def test_short_profit(self):
        # SHORT, price went down: (100 - 99) * 10 = 10
        assert calc_pnl_usdt("short", entry=100.0, exit=99.0, qty=10.0) == 10.0

    def test_short_loss(self):
        # SHORT, price went up: (100 - 101) * 10 = -10
        assert calc_pnl_usdt("short", entry=100.0, exit=101.0, qty=10.0) == -10.0

    def test_pepe_long_with_scale(self):
        # PEPE scaled prices: 0.004144 → 0.004149, 1M PEPE
        pnl = calc_pnl_usdt("long", entry=0.004144, exit=0.004149, qty=1_000_000)
        assert abs(pnl - 5.0) < 1e-9

    def test_fees_subtracted(self):
        # Gross profit 10, fees 0.5 → net 9.5
        pnl = calc_pnl_usdt("long", entry=100.0, exit=101.0, qty=10.0, fees_usdt=0.5)
        assert pnl == 9.5

    def test_zero_qty(self):
        assert calc_pnl_usdt("long", entry=100.0, exit=101.0, qty=0.0) == 0.0

    def test_zero_entry(self):
        # Defensive: bad data → 0 (don't crash)
        assert calc_pnl_usdt("long", entry=0.0, exit=101.0, qty=10.0) == 0.0

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError):
            calc_pnl_usdt("up", entry=100.0, exit=101.0, qty=10.0)


class TestCalcRoiPct:
    def test_basic(self):
        # $2.50 PnL on $25 margin → 10%
        assert calc_roi_pct(pnl_usdt=2.5, margin_usdt=25.0) == 10.0

    def test_loss(self):
        assert calc_roi_pct(pnl_usdt=-1.25, margin_usdt=25.0) == -5.0

    def test_zero_margin(self):
        # Don't divide by zero — return 0
        assert calc_roi_pct(pnl_usdt=10.0, margin_usdt=0.0) == 0.0

    def test_negative_margin(self):
        # Defensive
        assert calc_roi_pct(pnl_usdt=10.0, margin_usdt=-5.0) == 0.0
