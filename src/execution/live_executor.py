"""
Live Executor — places real orders on MEXC futures via WebkeyClientPool.

Used by ShadowEngine when a pair's mode == 'live'. Otherwise the engine
falls back to its simulated IOC executor.

Safety design:
  - All operations have hard timeouts (15s open, 10s close).
  - On any error, we DO NOT retry blindly — we record the failure and
    skip the entry. Better to miss a trade than open a phantom position.
  - On close, we use /position/close_all (atomic on MEXC side, no race).
  - Position tracking is via MEXC's own orderId + position state, with
    periodic reconciliation against /position/open_positions.

Conversion notes:
  - notional_usdt → vol contracts:
    For ZEC_USDT, 1 contract = 0.01 ZEC. So if ZEC = $432:
      $1250 notional / ($432 * 0.01) = ~289 contracts? No, wrong.
      Actually for ZEC: 1 contract = 0.01 ZEC, 1 USDT margin × 50x = $50 notional
      → vol_contracts = notional / (price × contract_size)
      → for ZEC at $432, 1 contract worth = 0.01 × $432 = $4.32
      → notional $1250 / $4.32 = 289 contracts
    Need symbol-specific contract_size. We fetch it from /contract/detail
    or use a hardcoded table for known pairs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

# MEXC REST API returns RAW prices (e.g. PEPE = 0.000004144),
# but the rest of the bot operates on Binance-equivalent SCALED prices
# (PEPE = 0.004144). All price fields returned by _poll_fill_price and
# _poll_close_fill must be multiplied by get_binance_scale(mexc_symbol)
# before they leak out into pos.entry_price / pos.exit_price.
from src.exchanges.mexc_rest import get_binance_scale, to_binance
from src.env_config import env_float

logger = logging.getLogger(__name__)

# MEXC refuses opens under three codes that all mean "this account is opening
# positions too fast": 10014 and 9082 carry the same text under different
# numbers, 2036 is the per-contract order limit. Declared here because this is
# where they arrive; src.strategy.shadow_engine imports the list rather than
# keeping a second copy that could drift.
OPEN_FREQ_CODES = ("10014", "9082", "2036")

# Hardcoded contract sizes for known pairs.
# 1 contract = X units of base asset.
# Verified from MEXC futures spec.
CONTRACT_SIZES: dict[str, float] = {
    "ZEC_USDT": 0.01,
    "TAO_USDT": 0.01,
    "BCH_USDT": 0.01,
    "LINK_USDT": 1.0,
    "HYPE_USDT": 0.1,
    "ENA_USDT": 10.0,
    "PENGU_USDT": 10.0,    # 1 contract = 10 PENGU (verified via /contract/detail)
                           # via /api/v1/contract/detail: actual contractSize=10.
                           # Wrong value made the bot under-size positions 10x:
                           # logged notional $1283 but real on-exchange ~$128.
    "ASTER_USDT": 10.0,
    "PEPE_USDT": 10000.0,  # 1 contract = 10000 PEPE
    "SHIB_USDT": 1.0,      # 1 contract = 1000 SHIB (MEXC 1000, scale 1000; /contract/detail 2026-07-04)
    "DOGE_USDT": 100.0,    # 1 contract = 100 DOGE
    "BTC_USDT": 0.0001,
    "ETH_USDT": 0.001,
    "SUI_USDT": 1.0,       # 1 contract = 1 SUI
    "XLM_USDT": 10.0,      # 1 contract = 10 XLM (verified /contract/detail 2026-05-31)
    "ONDO_USDT": 10,      # 1 contract = 10 ONDO (verified /contract/detail 2026-06-19)
    "SOL_USDT":  0.1,     # 1 contract = 0.1 SOL (verified /contract/detail 2026-06-19)
    "XRP_USDT":  1,       # 1 contract = 1 XRP (verified /contract/detail 2026-06-19)
    "XRP_USDC":  1,       # 1 contract = 1 XRP (USDC)
    "AVAX_USDT": 0.1,      # 1 contract = 0.1 AVAX (verified /contract/detail 2026-06-07)
    "WLD_USDT": 1.0,       # 1 contract = 1 WLD
    "XMR_USDT": 0.01,      # 1 contract = 0.01 XMR (verified 2026-06-13) (verified /contract/detail 2026-06-13)
    # Стокові перпи (contractSize з /contract/detail 2026-08-11)
    "SKHYNIXSTOCK_USDT": 0.001,
    "SPCXSTOCK_USDT":    0.01,
    "SOXL_USDT":         0.01,
    "MUSTOCK_USDT":      0.01,
    "SNDKSTOCK_USDT":    0.001,
}


# MEXC requires order prices to be multiples of priceUnit.
# priceScale = decimals after the decimal point. Sending more precision than
# allowed → MEXC api_error_2015 (parameter error). Source: /api/v1/contract/detail.
PRICE_SCALES: dict[str, int] = {
    "PEPE_USDT": 10,   # priceUnit ≤ 1e-10
    "SHIB_USDT": 9,    # priceUnit=1e-09
    "DOGE_USDT": 5,    # priceUnit=1e-05
    "ENA_USDT": 5,     # priceUnit=1e-05
    "SKHYNIXSTOCK_USDT": 2,  # priceUnit=0.01 (stock)
    "SPCXSTOCK_USDT": 2,     # priceUnit=0.01 (stock)
    "SOXL_USDT": 2,          # priceUnit=0.01 (stock)
    "MUSTOCK_USDT": 2,       # priceUnit=0.01 (stock)
    "SNDKSTOCK_USDT": 2,     # priceUnit=0.01 (stock)
    "ZEC_USDT": 2,     # priceUnit=0.01
    "TAO_USDT": 2,
    "BCH_USDT": 2,
    "LINK_USDT": 3,    # priceUnit=0.001
    "HYPE_USDT": 3,    # priceUnit=0.001
    "PENGU_USDT": 6,   # priceUnit=1e-06
    "ASTER_USDT": 4,   # priceUnit=0.0001
    "BTC_USDT": 1,
    "ETH_USDT": 2,
    "SUI_USDT": 4,     # priceUnit=0.0001
    "XLM_USDT": 5,     # priceUnit=1e-05
    "WLD_USDT": 4,     # priceUnit=0.0001
    "XMR_USDT": 2,     # priceUnit=0.01
    "ONDO_USDT": 4,    # priceUnit=0.0001
    "SOL_USDT":  2,    # priceUnit=0.01
    "XRP_USDT":  4,    # priceUnit=0.0001
    "XRP_USDC":  4,    # priceUnit=0.0001
    "AVAX_USDT": 3,    # priceUnit=0.001
}


def format_price_for_mexc(price_raw: float, mexc_symbol: str) -> str:
    """
    Format a raw price for MEXC submit_order according to the contract's priceScale.
    Falls back to a permissive 10-decimal format for unknown symbols (may still
    trigger error 2015 — caller should add the symbol to PRICE_SCALES).
    """
    scale = PRICE_SCALES.get(mexc_symbol)
    if scale is None:
        # Unknown symbol — use permissive format (was original behavior)
        return f"{price_raw:.10f}".rstrip("0").rstrip(".") or f"{price_raw:.10f}"
    # Round to tick (use round() not int() to handle floating-point imprecision —
    # e.g. 0.10374 + 1e-5 evaluates to 0.10375 but int(0.10375/1e-5)=10374 due to float).
    return f"{round(price_raw, scale):.{scale}f}"


@dataclass
class LiveOrderResult:
    """Result of a live order placement."""
    success: bool
    order_id: str | None = None
    error_code: int | None = None
    error_msg: str | None = None
    fill_price: float = 0.0          # populated after polling for fill
    fill_qty_contracts: int = 0
    notional_usdt: float = 0.0
    latency_ms: int = 0              # API call latency (total from t0 to result)
    raw_response: dict[str, Any] = field(default_factory=dict)
    # latency breakdown (all ms):
    submit_latency_ms: int = 0       # t0 → POST returned (= response received)
    response_latency_ms: int = 0     # POST returned → parse done
    fill_poll_latency_ms: int = 0    # parse done → fill confirmed via poll
    # The limit price we ACTUALLY submitted, in the scaled domain (same domain
    # as fill_price). Computed inside place_ioc_open but never surfaced, so
    # entry_slippage_pct was written from a stub and came out identically 0.0 on
    # all 86,719 live rows — i.e. the column was write-only and we were blind to
    # how much worse than our limit we really fill. WRITE-ONLY for the order
    # path: nothing in placement or risk reads it.
    limit_price_scaled: float = 0.0
    # `time.perf_counter()` у ТУ САМУ мить, коли з книги знято BBO, з якого
    # виведено limit_price_scaled. Потрібен shadow_twin: без нього неможливо
    # дістати зі стрічки книгу віком «момент ціноутворення + затримка».
    # Раніше twin брав знімок книги на t0 і судив філ проти ліміту, виведеного
    # з ТОГО САМОГО обʼєкта книги, — тобто min(asks) <= best_ask+offset*tick
    # тотожно істинне, і симулятор не міг протухнути ЖОДНОГО разу (0 із 360).
    # WRITE-ONLY для ордерного шляху: нічого в розміщенні чи ризику це не читає.
    priced_at_perf: float = 0.0


@dataclass
class LiveClosePosResult:
    """Result of closing a position via /position/close_all."""
    success: bool
    error_code: int | None = None
    error_msg: str | None = None
    latency_ms: int = 0
    raw_response: dict[str, Any] = field(default_factory=dict)
    # real PnL captured from position snapshot just before close
    realized_pnl_usdt: float = 0.0
    exit_price: float = 0.0
    entry_price_confirmed: float = 0.0  # MEXC's holdAvgPrice before close
    # latency breakdown for close:
    submit_latency_ms: int = 0
    response_latency_ms: int = 0


# MEXC contract order types (from /api/v1/private/order/create spec)
#   1 = Limit (GTC by default — stays in book until filled or cancelled)
#   2 = Post-only maker (rejects if would cross spread as taker)
#   3 = IOC (Immediate-Or-Cancel — fill what's possible NOW, cancel rest)
#   4 = FOK (Fill-Or-Kill — fill ALL or cancel ALL, no partial)
#   5 = Market (taker fee, full slippage)
#   6 = Convert market
# was misnamed — type=1 is actually a regular GTC LIMIT, not IOC. The bot's
# behavior LOOKED like IOC because an aggressive entry offset made every limit
# cross the spread and fill instantly. PDF export from MEXC labels these as
# "LIMIT" (not IMMEDIATE_OR_CANCEL), confirming the misnomer.
# The friend's PDF shows IMMEDIATE_OR_CANCEL — proving they use type=3.
# Real IOC behavior matters most with PASSIVE placement (limit AT touch, not
# crossing): MEXC instantly cancels if not fillable now, instead of leaving the
# order resting in book.
ORDER_TYPE_MARKET = "5"
ORDER_TYPE_LIMIT = "1"            # plain LIMIT — currently unused; kept for ref.
ORDER_TYPE_IOC = "3"              # Real IOC — fill-now-or-cancel, no resting in book.
# Back-compat alias used by place_ioc_open. Re-aimed at the REAL IOC type now.
ORDER_TYPE_IOC_LIMIT = ORDER_TYPE_IOC

# MEXC sides (per their /order/create spec)
SIDE_OPEN_LONG = 1
SIDE_CLOSE_SHORT = 2
SIDE_OPEN_SHORT = 3
SIDE_CLOSE_LONG = 4

# MEXC openType
OPEN_TYPE_ISOLATED = 1



# IOC retry tuning.
# Static-gap signals have a half-life of 200-800ms — the gap exists because
# MEXC hasn't yet caught up to Binance, and once it does (or once another
# taker eats the touch we wanted), the entry premise is gone.
#
# IOC LIMIT at passive touch (LONG: limit = best_ask) misses the fill when
# the local OB snapshot diverges from MEXC's matching-engine view at submit
# time. Retries do NOT fix this — they wait while the gap either closed or
# moved further, and we'd end up entering on a STALE opportunity in a
# different market state.
#
# attempts + retry delay are now PER-PAIR (pair YAML execution:
# ioc_max_attempts / ioc_attempt_interval_ms via ConfigLoader), passed
# explicitly by the caller. These module constants are only fallbacks if a
# caller omits them — NOT env-driven.
IOC_DEFAULT_MAX_ATTEMPTS = 1
IOC_DEFAULT_RETRY_DELAY_MS = 0
IOC_FILL_POLL_TIMEOUT_SEC = 0.6
IOC_PRE_RETRY_POSITION_CHECK = True

# Phantom-fill guard: after a believed-EXPIRED IOC open (fill-poll saw no fill),
# a BACKGROUND task re-checks the order's ACTUAL deals after this delay. If the
# order really filled (poll/WS missed a late-settling fill), the position is
# naked → flatten it. Fully off the hot path (fire-and-forget) = ZERO added open
# latency; one extra read-only deal_details GET per expired IOC (not the
# /order/create rate-limit). This is the direct guard against the 2026-07-20
# PEPE liquidation (believed-expired IOC that actually filled → 49min naked
# short → −$25.82 liquidation, with no bot record and no TG alert).
PHANTOM_FILL_CHECK_DELAY_SEC = env_float("PHANTOM_FILL_CHECK_DELAY_SEC", 2.5, lo=0.0)
# Multi-window phantom re-check: a believed-EXPIRED IOC can have its fill/deal
# land on MEXC AFTER the first (2.5s) window, so a single check left the naked
# position to the 60s reconcile. Re-check at these ABSOLUTE delays (seconds) and
# flatten the moment a fill appears (early-exit). A genuine no-fill polls every
# window (the safety cost — cheap reads); persistent unreadable falls through to
# the 60s periodic reconcile backstop. Override via env (comma list). (2026-08-13.)
PHANTOM_FILL_CHECK_DELAYS_SEC = [
    float(x) for x in os.environ.get(
        "PHANTOM_FILL_CHECK_DELAYS_SEC",
        f"{PHANTOM_FILL_CHECK_DELAY_SEC},6,12").split(",") if x.strip()
]

# Sleep between /position/open_positions polls inside _poll_fill_price
# while waiting for an IOC fill to appear. Each poll is a network roundtrip
# of ~150–300ms; MEXC's fill-visibility lag is ~50–200ms after submit
# returns orderId. 0.05s sleep gives ~3 polls in a 0.6s window. Override:
#   IOC_FILL_POLL_INTERVAL_SEC=0.05 docker compose up -d
# Trade-off: higher poll rate against MEXC's /position/open_positions. At
# 0.05s sleep + ~200ms per request, real cadence is ~250ms (network-bound,
# not sleep-bound), so this is well within practical rate limits. Override
# via env if needed:
#   IOC_FILL_POLL_INTERVAL_SEC=0.05 docker compose up -d
IOC_FILL_POLL_INTERVAL_SEC = float(
    os.environ.get("IOC_FILL_POLL_INTERVAL_SEC", "0.05")
)

# the FIRST sleep in _poll_fill_price uses a tighter
# interval than the rest of the loop. Rationale: deal_details/{order_id}
# typically surfaces 30–100ms after submit returns. A 50ms first sleep
# means the bot waits at minimum 50ms even when MEXC was ready at 35ms.
# 25ms first sleep matches MEXC's typical first-visibility window and shaves
# 25ms off the median fill confirmation, with no measurable downside.
# Subsequent polls use IOC_FILL_POLL_INTERVAL_SEC (50ms default) — once
# the first quick check missed, the trade-table is probably still settling
# and a faster cadence won't help, but will increase rate-pressure on MEXC.
# Override via env:
#   IOC_FILL_POLL_INTERVAL_FIRST_SEC=0.025 docker compose up -d
IOC_FILL_POLL_INTERVAL_FIRST_SEC = float(
    os.environ.get("IOC_FILL_POLL_INTERVAL_FIRST_SEC", "0.025")
)

# Private-WS fill push (MexcPrivateWS): when enabled and a private WS is wired,
# the entry path tries to learn the IOC fill from a server PUSH
# (push.personal.order, keyed by orderId) BEFORE the REST poll. Saves the
# ~25ms first-poll wait + the poll roundtrip, and cuts REST volume → fewer 510
# rate-limits. Falls back to _poll_fill_price if the push doesn't arrive within
# the wait window, so behaviour is unchanged whenever the WS is down/missed.
# Kill-switch: PRIVATE_WS_FILL=0 disables it (pure REST poll, pre-change path).
PRIVATE_WS_FILL_ENABLED = os.environ.get("PRIVATE_WS_FILL", "1") == "1"
# How long to wait for the fill push before falling back to the REST poll.
# Kept short: by the time submit returns the orderId (~150-200ms), a fill push
# is usually ALREADY buffered (resolves in ~0ms); observed real fills land
# within 0-68ms. A short window catches ~all of them while bounding the extra
# slot-lock hold on the common expired-IOC case (which finds no push and falls
# through to the REST poll). 100ms is the catch-all-fills / minimal-stall knee.
PRIVATE_WS_FILL_WAIT_SEC = env_float("PRIVATE_WS_FILL_WAIT_MS", 100.0, lo=0.0) / 1000.0

# WS-expiry-trust: when the private WS pushes a TERMINAL order with dealVol==0
# (state=4 cancelled = IOC expired, confirmed by live probe 2026-06-07), trust
# it as a definitive no-fill and SKIP the ~600ms REST fill-poll. Most IOCs
# expire, so this removes the bulk of REST GET volume → fewer 510 rate-limits
# → higher live fill rate. Safe: state 3=filled / 4=cancelled are mutually
# exclusive, and a partial fill arrives as dealVol>0 (caught by the fill
# branch); only a true zero-fill terminal trips this. No push → REST fallback
# unchanged. Kill-switch: PRIVATE_WS_TRUST_EXPIRY=0.
PRIVATE_WS_TRUST_EXPIRY = os.environ.get("PRIVATE_WS_TRUST_EXPIRY", "1") == "1"

# Private-WS CLOSE-fill push (push.personal.position state=3): the close path
# tries the WS position-close push BEFORE the REST _poll_close_fill. Measured
# ~300-390ms faster than the history_positions REST poll (which needs 4-5 polls
# + slow position-history visibility). Falls back to REST if the push doesn't
# arrive in the wait window, so a missed event NEVER strands a position as open.
# Kill-switch: PRIVATE_WS_CLOSE_FILL=0.
PRIVATE_WS_CLOSE_FILL_ENABLED = os.environ.get("PRIVATE_WS_CLOSE_FILL", "1") == "1"
PRIVATE_WS_CLOSE_WAIT_SEC = env_float("PRIVATE_WS_CLOSE_WAIT_MS", 400.0, lo=0.0) / 1000.0

# symmetric to fill-poll-fast but for the exit
# path. _poll_close_fill polls /position/list/history_positions waiting for
# MEXC to surface the freshly-closed position with closeAvgPrice/realised.
# Was hard-coded 0.25s. Bigger gain potential than entry-side because the
# poll window is 8s (vs 0.6s for entry), so a 5x faster cadence translates
# directly into faster PnL realisation across more potential poll cycles.
# Caveat: /position/list/history_positions has higher MEXC-side propagation
# delay than /open_positions (rows need to settle to history). If MEXC
# itself is the bottleneck (typical first-row-visible at T=300–800ms after
# close), the sleep tightening helps less than the entry case. Real
# expected wins ~50–150ms per close vs the entry-side 75–100ms — measure
# in the [CLOSE FILL] log timestamps before assuming.
# Trade-off: 8s window × 0.05s cadence → up to ~32 polls worst case (vs
# ~17 at 0.25s). Still well within practical limits. Override via env:
#   CLOSE_FILL_POLL_INTERVAL_SEC=0.05 docker compose up -d
CLOSE_FILL_POLL_INTERVAL_SEC = float(
    os.environ.get("CLOSE_FILL_POLL_INTERVAL_SEC", "0.05")
)

# ENTRY EXECUTION MODE — per pair, driven by cfg.ioc_offset_ticks (TICK-EXACT,
# passed as offset_ticks to place_ioc_open). Replaced the old bps-based offset
# 2026-06: ticks are price-independent (bps drifted with price) and collapse the
# former two-branch (cross / passive) logic into a single formula.
#   offset_ticks == 0 → at-touch. IOC limit at the opposite-side touch
#     (LONG = best_ask, SHORT = best_bid). Fills only if liquidity is still there
#     at submit; if price moved a tick during latency → IOC cancels. Converts
#     execution slippage into "skipped signal" — never pay worse than the touch.
#   offset_ticks  > 0 → cross the spread by N ticks (limit = ask+N×tick /
#     bid−N×tick). Fills more, incl. winners (kills adverse selection), at the
#     cost of bounded N-tick slippage. Promo fee=0 → no taker-fee drag.
#   offset_ticks  < 0 → rest INSIDE the spread (maker-style, less aggressive).
#   NB: a *global* aggressive default previously generated SL losers — that's
#   why crossing is opt-in per pair via cfg.ioc_offset_ticks, not a blanket mode.

# Fee guard: this strategy only has edge while MEXC charges 0% maker fee.
# If ANY fill comes back with a fee above this dust threshold (USDT), the
# premise is broken — the slot's live trading is halted automatically.
# Real taker/maker fees are orders of magnitude larger than 1e-6, so this
# only filters float dust, never a genuine 0-fee fill.
FEE_GUARD_EPSILON_USDT = 1e-6

# Mapping: symbol → tick size (priceUnit). Used by passive placement.
# Synced with PRICE_SCALES — these are 10^(-priceScale).
TICK_SIZES: dict[str, float] = {
    "PEPE_USDT": 1e-10,   # priceScale=10
    "SHIB_USDT": 1e-9,   # priceScale=9
    "DOGE_USDT": 1e-5,   # priceScale=5
    "ENA_USDT":  1e-5,    # priceScale=5
    "ZEC_USDT":  1e-2,    # priceScale=2
    "TAO_USDT":  1e-2,
    "BCH_USDT":  1e-2,
    "LINK_USDT": 1e-3,    # priceScale=3
    "HYPE_USDT": 1e-3,    # priceScale=3
    "PENGU_USDT": 1e-6,   # priceScale=6
    "ASTER_USDT": 1e-4,   # priceScale=4
    "BTC_USDT":  1e-1,
    "ETH_USDT":  1e-2,
    "SUI_USDT":  1e-4,    # priceScale=4
    "XLM_USDT":  1e-5,    # priceScale=5
    "SOL_USDT":  1e-2,    # priceScale=2 (was fallback 1e-5 = 1000x off)
    "XRP_USDT":  1e-4,    # priceScale=4 (was fallback 1e-5 = 10x off)
    "XRP_USDC":  1e-4,    # USDC variant (same tick as USDT)
    "AVAX_USDT": 1e-3,    # priceScale=3 (was fallback 1e-5 = 100x off)
    "ONDO_USDT": 1e-4,    # priceScale=4 (fixed 2026-06-19)
    "WLD_USDT":  1e-4,    # priceScale=4 (was fallback 1e-5 = 10x off, fixed 2026-06-19)
    "XMR_USDT":  1e-2,    # priceScale=2 (was fallback 1e-5 = 1000x off, fixed 2026-06-19)
    # Стокові перпи (priceUnit=0.01 з /contract/detail 2026-08-11)
    "SKHYNIXSTOCK_USDT": 1e-2,
    "SPCXSTOCK_USDT":    1e-2,
    "SOXL_USDT":         1e-2,
    "MUSTOCK_USDT":      1e-2,
    "SNDKSTOCK_USDT":    1e-2,
}


def get_tick_size(symbol: str) -> float:
    """Tick size in RAW MEXC price domain. Falls back to 1e-5 for unknown symbols."""
    return TICK_SIZES.get(symbol, 1e-5)


def calculate_vol_contracts(
    symbol: str,
    notional_usdt: float,
    price: float,
) -> int:
    """
    Convert notional USDT amount to integer number of contracts.

    Uses a hardcoded contract-size lookup. Returns 0 for an UNKNOWN symbol
    (refuse-to-guess — the caller's `if vol < 1` guard aborts the entry rather
    than mis-size; see below). For a known symbol, clamps to at least 1.
    """
    contract_size = CONTRACT_SIZES.get(symbol)
    if contract_size is None:
        # REFUSE rather than guess. The old 1.0/price fallback mis-sized a
        # REAL order (over-ordered by ~price*realContractSize). Return 0 so the
        # caller's `if vol < 1` guard aborts the entry. Add the pair to
        # CONTRACT_SIZES (verify via /contract/detail) before trading it live.
        logger.error(
            "REFUSING live order: %s missing from CONTRACT_SIZES — "
            "add its contractSize before live trading", symbol,
        )
        return 0

    if price <= 0 or contract_size <= 0:
        return 1

    contract_value_usdt = price * contract_size
    if contract_value_usdt <= 0:
        return 1

    vol = max(1, int(round(notional_usdt / contract_value_usdt)))
    return vol


def signal_direction_to_side(direction: str) -> int:
    """Map our direction string to MEXC side code for OPENING a position."""
    return SIDE_OPEN_LONG if direction == "long" else SIDE_OPEN_SHORT


# Account-level errors that indicate the slot itself has a problem.
# These are NOT pair-specific — the entire slot is unusable until the user
# resolves them on MEXC (face verification, risk check, account frozen, etc.)
# Detection by case-insensitive substring match on MEXC error message.
SLOT_LEVEL_ERROR_KEYWORDS = (
    "risk control",          # "Position opening is unavailable until risk control verification is completed"
    "verification",          # face verification required
    "kyc",                   # KYC not completed
    "frozen",                # account frozen / suspended
    "suspended",
    "locked",                # withdraw/trade locked
    "trading restricted",
    "compliance review",
    "identity verification",
)


def is_slot_level_error(msg: str | None) -> bool:
    """
    Check if a MEXC error message indicates an account-level problem
    (face verification, risk control, KYC) rather than a transient/pair-specific issue.
    """
    if not msg:
        return False
    lower = msg.lower()
    return any(kw in lower for kw in SLOT_LEVEL_ERROR_KEYWORDS)


async def _poll_fill_price(
    client,
    symbol: str,
    order_id: str | None = None,
    timeout_sec: float = 2.0,
    interval_sec: float = IOC_FILL_POLL_INTERVAL_SEC,
    interval_first_sec: float | None = None,
    fee_out: list[float] | None = None,
) -> tuple[float, int]:
    """
    Poll MEXC for the actual avg fill price after a successful order so
    we can compute REAL notional and PnL.

    Returns Binance-equivalent SCALED price (raw × scale), consistent
    with how the rest of the bot represents prices internally. Also
    returns the REAL holdVol (filled contracts) so the caller can detect
    IOC partial fills.

    Poll cadence configurable via `interval_sec` and `interval_first_sec`.

    order_id-aware: when an order_id is provided, primary read goes to
    /order/deal_details/{order_id}. This returns ONLY the deals for the
    specific IOC order — much smaller payload than /position/open_positions
    (~300 bytes vs ~5–20 KB) and faster MEXC-side visibility (trade table
    settles before position aggregator). Aggregates partials into a
    weighted-avg fill price client-side. Falls back to get_open_positions
    on any deal_details miss, so callers that legitimately have no
    order_id (e.g. MARKET reconciliation) continue working unchanged.

    Adaptive interval: first sleep = `interval_first_sec` (default 25ms),
    subsequent sleeps = `interval_sec` (default 50ms). Most fills are
    visible 30–100ms after submit returns, so the first quick check
    catches them sooner without spamming MEXC on the slow tail.

    Args:
        client: MexcWebClient
        symbol: e.g. "ZEC_USDT" — used for scale lookup and position-fallback filter
        order_id: orderId returned by submit_order. If None, skip deal_details
                  and go straight to the open_positions cascade.
        timeout_sec: total wall-clock budget for fill confirmation
        interval_sec: sleep between polls #2..N
        interval_first_sec: sleep between poll #1 and poll #2. Defaults to
                            module-level IOC_FILL_POLL_INTERVAL_FIRST_SEC.

    Returns:
        (scaled_avg_fill_price, total_filled_contracts).
        (0.0, 0) on timeout/no-fill.
    """
    if interval_first_sec is None:
        interval_first_sec = IOC_FILL_POLL_INTERVAL_FIRST_SEC

    scale = get_binance_scale(symbol)
    deadline = time.monotonic() + timeout_sec
    poll_count = 0

    while time.monotonic() < deadline:
        poll_count += 1

        # Primary read: deal_details/{order_id} — small, targeted, faster
        # MEXC visibility. Skip only if caller didn't provide an order_id.
        if order_id is not None:
            try:
                resp = await client.get_order_deals(order_id)
                if resp.get("code") == 0:
                    deals = resp.get("data") or []
                    if deals:
                        # Aggregate partials: weighted-avg price by vol.
                        # Each deal: {price, vol, ...}. price is RAW (MEXC
                        # native), needs ×scale to become Binance-equivalent.
                        total_vol = 0
                        weighted_price_sum = 0.0
                        total_fee = 0.0
                        for d in deals:
                            try:
                                p = float(d.get("price", 0) or 0)
                                v = int(d.get("vol", 0) or 0)
                            except (TypeError, ValueError):
                                continue
                            # Fee is reported per partial fill. MEXC charges 0
                            # under the maker promo; a non-zero value means the
                            # account is being charged (see fee guard).
                            try:
                                total_fee += float(d.get("fee", 0) or 0)
                            except (TypeError, ValueError):
                                pass
                            if p > 0 and v > 0:
                                total_vol += v
                                weighted_price_sum += p * v
                        if total_vol > 0 and weighted_price_sum > 0:
                            if fee_out is not None:
                                fee_out.append(total_fee)
                            avg_price_raw = weighted_price_sum / total_vol
                            return avg_price_raw * scale, total_vol
            except Exception:
                # deal_details temporarily unavailable (network blip, MEXC
                # 5xx, etc.) — fall through to open_positions fallback below.
                # Do NOT log.exception here; the fallback is the normal path
                # on cold-cache misses and noise would be high.
                pass

            # Fallback: open_positions (legacy path). Useful when deal_details
            # hasn't surfaced yet — rare per MEXC's write ordering but cheap
            # insurance.
            try:
                resp = await client.get_open_positions()
                positions = resp.get("data", []) if resp.get("code") == 0 else []
                for p in positions:
                    if p.get("symbol") == symbol:
                        avg_price = float(p.get("holdAvgPrice", 0) or 0)
                        hold_vol = int(float(p.get("holdVol", 0) or 0))
                        if avg_price > 0 and hold_vol > 0:
                            return avg_price * scale, hold_vol
            except Exception:
                pass
        else:
            # Legacy path: no order_id available. Use open_positions only.
            try:
                resp = await client.get_open_positions()
                positions = resp.get("data", []) if resp.get("code") == 0 else []
                for p in positions:
                    if p.get("symbol") == symbol:
                        avg_price = float(p.get("holdAvgPrice", 0) or 0)
                        hold_vol = int(float(p.get("holdVol", 0) or 0))
                        if avg_price > 0 and hold_vol > 0:
                            return avg_price * scale, hold_vol
            except Exception:
                pass

        # Adaptive interval: first wait is shorter (catch the typical
        # 30–100ms visibility window without slamming MEXC), then settle
        # into the standard cadence.
        sleep_for = interval_first_sec if poll_count == 1 else interval_sec
        await asyncio.sleep(sleep_for)

    return 0.0, 0


async def _poll_close_fill(
    client,
    symbol: str,
    position_id: int | None,
    close_after_ts_ms: int,
    timeout_sec: float = 8.0,
    interval_sec: float = CLOSE_FILL_POLL_INTERVAL_SEC,
) -> tuple[float, float, float]:
    """
    Poll /position/list/history_positions to find the freshly closed
    position and return (closeAvgPrice, realised, openAvgPrice) — the
    EXCHANGE-AUTHORITATIVE exit/entry prices and realized PnL.

    closeAvgPrice and openAvgPrice are SCALED to Binance-equivalent
    (raw × scale) so they line up with pos.entry_price / pos.exit_price
    elsewhere. realised is in USDT (absolute) and is NOT scaled.

    Args:
        position_id: positionId from snapshot before close (best filter,
                     can be None — fallback to most-recent matching symbol).
        close_after_ts_ms: only consider rows with updateTime >= this.

    Returns:
        (close_avg_price, realised_pnl, open_avg_price). All 0.0 if not found.
    """
    scale = get_binance_scale(symbol)
    deadline = time.monotonic() + timeout_sec
    poll_n = 0
    last_resp_summary = "no response yet"
    while time.monotonic() < deadline:
        poll_n += 1
        try:
            resp = await client.get_history_positions(symbol=symbol, page_size=10)
            code = resp.get("code", -1)
            rows = resp.get("data", []) or []
            if code != 0:
                last_resp_summary = f"code={code} msg={resp.get('msg')}"
            else:
                samples = []
                for p in rows[:3]:
                    samples.append(
                        f"pid={p.get('positionId')} state={p.get('state')} "
                        f"ut={p.get('updateTime')} closeAvg={p.get('closeAvgPrice')} "
                        f"openAvg={p.get('openAvgPrice')} realised={p.get('realised')}"
                    )
                last_resp_summary = f"n_rows={len(rows)} | first_3=[{' || '.join(samples)}]"
                for p in rows:
                    if p.get("symbol") != symbol:
                        continue
                    if int(p.get("state", 0) or 0) != 3:
                        continue
                    update_ts = int(p.get("updateTime", 0) or 0)
                    # MEXC rounds updateTime to whole seconds,
                    # so a position closed milliseconds before our close_request_ts
                    # gets a stale `ut` that's 1-999ms < our ts. Strict `<` filter
                    # then rejects the correct row and triggers FALLBACK to
                    # mid_price PnL — causing 12-30% PnL discrepancy vs MEXC UI.
                    # Allow up to 5s tolerance — between-trade gaps are minutes,
                    # so we won't accidentally match the previous trade.
                    if update_ts < (close_after_ts_ms - 5000):
                        continue
                    if position_id is not None and position_id > 0:
                        if int(p.get("positionId", 0) or 0) != position_id:
                            continue
                    close_avg_raw = float(p.get("closeAvgPrice", 0) or 0)
                    realised = float(p.get("realised", 0) or 0)
                    open_avg_raw = float(p.get("openAvgPrice", 0) or 0)
                    if close_avg_raw > 0:
                        # Scale to Binance-equivalent before returning
                        close_avg = close_avg_raw * scale
                        open_avg = open_avg_raw * scale
                        logger.info(
                            "[CLOSE FILL] %s matched after %d polls: positionId=%s "
                            "openAvgPrice=%.6f closeAvgPrice=%.6f realised=$%+.4f "
                            "(scale=%g, raw open=%.8f close=%.8f)",
                            symbol, poll_n, p.get("positionId"),
                            open_avg, close_avg, realised,
                            scale, open_avg_raw, close_avg_raw,
                        )
                        return close_avg, realised, open_avg
        except Exception as e:
            last_resp_summary = f"exception: {type(e).__name__}: {e}"
            logger.debug("history_positions poll failed", exc_info=True)
        await asyncio.sleep(interval_sec)
    logger.warning(
        "[CLOSE FILL] %s timeout after %d polls (target_pid=%s after_ts=%d). "
        "Last response: %s",
        symbol, poll_n, position_id, close_after_ts_ms, last_resp_summary,
    )
    return 0.0, 0.0, 0.0





class LiveExecutor:
    """
    Real MEXC order placement using WebkeyClientPool.

    Designed to be a drop-in replacement for IOCExecutor.simulate_ioc_entry()
    with similar return shape but as async (because real API calls).

    Usage:
        executor = LiveExecutor(client_pool=pool, slot_id=1)
        result = await executor.place_market_open(
            symbol="ZEC_USDT",
            direction="long",
            notional_usdt=250.0,
            leverage=50,
            current_price=432.0,
        )
    """

    # Class-level default so construction paths that bypass __init__ (e.g.
    # tests building via LiveExecutor.__new__) still see a safe value on the
    # fill hot path. Real instances set it in __init__.
    private_ws_pool = None

    def __init__(
        self,
        client_pool,  # WebkeyClientPool
        slot_id: int = 1,
        order_timeout_sec: float = 15.0,
        close_timeout_sec: float = 10.0,
        webkey_store=None,  # Optional[WebkeyStore] — for slot-level error tracking
        alerts=None,        # Optional[TelegramAlerts] — for instant notifications
        private_ws_pool=None,  # Optional[MexcPrivateWSPool] — push-based fills
    ) -> None:
        self.client_pool = client_pool
        self.slot_id = slot_id
        self.order_timeout_sec = order_timeout_sec
        self.close_timeout_sec = close_timeout_sec
        self.webkey_store = webkey_store
        self.alerts = alerts
        self.private_ws_pool = private_ws_pool

        # Fee guard: set True once a non-zero MEXC fee is observed on a fill.
        # While True, place_ioc_open refuses to open (slot also disabled in DB).
        self._halted = False

        # Fire-and-forget phantom-fill checks (strong refs so the GC can't kill
        # an in-flight check before it flattens a naked position).
        self._phantom_tasks: set[asyncio.Task] = set()
        # Символи, де останній IOC завершився НЕВІДОМО (біржа не сказала ні
        # 'налився', ні 'протух') і фантом-перевірка ще не дала відповіді.
        # Поки триває — нових відкриттів по цьому символу не робимо.
        self._phantom_unknown: set[str] = set()

        # Stats
        self.opens_attempted = 0
        self.opens_succeeded = 0
        self.opens_failed = 0
        self.closes_attempted = 0
        self.closes_succeeded = 0
        self.closes_failed = 0
        self.last_error: str | None = None
        # Per-slot account-level error (face verification, risk control, etc.)
        self.slot_level_error: str | None = None
        self.slot_level_error_at_ts: int = 0

        # Surface IOC fill-poll config on startup (verify deploy at a glance).
        # NB: entry offset / max_attempts / retry_delay are now PER-PAIR
        # (pair YAML execution: ioc_offset_ticks / ioc_max_attempts /
        # ioc_attempt_interval_ms via ConfigLoader), no longer global env.
        if slot_id == 1:  # only log once per process — slot 1 is always present
            logger.info(
                "IOC fill-poll config: FILL_POLL_TIMEOUT=%.1fs FILL_POLL_INTERVAL=%.3fs CLOSE_FILL_POLL_INTERVAL=%.3fs",
                IOC_FILL_POLL_TIMEOUT_SEC, IOC_FILL_POLL_INTERVAL_SEC,
                CLOSE_FILL_POLL_INTERVAL_SEC,
            )

    async def place_market_open(
        self,
        symbol: str,                 # MEXC format: "ZEC_USDT"
        direction: str,              # "long" or "short"
        notional_usdt: float,
        leverage: int,
        current_price: float,
        open_type: int = OPEN_TYPE_ISOLATED,  # isolated is safer than cross
    ) -> LiveOrderResult:
        """
        Place market order to OPEN a position.

        Returns LiveOrderResult with success flag, orderId, error info.
        Note: fill_price is NOT yet populated — caller must poll for it
        via /order/list/open_orders or /position/open_positions if needed.
        For our use case (immediate close after hold), we can use
        the position's avg_price reported by close_all response.
        """
        self.opens_attempted += 1

        side = signal_direction_to_side(direction)
        vol = calculate_vol_contracts(symbol, notional_usdt, current_price)

        if vol < 1:
            self.opens_failed += 1
            return LiveOrderResult(
                success=False,
                error_msg=f"vol calculation failed: {vol}",
            )

        client = await self.client_pool.get(self.slot_id)
        if client is None:
            self.opens_failed += 1
            return LiveOrderResult(
                success=False,
                error_msg=f"slot {self.slot_id} not in pool",
            )

        t0 = time.monotonic()
        try:
            response = await asyncio.wait_for(
                client.submit_order(
                    symbol=symbol,
                    side=side,
                    vol=vol,
                    leverage=leverage,
                    open_type=open_type,
                    order_type=ORDER_TYPE_MARKET,
                ),
                timeout=self.order_timeout_sec,
            )
        except asyncio.TimeoutError:
            self.opens_failed += 1
            self.last_error = "open_timeout"
            return LiveOrderResult(
                success=False,
                error_msg=f"open timeout after {self.order_timeout_sec}s",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            self.opens_failed += 1
            self.last_error = f"open_exception: {type(e).__name__}"
            logger.exception("Live open failed for %s", symbol)
            return LiveOrderResult(
                success=False,
                error_msg=f"exception: {e}",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        latency_ms = int((time.monotonic() - t0) * 1000)
        code = response.get("code", -1)
        # MEXC puts the human-readable error under "message" on rejects
        # (e.g. code 510) and "msg" on others — read both so the real text
        # isn't dropped (otherwise error_msg becomes "api_error_510: None").
        msg = response.get("msg") or response.get("message")

        if code != 0:
            self.opens_failed += 1
            self.last_error = f"api_error_{code}: {msg}"
            # #4: персист причини стопу (throttle/delay коди) у БД, щоб
            # панель показувала ЧОМУ opens стали. Account-level (risk-control
            # текст) персиститься нижче; health-check із error=None очистить,
            # коли акаунт відновиться.
            if str(code) in OPEN_FREQ_CODES and self.webkey_store is not None:
                try:
                    await self.webkey_store.set_slot_error(
                        self.slot_id, f"⚠️ order throttled: api_error_{code} ({msg})")
                except Exception:
                    pass

            # Slot-level error detection (face verification, risk control, etc.)
            # These errors mean the entire slot is unusable until resolved on MEXC
            if is_slot_level_error(str(msg)):
                # Detect TRANSITION (was OK, now broken) — alert only on transition
                # to avoid spam if every signal triggers same error
                first_time_detected = (self.slot_level_error is None)
                self.slot_level_error = str(msg)
                self.slot_level_error_at_ts = int(time.time())
                logger.warning(
                    "🚨 [SLOT %d] Account-level error: %s — "
                    "user must resolve on MEXC before slot can trade",
                    self.slot_id, msg,
                )
                # Persist to DB so /balance can show it
                if self.webkey_store is not None:
                    try:
                        await self.webkey_store.update_health(
                            slot_id=self.slot_id,
                            latency_ms=latency_ms,
                            balance_usdt=None,  # don't update balance — was probably fine before
                            error=f"⚠️ {msg}",
                        )
                    except Exception:
                        logger.exception("Failed to persist slot-level error")
                # Send Telegram alert (throttled to once per hour per slot)
                if self.alerts is not None and first_time_detected:
                    try:
                        await self.alerts.send(
                            text=(
                                f"🚨 <b>SLOT{self.slot_id} blocked</b>\n\n"
                                f"<b>MEXC error:</b> <i>{msg}</i>\n\n"
                                f"⚠️ <b>Action required:</b>\n"
                                f"Open MEXC app/website and complete the verification "
                                f"(face verification / risk control / KYC).\n\n"
                                f"After resolving, the bot will automatically resume "
                                f"on the next successful trade."
                            ),
                            category=f"slot_{self.slot_id}_blocked",
                            throttle_sec=3600,  # max 1 alert per hour per slot
                            suppress_during_quiet=False,  # ALWAYS notify — this is critical
                        )
                    except Exception:
                        logger.exception("Failed to send slot-level error alert")
            else:
                logger.error(
                    "Live open rejected: %s %s code=%s msg=%s",
                    symbol, direction, code, msg,
                )
            return LiveOrderResult(
                success=False,
                error_code=code,
                error_msg=str(msg),
                latency_ms=latency_ms,
                raw_response=response,
            )

        order_id = str(response.get("data", {}).get("orderId", ""))
        if not order_id:
            self.opens_failed += 1
            self.last_error = "no_order_id"
            return LiveOrderResult(
                success=False,
                error_msg="API returned no orderId",
                latency_ms=latency_ms,
                raw_response=response,
            )

        self.opens_succeeded += 1

        # poll for actual fill price (accurate PnL)
        # also get real filled volume (MARKET shouldn't partial-fill,
        # but using authoritative number from MEXC is always safer than `vol`).
        # pass order_id so the poll uses deal_details/{order_id} as
        # the primary read (smaller payload, faster visibility).
        fill_price = 0.0
        real_filled_vol = vol  # fallback to requested if poll fails
        try:
            fill_price, polled_vol = await _poll_fill_price(
                client, symbol, order_id=order_id, timeout_sec=2.0,
            )
            if polled_vol > 0:
                real_filled_vol = polled_vol
        except Exception:
            logger.exception("Fill price polling failed for %s", symbol)

        # Recompute notional from real fill price × real filled vol × contract_size.
        # Falls back to requested notional if polling failed.
        contract_size = CONTRACT_SIZES.get(symbol, 1.0)
        if fill_price > 0 and real_filled_vol > 0:
            real_notional = fill_price * real_filled_vol * contract_size
        else:
            real_notional = notional_usdt

        logger.info(
            "[LIVE OPEN] %s %s vol=%d filled=%d lev=%dx orderId=%s latency=%dms fill=%.6f",
            symbol, direction.upper(), vol, real_filled_vol, leverage, order_id, latency_ms,
            fill_price,
        )

        # include fill_price in result
        # report REAL filled vol and notional (not requested)
        result = LiveOrderResult(
            success=True,
            order_id=order_id,
            fill_price=fill_price,
            fill_qty_contracts=real_filled_vol,
            notional_usdt=real_notional,
            latency_ms=latency_ms,
            raw_response=response,
        )

        # Clear any previous slot-level error — if we successfully opened,
        # the user must have resolved the verification/risk issue.
        if self.slot_level_error is not None:
            previous_error = self.slot_level_error
            logger.info(
                "[SLOT %d] Account-level error cleared (successful order)",
                self.slot_id,
            )
            self.slot_level_error = None
            self.slot_level_error_at_ts = 0
            if self.webkey_store is not None:
                try:
                    await self.webkey_store.update_health(
                        slot_id=self.slot_id,
                        latency_ms=latency_ms,
                        balance_usdt=None,
                        error=None,  # clear
                    )
                except Exception:
                    logger.exception("Failed to clear slot-level error in DB")
            # Notify user that slot is back online
            if self.alerts is not None:
                try:
                    await self.alerts.send(
                        text=(
                            f"✅ <b>SLOT{self.slot_id} resumed</b>\n\n"
                            f"Previous error: <i>{previous_error}</i>\n\n"
                            f"Trading resumed on this slot."
                        ),
                        category=f"slot_{self.slot_id}_resumed",
                        throttle_sec=60,  # don't double-send if multiple opens succeed quickly
                        suppress_during_quiet=False,
                    )
                except Exception:
                    logger.exception("Failed to send slot resume alert")

        # Return the polled result (with real fill_price + real_filled_vol +
        # real_notional), not the originally-requested values.
        return result

    async def _has_open_position(self, client, symbol: str) -> bool:
        """
        Check if there's an existing open position for this symbol on this slot.
        Used as a safety gate before retrying IOC: if attempt N partially filled
        and we open another order, we'd end up with 1.5x intended size.

        Returns True if any position with hold_vol > 0 exists for symbol.
        Errors / API failures → return False (caller proceeds; better to risk
        a duplicate than skip a valid retry).
        """
        try:
            resp = await asyncio.wait_for(
                client.get_open_positions(), timeout=1.5,
            )
            if resp.get("code") != 0:
                return False
            for p in resp.get("data", []) or []:
                if p.get("symbol") != symbol:
                    continue
                hold_vol = float(p.get("holdVol", 0) or 0)
                if hold_vol > 0:
                    return True
            return False
        except Exception:
            return False

    async def _trip_fee_guard(self, symbol: str, fee_usdt: float) -> None:
        """A fill came back with a non-zero MEXC fee → the 0%-maker premise that
        makes this strategy profitable is broken. Halt this slot's live trading:

          - in-process: `_halted` makes every further place_ioc_open refuse;
          - durable: disable the slot in the DB so it stays off across restarts
            (fees don't vanish on restart — the operator must investigate first);
          - alert the operator via Telegram.

        The position that just filled is real and is still managed/closed
        normally; only NEW live entries are blocked. Idempotent — trips once.
        """
        if self._halted:
            return
        self._halted = True
        logger.critical(
            "🚨 FEE GUARD: slot %d %s fill charged fee=$%.6f — strategy needs 0%% "
            "maker fee. Disabling live on this slot.",
            self.slot_id, symbol, fee_usdt,
        )
        if self.webkey_store is not None:
            try:
                await self.webkey_store.set_live_enabled(self.slot_id, False)
            except Exception:
                logger.exception(
                    "fee guard: failed to disable slot %d in store", self.slot_id
                )
            try:
                await self.webkey_store.set_slot_error(
                    self.slot_id,
                    f"⚠️ fee guard: MEXC стягнула комісію ${fee_usdt:.6f} (0% премісу зламано)")
            except Exception:
                logger.exception("fee guard: failed to persist last_error slot %d", self.slot_id)
            # Durably flip the pair to SHADOW. live_enabled alone leaves the
            # pair stuck in pair_states.state='live' (the REAL live determinant
            # PairStateManager.is_in_live checks) — so without this the alert's
            # "falls back to shadow" was not actually happening. _halted blocks
            # new entries instantly; this makes the demotion real + durable.
            try:
                # A fee is a property of THIS account. live_enabled=0 above
                # already stops this slot; demoting the pair would also stop a
                # second account that never saw a fee, so only do it when no
                # other slot is left to trade the pair.
                _pair_b = to_binance(symbol)
                if await self.webkey_store._pair_has_another_slot(
                        _pair_b, self.slot_id):
                    logger.warning(
                        "fee guard: slot %d halted on %s — pair stays LIVE, "
                        "another slot still trades it",
                        self.slot_id, _pair_b,
                    )
                else:
                    await self.webkey_store.demote_pair_to_shadow(
                        _pair_b, "fee detected — auto-shadow"
                    )
            except Exception:
                logger.exception(
                    "fee guard: failed to demote %s to shadow", symbol
                )
        if self.alerts is not None:
            try:
                await self.alerts.send(
                    f"🚨 <b>FEE DETECTED — SLOT{self.slot_id} live DISABLED</b>\n\n"
                    f"{symbol}: MEXC charged a non-zero fee (${fee_usdt:.6f}) on a fill.\n\n"
                    f"This bot only has edge at <b>0% maker fee</b>, so live trading on "
                    f"slot {self.slot_id} was halted automatically. The pair falls back "
                    f"to shadow.\n\n"
                    f"Check the account's fee tier on MEXC; once it's 0% again, "
                    f"re-enable via 🔑 Webkey → slot {self.slot_id}.",
                    category=f"fee_guard_{self.slot_id}",
                )
            except Exception:
                logger.exception("fee guard: alert failed for slot %d", self.slot_id)

    def reset_fee_guard(self) -> bool:
        """Manually clear the in-memory fee-guard halt so this slot can place
        live orders again. Use when the fee that tripped the guard was
        pair-specific (this account is 0%-fee on SOME pairs but not others) —
        a different, 0%-fee pair on the same slot is wrongly blocked by the
        prior pair's trip. Returns True if it had been halted.
        """
        was = self._halted
        self._halted = False
        if was:
            logger.warning("Fee guard manually RESET for slot %d", self.slot_id)
        return was

    async def place_ioc_open(
        self,
        symbol: str,                         # MEXC format: "PEPE_USDT"
        direction: str,                      # "long" or "short"
        notional_usdt: float,
        leverage: int,
        mexc_ob,                             # OrderBook (already scaled to Binance-equivalent)
        offset_ticks: int = 0,               # 0 = at-touch; >0 = cross N ticks; <0 = inside spread (per-pair cfg.ioc_offset_ticks)
        max_attempts: int = IOC_DEFAULT_MAX_ATTEMPTS,
        retry_delay_ms: int = IOC_DEFAULT_RETRY_DELAY_MS,
        open_type: int = OPEN_TYPE_ISOLATED,
        t_signal_created: float = 0.0,       # profiling: time.perf_counter() at signal emit
    ) -> LiveOrderResult:
        """
        Place IOC LIMIT order to OPEN a position. This is the economic core
        of the strategy: IOC limit fills count as MAKER on MEXC promo (0% fee),
        whereas market entry pays ~0.04% taker × leverage = strategy-killing.

        Behaviour:
          - For LONG: limit_price = best_ask + offset_ticks×tick → 0=at-touch, N>0=cross N ticks
          - For SHORT: limit_price = best_bid − offset_ticks×tick → 0=at-touch, N>0=cross N ticks
          - On MEXC, IOC orders auto-cancel any unfilled remainder (no /order/cancel needed)
          - Up to `max_attempts` tries; between attempts re-fetch fresh OB + check open_positions
          - If all attempts expire → return success=False with error_msg='ioc_all_expired'
            (caller should treat this as "skipped signal", NOT fall back to market)

        Price scaling:
          - mexc_ob has prices in Binance-equivalent SCALED form (e.g. PEPE 0.004144)
          - MEXC API expects RAW prices (PEPE 0.000004144)
          - We divide by get_binance_scale(symbol) before submit_order

        Returns LiveOrderResult.success = True only if a fill is observed.
        fill_price (when set) is in Binance-equivalent SCALED form, consistent
        with the rest of the bot.
        """
        self.opens_attempted += 1

        # Fee guard tripped earlier this run → never open live again until the
        # operator re-enables the slot. Pair falls back to shadow.
        # getattr-guarded: some call paths build LiveExecutor without __init__.
        if getattr(self, "_halted", False):
            self.opens_failed += 1
            return LiveOrderResult(
                success=False,
                error_msg="fee_guard_halted: non-zero MEXC fee detected, live disabled",
            )

        if direction not in ("long", "short"):
            self.opens_failed += 1
            return LiveOrderResult(
                success=False,
                error_msg=f"invalid direction: {direction}",
            )

        # The previous IOC on this symbol ended with the exchange telling us
        # NOTHING — no fill push, no terminal state, REST poll timed out — and
        # the phantom re-check has not answered yet. Sending another order now
        # means stacking on a position that may already exist: on 2026-08-21
        # 12:03 that produced FIVE phantom fills in 34 seconds plus
        # api_error_2021 ("leverage inconsistent with the existing position").
        # Bounded wait: the check gives up after its last window (~12s).
        # A CONFIRMED expiry never lands here — 211 of those in the log, zero
        # phantoms — so the normal path keeps its full speed.
        if symbol in self._phantom_unknown:
            self.opens_failed += 1
            self.last_error = "phantom_check_pending"
            logger.warning(
                "[PHANTOM] %s: попередній IOC без відповіді біржі — новий ордер "
                "відкладено до кінця перевірки", symbol,
            )
            return LiveOrderResult(
                success=False,
                error_msg="phantom_check_pending: previous IOC outcome unknown",
            )

        if mexc_ob is None or not getattr(mexc_ob, "is_synced", False):
            self.opens_failed += 1
            self.last_error = "ioc_orderbook_not_synced"
            return LiveOrderResult(
                success=False,
                error_msg="orderbook not synced — cannot price IOC limit",
            )

        client = await self.client_pool.get(self.slot_id)
        if client is None:
            self.opens_failed += 1
            return LiveOrderResult(
                success=False,
                error_msg=f"slot {self.slot_id} not in pool",
            )

        side = signal_direction_to_side(direction)
        scale = get_binance_scale(symbol)

        t_overall_start = time.monotonic()
        last_error_msg: str | None = None
        last_response: dict[str, Any] = {}
        last_order_id: str | None = None
        last_latency_ms: int = 0

        # Sticky across attempts — see the final verdict below.
        freq_error_msg: str | None = None
        # True, якщо остання спроба завершилась БЕЗ відповіді біржі про долю
        # ордера (ні WS-філ, ні WS-термінал, а REST-полл вийшов у таймаут).
        _outcome_unknown = False
        # Присвоюється всередині циклу спроб, а читається на виході з нього
        # (для shadow_twin). Якщо кожна ітерація вийде раніше через continue,
        # ім'я лишиться незвʼязаним -> NameError НА ЖИВОМУ ОРДЕРНОМУ ШЛЯХУ.
        # 0.0 читається як «ліміт невідомий», і twin такий рядок просто пропускає.
        limit_scaled = 0.0
        # Ініціалізується ПЕРЕД циклом із тієї ж причини, що й limit_scaled:
        # присвоєння живе всередині циклу, а читається після нього, і в циклі є
        # гілки з continue → інакше NameError на живому ордері.
        priced_at_perf = 0.0
        for attempt in range(1, max_attempts + 1):
            # Safety: don't open a duplicate if a previous attempt partially filled.
            # Skipped on attempt #1 (no prior order possible).
            if attempt > 1 and IOC_PRE_RETRY_POSITION_CHECK:
                already_open = await self._has_open_position(client, symbol)
                if already_open:
                    # Position is open — attempt #N-1 filled despite our
                    # poll missing the fill window. Poll fill data and
                    # return success here so the caller doesn't see this
                    # as a failed entry (which would orphan the real
                    # position on MEXC).
                    logger.info(
                        "[IOC OPEN] %s attempt #%d aborted — position already "
                        "open (previous attempt likely filled). Polling fill.",
                        symbol, attempt,
                    )
                    t_before_poll = time.monotonic()
                    pre_retry_fill_price, pre_retry_filled_vol = await _poll_fill_price(
                        client, symbol,
                        order_id=last_order_id,
                        timeout_sec=IOC_FILL_POLL_TIMEOUT_SEC,
                    )
                    t_after_poll = time.monotonic()
                    if pre_retry_fill_price > 0 and pre_retry_filled_vol > 0:
                        contract_size = CONTRACT_SIZES.get(symbol, 1.0)
                        pre_retry_notional = (
                            pre_retry_fill_price * pre_retry_filled_vol * contract_size
                        )
                        self.opens_succeeded += 1
                        total_lat = int((time.monotonic() - t_overall_start) * 1000)
                        fill_poll_lat = int((t_after_poll - t_before_poll) * 1000)
                        logger.info(
                            "[IOC OPEN] %s pre-retry-fill detected: "
                            "filled_vol=%d fill=%.8f notional=$%.2f total=%dms",
                            symbol, pre_retry_filled_vol, pre_retry_fill_price,
                            pre_retry_notional, total_lat,
                        )
                        return LiveOrderResult(
                            success=True,
                            order_id=last_order_id,  # from prior attempt
                            fill_price=pre_retry_fill_price,
                            fill_qty_contracts=pre_retry_filled_vol,
                            notional_usdt=pre_retry_notional,
                            latency_ms=total_lat,
                            submit_latency_ms=last_latency_ms,
                            response_latency_ms=0,
                            fill_poll_latency_ms=fill_poll_lat,
                            raw_response=last_response,
                        )
                    # Position-check said "yes open" but poll returned nothing.
                    # Could be a stale read or MEXC inter-call inconsistency.
                    # Fall through to normal failure path so reconciliation
                    # picks up the orphan within 60s (rather than reporting
                    # false success on phantom data).
                    logger.warning(
                        "[IOC OPEN] %s pre-retry sees open position but poll "
                        "returned no fill data — letting reconciliation handle",
                        symbol,
                    )
                    break

            # Fresh price each attempt — orderbook moves
            best_bid = mexc_ob.best_bid()
            best_ask = mexc_ob.best_ask()
            if not best_bid or not best_ask:
                last_error_msg = "empty_orderbook"
                logger.debug("[IOC OPEN] %s attempt #%d: empty orderbook", symbol, attempt)
                if attempt < max_attempts:
                    await asyncio.sleep(retry_delay_ms / 1000)
                    continue
                break

            # Per-pair execution mode, driven by cfg.ioc_offset_ticks (TICK-EXACT,
            # price-independent — unlike the old bps which drifted with price):
            #   offset_ticks == 0 → at-touch. LONG: limit=best_ask, SHORT: limit=best_bid.
            #     IOC takes the touch if still there at submit; if the book moved a
            #     tick during latency → IOC cancels (low fill, best price).
            #   offset_ticks  > 0 → cross the spread by N ticks so the IOC still
            #     fills after the book moves (incl. winners). Promo fee=0 → cost is
            #     bounded slippage of N ticks.
            #   offset_ticks  < 0 → rest INSIDE the spread (maker-style, less aggressive).
            tick_raw = get_tick_size(symbol)
            tick_scaled = tick_raw * scale if scale > 0 else tick_raw
            # Мітка миті ціноутворення — знімається РАЗОМ із BBO, з якого
            # виводиться ліміт. shadow_twin бере зі стрічки книгу віком
            # (ця мітка + модельована затримка); без спільної точки відліку
            # порівняння знову стало б тавтологією.
            priced_at_perf = time.perf_counter()
            if direction == "long":
                limit_scaled = best_ask.price + offset_ticks * tick_scaled
            else:
                limit_scaled = best_bid.price - offset_ticks * tick_scaled

            # Convert to MEXC raw price (API native)
            limit_raw = limit_scaled / scale if scale > 0 else limit_scaled

            # ─── DIAGNOSTIC: log orderbook snapshot used for this submit ─────
            # Helps verify that bot's best_bid/best_ask matches what user sees
            # on MEXC UI. If user reports "submit price seems off", compare
            # this log against MEXC orderbook UI screenshot at same timestamp.
            # Format: [IOC_OB] SYMBOL DIR bid=PRICE×SIZE ask=PRICE×SIZE submit=PRICE
            _mode = "at-touch" if offset_ticks == 0 else ("cross %+d ticks" % offset_ticks)
            logger.info(
                "[IOC_OB] %s %s bid=%s×%s ask=%s×%s submit=%s [%s]",
                symbol, direction.upper(),
                best_bid.price, best_bid.size if hasattr(best_bid, 'size') else getattr(best_bid, 'qty', '?'),
                best_ask.price, best_ask.size if hasattr(best_ask, 'size') else getattr(best_ask, 'qty', '?'),
                limit_raw, _mode,
            )

            # Use scaled price for vol calc (calculate_vol_contracts expects whatever
            # price domain matches contract_size table — currently scaled domain)
            vol = calculate_vol_contracts(symbol, notional_usdt, limit_scaled)
            if vol < 1:
                last_error_msg = f"vol calc returned {vol}"
                break

            # format price respecting MEXC priceScale per symbol.
            # Sending more decimals than allowed triggers api_error_2015.
            price_str = format_price_for_mexc(limit_raw, symbol)
            if not price_str or price_str == "-":
                price_str = f"{limit_raw:.10f}"

            # measure end-to-end Python overhead from
            # signal creation to submit. If > 50ms — Python/asyncio is bottleneck.
            # If < 20ms — overhead is network/MEXC.
            if t_signal_created > 0:
                python_overhead_ms = (time.perf_counter() - t_signal_created) * 1000
                logger.info(
                    "[PROFILING] %s signal_to_submit overhead=%.1fms",
                    symbol, python_overhead_ms,
                )

            t0 = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    client.submit_order(
                        symbol=symbol,
                        side=side,
                        vol=vol,
                        leverage=leverage,
                        open_type=open_type,
                        order_type=ORDER_TYPE_IOC_LIMIT,
                        price=price_str,
                    ),
                    timeout=self.order_timeout_sec,
                )
            except asyncio.TimeoutError:
                last_error_msg = "submit_timeout"
                logger.warning("[IOC OPEN] %s attempt #%d: submit timeout", symbol, attempt)
                if attempt < max_attempts:
                    await asyncio.sleep(retry_delay_ms / 1000)
                    continue
                break
            except Exception as e:
                last_error_msg = f"submit_exception: {type(e).__name__}"
                logger.exception("[IOC OPEN] %s attempt #%d: submit exception", symbol, attempt)
                break  # don't retry on unexpected exceptions

            last_latency_ms = int((time.monotonic() - t0) * 1000)
            last_response = response
            code = response.get("code", -1)
            # MEXC error text lives under "message" on rejects (code 510),
            # "msg" on others — read both so the real reason isn't lost.
            msg = response.get("msg") or response.get("message")

            if code != 0:
                last_error_msg = f"api_error_{code}: {msg}"
                if str(code) in OPEN_FREQ_CODES:
                    # Remember it: a later attempt that merely expires would
                    # otherwise erase the evidence, and the throttle latch keys
                    # off the returned message.
                    freq_error_msg = last_error_msg
                logger.warning("[IOC FULL RESP] %s attempt=%d code=%s response=%s", symbol, attempt, code, response)

                # Slot-level error: abort all retries (face verification, KYC, etc.)
                if is_slot_level_error(str(msg)):
                    first_time = (self.slot_level_error is None)
                    self.slot_level_error = str(msg)
                    self.slot_level_error_at_ts = int(time.time())
                    logger.error(
                        "[IOC OPEN] %s slot-level error code=%s msg=%s — aborting retries",
                        symbol, code, msg,
                    )
                    if first_time and self.alerts is not None:
                        try:
                            await self.alerts.send(
                                text=(
                                    f"🚨 <b>SLOT{self.slot_id} blocked</b>\n\n"
                                    f"Error: <i>{msg}</i>"
                                ),
                                category=f"slot_{self.slot_id}_error",
                                throttle_sec=600,
                                suppress_during_quiet=False,
                            )
                        except Exception:
                            logger.exception("Failed to send slot-level error alert")
                    self.opens_failed += 1
                    return LiveOrderResult(
                        success=False,
                        error_code=code,
                        error_msg=str(msg),
                        latency_ms=last_latency_ms,
                        raw_response=response,
                    )

                # Transient error — retry
                logger.debug(
                    "[IOC OPEN] %s attempt #%d: code=%s msg=%s",
                    symbol, attempt, code, msg,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(retry_delay_ms / 1000)
                    continue
                break

            # API accepted the order. orderId returned.
            order_id = (response.get("data") or {}).get("orderId")
            if order_id is None:
                last_error_msg = "no_order_id_in_response"
                if attempt < max_attempts:
                    await asyncio.sleep(retry_delay_ms / 1000)
                    continue
                break
            last_order_id = str(order_id)

            # IOC orders fill OR auto-cancel within ~100ms of submit.
            # Poll briefly to detect fill.
            # poll returns BOTH avg fill price AND
            # real holdVol. IOC LIMIT often partial-fills when top-of-book is
            # thin — without this, the bot assumed fill_qty == requested_vol
            # and wildly mis-reported notional ($1500 logged vs $100 real).
            t_before_poll = time.monotonic()
            _fee_box: list[float] = []
            fill_price_scaled, real_filled_vol = 0.0, 0
            _fill_via = "rest"
            # PUSH-FIRST: learn the fill from the private WS
            # (push.personal.order, keyed by orderId) before hitting REST.
            # Conservative on live money: trust the push ONLY when it confirms
            # a fill (deal_vol>0). A pushed "expired" (vol==0) still falls
            # through to the REST poll as a double-check — so the WS can only
            # ever ACCELERATE a confirmed fill, never cause a false no-fill.
            if PRIVATE_WS_FILL_ENABLED and self.private_ws_pool is not None:
                try:
                    ws_fill = await self.private_ws_pool.wait_fill(
                        self.slot_id, last_order_id,
                        timeout_sec=PRIVATE_WS_FILL_WAIT_SEC,
                    )
                except Exception:
                    ws_fill = None
                if ws_fill is not None and ws_fill.deal_vol > 0:
                    fill_price_scaled = ws_fill.deal_avg_price_raw * get_binance_scale(symbol)
                    real_filled_vol = ws_fill.deal_vol
                    _fee_box.append(ws_fill.fee)
                    _fill_via = "ws"
                elif (PRIVATE_WS_TRUST_EXPIRY and ws_fill is not None
                        and ws_fill.terminal and ws_fill.deal_vol == 0):
                    # Definitive no-fill from MEXC (terminal state=4, dealVol=0).
                    # Skip the REST poll: fill stays (0, 0) → treated as expired.
                    _fill_via = "ws_expired"
            if _fill_via not in ("ws", "ws_expired"):
                fill_price_scaled, real_filled_vol = await _poll_fill_price(
                    client, symbol,
                    order_id=last_order_id,
                    timeout_sec=IOC_FILL_POLL_TIMEOUT_SEC,
                    fee_out=_fee_box,
                )
            t_after_poll = time.monotonic()
            # Біржа не сказала нічого певного: WS промовчав, REST-полл вичерпав
            # таймаут. Саме цей клас дає фантоми — див. коментар нижче на виході.
            _outcome_unknown = (_fill_via == "rest"
                                and not (fill_price_scaled > 0 and real_filled_vol > 0))
            if _fill_via == "ws":
                logger.info(
                    "[FILL SRC] %s via=ws price=%.8f vol=%d %.0fms (poll-free)",
                    symbol, fill_price_scaled, real_filled_vol,
                    (t_after_poll - t_before_poll) * 1000,
                )
            elif _fill_via == "ws_expired":
                logger.info(
                    "[FILL SRC] %s via=ws_expired %.0fms (REST poll skipped — definitive no-fill)",
                    symbol, (t_after_poll - t_before_poll) * 1000,
                )
            if fill_price_scaled > 0 and real_filled_vol > 0:
                # 0%-maker fee is the entire economic edge. If MEXC charged
                # anything on this fill, halt live on this slot (this position
                # is real and still gets managed/closed; only new entries stop).
                _fill_fee = _fee_box[0] if _fee_box else 0.0
                if _fill_fee > FEE_GUARD_EPSILON_USDT:
                    await self._trip_fee_guard(symbol, _fill_fee)
                # FILLED (possibly partial) — compute REAL notional from
                # MEXC-authoritative numbers.
                contract_size = CONTRACT_SIZES.get(symbol, 1.0)
                # entry_price domain: scaled (Binance-equivalent). Notional
                # in USDT = scaled_price × vol × contract_size, since
                # contract_size table is calibrated to that domain.
                real_notional = fill_price_scaled * real_filled_vol * contract_size

                self.opens_succeeded += 1
                fill_pct = (100.0 * real_filled_vol / vol) if vol > 0 else 0.0
                logger.info(
                    "[IOC OPEN] %s %s req_vol=%d filled_vol=%d (%.0f%%) "
                    "lev=%dx orderId=%s attempt=%d/%d "
                    "limit_raw=%s fill_scaled=%.8f notional=$%.2f latency=%dms",
                    symbol, direction.upper(), vol, real_filled_vol, fill_pct,
                    leverage, last_order_id,
                    attempt, max_attempts, price_str, fill_price_scaled,
                    real_notional, last_latency_ms,
                )

                # Clear stale slot-level error if any
                if self.slot_level_error is not None:
                    previous_error = self.slot_level_error
                    logger.info(
                        "[SLOT %d] Account-level error cleared (IOC fill)",
                        self.slot_id,
                    )
                    self.slot_level_error = None
                    if self.webkey_store is not None:
                        try:
                            await self.webkey_store.clear_slot_error(self.slot_id)
                        except Exception:
                            logger.debug("clear_slot_error failed", exc_info=True)
                    if self.alerts is not None:
                        try:
                            await self.alerts.send(
                                text=(
                                    f"✅ <b>SLOT{self.slot_id} resumed</b>\n\n"
                                    f"Previous error: <i>{previous_error}</i>"
                                ),
                                category=f"slot_{self.slot_id}_resumed",
                                throttle_sec=60,
                                suppress_during_quiet=False,
                            )
                        except Exception:
                            logger.exception("Failed to send slot resume alert")

                # latency breakdown
                # last_latency_ms = submit→response (line 912)
                # t_before_poll → t_after_poll = poll waiting time
                submit_lat = last_latency_ms  # entire POST roundtrip
                # response_latency_ms split: ~0 ms for the parse itself,
                # most is bundled into last_latency_ms (submit→response).
                # Keep separate field for explicit reporting.
                response_lat = 0
                fill_poll_lat = int((t_after_poll - t_before_poll) * 1000)
                total_lat = int((time.monotonic() - t_overall_start) * 1000)

                logger.info(
                    "[LATENCY] %s entry: total=%dms (submit=%d poll=%d)",
                    symbol, total_lat, submit_lat, fill_poll_lat,
                )

                return LiveOrderResult(
                    success=True,
                    order_id=last_order_id,
                    fill_price=fill_price_scaled,
                    fill_qty_contracts=real_filled_vol,
                    notional_usdt=real_notional,
                    latency_ms=total_lat,
                    submit_latency_ms=submit_lat,
                    response_latency_ms=response_lat,
                    fill_poll_latency_ms=fill_poll_lat,
                    limit_price_scaled=limit_scaled,
                    priced_at_perf=priced_at_perf,
                    raw_response=response,
                )

            # No fill — IOC expired (auto-cancelled by MEXC). Retry.
            last_error_msg = "ioc_expired_no_fill"
            logger.debug(
                "[IOC OPEN] %s attempt #%d expired (no fill within %sms)",
                symbol, attempt, int(IOC_FILL_POLL_TIMEOUT_SEC * 1000),
            )
            if attempt < max_attempts:
                await asyncio.sleep(retry_delay_ms / 1000)
                continue

        # All attempts exhausted (or aborted) without confirmed fill.
        # IMPORTANT: we do NOT fall back to market. By design — preserves the
        # 0% maker fee economic edge. Caller should treat this as a skipped signal.
        self.opens_failed += 1
        _final_err = last_error_msg or "ioc_all_expired"
        if freq_error_msg and _final_err in (
                "ioc_expired_no_fill", "ioc_all_expired"):
            # A plain expiry must not hide that MEXC refused us for rate.
            _final_err = freq_error_msg
        self.last_error = _final_err
        total_latency_ms = int((time.monotonic() - t_overall_start) * 1000)
        logger.info(
            "[IOC OPEN] %s SKIPPED after %d attempts: %s (total %dms)",
            symbol, max_attempts, self.last_error, total_latency_ms,
        )
        # PHANTOM-FILL GUARD: an ACCEPTED IOC we believe EXPIRED may have actually
        # filled (fill-poll/private-WS missed a late-settling fill) → naked
        # position → silent liquidation. Re-verify the order's real deals in the
        # BACKGROUND (fire-and-forget = zero added latency). Only for the
        # accepted-but-no-fill case (we have an order_id and an ioc_* verdict),
        # NOT rejects (api_error_401/510/… never opened a position).
        # Any order id we hold may have filled late — including one created on
        # an earlier attempt whose verdict was then overwritten by a reject
        # (e.g. attempt 1 expired, attempt 2 came back 10014). Restricting
        # this to ioc_* verdicts skipped exactly that case, which is the
        # -$25.82 liquidation class.
        if last_order_id:
            # Класифікація має значення. `ws_expired` — це ВІДПОВІДЬ біржі
            # (terminal state, dealVol=0): за 211 таких випадків у логу жодного
            # фантома. А ось коли WS промовчав і REST-полл вийшов у таймаут, ми
            # не знаємо нічого — у логу таких 28, і 5 із них НАСПРАВДІ налились.
            # Тому блокуємо нові відкриття по символу лише в другому випадку:
            # інакше стріляємо поверх позиції, якої «нема», і ловимо каскад
            # (2026-08-21 12:03 — 5 фантомів за 34с) плюс api_error_2021
            # 'leverage inconsistent with existing position'.
            if _outcome_unknown:
                self._phantom_unknown.add(symbol)
            self._schedule_phantom_check(last_order_id, symbol, direction, leverage,
                                         unknown=_outcome_unknown)
        return LiveOrderResult(
            success=False,
            order_id=last_order_id,
            error_msg=self.last_error,
            latency_ms=total_latency_ms,
            # ПОТРІБНО і на цьому шляху. shadow_twin порівнює симулятор із
            # біржею на тому самому ліміті, а поле заповнювалось ЛИШЕ на
            # успішному філі — тож у таблицю потрапляли самі лише випадки,
            # де live налився. Звідси «100% збіг» на 255 рядках: ми просто
            # ніколи не дивились на протухлі. А саме вони й показують, чи
            # симулятор філиться там, де біржа не змогла.
            limit_price_scaled=limit_scaled,
            priced_at_perf=priced_at_perf,
            # Те саме стосується RTT. `shadow_twin` виносить три вердикти —
            # d0 (контроль), draw (продакшн-shadow) і rtt (реальний round-trip
            # ЦЬОГО ордера), — але на шляху відмови поле лишалось нулем, а
            # `verdict(0.0)` — це БУКВАЛЬНО той самий виклик, що й контроль d0.
            # Тобто третя точка кривої була структурно мертвою рівно там, де
            # вона цікава: на протухлих ордерах.
            submit_latency_ms=last_latency_ms,
            raw_response=last_response,
        )

    def _schedule_phantom_check(self, order_id, symbol, direction, leverage,
                                unknown: bool = False) -> None:
        """Fire-and-forget scheduler — the caller returns immediately (hot path
        untouched). Strong-refs the task so it can't be GC'd mid-flight.

        `unknown=True` means the slot is BLOCKED on this symbol until the check
        finishes, so the release must happen whatever the outcome — including a
        crash in the check itself, otherwise the symbol would be blocked forever.
        """
        try:
            task = asyncio.create_task(
                self._phantom_open_check(order_id, symbol, direction, leverage),
                name=f"phantom_check:{symbol}:{order_id}",
            )
            self._phantom_tasks.add(task)
            task.add_done_callback(self._phantom_tasks.discard)
            if unknown:
                task.add_done_callback(
                    lambda _t, _s=symbol: self._phantom_unknown.discard(_s))
        except Exception:
            logger.exception("[PHANTOM] failed to schedule check for %s order %s", symbol, order_id)
            # Планувальник упав — блокування нікому знімати. Знімаємо самі,
            # інакше символ мовчки випадає з торгівлі назавжди.
            self._phantom_unknown.discard(symbol)

    async def _phantom_open_check(self, order_id, symbol, direction, leverage) -> None:
        """Re-check the order's REAL deals at widening windows. A fill can appear
        on MEXC AFTER the first window, so a single check left the naked position
        to the 60s reconcile; we re-poll and flatten the moment a fill shows
        (early-exit). Genuine no-fills poll every window (the safety cost);
        persistent unreadable falls through to the periodic reconcile backstop."""
        prev = 0.0
        for i, delay in enumerate(PHANTOM_FILL_CHECK_DELAYS_SEC):
            await asyncio.sleep(max(0.0, delay - prev))
            prev = delay
            try:
                if await self._phantom_check_once(
                        order_id, symbol, direction, leverage, i + 1, delay):
                    return  # fill found + flattened — stop
            except Exception:
                logger.exception(
                    "[PHANTOM] check %d/%d failed for %s order %s",
                    i + 1, len(PHANTOM_FILL_CHECK_DELAYS_SEC), symbol, order_id)

    async def _phantom_check_once(
            self, order_id, symbol, direction, leverage, attempt, delay) -> bool:
        """One phantom re-check window. Returns True to STOP (fill found +
        flatten attempted), False to RETRY at the next window (no fill yet /
        transient unreadable — the 60s reconcile is the ultimate backstop)."""
        client = await self.client_pool.get(self.slot_id)
        if client is None:
            return False
        resp = await client.get_order_deals(order_id)
        if not isinstance(resp, dict) or resp.get("code") not in (0, 200, None):
            # Unreadable (e.g. 401/510) — retry next window; reconcile backstop.
            logger.warning(
                "[PHANTOM] %s order %s deal-check unreadable (code=%s) — retry %d/%d",
                symbol, order_id, resp.get("code") if isinstance(resp, dict) else "?",
                attempt, len(PHANTOM_FILL_CHECK_DELAYS_SEC),
            )
            return False
        deals = resp.get("data") or []
        filled = sum(int(d.get("vol", 0) or 0) for d in deals)
        if filled <= 0:
            return False  # no fill YET — re-check at the next (later) window
        # PHANTOM: believed-expired but ACTUALLY filled → naked position.
        logger.error(
            "🚨 [PHANTOM FILL] %s %s order=%s believed-EXPIRED but ACTUALLY FILLED %d cont "
            "(window %d/%d @%.1fs) — flattening to avoid a naked position/liquidation",
            symbol, direction.upper(), order_id, filled, attempt,
            len(PHANTOM_FILL_CHECK_DELAYS_SEC), delay,
        )
        close_side = SIDE_CLOSE_SHORT if direction == "short" else SIDE_CLOSE_LONG
        try:
            cresp = await asyncio.wait_for(
                client.submit_order(
                    symbol=symbol, side=close_side, vol=filled,
                    leverage=leverage, open_type=OPEN_TYPE_ISOLATED, order_type="5",
                ),
                timeout=self.close_timeout_sec,
            )
            code = cresp.get("code", -1) if isinstance(cresp, dict) else -1
            ok = code in (0, 200)
            already = code == 2009  # position already gone — also fine
            logger.error(
                "[PHANTOM FILL] %s flatten %s: code=%s resp=%s",
                symbol, "OK" if ok else ("already-flat" if already else "FAILED"), code, cresp,
            )
            if self.alerts is not None:
                await self.alerts.send(
                    text=(
                        f"🚨 <b>PHANTOM FILL flattened</b>\n\n"
                        f"<b>Slot:</b> SLOT{self.slot_id}\n"
                        f"<b>Pair:</b> {symbol} {direction.upper()}\n"
                        f"<b>Qty:</b> {filled} cont\n<b>Order:</b> {order_id}\n\n"
                        f"An IOC we believed EXPIRED actually FILLED and was auto-flattened "
                        f"({'OK' if ok else ('already flat' if already else 'CLOSE FAILED — CHECK MEXC')})."
                    ),
                    category=f"phantom_{symbol}_{order_id}",
                    throttle_sec=0, suppress_during_quiet=False,
                )
        except Exception:
            logger.exception(
                "[PHANTOM FILL] %s flatten threw — CHECK MEXC MANUALLY (order %s, %d cont)",
                symbol, order_id, filled,
            )
        return True  # fill handled (flatten attempted) — do not re-check/re-flatten

    async def _guard_close_fee(self, close_order_id: str, symbol: str) -> None:
        """Fee-guard on the CLOSE order (mirror of the entry guard).

        The 0%-fee premise is the whole edge; the entry guard catches a promo
        change on opens, but in theory a fee tier could change on exits only.
        Non-blocking peek of the close order's pushed fee (wait_fill checks the
        buffer first; the close order push is already buffered by the time the
        close confirms). None / no push → skip, so this can only ACCELERATE a
        real-fee halt, never false-trip.
        """
        if not (close_order_id and PRIVATE_WS_FILL_ENABLED
                and self.private_ws_pool is not None):
            return
        try:
            cf = await self.private_ws_pool.wait_fill(self.slot_id, close_order_id, 0.05)
        except Exception:
            return
        if cf is not None and cf.fee > FEE_GUARD_EPSILON_USDT:
            await self._trip_fee_guard(symbol, cf.fee)

    async def _close_fill_ws_or_rest(
        self, client, symbol, position_id, close_after_ts_ms, after_ts_monotonic,
        rest_timeout_sec, close_order_id="",
    ) -> tuple[float, float, float]:
        """WS-first close confirmation with REST fallback. Returns
        (close_avg_scaled, realised, open_avg_scaled) — identical contract to
        _poll_close_fill. Tries push.personal.position (state=3) keyed by
        symbol+after_ts; trusts it only with a real closeAvgPrice>0; otherwise
        falls through to the REST poll so a missed push never strands a position.
        """
        result: tuple[float, float, float] | None = None
        if PRIVATE_WS_CLOSE_FILL_ENABLED and self.private_ws_pool is not None:
            try:
                wc = await self.private_ws_pool.wait_close(
                    self.slot_id, symbol, after_ts_monotonic, PRIVATE_WS_CLOSE_WAIT_SEC,
                )
            except Exception:
                wc = None
            if wc is not None and wc.close_avg_price_raw > 0:
                scale = get_binance_scale(symbol)
                exit_p = wc.close_avg_price_raw * scale
                entry_p = wc.open_avg_price_raw * scale
                logger.info(
                    "[CLOSE SRC] %s via=ws close=%.8f realised=$%+.4f (push-confirmed)",
                    symbol, exit_p, wc.realised,
                )
                result = (exit_p, wc.realised, entry_p)
        if result is None:
            # REST fallback — never strands a position.
            result = await _poll_close_fill(
                client, symbol=symbol, position_id=position_id,
                close_after_ts_ms=close_after_ts_ms, timeout_sec=rest_timeout_sec,
            )
        # Fee-guard the close order (non-blocking; safe no-op if no push/fee).
        await self._guard_close_fee(close_order_id, symbol)
        return result

    async def close_position_capped(
        self,
        symbol: str,
        direction: str,
        qty_contracts: int,
        leverage: int,
        limit_price_raw: float,
    ) -> "LiveClosePosResult":
        """Capped IOC-LIMIT close with a GUARANTEED market fallback.

        Crosses only up to a bounded limit so a reversal close never sweeps the
        whole gap (the −13bps binance_reversal fills). ANY miss/error/non-fill
        routes to self.close_position (close_all MARKET) — so this is never worse
        than the plain market close, only potentially better. Used only for
        reversal exits where the book is gapping.
        """
        client = await self.client_pool.get(self.slot_id)
        if client is None or qty_contracts <= 0 or limit_price_raw <= 0:
            return await self.close_position(symbol)   # safe fallback

        close_side = SIDE_CLOSE_SHORT if direction == "short" else SIDE_CLOSE_LONG

        # Snapshot positionId + exchange-authoritative qty (mirrors market close).
        snapshot_position_id: int | None = None
        try:
            snap = await asyncio.wait_for(client.get_open_positions(), timeout=2.0)
            if snap.get("code") == 0:
                for p in snap.get("data", []) or []:
                    if p.get("symbol") == symbol:
                        pid = int(p.get("positionId", 0) or 0)
                        if pid > 0:
                            snapshot_position_id = pid
                        ex_vol = int(float(p.get("holdVol", 0) or 0))
                        if ex_vol > 0:
                            qty_contracts = ex_vol
                        break
        except Exception:
            logger.debug("[CAPPED CLOSE] pre-close snapshot failed", exc_info=True)

        price_str = format_price_for_mexc(limit_price_raw, symbol) or f"{limit_price_raw:.10f}"
        close_ts = int(time.time() * 1000)
        t0 = time.monotonic()
        self.closes_attempted += 1
        try:
            resp = await asyncio.wait_for(
                client.submit_order(
                    symbol=symbol, side=close_side, vol=qty_contracts,
                    leverage=leverage, open_type=1,
                    order_type=ORDER_TYPE_IOC, price=price_str,
                ),
                timeout=self.close_timeout_sec,
            )
        except Exception:
            logger.warning("[CAPPED CLOSE] %s IOC submit failed → market fallback", symbol)
            return await self.close_position(symbol)

        if resp.get("code", -1) != 0:
            logger.warning(
                "[CAPPED CLOSE] %s IOC rejected code=%s → market fallback",
                symbol, resp.get("code"),
            )
            return await self.close_position(symbol)

        # Poll for the exchange-authoritative close fill.
        real_exit = real_pnl = real_entry = 0.0
        try:
            real_exit, real_pnl, real_entry = await self._close_fill_ws_or_rest(
                client, symbol=symbol, position_id=snapshot_position_id,
                close_after_ts_ms=close_ts, after_ts_monotonic=t0,
                rest_timeout_sec=0.5,
                close_order_id=(str((resp.get("data") or {}).get("orderId", "") or "")
                               if isinstance(resp.get("data"), dict) else ""),
            )
        except Exception:
            logger.exception("[CAPPED CLOSE] fill poll failed for %s", symbol)

        if real_exit > 0:
            self.closes_succeeded += 1
            logger.info(
                "[CAPPED CLOSE] %s %s filled @ %.6f (IOC capped — no sweep)",
                symbol, direction.upper(), real_exit,
            )
            return LiveClosePosResult(
                success=True, exit_price=real_exit,
                realized_pnl_usdt=real_pnl, entry_price_confirmed=real_entry,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        # IOC unfilled (price gapped past the cap) → position still open →
        # GUARANTEED market close (never leave a position open).
        logger.warning(
            "[CAPPED CLOSE] %s IOC unfilled (gap past cap) → market fallback", symbol
        )
        return await self.close_position(symbol)

    async def _verify_position_gone(self, client, symbol: str) -> bool:
        """True ONLY when the exchange explicitly confirms no position for
        `symbol`. FAIL-CLOSED: any exception / timeout / non-zero code → False
        (treat the position as possibly still open, so the caller escalates and
        reconcile finishes the job). Single source of truth for both close paths
        — never silently abandon a live leveraged position on a flaky verify."""
        try:
            resp = await asyncio.wait_for(client.get_open_positions(), timeout=3.0)
        except Exception:
            logger.exception(
                "[VERIFY] get_open_positions raised for %s — fail-CLOSED", symbol,
            )
            return False
        if resp.get("code") != 0:
            logger.warning(
                "[VERIFY] non-zero code %r for %s — fail-CLOSED",
                resp.get("code"), symbol,
            )
            return False
        return not any(
            p.get("symbol") == symbol for p in (resp.get("data") or [])
        )

    async def close_position(
        self,
        symbol: str,
    ) -> LiveClosePosResult:
        """
        Close any open position on this symbol via /position/close_all.

        This is atomic on MEXC side — no race conditions with multiple
        positions, partial fills, or order book gaps.
        """
        self.closes_attempted += 1

        client = await self.client_pool.get(self.slot_id)
        if client is None:
            self.closes_failed += 1
            return LiveClosePosResult(
                success=False,
                error_msg=f"slot {self.slot_id} not in pool",
            )

        # Race the pre-close positionId snapshot with the close POST in
        # parallel. close POST is the critical path (it determines real
        # PnL) so we await it with the full timeout. The snap is
        # best-effort with a tight follow-up timeout — if it doesn't
        # arrive by the time close completes, _poll_close_fill below
        # falls back to matching the freshest history row by symbol and
        # close_after_ts_ms, so positionId=None is graceful degradation.
        #
        # curl_cffi AsyncSession dispatches concurrent requests on separate
        # libcurl connections (default max-connections-per-host = 6), so
        # both requests truly run in parallel.
        close_request_ts_ms = int(time.time() * 1000)
        t0 = time.monotonic()

        snap_task = asyncio.create_task(
            asyncio.wait_for(client.get_open_positions(), timeout=2.0),
            name=f"pre_close_snap:{symbol}",
        )
        close_task = asyncio.create_task(
            asyncio.wait_for(
                client.close_all_positions(symbol=symbol),
                timeout=self.close_timeout_sec,
            ),
            name=f"close_all:{symbol}",
        )

        # ---- Await close (critical path) ----
        try:
            response = await close_task
        except asyncio.TimeoutError:
            snap_task.cancel()
            self.closes_failed += 1
            self.last_error = "close_timeout"
            logger.error("[LIVE CLOSE] %s TIMEOUT after %ss",
                         symbol, self.close_timeout_sec)
            return LiveClosePosResult(
                success=False,
                error_msg=f"close timeout after {self.close_timeout_sec}s",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            snap_task.cancel()
            self.closes_failed += 1
            self.last_error = f"close_exception: {type(e).__name__}"
            logger.exception("Live close failed for %s", symbol)
            return LiveClosePosResult(
                success=False,
                error_msg=f"exception: {e}",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        # ---- Harvest snap (best-effort) ----
        # If snap is already done (most common case, raced in parallel with
        # close), this returns immediately. If snap is still in-flight, wait
        # up to 200ms and then give up — at that point _poll_close_fill's
        # close_after_ts_ms fallback is good enough.
        snapshot_position_id: int | None = None
        try:
            snap_resp = await asyncio.wait_for(snap_task, timeout=0.2)
            if snap_resp.get("code") == 0:
                for p in snap_resp.get("data", []) or []:
                    if p.get("symbol") == symbol:
                        pid = int(p.get("positionId", 0) or 0)
                        if pid > 0:
                            snapshot_position_id = pid
                        break
        except asyncio.TimeoutError:
            snap_task.cancel()
            logger.debug(
                "[LIVE CLOSE] %s snap did not complete within 200ms of close — "
                "_poll_close_fill will fall back to close_after_ts_ms matching",
                symbol,
            )
        except Exception:
            # snap_task internal failure — already cancelled by its own
            # wait_for, or network error etc. Not fatal.
            logger.debug("pre-close positionId snapshot failed", exc_info=True)

        latency_ms = int((time.monotonic() - t0) * 1000)
        code = response.get("code", -1)
        # MEXC puts the error text under "message" on rejects, "msg" on others.
        msg = response.get("msg") or response.get("message")

        if code != 0:
            self.closes_failed += 1
            self.last_error = f"close_api_error_{code}: {msg}"
            logger.error(
                "[LIVE CLOSE] %s rejected code=%s msg=%s",
                symbol, code, msg,
            )
            return LiveClosePosResult(
                success=False,
                error_code=code,
                error_msg=str(msg),
                latency_ms=latency_ms,
                raw_response=response,
            )

        self.closes_succeeded += 1

        # Poll history_positions for exchange-authoritative
        # closeAvgPrice + realised + openAvgPrice. entry_price and
        # exit_price reflect actual fills; mid_price() approximation
        # would carry a half-spread bias.
        # Fall back to 0.0 on timeout — caller will use mid_price as backup.
        real_exit_price = 0.0
        real_realised_pnl = 0.0
        real_entry_price = 0.0
        try:
            real_exit_price, real_realised_pnl, real_entry_price = await self._close_fill_ws_or_rest(
                client,
                symbol=symbol,
                position_id=snapshot_position_id,
                close_after_ts_ms=close_request_ts_ms,
                after_ts_monotonic=t0,
                rest_timeout_sec=8.0,
                close_order_id=(str((response.get("data") or {}).get("orderId", "") or "")
                               if isinstance(response.get("data"), dict) else ""),
            )
        except Exception:
            logger.exception("history_positions polling failed for %s", symbol)

        # ─── orphan-detection guard ─────────────────────────────────────
        # When /position/close_all returns code=0 ("request accepted"),
        # MEXC's internal IOC may still expire and leave the position
        # OPEN. Without this check the caller would mark the position
        # closed internally, forget about it, and let it drift.
        #
        # If history_positions polling found no fill (real_exit_price=0),
        # double-check via /position/open_positions. If position is STILL
        # OPEN there, this close did NOT succeed despite code=0.
        if real_exit_price == 0:
            # FAIL-CLOSED: only treat the position as closed if MEXC EXPLICITLY
            # confirms it's absent. A verify exception/timeout/non-zero code is
            # NOT proof of closure — reading it as "closed" (the old default)
            # would silently abandon a still-open leveraged position (no stop,
            # no trail, no alert). Same helper as market_close_position.
            if not await self._verify_position_gone(client, symbol):
                self.closes_failed += 1
                self.closes_succeeded -= 1  # roll back the optimistic increment above
                self.last_error = "close_accepted_but_position_still_open"
                logger.error(
                    "🚨 [LIVE CLOSE FALSE-SUCCESS] %s — /position/close_all "
                    "returned code=0 but position NOT confirmed closed (still "
                    "listed, or verify failed). Returning failure so caller can "
                    "retry/escalate (e.g. market close).",
                    symbol,
                )
                return LiveClosePosResult(
                    success=False,
                    error_code=0,  # MEXC said OK; we know better
                    error_msg="close_accepted_but_position_still_open",
                    latency_ms=latency_ms,
                    raw_response=response,
                )
            # else: confirmed gone, just no history row yet → fall through as
            # success with empty fill data, caller uses mid_price fallback.
        # ────────────────────────────────────────────────────────────────

        if real_exit_price > 0:
            logger.info(
                "[LIVE CLOSE] %s OK latency=%dms real_entry=%.6f real_exit=%.6f "
                "real_pnl=$%+.4f (positionId=%s)",
                symbol, latency_ms, real_entry_price, real_exit_price,
                real_realised_pnl, snapshot_position_id,
            )
        else:
            logger.warning(
                "[LIVE CLOSE] %s OK latency=%dms but history_positions did not return "
                "fill data within timeout — caller will fall back to mid_price",
                symbol, latency_ms,
            )

        return LiveClosePosResult(
            success=True,
            latency_ms=latency_ms,
            raw_response=response,
            exit_price=real_exit_price,
            realized_pnl_usdt=real_realised_pnl,
            entry_price_confirmed=real_entry_price,
        )


    async def market_close_position(
        self,
        symbol: str,
        direction: str,
        qty_contracts: int,
        leverage: int,
    ) -> LiveClosePosResult:
        """
        FORCE close position via MARKET order (type=5).

        Unlike close_position() which uses /position/close_all (MEXC may
        internally use IOC and can leave position open if liquidity gaps),
        this uses /order/create with type=5 MARKET — which executes
        against whatever liquidity is available, guaranteed to fill.

        Trade-off: accepts potentially worse slippage in exchange for
        deterministic close. Used as last resort when close_position()
        fails repeatedly (orphan recovery path).

        Args:
            symbol: MEXC symbol (e.g. "PENGU_USDT")
            direction: "long" or "short" (the direction of the OPEN position)
            qty_contracts: position quantity in contracts (pos.qty in raw int)
            leverage: leverage used for the position

        Returns:
            LiveClosePosResult with success flag and fill info.
        """
        self.closes_attempted += 1

        client = await self.client_pool.get(self.slot_id)
        if client is None:
            self.closes_failed += 1
            return LiveClosePosResult(
                success=False,
                error_msg=f"slot {self.slot_id} not in pool",
            )

        # MEXC side codes: 2 = CLOSE_SHORT (buy), 4 = CLOSE_LONG (sell).
        # Default from the caller's belief — but OVERRIDDEN below by the
        # exchange's ACTUAL position side when the snapshot succeeds. The
        # caller's `direction` can be stale/wrong (the bot thought SHORT while
        # the real position was LONG); a "close short" against a real long gets
        # MEXC api_error_2009 "Position is nonexistent or closed" and orphans
        # the position — which the periodic reconcile then has to clean up.
        # Closing the side that ACTUALLY exists mirrors reconcile's proven path.
        close_side = SIDE_CLOSE_SHORT if direction == "short" else SIDE_CLOSE_LONG

        # Snapshot positionId before close so history_positions match is reliable
        snapshot_position_id: int | None = None
        snapshot_ok = False       # the open_positions query itself succeeded
        found_position = False    # …and it listed a position for this symbol
        try:
            snap_resp = await asyncio.wait_for(
                client.get_open_positions(), timeout=2.0,
            )
            if snap_resp.get("code") == 0:
                snapshot_ok = True
                for p in snap_resp.get("data", []) or []:
                    if p.get("symbol") == symbol:
                        found_position = True
                        pid = int(p.get("positionId", 0) or 0)
                        if pid > 0:
                            snapshot_position_id = pid
                        # Exchange-authoritative SIDE: close what is REALLY open,
                        # not what the caller believes. positionType 1=LONG, 2=SHORT.
                        pos_type = int(p.get("positionType", 0) or 0)
                        if pos_type == 1:
                            close_side = SIDE_CLOSE_LONG
                        elif pos_type == 2:
                            close_side = SIDE_CLOSE_SHORT
                        if pos_type in (1, 2) and (
                            (direction == "short") != (pos_type == 2)
                        ):
                            logger.warning(
                                "[MARKET CLOSE] %s side mismatch: caller=%s "
                                "exchange=%s — closing the exchange side",
                                symbol, direction.upper(),
                                "LONG" if pos_type == 1 else "SHORT",
                            )
                        # Also use exchange-authoritative qty if our cached value
                        # disagrees — safer than trusting our number.
                        ex_vol = int(float(p.get("holdVol", 0) or 0))
                        if ex_vol > 0 and ex_vol != qty_contracts:
                            logger.warning(
                                "[MARKET CLOSE] %s qty mismatch: bot=%d exchange=%d "
                                "— using exchange value",
                                symbol, qty_contracts, ex_vol,
                            )
                            qty_contracts = ex_vol
                        break
        except Exception:
            logger.debug("pre-market-close snapshot failed", exc_info=True)

        # Exchange CONFIRMS no such position → it is already closed. Do NOT
        # submit a close order (it returns api_error_2009 and fires a false
        # ORPHAN alert). Report success so the caller settles cleanly.
        if snapshot_ok and not found_position:
            logger.info(
                "[MARKET CLOSE] %s already closed on exchange (absent from "
                "open_positions) — nothing to do", symbol,
            )
            return LiveClosePosResult(
                success=True,
                error_msg="no_position_to_close",
                latency_ms=0,
            )

        if qty_contracts <= 0:
            # Nothing to close (or could not determine quantity).
            # Verify position truly absent.
            try:
                v = await asyncio.wait_for(client.get_open_positions(), timeout=3.0)
                still_open = any(
                    p.get("symbol") == symbol
                    for p in (v.get("data", []) or [])
                )
                if still_open:
                    self.closes_failed += 1
                    return LiveClosePosResult(
                        success=False,
                        error_msg="market_close_qty_unknown_but_position_open",
                    )
            except Exception:
                pass
            return LiveClosePosResult(
                success=True,
                error_msg="no_position_to_close",
                latency_ms=0,
            )

        close_request_ts_ms = int(time.time() * 1000)
        t0 = time.monotonic()
        logger.warning(
            "[MARKET CLOSE] FORCE-closing %s %s qty=%d lev=%dx — last-resort path",
            symbol, direction.upper(), qty_contracts, leverage,
        )
        try:
            response = await asyncio.wait_for(
                client.submit_order(
                    symbol=symbol,
                    side=close_side,
                    vol=qty_contracts,
                    leverage=leverage,
                    open_type=1,   # isolated; consistent with how we open
                    order_type="5",  # MARKET — guaranteed fill
                ),
                timeout=self.close_timeout_sec,
            )
        except asyncio.TimeoutError:
            self.closes_failed += 1
            self.last_error = "market_close_timeout"
            logger.error("[MARKET CLOSE] %s TIMEOUT after %ss",
                         symbol, self.close_timeout_sec)
            return LiveClosePosResult(
                success=False,
                error_msg=f"market close timeout after {self.close_timeout_sec}s",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            self.closes_failed += 1
            self.last_error = f"market_close_exception: {type(e).__name__}"
            logger.exception("Market close failed for %s", symbol)
            return LiveClosePosResult(
                success=False,
                error_msg=f"market_close_exception: {e}",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        latency_ms = int((time.monotonic() - t0) * 1000)
        code = response.get("code", -1)
        # MEXC error text lives under "message" on rejects, "msg" on others.
        msg = response.get("msg") or response.get("message")
        # MEXC 2009 = "Position is nonexistent or closed" for the side we sent.
        # It does NOT prove the SYMBOL is flat (a stale/opposite side can still
        # be open), so re-verify: if open_positions no longer lists the symbol
        # at all, the position is genuinely gone → success (no false orphan);
        # otherwise fall through to failure so reconcile finishes the job.
        if code == 2009:
            try:
                v = await asyncio.wait_for(client.get_open_positions(), timeout=3.0)
                if v.get("code") == 0 and not any(
                    p.get("symbol") == symbol for p in (v.get("data", []) or [])
                ):
                    logger.info(
                        "[MARKET CLOSE] %s api_error_2009 and symbol absent from "
                        "open_positions — already flat, treating as success", symbol,
                    )
                    self.closes_succeeded += 1
                    return LiveClosePosResult(
                        success=True,
                        error_msg="already_closed_2009_verified",
                        latency_ms=latency_ms,
                        raw_response=response,
                    )
            except Exception:
                logger.exception(
                    "[MARKET CLOSE] %s 2009 re-verify failed — treating as failure",
                    symbol,
                )
        if code != 0:
            self.closes_failed += 1
            self.last_error = f"market_close_api_error_{code}: {msg}"
            logger.error(
                "[MARKET CLOSE] %s rejected code=%s msg=%s",
                symbol, code, msg,
            )
            return LiveClosePosResult(
                success=False,
                error_code=code,
                error_msg=f"market_close_api_error_{code}: {msg}",
                latency_ms=latency_ms,
                raw_response=response,
            )

        # Market order — should fill near-instantly. Poll history_positions
        # briefly to get exchange-authoritative fill price.
        real_exit_price = 0.0
        real_realised_pnl = 0.0
        real_entry_price = 0.0
        try:
            real_exit_price, real_realised_pnl, real_entry_price = await self._close_fill_ws_or_rest(
                client,
                symbol=symbol,
                position_id=snapshot_position_id,
                close_after_ts_ms=close_request_ts_ms,
                after_ts_monotonic=t0,
                rest_timeout_sec=5.0,
                close_order_id=(str((response.get("data") or {}).get("orderId", "") or "")
                               if isinstance(response.get("data"), dict) else ""),
            )
        except Exception:
            logger.exception("history_positions polling failed for %s (market close)", symbol)

        # Same orphan-detection guard as close_position: even though we
        # submitted MARKET, verify position is actually gone.
        if real_exit_price == 0:
            # Default to "assume open until proven closed". If the
            # verification call times out or raises, we must NOT report
            # the close as successful — that would leave an orphan
            # position drifting unbounded. Better to fail-CLOSED so the
            # caller alerts the user and periodic reconciliation finishes
            # the job.
            still_open = True
            verify_ok = False
            try:
                v = await asyncio.wait_for(client.get_open_positions(), timeout=3.0)
                if v.get("code") == 0:
                    verify_ok = True
                    # Confirmed answer from MEXC: search positions list.
                    found = False
                    for p in v.get("data", []) or []:
                        if p.get("symbol") == symbol:
                            found = True
                            break
                    still_open = found
            except Exception:
                logger.exception(
                    "[MARKET CLOSE] %s post-close verification raised — "
                    "treating as UNCONFIRMED (failing close)", symbol,
                )
            if not verify_ok:
                # MEXC returned non-OK code; we can't tell either way.
                # Stay with still_open=True — caller will alert and the
                # periodic reconcile loop will clean up if needed.
                logger.warning(
                    "[MARKET CLOSE] %s post-close /open_positions verify "
                    "returned non-OK code — treating as UNCONFIRMED", symbol,
                )
            if still_open:
                # This should be near-impossible for a MARKET order — but if
                # it happens (e.g. partial fill, account-level issue), we
                # report failure so caller can alert user.
                self.closes_failed += 1
                self.last_error = "market_close_position_still_open"
                logger.error(
                    "🚨 [MARKET CLOSE FALSE-SUCCESS] %s — MARKET order returned "
                    "code=0 but position still open OR verify failed. "
                    "Possible partial fill or MEXC-side issue. "
                    "MANUAL INTERVENTION REQUIRED.",
                    symbol,
                )
                return LiveClosePosResult(
                    success=False,
                    error_msg="market_close_position_still_open",
                    latency_ms=latency_ms,
                    raw_response=response,
                )

        self.closes_succeeded += 1
        if real_exit_price > 0:
            logger.info(
                "[MARKET CLOSE] %s OK latency=%dms real_exit=%.6f real_pnl=$%+.4f",
                symbol, latency_ms, real_exit_price, real_realised_pnl,
            )
        else:
            logger.warning(
                "[MARKET CLOSE] %s OK latency=%dms but no history row yet "
                "(position confirmed gone). Caller will use mid_price fallback.",
                symbol, latency_ms,
            )
        return LiveClosePosResult(
            success=True,
            latency_ms=latency_ms,
            raw_response=response,
            exit_price=real_exit_price,
            realized_pnl_usdt=real_realised_pnl,
            entry_price_confirmed=real_entry_price,
        )

