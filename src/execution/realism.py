"""
Realism — bridge between shadow simulation and live trading.

Shadow trades by default use:
  - Zero latency (instant fill)
  - Idealized slippage estimates
  - No fees
  - 100% fill rate
  - No order rejection
  - No funding cost

Real live trading has all of these. This module provides realistic models
to bring shadow PnL closer to what live PnL would actually be.

Settings can be:
  - DISABLED for legacy shadow behavior (DEFAULT_PROFILE='legacy')
  - ENABLED with measured-from-real-trades values (DEFAULT_PROFILE='realistic')

Measured values (from micro_trade test, Tokyo VPS → MEXC):
  - signal_to_order_latency:  324 ms
  - order_to_fill_processing:  ~50 ms (MEXC server processes match)
  - close_latency:              60 ms (already-warm position close is faster)

Total realistic gap from signal to first fill: ~370-400ms
This is the DELAY during which the catch-up move can develop further,
making your entry potentially WORSE than signal price (if move continues)
or BETTER (if move reverses immediately — rare).

Configurable per-deployment via ShadowConf.realism_profile:
  'legacy'    — original behavior (no latency, no fees, no rejection)
  'realistic' — measured values applied
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class RealismProfile:
    """All realism knobs in one place."""

    # Latency (ms) — actual measurements from micro_trade test
    signal_to_order_latency_ms: int = 0      # delay before order reaches MEXC
    order_to_fill_latency_ms: int = 0        # MEXC processing time before fill confirmed
    close_signal_to_order_latency_ms: int = 0  # delay before close order reaches MEXC

    # Fees (per trade per side, fraction of notional)
    # 0 for MEXC zero-fee promo on alt-alts; ~0.0004 for normal taker
    entry_fee_pct: float = 0.0

    # Order rejection probability (0.0-1.0)
    base_rejection_rate: float = 0.0       # always rejected this %
    extreme_price_rejection_rate: float = 0.0  # additional if price >1% from mark

    # Funding cost simulation (if position crosses funding window)
    enable_funding_cost: bool = False
    typical_funding_rate: float = 0.0001   # 0.01% per 8h, average
    # Level 8: variable funding — randomly fluctuate around typical
    funding_rate_std: float = 0.00005      # ±0.005% std deviation

    # IOC retry latency (between attempts)
    ioc_retry_min_ms: int = 50
    ioc_retry_max_ms: int = 200

    # Level 6: Server timeout/error simulation (real MEXC has ~1-3% transient errors)
    # On timeout: order may have placed but no confirmation, treated as failure
    server_error_rate: float = 0.0         # 0.01-0.03 typical
    server_timeout_extra_ms: int = 0       # additional latency penalty on errors

    # Level 7: Spread widening events (news/breakout)
    # Each second has X% chance of spread blowout for next 2-5s
    spread_blowout_per_sec_chance: float = 0.0  # 0.0001 = ~once per 10000 sec on average
    spread_blowout_multiplier: float = 1.0       # how much spread widens (3-5x typical)
    spread_blowout_duration_sec_min: float = 2.0
    spread_blowout_duration_sec_max: float = 5.0


PROFILES: dict[str, RealismProfile] = {
    'legacy': RealismProfile(),  # all zeros — original shadow behavior

    'realistic': RealismProfile(
        # signal_to_order_latency_ms was 324ms, but the shadow_engine latency
        # sim already models 100-250ms entry latency from cfg.entry_latency_ms.
        # Stacking 324ms on top of it resulted in shadow latency ~424-574ms,
        # while real BCH live trades show median ~165-225ms (measured from
        # live_trades.latency_submit_ms). Shadow was 2-3x slower than reality,
        # killing shadow PnL on lead-lag arb where the gap closes within
        # 200-400ms. Zeroed here so the shadow_engine latency sim alone models
        # the entry network roundtrip — total shadow latency ~100-250ms, matching live.
        signal_to_order_latency_ms=0,
        order_to_fill_latency_ms=50,
        close_signal_to_order_latency_ms=60,
        entry_fee_pct=0.0,        # zero fee promo on alts
        base_rejection_rate=0.01,        # 1% baseline
        extreme_price_rejection_rate=0.05,
        enable_funding_cost=True,
        typical_funding_rate=0.0001,
        funding_rate_std=0.00005,
        # Level 6: server reliability
        server_error_rate=0.015,         # 1.5% transient errors (MEXC observed)
        server_timeout_extra_ms=2000,    # ~2s extra latency on retries
        # Level 7: spread blowouts (rare news/breakouts)
        spread_blowout_per_sec_chance=0.0001,  # ~1 per 10000s = 1 per 2.7h
        spread_blowout_multiplier=4.0,         # 4x spread widening
        spread_blowout_duration_sec_min=2.0,
        spread_blowout_duration_sec_max=5.0,
    ),
}


# Pairs known to have non-zero fees (e.g. BTC/ETH not in alt-alt promo)
# This lets us model true taker fees on majors, while alts keep zero fee
# Verified: alt-alts have 0% on MEXC promo
NON_ZERO_FEE_PAIRS = {
    # Majors typically have standard taker fee (~0.04%)
    # BTC, ETH on MEXC futures may NOT be in zero-fee promo
    # Conservative: assume zero unless we know otherwise
    # Empty for now — all whitelist pairs are alt-alt with confirmed 0% fee
}


def get_profile(name: str = 'realistic') -> RealismProfile:
    """Get a realism profile by name. Falls back to 'realistic' if unknown."""
    return PROFILES.get(name, PROFILES['realistic'])


def fee_pct_for_pair(symbol: str, profile: RealismProfile, side: str = 'taker') -> float:
    """Get fee % for a specific pair. Most alts = 0; majors may have taker fee."""
    if symbol in NON_ZERO_FEE_PAIRS:
        return 0.0004  # 0.04% standard taker
    return profile.entry_fee_pct


def should_reject_order(
    signal_price: float,
    mark_price: float,
    profile: RealismProfile,
) -> tuple[bool, str]:
    """
    Decide if this order should be 'rejected' by exchange.

    Returns (rejected, reason).
    Models real-world causes: insufficient margin, risk control, etc.
    """
    if profile.base_rejection_rate > 0 and random.random() < profile.base_rejection_rate:
        return True, "base_rejection"

    if mark_price > 0:
        price_deviation = abs(signal_price - mark_price) / mark_price
        if price_deviation > 0.01:  # >1% from mark
            if random.random() < profile.extreme_price_rejection_rate:
                return True, "extreme_price"

    return False, ""


def crosses_funding_window(
    opened_at_ms: int,
    closed_at_ms: int,
    profile: RealismProfile,
) -> bool:
    """
    Check if position is open during a funding payment window.

    MEXC funding times: 00:00, 08:00, 16:00 UTC (every 8 hours).
    """
    if not profile.enable_funding_cost:
        return False

    funding_hours_utc = [0, 8, 16]
    open_hour = (opened_at_ms // 1000 // 3600) % 24
    close_hour = (closed_at_ms // 1000 // 3600) % 24
    duration_hours = (closed_at_ms - opened_at_ms) / 3600000

    if duration_hours < 0.01:  # <36s — can't cross window
        return False

    # Simple: did we cross any of these hour marks?
    for fh in funding_hours_utc:
        if open_hour < fh <= close_hour or (close_hour < open_hour and (fh > open_hour or fh <= close_hour)):
            return True

    # Long positions (>8h) always cross at least one
    if duration_hours >= 8:
        return True

    return False


def calculate_funding_cost(
    notional_usdt: float,
    direction: str,
    profile: RealismProfile,
    realized_funding_rate: float | None = None,
) -> float:
    """
    Calculate funding payment for crossing a window.

    Long positions PAY when funding rate is positive.
    Short positions PAY when funding rate is negative.

    Returns cost in USDT (positive = our cost, negative = our credit).
    """
    if realized_funding_rate is not None:
        rate = realized_funding_rate
    elif profile.funding_rate_std > 0:
        # Level 8: variable funding rate within reasonable range
        rate = random.gauss(profile.typical_funding_rate, profile.funding_rate_std)
        # Clip to reasonable bounds (-0.1% to +0.1%)
        rate = max(-0.001, min(0.001, rate))
    else:
        rate = profile.typical_funding_rate

    if direction == 'long':
        return notional_usdt * rate
    else:  # short
        return -notional_usdt * rate


def simulate_server_error(profile: RealismProfile) -> tuple[bool, int]:
    """
    Level 6: Simulate transient MEXC server error (timeout/500).

    Returns (errored, extra_latency_ms).
    On error, the request is treated as failed — typically retried after delay.
    """
    if profile.server_error_rate <= 0:
        return False, 0

    if random.random() < profile.server_error_rate:
        # Add jitter to extra latency
        extra = int(profile.server_timeout_extra_ms * random.uniform(0.5, 1.5))
        return True, extra

    return False, 0


# Level 7: spread blowout state — module-level dict per symbol
# Format: {symbol: blowout_until_unix_ts}
_spread_blowout_until: dict[str, float] = {}


def check_and_apply_spread_blowout(
    symbol: str,
    current_time_sec: float,
    profile: RealismProfile,
) -> float:
    """
    Level 7: Check if spread is "blown out" right now (news/breakout event).

    Returns multiplier to apply to slippage (1.0 = normal, 4.0 = blown out 4x).

    Per-second chance of triggering a blowout. Once triggered, lasts 2-5s.
    """
    if profile.spread_blowout_per_sec_chance <= 0:
        return 1.0

    blowout_until = _spread_blowout_until.get(symbol, 0.0)

    # Already in blowout state?
    if current_time_sec < blowout_until:
        return profile.spread_blowout_multiplier

    # Roll for new blowout
    if random.random() < profile.spread_blowout_per_sec_chance:
        duration = random.uniform(
            profile.spread_blowout_duration_sec_min,
            profile.spread_blowout_duration_sec_max,
        )
        _spread_blowout_until[symbol] = current_time_sec + duration
        return profile.spread_blowout_multiplier

    return 1.0
