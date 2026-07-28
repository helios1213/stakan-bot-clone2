"""Two slots on the SAME pair must each get their own alert.

send() drops a message when another of the same `category` went out inside
throttle_sec. The category used to be keyed on the pair alone, so once signals
fanned out to both live slots on one symbol, two genuine trades shared a key and
one alert was silently dropped.

Observed on the clone, 2026-07-28 22:07:43: slot1 and slot2 both opened and both
closed 1000PEPEUSDT in the same second, both rows are in live_trades, and only
slot2's [LIVE CLOSE] reached Telegram.

The throttle itself is deliberately kept — it still suppresses a repeat for the
same pair AND the same account inside a second.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.telegram_bot.alerts import TelegramAlerts


def _alerts() -> TelegramAlerts:
    with patch("src.telegram_bot.alerts.Bot"):
        a = TelegramAlerts(bot_token="x:y", owner_id=1, quiet_hours=None)
    a.bot.send_message = AsyncMock()
    return a


CLOSE = dict(symbol="1000PEPEUSDT", direction="long", entry_price=0.0028080,
             exit_price=0.0028080, exit_reason="simple_stalled", roi_pct=-0.06,
             net_pnl_usdt=-0.0349, duration_sec=3, mfe_pct=0.0, mae_pct=-0.01,
             duration_ms=3010, mode="live")

OPEN = dict(symbol="1000PEPEUSDT", direction="long", entry_price=0.0028080,
            leverage=45, margin_usdt=48.97, notional_usdt=2203.0, gap_ticks=5.0,
            detector_source="static_gap", mode="live")


@pytest.mark.asyncio
async def test_two_slots_closing_same_pair_both_alert():
    """The screenshot case: same symbol, same second, two accounts."""
    a = _alerts()
    await a.trade_close(**CLOSE, account_label="slot1")
    await a.trade_close(**CLOSE, account_label="slot2")
    assert a.bot.send_message.await_count == 2, "one slot's close was swallowed"
    assert a.total_throttled == 0


@pytest.mark.asyncio
async def test_two_slots_opening_same_pair_both_alert():
    a = _alerts()
    await a.trade_open(**OPEN, account_label="slot1")
    await a.trade_open(**OPEN, account_label="slot2")
    assert a.bot.send_message.await_count == 2
    assert a.total_throttled == 0


@pytest.mark.asyncio
async def test_same_slot_same_pair_is_still_throttled():
    """The throttle must keep doing its actual job."""
    a = _alerts()
    await a.trade_close(**CLOSE, account_label="slot1")
    await a.trade_close(**CLOSE, account_label="slot1")
    assert a.bot.send_message.await_count == 1
    assert a.total_throttled == 1


@pytest.mark.asyncio
async def test_different_pairs_still_independent():
    """The earlier fix (BCH close lost behind PENGU's) must not regress."""
    a = _alerts()
    await a.trade_close(**{**CLOSE, "symbol": "BCHUSDT"}, account_label="slot1")
    await a.trade_close(**{**CLOSE, "symbol": "PENGUUSDT"}, account_label="slot1")
    assert a.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_missing_account_label_does_not_collapse_with_a_named_slot():
    """A None label must be its own key, not merge into slot1's."""
    a = _alerts()
    await a.trade_close(**CLOSE, account_label=None)
    await a.trade_close(**CLOSE, account_label="slot1")
    assert a.bot.send_message.await_count == 2
