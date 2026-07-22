"""
PnL calculation helpers — centralised so the formula isn't duplicated across
shadow_engine fallbacks, shadow_position close, and any future entry points.

The bot operates on the same convention everywhere:
  - LONG:  pnl = (exit - entry) * qty
  - SHORT: pnl = (entry - exit) * qty
  - prices are in Binance-equivalent SCALED domain (multiplied by SYMBOL_SCALE_TO_BINANCE)
  - qty is in base-asset units after scaling

For live trades, fees on MEXC futures promo are 0% (both maker via IOC and
taker via close_all). Fees parameter is here for future-proofing.

Usage:
    from src.utils.pnl import calc_pnl_usdt, calc_roi_pct

    pnl = calc_pnl_usdt(direction="long", entry=100.0, exit=101.0, qty=2.5)
    # → 2.5

    roi = calc_roi_pct(pnl=2.5, margin=25.0)
    # → 10.0  (10% ROI on $25 margin)
"""
from __future__ import annotations


def calc_pnl_usdt(
    direction: str,
    entry: float,
    exit: float,
    qty: float,
    fees_usdt: float = 0.0,
) -> float:
    """
    Compute realised PnL in USDT for a closed position.

    direction: "long" or "short"
    entry / exit: prices in same domain (both raw or both scaled)
    qty: base-asset quantity (already accounts for contract size if relevant)
    fees_usdt: total fees paid (both entry and exit). Subtracted from gross PnL.

    Returns net PnL (positive = profit, negative = loss).
    """
    if entry <= 0 or qty <= 0:
        return 0.0
    if direction == "long":
        gross = (exit - entry) * qty
    elif direction == "short":
        gross = (entry - exit) * qty
    else:
        raise ValueError(f"Invalid direction: {direction!r} (expected 'long' or 'short')")
    return gross - fees_usdt


def calc_roi_pct(pnl_usdt: float, margin_usdt: float) -> float:
    """
    Return-on-margin percentage. Returns 0.0 if margin is zero (avoid div-by-zero).

    Note: this is ROI on margin (leveraged), NOT on notional.
    For 10% ROI at 50x leverage → price moved 0.2% in your favour.
    """
    if margin_usdt <= 0:
        return 0.0
    return (pnl_usdt / margin_usdt) * 100.0
