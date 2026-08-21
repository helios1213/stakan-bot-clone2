"""
ShadowPosition — in-memory representation of an open virtual trade.

Tracks MFE/MAE (max favorable/adverse excursion) for analytics.
Computes ROI from current MEXC mid price.

Lifecycle:
  Created when IOC entry succeeds → entry_price, leverage, margin recorded.
  Updated each tick with current_price → roi/mfe/mae kept current.
  Closed via close() → exit_price, exit_reason, final pnl computed.

────────────────────────────────────────────────────────────────────────
Peak-listener mechanism:
Added `update_peak_only(current_mexc_price)` — a fast-path that ONLY
maintains `peak_price_favorable` and `last_progress_ms`. It does NOT
touch ROI / MFE / MAE / peak_roi etc. (those are still computed in
`update_price` from the 20ms watch-loop poll).

Rationale: ShadowEngine registers this method as an OrderBook listener,
so it is invoked on every MEXC WS depth update (~50-200/sec at peak).
Recomputing ROI/MFE/MAE that frequently is unnecessary and wasteful;
they are read only when the watch loop ticks for exit decisions, and
they're derived from `current_price` which the poll already snapshots.

`update_price` is left unchanged — it's still called once per 20ms poll
and stays the canonical source of `current_price`, `mfe_pct`, `mae_pct`,
and `current_roi_pct`. The two paths are independent and idempotent.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.utils.pnl import calc_pnl_usdt


@dataclass
class ShadowPosition:
    # Identity
    symbol: str
    direction: str                  # 'long' | 'short'
    detector_source: str            # which detector emitted the original signal
    confidence: float
    signal_id: int | None = None
    signal_uid: int | None = None   # signal.created_at_ms — joins trade→signal_features (no FK)
    gap_ticks: float = 0.0          # signal gap size at entry (bid/ask ticks) — the real entry metric

    # Sizing
    leverage: int = 50
    margin_usdt: float = 25.0
    notional_usdt: float = 0.0      # margin × leverage
    qty: float = 0.0                # filled quantity in base asset

    # Entry
    entry_target_price: float = 0.0    # what we asked for (IOC limit)
    # The limit price REALLY submitted to MEXC (live only; 0.0 in shadow).
    # Separate from entry_target_price on purpose: for live pairs the latter is
    # a stub equal to the signal price, so slippage measured against it came out
    # identically 0.0 on all 86,719 live rows. Kept as its OWN column so the
    # historical meaning of entry_target_price stays intact.
    entry_limit_price: float = 0.0
    entry_price: float = 0.0           # what we got (avg fill)
    entry_slippage_pct: float = 0.0    # vs target (live: vs entry_limit_price)
    entry_status: str = "filled"       # 'filled' | 'partial'
    entry_filled_pct: float = 1.0
    entry_fees_usdt: float = 0.0       # 0 for IOC limit on MEXC
    binance_price_at_entry: float = 0.0
    mexc_price_at_entry: float = 0.0
    mexc_lag_at_entry_pct: float = 0.0
    entry_spread_bps: float = 0.0      # MEXC top-of-book spread at entry (bps) — wide spread → adverse-prone fill
    opened_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    entry_attempted_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    # Live tracking (updated on each tick)
    current_price: float = 0.0
    current_roi_pct: float = 0.0       # ROI relative to MARGIN (so 100% = doubled margin)
    peak_roi_pct: float = 0.0          # best ROI we've seen
    trough_roi_pct: float = 0.0        # worst ROI we've seen
    mfe_pct: float = 0.0               # max favorable price excursion %
    mae_pct: float = 0.0               # max adverse price excursion %
    time_to_max_favorable_sec: int = 0

    # Exit (filled when closed)
    exit_price: float = 0.0
    exit_slippage_pct: float = 0.0
    exit_fees_usdt: float = 0.0
    exit_reason: str | None = None
    closed_at_ms: int = 0
    exit_decided_at_ms: int = 0    # when exit decision was made (before MEXC close)
    pnl_usdt: float = 0.0
    net_pnl_usdt: float = 0.0
    duration_sec: int = 0          # truncated to int — kept for backward compat
    duration_ms: int = 0           # exact duration in milliseconds (new)

    # Mode
    mode: str = "shadow"               # 'shadow' | 'live'
    account_label: str | None = None

    # Live-specific tracking (only set when mode='live')
    live_order_id: str | None = None
    live_open_latency_ms: int = 0
    live_close_latency_ms: int = 0
    live_open_error: str | None = None
    live_close_error: str | None = None
    live_exit_price_real: float = 0.0
    live_realized_pnl_usdt: float = 0.0

    # latency breakdown (live trades only).
    # All in milliseconds. Sum approximates real_entry_latency_ms.
    #   signal_to_pickup_ms : signal.created_at → engine picked it up
    #   live_open_submit_ms : engine start → MEXC POST returned
    #   live_open_response_ms : separate response parse delay (usually ~0)
    #   live_open_fill_poll_ms : poll until fill confirmed
    #   live_close_submit_ms / live_close_response_ms : close-side analog
    signal_to_pickup_ms: int = 0
    live_open_submit_ms: int = 0
    live_open_response_ms: int = 0
    live_open_fill_poll_ms: int = 0
    live_close_submit_ms: int = 0
    live_close_response_ms: int = 0

    # Adaptive-exit tracking (used by adaptive_stalled / adaptive_reversal):
    # peak_price_favorable: most favorable MEXC price seen since entry.
    #   For long: highest. For short: lowest.
    # last_progress_ms: timestamp when peak was last improved.
    #   If now - last_progress_ms > adaptive_stall_ms → market stalled.
    peak_price_favorable: float = 0.0
    last_progress_ms: int = 0

    # Warmup proof-of-life snapshots.
    # Captured by ShadowEngine._watch_position at fixed milestones from open.
    # peak_ticks = (peak_price_favorable - entry_price) / tick_scaled, normalized
    # so positive = position has been in profit at that time, 0/negative = not.
    # Used post-hoc to evaluate warmup-exit hypothesis: if a "proof of life"
    # rule (peak_ticks >= 1 at t=1500ms) reliably separates winners from
    # phase_1_gap_collapse losers.
    # NULL when trade closed before that milestone or tick lookup failed.
    peak_ticks_at_500ms: float | None = None
    peak_ticks_at_1000ms: float | None = None
    peak_ticks_at_1500ms: float | None = None
    peak_ticks_at_2000ms: float | None = None
    # Instantaneous adverse excursion at 1000ms, in ticks (positive =
    # against us). nevergreen_cut tests exactly this quantity once a
    # position is 900ms old; mae_pct is the terminal maximum and cannot
    # stand in for it.
    adverse_ticks_at_1000ms: float | None = None

    # is_closing flag:
    # Set to True at start of _close_position to prevent concurrent close calls.
    # Python GIL guarantees that `pos.is_closing = True` is atomic relative to
    # asyncio scheduler — no yield point between check and set.
    # Protects against:
    #   - engine_shutdown firing while watch loop calls _close_position
    #   - watcher_error fallback firing while normal close in-flight
    #   - Future refactors adding new close callers
    # Normal flow doesn't need this (watch loop does break after close), but
    # defensive engineering is cheap insurance.
    is_closing: bool = False

    # ─── per-pair static cache (filled by ShadowEngine on open) ─────────
    # These values depend only on the pair's symbol and YAML exit_strategy
    # config — they DO NOT change over the lifetime of a position. The
    # 20ms watcher previously recomputed them on every tick (to_mexc +
    # get_tick_size + get_binance_scale + multiply + loader.get(...).
    # exit_strategy.binance_reversal_max_ms inside a try/except). Caching
    # at open eliminates ~50/sec × N positions of redundant work in the
    # hottest path of the bot.
    #
    # tick_scaled: MEXC tick size × binance_scale. Hot path uses this for
    #   gap/SL/trail tick math. 0.0 sentinel = "not cached yet" (fall back).
    # exit_strategy_cached: ExitStrategyConfig snapshot from YAML at open.
    #   Read-only reference — safe to share since ConfigLoader returns
    #   immutable dataclasses. None = "no loader available".
    # binance_reversal_max_ms_cached: extracted from exit_strategy at open
    #   so the watch loop avoids the per-tick `loader.get(...).exit_strategy`
    #   attribute chain plus try/except.
    tick_scaled: float = 0.0
    exit_strategy_cached: object = None  # ExitStrategyConfig | None
    binance_reversal_max_ms_cached: int = 0

    @property
    def is_open(self) -> bool:
        return self.exit_reason is None

    @property
    def elapsed_sec(self) -> float:
        return (time.time() * 1000 - self.opened_at_ms) / 1000

    def record_peak_snapshot(self, elapsed_ms: int, peak_ticks: float,
                             adverse_ticks: float | None = None) -> None:
        """Record peak_ticks at a fixed elapsed-time milestone.

        Called from ShadowEngine._watch_position when elapsed_ms crosses
        one of [500, 1000, 1500, 2000]. Subsequent calls for the same
        milestone are no-ops (first-cross wins, no overwrite).

        `adverse_ticks` is the INSTANTANEOUS excursion against us at the same
        moment — the quantity nevergreen_cut actually tests.
        """
        if (adverse_ticks is not None and elapsed_ms >= 1000
                and self.adverse_ticks_at_1000ms is None):
            self.adverse_ticks_at_1000ms = adverse_ticks
        if elapsed_ms >= 500 and self.peak_ticks_at_500ms is None:
            self.peak_ticks_at_500ms = peak_ticks
        if elapsed_ms >= 1000 and self.peak_ticks_at_1000ms is None:
            self.peak_ticks_at_1000ms = peak_ticks
        if elapsed_ms >= 1500 and self.peak_ticks_at_1500ms is None:
            self.peak_ticks_at_1500ms = peak_ticks
        if elapsed_ms >= 2000 and self.peak_ticks_at_2000ms is None:
            self.peak_ticks_at_2000ms = peak_ticks

    # ─── peak-listener patch ─────────────────────────────────
    def update_peak_only(self, current_mexc_price: float) -> None:
        """Fast-path peak update for OrderBook listener.

        Invoked on every MEXC WS depth update (~50-200/sec). Updates ONLY
        `peak_price_favorable` and `last_progress_ms`. Does NOT recompute
        ROI/MFE/MAE — those are maintained by the polling watch loop.

        Safe to call after position is closed (returns silently).
        Safe to call before entry_price is set (returns silently).
        """
        if not self.is_open or self.is_closing:
            return
        if self.entry_price <= 0 or current_mexc_price <= 0:
            return

        if self.peak_price_favorable == 0.0:
            # First call — seed peak to current price. Subsequent calls
            # will only improve it.
            self.peak_price_favorable = current_mexc_price
            self.last_progress_ms = int(time.time() * 1000)
            return

        improved = False
        if self.direction == "long":
            if current_mexc_price > self.peak_price_favorable:
                self.peak_price_favorable = current_mexc_price
                improved = True
        else:  # short
            if current_mexc_price < self.peak_price_favorable:
                self.peak_price_favorable = current_mexc_price
                improved = True

        if improved:
            # Only update timestamp on actual improvement — matches
            # update_price semantics so phase-3 stall detection sees
            # the same `last_progress_ms` distribution.
            self.last_progress_ms = int(time.time() * 1000)

    def update_price(self, current_mexc_price: float) -> None:
        """Update current price and recompute live metrics."""
        self.current_price = current_mexc_price
        if self.entry_price <= 0:
            return
        if self.direction == "long":
            price_pct = (current_mexc_price - self.entry_price) / self.entry_price * 100
        else:
            price_pct = (self.entry_price - current_mexc_price) / self.entry_price * 100

        # ROI on margin = price_pct × leverage
        self.current_roi_pct = price_pct * self.leverage

        # MFE/MAE in price terms
        if price_pct > self.mfe_pct:
            self.mfe_pct = price_pct
            self.time_to_max_favorable_sec = int(self.elapsed_sec)
        if price_pct < self.mae_pct:
            self.mae_pct = price_pct

        # Peak/trough ROI
        if self.current_roi_pct > self.peak_roi_pct:
            self.peak_roi_pct = self.current_roi_pct
        if self.current_roi_pct < self.trough_roi_pct:
            self.trough_roi_pct = self.current_roi_pct

        # Adaptive exit: track peak favorable price.
        # First call initializes peak to entry; subsequent calls update only
        # when price improves in our direction.
        # NOTE: when the OrderBook listener
        # is active, `peak_price_favorable` is updated at full WS resolution
        # by `update_peak_only`. The block below is still correct (only
        # improves peak, idempotent if already improved) and serves as a
        # safety net if the listener is not registered.
        now_ms = int(time.time() * 1000)
        if self.peak_price_favorable == 0.0:
            self.peak_price_favorable = current_mexc_price
            self.last_progress_ms = now_ms
        elif self.direction == "long":
            if current_mexc_price > self.peak_price_favorable:
                self.peak_price_favorable = current_mexc_price
                self.last_progress_ms = now_ms
        else:  # short
            if current_mexc_price < self.peak_price_favorable:
                self.peak_price_favorable = current_mexc_price
                self.last_progress_ms = now_ms

    def close(
        self,
        exit_price: float,
        exit_slippage_pct: float,
        exit_fees_usdt: float,
        exit_reason: str,
    ) -> None:
        """Finalize the position with exit data and compute final PnL."""
        self.exit_price = exit_price
        self.exit_slippage_pct = exit_slippage_pct
        self.exit_fees_usdt = exit_fees_usdt
        self.exit_reason = exit_reason
        self.closed_at_ms = int(time.time() * 1000)
        # duration = time from open to exit DECISION (not MEXC close).
        # exit_decided_at_ms is set by _close_position before MEXC call.
        # If not set (shadow-only, or legacy path), fall back to closed_at_ms.
        decision_ms = self.exit_decided_at_ms if self.exit_decided_at_ms > 0 else self.closed_at_ms
        self.duration_ms = decision_ms - self.opened_at_ms
        self.duration_sec = int(self.duration_ms / 1000)

        # PnL: centralised in src.utils.pnl.calc_pnl_usdt — same formula
        # is used in shadow_engine fallbacks too.
        self.pnl_usdt = calc_pnl_usdt(
            direction=self.direction,
            entry=self.entry_price,
            exit=exit_price,
            qty=self.qty,
        )

        self.net_pnl_usdt = self.pnl_usdt - self.entry_fees_usdt - self.exit_fees_usdt

        # Recompute ROI from the REALIZED exit. Previously current_roi_pct was
        # left at the last watcher-tick value (price_pct*leverage on the last
        # 20ms poll), which never equals net_pnl/margin — so the persisted
        # roi_pct was inconsistent with net_pnl_usdt and not comparable to the
        # live path (which sets roi = realized pnl/margin). Now both match.
        if self.margin_usdt > 0:
            self.current_roi_pct = (self.net_pnl_usdt / self.margin_usdt) * 100.0
