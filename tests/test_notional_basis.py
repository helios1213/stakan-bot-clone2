"""Межа просадки міряється на ЗАЛИТОМУ нотіоналі, не на замовленому.

Спіймано 2026-08-08: PENGU просадка $46.32 при межі $45.63. Одна з причин —
`record_close` отримував `margin × leverage` (ЗАМОВЛЕНО), тоді як заливалось
~69%. Межа виходила $45.63 замість ~$31 — на 46% вище, ніж каже «1% експозиції».

Це ще й неузгодженість: кожен bps у проєкті рахується на `notional_usdt`
(залитому), а єдиний запобіжник рахувався на замовленому.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.execution.live_safety import LiveSafetyController


def test_limit_follows_the_filled_size_not_the_ordered_one():
    """PENGU 2026-08-08: замовлено $4563, залито $3134.

    Стеля відсунута навмисно — інакше обидва числа впираються в $25 і різниця
    між залитим і замовленим стає невидимою. У бою стеля, звісно, діє.
    """
    c = LiveSafetyController(max_drawdown_usdt=999.0,
                             drawdown_pct_of_notional=0.01)
    c.record_close("PENGUUSDT", 0.0, notional_usdt=3134.0)
    assert c.drawdown_limit() == pytest.approx(31.34)
    assert c.drawdown_limit() < 45.63, "замовлений розмір завищував межу на 46%"


def test_partial_fills_do_not_inflate_the_limit():
    """Серія половинчастих наливок має тримати межу на реальній експозиції."""
    c = LiveSafetyController(max_drawdown_usdt=999.0,
                             drawdown_pct_of_notional=0.01)
    for _ in range(60):
        c.record_close("X", 0.0, notional_usdt=2000.0)   # залито
    assert c.drawdown_limit() == pytest.approx(20.0, rel=0.02)


def test_ema_smooths_a_single_tiny_fill():
    """Одна крихітна наливка не має обвалити межу — EMA 0.9/0.1 її згладжує.

    Це і був аргумент проти залитого розміру; перевіряємо, що він не спрацьовує.
    """
    c = LiveSafetyController(max_drawdown_usdt=999.0,
                             drawdown_pct_of_notional=0.01)
    for _ in range(40):
        c.record_close("X", 0.0, notional_usdt=3000.0)
    before = c.drawdown_limit()
    c.record_close("X", 0.0, notional_usdt=50.0)          # 1.7% наливки
    after = c.drawdown_limit()
    assert after > before * 0.88, f"одна наливка зрушила межу забагато: {before}->{after}"
