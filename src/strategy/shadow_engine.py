"""
ShadowEngine — orchestrates virtual trading from signals.

Flow per signal:
  1. Receive signal (from static_gap detector via callback)
  2. Pre-flight checks: pair tradeable? not in cooldown? funding ok?
  3. Lookup pair_config for tunable params (per-pair)
  4. Try IOC entry up to ioc_max_attempts
  5. If filled → create ShadowPosition, start watching task
  6. Watching task ticks every 100ms checking exit conditions:
     - stop_loss (catastrophic protection, after sl_grace_sec)
     - max_hold_sec timeout
     - adaptive_stalled / adaptive_reversal (when adaptive_exit_enabled)
     - reverse_signal (if enabled)
  7. When exit triggered → MarketExecutor → record trade in DB
  8. Apply post-trade cooldown (per-pair)

The engine is thread-safe per symbol — one position per symbol max (configurable).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass

from src.config import ShadowConf
from src.exchanges.mexc_rest import to_mexc, get_binance_scale
from src.exchanges.orderbook import OrderBookManager
from src.execution.funding_guard import FundingGuard
from src.execution.ioc_executor import IOCExecutor, IOCAttemptResult
from src.execution.live_executor import (
    get_tick_size,
    CONTRACT_SIZES,
    OPEN_FREQ_CODES,
)

# ── Shadow-only sizing (DECOUPLED from the live cfg margin/leverage) ──
# Shadow trades use THIS fixed margin/leverage so that tuning the per-pair LIVE
# margin never shifts shadow stats (stable benchmark for comparison). LIVE pairs
# override pos.margin_usdt -> live_margin/live_leverage further below (~line 1211),
# so the real orders + live_trades are UNAFFECTED by these values.
SHADOW_MARGIN_MIN_USDT = 25.0
SHADOW_MARGIN_MAX_USDT = 30.0
SHADOW_LEVERAGE_MIN = 50
SHADOW_LEVERAGE_MAX = 60
from src.execution.market_executor import MarketExecutor
from src.execution.realism import (
    RealismProfile,
    calculate_funding_cost,
    check_and_apply_spread_blowout,
    crosses_funding_window,
    fee_pct_for_pair,
    get_profile,
    should_reject_order,
    simulate_server_error,
)
from src.state.pair_state_manager import PairStateManager
from src.storage.db import Database
from src.strategy.shadow_position import ShadowPosition
from src.strategy.signal import Signal
from src.utils.pnl import calc_pnl_usdt

logger = logging.getLogger(__name__)

def open_freq_limit_code(err_msg: str | None) -> str | None:
    """Which open-rate limit MEXC just returned, or None.

    Matched on `api_error_<code>` so a bare number inside some other message
    cannot trigger a six-hour throttle by accident. The text fallback catches
    a future code that reuses the same wording.
    """
    if not err_msg:
        return None
    for code in OPEN_FREQ_CODES:
        needle = f"api_error_{code}"
        at = err_msg.find(needle)
        # Right boundary: without it "api_error_2036" also matches
        # "api_error_20360" and would arm a six-hour throttle for an
        # unrelated code.
        if at >= 0 and not err_msg[at + len(needle):at + len(needle) + 1].isdigit():
            return code
    if "position-opening frequency" in err_msg:
        return "unknown"
    return None

# Hardcoded upper bound on position age, independent of strategy config.
# If a position lives this long, force-close it regardless of strategy
# state (phase exits, trailing stops, etc.). Catches scenarios where the
# normal exit logic fails to fire — WS stall, phase config bug, watcher
# desync. Default 600s = 10 min is far beyond normal trade duration; any
# trade reaching this is presumed stuck.
# ⚠️ This is the ABSOLUTE SAFETY CEILING (infra/env, deploy-level), NOT the
# per-pair `max_hold_sec` exit-limit (yaml execution, ~60s). Different things,
# similar names. Override via env STAKAN_ABSOLUTE_MAX_HOLD_SEC. Set 0 to disable
# (NOT RECOMMENDED — disables a key safety net).
_ABSOLUTE_MAX_HOLD_SEC = int(os.environ.get("STAKAN_ABSOLUTE_MAX_HOLD_SEC", "600"))

# ─── Position-watcher tuning (event-driven exits + staleness guard) ──────────
# Timer floor for the watch loop: even with no book updates, re-check this
# often so TIME-based exits (DOA, stall, max_hold, sl_grace) and the staleness
# guard still fire. Book updates wake the loop sooner (event-driven) for
# PRICE-based exits (stop, trail) — keeps the old robustness-against-WS-stall
# intent (a timeout floor) while reacting instantly to real price moves.
_WATCH_TIME_TICK_SEC = float(os.environ.get("STAKAN_WATCH_TIME_TICK_SEC", "0.05"))
# Force-close a position if its MEXC book hasn't updated within this window. A
# stalled feed means the watcher reads a frozen price and no price-exit can
# fire, so the position would ride blind until _ABSOLUTE_MAX_HOLD_SEC (600s).
# This tightens that net to ~2s. Conservative enough that a healthy-but-quiet
# book on an active pair won't false-trigger. Env-tunable; 0 disables.
_FEED_STALE_MS = int(os.environ.get("STAKAN_FEED_STALE_MS", "2000"))


def _feed_is_stale(last_update_ts_ms: int, now_ms: int, stale_ms: int) -> bool:
    """True when the orderbook hasn't updated within ``stale_ms``.

    Returns False before the first update (last_update_ts_ms == 0) and when
    stale_ms <= 0 (guard disabled), so it never false-fires on a fresh or
    intentionally-unguarded book.
    """
    if stale_ms <= 0 or last_update_ts_ms <= 0:
        return False
    return (now_ms - last_update_ts_ms) > stale_ms


def _needs_binance_book(cfg) -> bool:
    """Whether the watch loop must read the Binance book this tick.

    The Binance top-of-book is consumed ONLY by the binance_reversal stop
    (cfg.binance_reversal_ticks). When that's disabled (0), reading it is
    dead work and a needless dependency on the Binance feed in the exit path.
    """
    return getattr(cfg, "binance_reversal_ticks", 0) > 0 or getattr(cfg, "gap_retrace_frac", 0) > 0


def _gap_relative_trigger_scaled(direction, entry_price, gap_ticks, tick_scaled, retrace_frac):
    """Binance price level (scaled) at which the entry gap has retraced
    retrace_frac of itself = the gap-relative binance_reversal trigger.

    Entry geometry: Binance led by gap_ticks at entry, so B0 ~ entry +/- gap.
    Cut when Binance gives back retrace_frac of that lead:
      long  -> entry + gap*tick*(1-frac)   (fire when binance_bid <= this)
      short -> entry - gap*tick*(1-frac)    (fire when binance_ask >= this)
    Watches Binance's OWN retrace, so a MEXC dip while Binance holds the gap
    does NOT trigger (we hold the recoverable dip). Returns 0.0 (inactive) when
    inputs are invalid so the caller falls back to fixed-tick.
    """
    if (entry_price <= 0 or gap_ticks <= 0 or tick_scaled <= 0
            or retrace_frac <= 0):
        return 0.0
    margin = gap_ticks * tick_scaled * (1.0 - retrace_frac)
    if direction == "long":
        return entry_price + margin
    return entry_price - margin


async def _wait_for_book_event(event, timeout_sec: float) -> bool:
    """Block until the MEXC book signals an update OR ``timeout_sec`` elapses.

    Returns True if woken by a book update, False on timeout. A None event
    degrades gracefully to a plain sleep (polling fallback). The timeout floor
    guarantees time-based exits still run even if the feed goes quiet.
    """
    if event is None:
        await asyncio.sleep(timeout_sec)
        return False
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout_sec)
        event.clear()
        return True
    except asyncio.TimeoutError:
        return False


def _entry_spread_bps(best_bid: float, best_ask: float) -> float:
    """MEXC top-of-book spread in bps at entry time.

    Logged per trade so we can later test whether adverse-stop trades cluster
    on a wide book (→ a spread-gate would help). Returns 0.0 when the book is
    not ready (non-positive or crossed quotes).
    """
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return 0.0
    mid = (best_bid + best_ask) / 2.0
    return (best_ask - best_bid) / mid * 1e4


# How far past the touch a capped reversal-close may cross. The plain MARKET
# close (/position/close_all) sweeps the WHOLE gap when binance_reversal fires
# into a violent move (the −13bps fills). A capped IOC-limit crosses only this
# many ticks then cancels (falling back to market), bounding the sweep. Tunable.
_REVERSAL_CLOSE_CAP_TICKS = float(os.environ.get("STAKAN_REVERSAL_CLOSE_CAP_TICKS", "5"))
# When 1 (default): binance_reversal cut uses the FAST close_all (market, ~66ms
# MEXC-side) instead of capped IOC (/order/create, ~150ms). 84ms-less latency =
# book drifts less before close lands -> tighter realized cut, often beating the
# capped-IOC slippage bound. Set 0 to revert to capped IOC.
_REVERSAL_FAST_CLOSE = os.environ.get("STAKAN_REVERSAL_FAST_CLOSE", "1") == "1"


def _capped_close_limit_scaled(
    direction: str, best_bid_scaled: float, best_ask_scaled: float,
    cap_ticks: float, tick_scaled: float,
) -> float:
    """Capped IOC-limit CLOSE price, scaled (binance-equiv) domain.

    Closing a LONG sells → cross DOWN to best_bid − cap; closing a SHORT buys →
    cross UP to best_ask + cap. The IOC fills the touch + bounded depth and
    cancels the rest, so it can NEVER sweep the whole gap. 0.0 if book unusable
    (caller then routes to the guaranteed market close).
    """
    if best_bid_scaled <= 0 or best_ask_scaled <= 0 or tick_scaled <= 0:
        return 0.0
    cap = max(0.0, cap_ticks) * tick_scaled
    if direction == "long":          # close = SELL
        return best_bid_scaled - cap
    return best_ask_scaled + cap     # close = BUY (short)


# Momentum entry filter — enabled PER-PAIR via the YAML execution block
# (momentum_filter / momentum_tau_sec / momentum_threshold_bps). No global
# env toggle: a pair is filtered iff its yaml momentum_filter is true.
if _ABSOLUTE_MAX_HOLD_SEC <= 0:
    # Disabled — set to a huge value rather than special-case. 1 year is
    # effectively "off" for any realistic trading.
    _ABSOLUTE_MAX_HOLD_SEC = 365 * 24 * 3600
    logger.warning(
        "STAKAN_ABSOLUTE_MAX_HOLD_SEC <= 0 — absolute max_hold kill switch "
        "is effectively disabled. This is a SAFETY net; disabling is risky."
    )
else:
    logger.info(
        "Absolute max_hold safety net: %d seconds (=%d min)",
        _ABSOLUTE_MAX_HOLD_SEC, _ABSOLUTE_MAX_HOLD_SEC // 60,
    )


# Per-pair config defaults — loaded from DB but cached
@dataclass
class PairExecConfig:
    """Subset of pair_configs that ShadowEngine uses for execution."""
    ioc_offset_ticks: int = 0    # 0=at-touch, N>0=cross N ticks, N<0=inside spread (tick-exact)
    ioc_max_attempts: int = 2
    ioc_attempt_interval_ms: int = 80

    # Position sizing (RANDOMIZED per trade)
    margin_min_usdt: float = 23.0
    margin_max_usdt: float = 30.0
    leverage_min: int = 50
    leverage_max: int = 80

    # Stop loss — TICK-BASED ONLY (leverage-invariant). ROI-based SL is
    # not used: it created leverage-dependent thresholds that interfered
    # with simple_trail and phase exits firing first. stop_loss_ticks
    # MUST be > 0 or stop_loss is effectively disabled.
    stop_loss_ticks: int = 5
    # SL grace period: block stop_loss exit during the first N seconds.
    # Lead-lag entries often hit a brief MAE spike during MEXC catch-up,
    # then recover. Grace prevents noise-triggered SL during that window.
    sl_grace_sec: float = 0.0

    # Time-based safety net (always active, exit at this elapsed time).
    max_hold_sec: int = 600

    cooldown_after_loss_sec: int = 10
    cooldown_after_win_sec: int = 5

    # Runtime mode label, NOT a gate. The live authority is pair_states.state
    # (PairStateManager.is_in_live); the pair_configs.mode column was dropped.
    # cfg.mode is unused by trading logic; pos.mode is set from the live-exec
    # outcome. Built from a graceful default in _reload_pair_configs.
    mode: str = "shadow"

    # ────────────────────────────────────────────────────────────────────
    # Exit logic — simple_trail only (adverse + trail + breakeven, plus the
    # stop_loss / max_hold safety nets). There is no mode switch: the old
    # phase_exit model and the exit_strategy.mode yaml key were removed; every
    # pair runs _check_simple_trail_exit.
    # ────────────────────────────────────────────────────────────────────

    # Binance-reversal early stop. When Binance reverses past entry by
    # binance_reversal_ticks ticks (within binance_reversal_max_ms after
    # entry), the lead-lag thesis is broken → exit immediately. Checked in
    # _check_exit for every position regardless of exit model.
    # Set to 0 to disable. (Name kept for DB/config compatibility.)
    binance_reversal_ticks: float = 2.0
    # Gap-relative binance_reversal: when >0, cut when Binance retraces this
    # fraction of the ENTRY GAP (not a fixed tick past entry). 0.7 = cut once
    # Binance gives back 70% of the lead. 0 = use fixed binance_reversal_ticks.
    gap_retrace_frac: float = 0.0

    # Momentum entry filter (per-pair; see ShadowEngine._momentum_ok).
    momentum_filter: bool = False
    momentum_tau_sec: float = 2.0
    momentum_threshold_bps: float = 2.0
    min_mexc_lag_pct: float = 0.0
    max_mexc_lag_pct: float = 0.0


class ShadowEngine:
    """
    Consumes signals → executes virtual trades → records results.

    Wire it up in main.py:
        shadow_engine = ShadowEngine(...)
        await shadow_engine.start()
        # Each detector pushes signals via:
        await shadow_engine.on_signal(signal)
    """

    def __init__(
        self,
        cfg: ShadowConf,
        db: Database,
        ob_manager: OrderBookManager,
        state_manager: PairStateManager,
        funding_guard: FundingGuard,
        ioc_executor: IOCExecutor | None = None,
        market_executor: MarketExecutor | None = None,
        max_positions_per_symbol: int = 1,
        realism_profile: str = 'realistic',
        live_pool=None,               # Optional[LiveExecutorPool] — multi-slot live trading
        live_db=None,                 # Optional[LiveDatabase] — separate DB for live trades
        alerts=None,                  # Optional[TelegramAlerts] — for orphan position alerts (live-fixes patch)
        config_loader=None,           # Optional[ConfigLoader] — for per-pair exit_strategy via YAML
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.live_db = live_db        # routed in _persist_trade based on pos.mode
        self.alerts = alerts          # critical alerts (orphan positions, etc.)
        self.ob_manager = ob_manager
        self.state_manager = state_manager
        self.funding_guard = funding_guard
        self.ioc_executor = ioc_executor or IOCExecutor()
        self.market_executor = market_executor or MarketExecutor()
        self.max_positions_per_symbol = max_positions_per_symbol
        self._config_loader = config_loader  # may be None (legacy behaviour: always 'phase')

        # Realism profile — bridges shadow ↔ live gap
        self.realism: RealismProfile = get_profile(realism_profile)
        self.realism_profile_name: str = realism_profile
        logger.info(
            "ShadowEngine realism profile = %s (entry_latency=%dms, "
            "close_latency=%dms, base_rejection=%.1f%%, funding=%s)",
            realism_profile,
            self.realism.signal_to_order_latency_ms,
            self.realism.close_signal_to_order_latency_ms,
            self.realism.base_rejection_rate * 100,
            self.realism.enable_funding_cost,
        )

        # Tracking realism-induced metrics
        self.entries_rejected: int = 0
        self.funding_cost_paid: float = 0.0

        # Live trading via multi-slot pool (None means shadow-only).
        # Each pair is routed to its assigned slot via live_pool.find_slots_for_pair().
        self.live_pool = live_pool
        self._slot_cooldown_until: dict[int, float] = {}
        # MEXC api_error_10014 = "position-opening frequency temporarily
        # limited" — an anti-abuse throttle, NOT the ordinary 510 rate limit.
        # Measured on this account: an isolated trip clears in 2-3s (100% <5s),
        # but once the account is in the punished state the same probe needs
        # minutes (median 78s, p90 328s) and every further attempt keeps it
        # alive. So: tiny first pause, escalate on repeats, reset on a fill.
        # per-slot: each account has its own ceiling, so strikes must not
        # bleed from one account onto another slot's pause length.
        self._last_open_ts: dict[int, float] = {}
        # Slot -> monotonic deadline of throttled mode. Latched by the
        # first 10014 and refreshed by any further one; while it lasts
        # the slot holds ~65s after each ACCEPTED open, which is the
        # measured ceiling (1 open per ~61s).
        self._open_rl_mode_until: dict[int, float] = {}
        # Last limit class seen per slot, and the deadline last written to
        # the DB — so a class change re-alerts and a lengthening window
        # keeps its persisted copy in step.
        self._open_rl_code: dict[int, str] = {}
        self._open_rl_persisted: dict[int, int] = {}
        # Last webkey_refreshed_at seen per slot. A change means the
        # account underneath was swapped, so its predecessor's open-rate
        # latch must not carry over.
        self._open_rl_wk_seen: dict[int, int | None] = {}
        if self.live_pool is not None:
            logger.info("LIVE TRADING ENABLED (multi-slot mode) — pair routing via LiveExecutorPool")
        else:
            logger.info("Live trading DISABLED (shadow-only mode)")

        # Per-symbol cooldown timestamps (when next entry is allowed)
        # Keyed (slot_id | None, symbol): one account's post-trade pause
        # must never silence another account on the same pair.
        self._cooldown_until: dict[tuple, int] = {}

        # Momentum filter: per-symbol time-decay EMA of the Binance leader price
        # + the wall-clock timestamp of its last update (for time-based decay).
        self._mom_ema: dict[str, float] = {}
        self._mom_ema_ts: dict[str, float] = {}

        # Open positions per symbol
        self._open_positions: dict[str, list[ShadowPosition]] = defaultdict(list)
        # Watcher tasks per position. Keyed by id(pos) — the CPython
        # object id, an integer that's unique while the position is alive.
        self._watcher_tasks: dict[int, asyncio.Task] = {}

        # Cached pair configs (refreshed periodically)
        self._pair_configs: dict[str, PairExecConfig] = {}
        self._configs_loaded_at: int = 0
        self._configs_ttl_sec: int = 30
        # Background reload task reference (prevents duplicate spawns)
        self._config_reload_task: asyncio.Task | None = None

        # Latency simulation — model real API roundtrip.
        # Reads from cfg.entry_latency_ms but adds randomization
        # If 0 → disabled (keeps shadow as-is)
        base_latency = getattr(cfg, "entry_latency_ms", 0)
        self._latency_enabled = base_latency > 0
        # IOC sleep window — calibrated to live PENGU submit-latency p10..p90
        # (2026-06: measured 150..205ms from live_trades.latency_submit_ms).
        # Explicit range, not a base±heuristic, so it can be tuned to live.
        self._latency_min_ms = getattr(cfg, "entry_latency_min_ms", 150)
        self._latency_max_ms = getattr(cfg, "entry_latency_max_ms", 205)
        # Maximum acceptable price drift during latency (% of mid).
        # If price moved more adversely → consider sigal stale, skip.
        # 0.05% on $80k BTC = $40 — reasonable threshold
        self._max_acceptable_drift_pct = 0.05
        if self._latency_enabled:
            logger.info(
                "Shadow IOC latency window = %d..%d ms (uniform) — calibrated to live PENGU submit p10..p90",
                self._latency_min_ms, self._latency_max_ms,
            )

        # Diagnostics
        self.signals_received = 0
        self.entries_attempted = 0
        self.entries_filled = 0
        self.entries_partial = 0
        self.entries_expired = 0
        self.signals_skipped_not_tradeable = 0
        self.signals_skipped_cooldown = 0
        self.signals_skipped_funding = 0
        self.signals_skipped_low_confidence = 0
        self.signals_skipped_max_positions = 0
        self.signals_skipped_momentum = 0

        # Slot-lock single-flight guard:
        # Set of symbols currently in the entry pipeline (between max_positions
        # check and addition to _open_positions). After decouple patch made
        # on_signal fire-and-forget, multiple concurrent signals on the same
        # symbol could all pass max_positions check (since the check happens
        # BEFORE the await on IOC submit). Result: duplicate live positions on
        # MEXC (2-3 concurrent positions per signal burst).
        # This set provides atomic single-flight guard:
        #   1. Check membership before max_positions check
        #   2. Add to set synchronously (no await in between)
        #   3. Discard in finally clause after IOC submit completes
        # WARNING: This is a SET, not a Lock. It only blocks signals that
        # arrive while another submission is in-flight on the same symbol.
        # It does NOT serialize submissions — if signals come 5s apart, both
        # proceed normally. Exactly the behavior we want.
        self._pending_submissions: set[tuple] = set()
        self.signals_skipped_pending_submit = 0
        # Slot passes: one per (signal, live slot). The per-slot gates are
        # counted against THIS, not against signals_received.
        self.signals_fanned_out = 0
        # (slot, reason) -> count. The global skip counters mix two accounts
        # as soon as both trade one pair, which makes the funnel line
        # uninterpretable exactly when a slot A/B needs it most. Kept in
        # memory and reported per slot; deliberately NOT rows in
        # live_open_misses, which every fill-rate query reads.
        self._slot_skips: dict[tuple, int] = {}
        # (symbol, slot) -> (message fingerprint, last logged ts). A standing
        # sizing mismatch is a fact, not an event — reloads happen every
        # 30-50s and repeating it that often buries the log.
        self._sizing_warned: dict[tuple, tuple] = {}

        self.signals_skipped_no_book = 0
        self.signals_skipped_lag_out_of_range = 0
        self.signals_skipped_latency_drift = 0
        self.positions_closed = 0

        self._stop = asyncio.Event()

    # ============================================================
    # Lifecycle
    # ============================================================

    async def start(self) -> None:
        await self._reload_pair_configs()
        # start heartbeat loop that warns owner via TG
        # if no live trades happened in the last N minutes despite live state.
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        # every 60s log signal funnel counters at INFO
        # level so we can see WHERE signals are dropping (cooldown / lag /
        # latency_drift / max_positions / no_book / etc.) without enabling DEBUG.
        self._funnel_task = asyncio.create_task(self._funnel_log_loop())
        # the old log line said "margin=$25 leverage=50x default"
        # — those were hardcoded literals that drifted away from the real
        # `pair_configs` values years ago. Misleading when grep'ing logs to
        # debug sizing. Real defaults vary per pair; consult pair_configs DB
        # for sizing facts.
        logger.info("ShadowEngine started (sizing per pair_configs DB)")

    async def stop(self) -> None:
        self._stop.set()
        # Cancel heartbeat first
        if hasattr(self, "_heartbeat_task") and self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
        # Cancel funnel-log task
        if hasattr(self, "_funnel_task") and self._funnel_task is not None:
            self._funnel_task.cancel()
            try:
                await self._funnel_task
            except (asyncio.CancelledError, Exception):
                pass
        # Force-close all open positions. Iterate over a COPY of the per-symbol
        # list: _close_position removes the position from self._open_positions[sym]
        # (the same list), which would skip every-other element mid-iteration and
        # leave live positions un-closed on shutdown when >1 per symbol.
        for symbol, positions in list(self._open_positions.items()):
            for pos in list(positions):
                try:
                    await self._close_position(pos, "engine_shutdown")
                except Exception as e:
                    logger.exception("Failed to close %s on shutdown: %s", symbol, e)
        # Cancel watcher tasks
        for task in self._watcher_tasks.values():
            task.cancel()
        await asyncio.gather(*self._watcher_tasks.values(), return_exceptions=True)
        logger.info("ShadowEngine stopped")

    async def _heartbeat_loop(self) -> None:
        """
        Every 5 minutes check if any live pair has been
        silent (no live_trades) for >30 min. If so, send a Telegram alert.
        Throttled to fire at most once per 60 minutes per pair.
        """
        check_interval_sec = 300       # check every 5min
        silence_threshold_sec = 1800   # 30min without trade = alarm
        last_alert: dict[str, int] = {}
        alert_cooldown_sec = 3600      # at most 1 alert/hour per pair

        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=check_interval_sec)
                    return  # _stop set → exit
                except asyncio.TimeoutError:
                    pass  # interval elapsed, run check

                if self.alerts is None or self.live_db is None or self.state_manager is None:
                    continue

                try:
                    now = int(time.time())
                    # Get all pairs in live state
                    live_pairs: list[str] = []
                    try:
                        live_pairs = [ps.symbol for ps in self.state_manager.states_by_status("live")]
                    except Exception:
                        continue

                    if not live_pairs:
                        continue

                    # One grouped query instead of N sequential ones.
                    # Old code did `await fetchall(... WHERE symbol=?)` per
                    # pair — N roundtrips at N×~10ms each, blocking the
                    # event loop. GROUP BY in a single query is O(1) trips.
                    try:
                        placeholders = ",".join("?" * len(live_pairs))
                        rows = await self.live_db.fetchall(
                            f"SELECT symbol, account_label, "
                            f"       MAX(opened_at) AS last_ts "
                            f"FROM live_trades "
                            f"WHERE symbol IN ({placeholders}) "
                            f"GROUP BY symbol, account_label",
                            tuple(live_pairs),
                        )
                    except Exception:
                        logger.exception("Heartbeat: batched fetch failed")
                        continue

                    # Keyed (symbol, account_label): grouped by symbol alone, a
                    # slot that stopped trading stayed invisible for as long as
                    # its sibling kept the pair's MAX(opened_at) fresh.
                    last_ts_by_key: dict[tuple, int] = {}
                    for r in rows or []:
                        try:
                            last_ts_by_key[(r["symbol"], r["account_label"])] = (
                                int(r["last_ts"]) if r["last_ts"] else 0)
                        except Exception:
                            logger.debug("Heartbeat: unparseable last_ts row %r", r)

                    for symbol, label in self._heartbeat_keys(live_pairs):
                        last_ts = last_ts_by_key.get((symbol, label), 0)
                        silence_sec = now - last_ts if last_ts else 999999

                        if silence_sec >= silence_threshold_sec:
                            # Throttle: don't alert more than once per cooldown
                            if now - last_alert.get((symbol, label), 0) < alert_cooldown_sec:
                                continue
                            last_alert[(symbol, label)] = now

                            silence_min = silence_sec // 60
                            try:
                                await self.alerts.send(
                                    f"😴 <b>NO TRADES</b>"
                                    f"{(' · <b>' + label.upper() + '</b>') if label else ''}\n"
                                    f"Symbol: <code>{symbol}</code>\n"
                                    f"Silent for: <b>{silence_min} min</b>\n"
                                    f"Bot is alive but not trading. Check: kill switch? state? webkey?",
                                    category=f"heartbeat:{symbol}:{label}",
                                    throttle_sec=0,  # already throttled by last_alert dict
                                    suppress_during_quiet=False,
                                )
                            except Exception:
                                logger.exception("Failed to send heartbeat alert for %s", symbol)
                except Exception:
                    logger.exception("Heartbeat loop iteration failed")
        except asyncio.CancelledError:
            return

    async def _funnel_log_loop(self) -> None:
        """
        Every 60s log a single INFO line summarising
        WHERE signals are being dropped between detector emit and trade open.

        Reads delta-counters since last tick so each line shows "what happened
        in the last 60s" rather than cumulative-since-bot-start. Compact,
        single-line, easy to grep for ("FUNNEL").
        """
        interval_sec = 60
        # prev/cur dicts MUST have identical keys.
        # Previous version had "detector_disabled" only in prev (counter never
        # existed on the engine) and "pending_submit" only in cur — the
        # dict-comprehension below then raised KeyError on the first
        # iteration, silently killing this task before any FUNNEL line ever
        # appeared in logs. Removed the phantom "detector_disabled" and
        # ensured pending_submit is wired through prev/skipped/log.
        prev = {
            "received": 0,
            "fanned_out": 0,
            "not_tradeable": 0,
            "low_confidence": 0,
            "lag_out_of_range": 0,
            "cooldown": 0,
            "funding": 0,
            "max_positions": 0,
            "pending_submit": 0,
            "no_book": 0,
            "latency_drift": 0,
        }
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval_sec)
                    return
                except asyncio.TimeoutError:
                    pass

                cur = {
                    "received": self.signals_received,
                    "fanned_out": self.signals_fanned_out,
                    "not_tradeable": self.signals_skipped_not_tradeable,
                    "low_confidence": self.signals_skipped_low_confidence,
                    "lag_out_of_range": self.signals_skipped_lag_out_of_range,
                    "cooldown": self.signals_skipped_cooldown,
                    "funding": self.signals_skipped_funding,
                    "max_positions": self.signals_skipped_max_positions,
                    "pending_submit": self.signals_skipped_pending_submit,
                    "no_book": self.signals_skipped_no_book,
                    "latency_drift": self.signals_skipped_latency_drift,
                }
                # Defensive: assert key parity at runtime so any future drift
                # is caught on the next iteration instead of via a silent task
                # death. AssertionError is caught by the wrapping try/except
                # so the loop continues even if it ever fires.
                assert set(cur.keys()) == set(prev.keys()), (
                    f"FUNNEL counter drift: cur={set(cur.keys())} prev={set(prev.keys())}"
                )
                d = {k: cur[k] - prev[k] for k in cur}
                prev = cur

                # Compute "passed" — signals that reached _try_enter (not all
                # converted to trades, but cleared all pre-enter filters).
                # pending_submit is the slot-lock single-flight drop — also pre-enter.
                # Two stages, two units. The first filters run once per
                # SIGNAL; everything from the cooldown down runs once per SLOT
                # pass, because each live slot decides independently. Subtracting
                # per-slot drops from a per-signal total made passed_pre go
                # negative as soon as a pair had two slots.
                fanned = d["received"] - (
                    d["not_tradeable"] + d["low_confidence"]
                    + d["lag_out_of_range"]
                )
                passed = d["fanned_out"] - (
                    d["cooldown"] + d["funding"]
                    + d["max_positions"] + d["pending_submit"]
                )

                logger.info(
                    "[FUNNEL 60s] recv=%d → signals_ok=%d (notrade=%d lowconf=%d "
                    "lag=%d) → slot_passes=%d → passed=%d (cooldown=%d funding=%d "
                    "maxpos=%d pending=%d) | enter_drops: nobook=%d drift=%d",
                    d["received"], fanned,
                    d["not_tradeable"], d["low_confidence"], d["lag_out_of_range"],
                    d["fanned_out"], passed,
                    d["cooldown"], d["funding"],
                    d["max_positions"], d["pending_submit"],
                    d["no_book"], d["latency_drift"],
                )

                # Per-slot breakdown: without it the line above averages two
                # accounts together and a stalled slot hides behind a busy one.
                if self._slot_skips:
                    per_slot: dict = {}
                    for (slot, reason), n in self._slot_skips.items():
                        per_slot.setdefault(slot, []).append((reason, n))
                    for slot in sorted(per_slot, key=lambda s: (s is None, s)):
                        items = sorted(per_slot[slot], key=lambda kv: -kv[1])
                        logger.info(
                            "[FUNNEL 60s]   %s skips: %s",
                            f"slot{slot}" if slot is not None else "shadow",
                            " ".join(f"{r}={n}" for r, n in items),
                        )
                    self._slot_skips.clear()
        except asyncio.CancelledError:
            return
        except Exception:
            # log but don't let the task die silently.
            # The wrapping while-loop is gone (we returned via CancelledError
            # above), so this just makes the failure visible.
            logger.exception("[FUNNEL] loop crashed unexpectedly")

    # ============================================================
    # Signal handling
    # ============================================================

    def _momentum_ok(self, signal, cfg) -> bool | None:
        """Per-pair leader-trend filter — TIME-DECAY EMA of the Binance price.

        Skips noise-gap signals: a gap is only worth trading when the leader
        (Binance) is actually moving in the signal's direction. Returns None
        when the filter is off for this pair (caller proceeds), True when the
        signal aligns with the recent leader trend (or there's no usable
        history), False when it fights it (caller skips).

        Why time-decay (vs a per-signal EMA): signals arrive in bursts with
        irregular spacing, so a fixed per-sample alpha makes the EMA window
        collapse during bursts — exactly when adverse entries cluster. Here the
        smoothing weight is ``alpha = 1 - exp(-dt/tau)`` where ``dt`` is the REAL
        seconds since the last update, so the EMA always reflects a ~tau-second
        trend regardless of signal rate. The current price is compared to the
        PRIOR trend (pre-update EMA), then the EMA is advanced for next time.
        """
        if not getattr(cfg, "momentum_filter", False):
            return None
        try:
            px = float(getattr(signal, "binance_price", 0) or 0)
        except (TypeError, ValueError):
            return True
        if px <= 0:
            return True  # no usable leader price — fail open, don't block
        sym = signal.symbol
        now = time.time()
        prev = self._mom_ema.get(sym)
        prev_ts = self._mom_ema_ts.get(sym)
        self._mom_ema_ts[sym] = now
        if prev is None or prev_ts is None:
            self._mom_ema[sym] = px
            return True  # warming up — don't block the first sample
        # Decide against the PRIOR trend, then advance the EMA toward px.
        # Clamp the threshold: a misconfig (e.g. bps mistyped as 2000 → 0.2)
        # would make `px > prev*1.2` impossible and silently reject EVERY signal
        # of a direction. A >50bps "lean" requirement is nonsensical here.
        thr = min(50.0, max(0.0, getattr(cfg, "momentum_threshold_bps", 2.0))) / 10000.0
        if signal.direction == "long":
            aligned = px > prev * (1.0 + thr)
        elif signal.direction == "short":
            aligned = px < prev * (1.0 - thr)
        else:
            aligned = True  # unknown direction — don't gate (fail-open)
        tau = max(0.1, getattr(cfg, "momentum_tau_sec", 2.0))
        alpha = 1.0 - math.exp(-max(0.0, now - prev_ts) / tau)
        self._mom_ema[sym] = prev + alpha * (px - prev)
        return aligned

    async def on_signal(self, signal: Signal, signal_id: int | None = None) -> None:
        """
        Called by detectors when a signal is emitted.

        signal_id can be passed if the detector wrote the signal first;
        otherwise ShadowEngine references the signal as inline.
        """
        if not self.cfg.enabled:
            return

        self.signals_received += 1
        symbol = signal.symbol

        # ---- Pre-flight checks ----
        if not self.state_manager.is_tradeable(symbol):
            self.signals_skipped_not_tradeable += 1
            return

        cfg = await self._get_pair_config(symbol)

        # Momentum entry filter (per-pair, time-decay EMA): skip noise-gap
        # signals where the Binance leader isn't trending in the signal
        # direction — these fill adverse (no real move to catch up to).
        if self._momentum_ok(signal, cfg) is False:
            self.signals_skipped_momentum += 1
            return

        # NOTE: entry is gated PURELY on gap_ticks (bid/ask) in the detector.
        # Removed here 2026-05-29:
        #   - min_confidence filter: confidence = gap_ticks/(2*min_ticks), so it
        #     was just a 2nd gap-size threshold in a fractional unit (and inert
        #     whenever min_confidence <= 0.5, since the detector already enforces
        #     gap_ticks >= min_ticks). Tune gap size via min_ticks instead.
        #   - min/max_mexc_lag_pct filter: gated on mid-to-mid %, a different
        #     reference than the tick gate; redundant + inconsistent.

        # Mid-gap entry floor (re-added per-pair 2026-06-19): signal.mexc_lag_pct is
        # the signed mid-to-mid percent (static_gap_detector). 0=off. Cuts small-
        # mid-gap dead entries (PEPE/ONDO: <3bps loses, >=4bps wins).
        if cfg.min_mexc_lag_pct > 0 and abs(signal.mexc_lag_pct) < cfg.min_mexc_lag_pct:
            self.signals_skipped_lag_out_of_range += 1
            return

        # UPPER bound on the same gap. Measured on 15,868 live PEPE trades: the
        # 2-3 bps band lost $210 at -0.39 bps and a 31% win rate while 1-2 bps
        # earned +0.32 at 52%, and its dead-on-arrival share jumps 22% -> 35% —
        # we arrive and the move is already over. Negative on 11 of 14 days and
        # reproduced on the clone at -0.35 bps. Same signal-time number as the
        # minimum above, so it is available before we submit.
        # cfg.max_mexc_lag_pct=0 disables (default; behaviour-preserving).
        if cfg.max_mexc_lag_pct > 0 and abs(signal.mexc_lag_pct) > cfg.max_mexc_lag_pct:
            self.signals_skipped_lag_out_of_range += 1
            return

        # Everything above is a property of the SIGNAL and is evaluated once.
        # Everything below is a property of an ACCOUNT: each live slot is its own
        # MEXC account and decides on its own, concurrently, with its own gates.
        _slots = self._entry_slots(symbol)
        if len(_slots) == 1:
            await self._enter_for_slot(signal, signal_id, cfg, _slots[0])
            return
        # One slow account must not delay the others, and its failure must not
        # cancel their entries — hence gather with return_exceptions.
        _res = await asyncio.gather(
            *(self._enter_for_slot(signal, signal_id, cfg, s) for s in _slots),
            return_exceptions=True,
        )
        for _s, _r in zip(_slots, _res):
            if isinstance(_r, BaseException):
                logger.exception(
                    "[ENTRY] slot=%s %s raised", _s, symbol, exc_info=_r)

    @staticmethod
    def _al_text(sid: int, code: str, hold: float, mode: float, symbol: str) -> str:
        """Alert body for an open refusal, worded for the code that arrived.

        2036 ("Number of orders has exceeded the limit") is a per-CONTRACT
        limit, not an account one: the same account kept trading normally after
        the slot was simply moved to another pair. Calling it a frequency limit
        would point the operator at the wrong lever.
        """
        if code == "2036":
            return (f"⚠️ <b>Ліміт ордерів на парі</b> · <b>SLOT{sid}</b> ({code})\n"
                    f"{symbol} · спробуй іншу пару на слоті · виходи працюють")
        return (f"⚠️ <b>Ліміт частоти MEXC</b> · <b>SLOT{sid}</b> ({code})\n"
                f"1 угода / {int(hold)}с · {mode / 3600:.0f} год · виходи працюють")

    def _note_slot_skip(self, slot, reason: str) -> None:
        """Count a skip against the slot it belongs to (None = shadow pass)."""
        key = (slot, reason)
        self._slot_skips[key] = self._slot_skips.get(key, 0) + 1

    def _heartbeat_keys(self, live_pairs) -> list:
        """(symbol, account_label) pairs the heartbeat should watch.

        One entry per live slot assigned to the pair, so a slot that stopped
        trading is noticed even while its sibling keeps the pair busy. Falls
        back to (symbol, None) when the slot map is unavailable — that is the
        original symbol-wide behaviour.
        """
        keys: list = []
        for symbol in live_pairs:
            slots = []
            if self.live_pool is not None:
                try:
                    slots = list(self.live_pool.find_slots_for_pair(symbol))
                except Exception:
                    slots = []
            if slots:
                keys.extend((symbol, f"slot{s}") for s in slots)
            else:
                keys.append((symbol, None))
        return keys

    def _entry_slots(self, symbol: str) -> list:
        """Slots that act on a signal for this pair, each one independently.

        [None] means no live slot is pinned — the shadow simulation path, and
        what every non-live pair keeps doing.
        """
        if self.live_pool is None or self.state_manager is None:
            return [None]
        try:
            if not self.state_manager.is_in_live(symbol):
                return [None]
            slots = list(self.live_pool.find_slots_for_pair(symbol))
        except Exception:
            return [None]
        return slots or [None]

    async def _enter_for_slot(self, signal: Signal, signal_id: int | None,
                              cfg: PairExecConfig, pin_slot) -> None:
        """The per-account half of on_signal: gates, then entry, for ONE slot."""
        symbol = signal.symbol
        # One per slot pass — the denominator every gate below is counted in.
        self.signals_fanned_out += 1
        # Gate key. Keyed by symbol alone, slot 1 holding a position or serving
        # a cooldown shut the pair for slot 2 as well.
        _gk = (pin_slot, symbol)
        _label = None if pin_slot is None else f"slot{pin_slot}"

        # Cooldown
        now_ms = int(time.time() * 1000)
        if now_ms < self._cooldown_until.get(_gk, 0) * 1000:
            self.signals_skipped_cooldown += 1
            self._note_slot_skip(pin_slot, "cooldown")
            return

        # Funding cutoff
        if self.funding_guard.is_too_close_for_entry():
            self.signals_skipped_funding += 1
            self._note_slot_skip(pin_slot, "funding")
            return

        # Max positions per symbol — for THIS account only. account_label is
        # set before the position joins _open_positions, so the count is exact.
        if sum(1 for p in self._open_positions[symbol]
               if (p.account_label or None) == _label) >= self.max_positions_per_symbol:
            self.signals_skipped_max_positions += 1
            self._note_slot_skip(pin_slot, "max_positions")
            return

        # Slot-lock single-flight guard:
        # Block duplicate submissions on the same (slot, symbol) while one is
        # in-flight. CRITICAL: must be checked AND added atomically (no await
        # between). Python's GIL guarantees this for single statements.
        if _gk in self._pending_submissions:
            self.signals_skipped_pending_submit += 1
            self._note_slot_skip(pin_slot, "pending_submit")
            logger.debug(
                "[SLOT_LOCK] %s slot=%s skip — submission already in-flight",
                symbol, pin_slot,
            )
            return
        self._pending_submissions.add(_gk)

        # ---- Attempt entry with EFFECTIVE config (per-detector strategy applied) ----
        try:
            await self._try_enter(signal, signal_id, cfg, pin_slot)
        finally:
            # ALWAYS release the lock — exceptions, early returns, success, all paths.
            self._pending_submissions.discard(_gk)

    async def _try_enter(self, signal: Signal, signal_id: int | None,
                         cfg: PairExecConfig, pin_slot=None) -> None:
        """
        Try IOC entry up to ioc_max_attempts. Open position if any attempt succeeds.

        Latency simulation:
          1. Sleep random(80, 300)ms to model real API roundtrip
          2. Re-fetch orderbook price after delay
          3. If price moved adversely > max_acceptable_drift_pct → expire (signal stale)
          4. Otherwise proceed with IOC entry at NEW price
        """
        symbol = signal.symbol
        mexc_ob = self.ob_manager.get("mexc", symbol)
        if mexc_ob is None or not mexc_ob.is_synced:
            self.signals_skipped_no_book += 1
            return

        # determine if this pair trades LIVE before any realism
        # gates. All realism blocks below (latency sim, should_reject_order,
        # simulate_server_error, signal_to_order_latency_ms sleep) are SHADOW-ONLY
        # simulation knobs designed to make backtests match real latency/error
        # rates. Applying them to live trades adds artificial delay (100-2000ms)
        # and random skips on top of REAL latency/errors that are already being
        # incurred at the LiveExecutor layer.
        _is_live_pair = False
        try:
            _is_live_pair = self.state_manager.is_in_live(symbol)
        except Exception:
            _is_live_pair = False

        # Freeze the SIGNAL-TIME IOC limit (the price the order carries at
        # submit) BEFORE the latency sleep. Live fixes the limit now and only
        # executes it against the book ~latency later — so any adverse move
        # past the limit EXPIRES the IOC. We evaluate this fixed limit against
        # the post-latency book in simulate_ioc_entry below, so shadow expires
        # exactly like live (incl. per-pair cross via cfg.ioc_offset_ticks).
        _sig_limit = None
        if not _is_live_pair:
            _ba = mexc_ob.best_ask()
            _bb = mexc_ob.best_bid()
            if _ba is not None and _bb is not None:
                try:
                    # tick-exact cross — parity with live place_ioc_open:
                    # limit = touch ± offset_ticks×tick in the scaled (binance-equiv)
                    # domain that mexc_ob prices live in.
                    _mxsym = to_mexc(symbol)
                    _tick_scaled = get_tick_size(_mxsym) * get_binance_scale(_mxsym)
                    if signal.direction == "long":
                        _sig_limit = float(_ba.price) + cfg.ioc_offset_ticks * _tick_scaled
                    else:
                        _sig_limit = float(_bb.price) - cfg.ioc_offset_ticks * _tick_scaled
                except (TypeError, ValueError, AttributeError):
                    _sig_limit = None

        # ---- Latency simulation ----
        # skip entirely for live pairs. The sleep + drift check were
        # adding 100-250ms artificial delay AND skipping signals when the
        # local OB snapshot moved during the sleep — counterproductive for live
        # where the static_gap detector signals on exactly that kind of OB
        # movement. Shadow keeps the realism block so backtests stay calibrated.
        if self._latency_enabled and not _is_live_pair:
            latency_ms = random.uniform(self._latency_min_ms, self._latency_max_ms)
            await asyncio.sleep(latency_ms / 1000)

            # Re-check orderbook is still synced
            if not mexc_ob.is_synced:
                self.signals_skipped_no_book += 1
                return

            # Check if price moved adversely during latency
            # For LONG: price going UP = bad (we'd buy higher than expected)
            # For SHORT: price going DOWN = bad
            current_mid = mexc_ob.mid_price()
            if current_mid and signal.mexc_price > 0:
                drift_pct = (current_mid - signal.mexc_price) / signal.mexc_price * 100
                if signal.direction == "long":
                    # adverse if mid went UP significantly
                    if drift_pct > self._max_acceptable_drift_pct:
                        self.signals_skipped_latency_drift += 1
                        logger.debug(
                            "[LATENCY DRIFT] %s LONG: price moved +%.4f%% during %.0fms → skip",
                            symbol, drift_pct, latency_ms,
                        )
                        return
                else:
                    # adverse if mid went DOWN significantly
                    if drift_pct < -self._max_acceptable_drift_pct:
                        self.signals_skipped_latency_drift += 1
                        logger.debug(
                            "[LATENCY DRIFT] %s SHORT: price moved %.4f%% during %.0fms → skip",
                            symbol, drift_pct, latency_ms,
                        )
                        return

        # Randomize position size per trade (within configured bounds)
        # Shadow uses its OWN fixed sizing (NOT cfg) so live-margin tuning does
        # not move shadow stats. Live pairs override this to live_margin below.
        chosen_margin = round(random.uniform(SHADOW_MARGIN_MIN_USDT, SHADOW_MARGIN_MAX_USDT), 2)
        chosen_leverage = random.randint(SHADOW_LEVERAGE_MIN, SHADOW_LEVERAGE_MAX)
        notional = chosen_margin * chosen_leverage

        self.entries_attempted += 1

        # === REALISM: order rejection check ===
        # Real exchange may reject order due to risk control, margin, etc.
        # skip for live pairs. Real MEXC rejections (margin, risk
        # control, api_error_6026) surface via LiveExecutor.place_ioc_open()'s
        # actual error_code/error_msg — no need to simulate a 1% random skip on top.
        if not _is_live_pair:
            rejected, reject_reason = should_reject_order(
                signal_price=signal.mexc_price,
                mark_price=signal.mexc_price,  # use mexc_price as proxy for mark
                profile=self.realism,
            )
            if rejected:
                self.entries_rejected += 1
                logger.debug(
                    "[REJECTED] %s %s reason=%s",
                    symbol, signal.direction, reject_reason,
                )
                return

        # === REALISM Level 6: server error / timeout simulation ===
        # Real MEXC has 1-3% transient errors (timeouts, 5xx). On error,
        # treat as failed entry — order may have been placed but no
        # confirmation received, typically the safest is to skip.
        # skip for live pairs. Real MEXC timeouts/5xx are handled
        # by LiveExecutor's asyncio.wait_for(submit, timeout=order_timeout_sec)
        # plus its retry loop. The simulation here adds a 2-second sleep AND
        # discards the live signal — punishing live trades for shadow-modeled
        # errors that already manifest naturally in the real submit path.
        if not _is_live_pair:
            errored, extra_latency_ms = simulate_server_error(self.realism)
            if errored:
                # Wait the extra latency penalty (simulating timeout/retry)
                await asyncio.sleep(extra_latency_ms / 1000)
                self.entries_rejected += 1
                logger.debug(
                    "[SERVER_ERROR] %s %s — entry treated as failed (+%dms)",
                    symbol, signal.direction, extra_latency_ms,
                )
                return

        # === REALISM: latency before order reaches MEXC ===
        # During this delay, the orderbook will move. We use the orderbook
        # snapshot AFTER waiting, not at signal time. This is the BIGGEST
        # source of shadow-vs-live divergence: catch-up moves can develop
        # further during the latency window.
        # Skip this artificial sleep for pairs in LIVE state.
        # In live mode, real network latency is measured by stage 12 metrics
        # (signal_to_pickup_ms, submit_latency_ms, fill_poll_latency_ms).
        # Adding 350ms artificial sleep on top of real latency was killing
        # entries — by the time we hit MEXC, the gap had already closed.
        # Shadow pairs still get realism sleep so simulator stays calibrated.
        if self.realism.signal_to_order_latency_ms > 0 and not _is_live_pair:
            await asyncio.sleep(self.realism.signal_to_order_latency_ms / 1000)
            # Re-fetch the orderbook after latency — this is what MEXC sees
            mexc_ob = self.ob_manager.get("mexc", symbol)
            if mexc_ob is None or not mexc_ob.is_synced:
                self.entries_rejected += 1
                logger.debug("[REJECTED] %s orderbook stale after latency", symbol)
                return

        last_result = None
        for attempt in range(cfg.ioc_max_attempts):
            # LIVE FAST-PATH:
            # simulate_ioc_entry walks the MEXC book to estimate avg_fill_price
            # + filled_qty for SHADOW analytics. For LIVE pairs the real fill
            # comes back from MEXC and _open_position overwrites entry_price
            # with the real value (see "use REAL filled notional from MEXC"
            # block below). The shadow walk was costing 5-15ms per signal
            # before MEXC submit even started. Build a minimal stub instead
            # so _open_position has what it needs to dispatch the live order.
            if _is_live_pair:
                best_ask = mexc_ob.best_ask()
                best_bid = mexc_ob.best_bid()
                if best_ask is None or best_bid is None:
                    self.entries_expired += 1
                    return
                # Defensive: validate price is a real positive number.
                # Guards against bad orderbook state AND tests that mock
                # best_ask without setting a numeric .price attribute.
                try:
                    ask_p = float(best_ask.price)
                    bid_p = float(best_bid.price)
                except (TypeError, ValueError, AttributeError):
                    self.entries_expired += 1
                    return
                if ask_p <= 0 or bid_p <= 0:
                    self.entries_expired += 1
                    return
                if signal.direction == "long":
                    target_price = ask_p   # stub only — real fill (incl. any tick cross) comes from live result below
                else:
                    target_price = bid_p   # stub only
                # Optimistic stub: pretend fully filled at target. _open_position
                # will overwrite entry_price/filled_qty/notional from live result.
                result = IOCAttemptResult(
                    status="filled",
                    target_price=target_price,
                    avg_fill_price=target_price,
                    filled_qty=notional / target_price if target_price > 0 else 0.0,
                    filled_notional_usdt=notional,
                    filled_pct=1.0,
                )
            else:
                result = self.ioc_executor.simulate_ioc_entry(
                    mexc_ob=mexc_ob,           # POST-latency book
                    direction=signal.direction,
                    notional_usdt=notional,
                    limit_price=_sig_limit,    # FIXED at signal time → expires if price moved
                    # OrderBook size is in CONTRACTS; convert level volume to USDT
                    # with the same contractSize the live path uses, else shadow
                    # mis-sizes fills (10x small for PENGU, 100x big for ZEC/BCH).
                    contract_size=CONTRACT_SIZES.get(to_mexc(symbol), 1.0),
                )
            last_result = result

            if result.status in ("filled", "partial"):
                # Open position with the randomized size
                await self._open_position(signal, signal_id, cfg, result,
                                          chosen_margin=chosen_margin,
                                          chosen_leverage=chosen_leverage,
                                          pin_slot=pin_slot)
                if result.status == "filled":
                    self.entries_filled += 1
                else:
                    self.entries_partial += 1
                return

            # Expired — wait briefly and retry (variable latency on retries)
            if attempt < cfg.ioc_max_attempts - 1:
                # Use realistic retry latency variance
                retry_ms = random.randint(
                    max(self.realism.ioc_retry_min_ms, cfg.ioc_attempt_interval_ms),
                    max(self.realism.ioc_retry_max_ms, cfg.ioc_attempt_interval_ms),
                )
                await asyncio.sleep(retry_ms / 1000)
                # Re-fetch orderbook for next attempt
                mexc_ob = self.ob_manager.get("mexc", symbol)
                if mexc_ob is None or not mexc_ob.is_synced:
                    break

        # All attempts expired
        self.entries_expired += 1
        # Per-pair shadow IOC-expiry log (shadow_trades stores only fills) so the
        # Trades Today view can show shadow expired%. Shadow pairs only; live
        # pairs record via _record_live_miss into live_open_misses. Best-effort.
        if not _is_live_pair:
            try:
                await self.db.execute(
                    "INSERT INTO shadow_open_misses (ts, symbol, reason) VALUES (?, ?, ?)",
                    (int(time.time()), symbol, "ioc_expired_no_fill"),
                )
            except Exception:
                pass
        if last_result:
            logger.debug(
                "[IOC EXPIRED] %s %s after %d attempts (%s)",
                symbol, signal.direction, cfg.ioc_max_attempts, last_result.expired_reason,
            )

    async def _open_position(
        self,
        signal: Signal,
        signal_id: int | None,
        cfg: PairExecConfig,
        ioc_result,
        chosen_margin: float = 25.0,
        chosen_leverage: int = 50,
        pin_slot=None,
    ):
        """Create ShadowPosition + start watcher task.

        Returns the ShadowPosition on success, None when nothing was opened.
        """
        # latency tracking: signal_to_pickup_ms.
        # signal.created_at_ms = when the detector emitted the signal.
        # now_ms = when we (the engine) picked it up for execution.
        # Captures: signal queue delay + DB write delay (signals table).
        signal_to_pickup_ms = max(0, int(time.time() * 1000) - signal.created_at_ms)

        pos = ShadowPosition(
            symbol=signal.symbol,
            direction=signal.direction,
            detector_source=signal.source,
            confidence=signal.confidence,
            gap_ticks=float((signal.metadata or {}).get("gap_ticks", 0.0)),
            signal_id=signal_id,
            signal_uid=signal.created_at_ms,  # stable per-signal join key (no FK)
            leverage=chosen_leverage,
            margin_usdt=chosen_margin,
            notional_usdt=ioc_result.filled_notional_usdt,
            qty=ioc_result.filled_qty,
            entry_target_price=ioc_result.target_price,
            entry_price=ioc_result.avg_fill_price,
            entry_status=ioc_result.status,
            entry_filled_pct=ioc_result.filled_pct,
            entry_fees_usdt=0.0,  # IOC limit on MEXC = 0 fee
            binance_price_at_entry=signal.binance_price,
            mexc_price_at_entry=signal.mexc_price,
            mexc_lag_at_entry_pct=signal.mexc_lag_pct,
        )
        pos.signal_to_pickup_ms = signal_to_pickup_ms

        # Capture MEXC book spread at entry (for adverse-stop / spread-gate
        # analysis later). Best-effort; never blocks the open.
        try:
            _eob = self.ob_manager.get("mexc", signal.symbol)
            if _eob is not None:
                pos.entry_spread_bps = _entry_spread_bps(
                    _eob.best_bid_price(), _eob.best_ask_price()
                )
        except Exception:
            pass

        # Compute slippage of fill vs target (zero if exactly at limit)
        if pos.entry_target_price > 0:
            if signal.direction == "long":
                # Long: lower fill is better (we paid less)
                pos.entry_slippage_pct = (pos.entry_price - pos.entry_target_price) / pos.entry_target_price * 100
            else:
                pos.entry_slippage_pct = (pos.entry_target_price - pos.entry_price) / pos.entry_target_price * 100

        # Cache static per-pair values once.
        # These never change over a position's lifetime, but the 20ms watcher
        # used to recompute them on every tick (tick math + loader.get() +
        # try/except). Cached here in the slow path (called once per open)
        # so the hot path can read them as plain attributes.
        try:
            _mexc_symbol = to_mexc(signal.symbol)
            _tick = get_tick_size(_mexc_symbol)
            _scale = get_binance_scale(_mexc_symbol)
            pos.tick_scaled = _tick * _scale if _scale > 0 else _tick
        except Exception:
            pos.tick_scaled = 0.0  # watcher gracefully falls back
        if self._config_loader is not None:
            try:
                _es = self._config_loader.get(signal.symbol).exit_strategy
                pos.exit_strategy_cached = _es
                pos.binance_reversal_max_ms_cached = _es.binance_reversal_max_ms
            except Exception:
                # Loader misconfigured for this pair — leave cache empty;
                # the watcher's getattr-with-default paths still work.
                pos.exit_strategy_cached = None
                pos.binance_reversal_max_ms_cached = 0

        # === LIVE TRADING BRANCH (multi-slot mode) ===
        # Find a slot that is configured to trade this pair, then
        # route the order through its dedicated LiveExecutor + SafetyController.
        # Shadow record is ALWAYS kept for comparison even if live succeeds.
        # Margin/leverage for LIVE are randomized per-slot from SLOT ranges (stealth sizing).
        chosen_slot_id: int | None = None
        # Why no slot was used (all skipped via continue) — surfaced in the
        # failure alert so it shows the real reason instead of "Raw: ?".
        _skip_reason: str | None = None
        # Which slot the attempt belonged to — needed by the failure alert:
        # MEXC limits accounts individually, so "LIVE FAILED" without a slot
        # is unactionable when more than one slot is live.
        _attempt_sid: int | None = None
        # The slot the recorded _skip_reason belongs to. _attempt_sid alone
        # is the LAST slot tried, so an alert could name a different slot
        # than the reason it prints.
        _skip_sid: int | None = None
        _pair_is_live = self.state_manager is not None and self.state_manager.is_in_live(signal.symbol)
        if self.live_pool is not None and _pair_is_live:
            # One pass = one account. The caller already fanned out over
            # every live slot, so cascading here would double-submit.
            slot_ids = ([pin_slot] if pin_slot is not None
                        else self.live_pool.find_slots_for_pair(signal.symbol))
            if not slot_ids:
                _skip_reason = "no_slot_config"
            for sid in slot_ids:
                _attempt_sid = sid
                executor = self.live_pool.get_executor(sid)
                safety = self.live_pool.get_safety(sid)
                if executor is None or safety is None:
                    continue

                # Skip slot if rate-limited after a recent 510
                if time.monotonic() < self._slot_cooldown_until.get(sid, 0.0):
                    # `or` — a pacing skip on a later slot must not erase an
                    # earlier safety_blocked reason (that one silences the
                    # kill-switch alert, which must stay visible).
                    if _skip_reason is None:
                        _skip_reason, _skip_sid = "slot_open_cooldown", sid
                    continue

                # Validate the slot↔pair has a config row (admission control).
                slot_cfg = await self.live_pool.get_slot_config(sid, symbol=signal.symbol)
                if slot_cfg is not None:
                    # A replaced webkey is a DIFFERENT MEXC account, and the
                    # open-rate limit is enforced per account. Dropping the latch
                    # here is what stops a fresh key inheriting the old one's
                    # six-hour throttle (measured: 5 requests/hour against an
                    # unthrottled slot's 104, because a spent request costs ~72s
                    # even when the IOC never fills).
                    _wk = slot_cfg.get("webkey_refreshed_at")
                    _ot_db = slot_cfg.get("open_throttle_until") or 0
                    # Two independent ways to learn the latch should go:
                    #   1. the webkey stamp moved (delete -> NULL, add -> now);
                    #   2. a deadline we persisted is no longer stored, which
                    #      only happens because delete() wiped it.
                    # (2) exists because (1) is a whole-second stamp: deleting
                    # and re-adding inside one second leaves it unchanged.
                    # (2) requires _open_rl_persisted to be set, so a failed DB
                    # write leaves the latch standing instead of releasing a
                    # limited account — a 10014 costs 30 days of opens.
                    _cleared_in_db = (self._open_rl_persisted.get(sid) is not None
                                      and _ot_db <= time.time())
                    # Stateless check, and the one that actually holds across a
                    # restart: the latch runs OPEN_THROTTLE_MODE_SEC from the
                    # refusal, so it was earned at (_ot_db - mode). A key
                    # refreshed AFTER that moment sits on a different account and
                    # did not earn it. Needed because the seen-map above only
                    # reacts to a CHANGE it witnessed, and a slot that was
                    # disabled and came back after a restart is seen for the
                    # FIRST time — which by design clears nothing. Measured:
                    # latch 09:17:39, key replaced 10:38:39, 4.6h inherited.
                    _mode_sec = self._env_float("OPEN_THROTTLE_MODE_SEC", 21600.0)
                    _key_is_newer = (_wk is not None and _ot_db > 0
                                     and _wk > _ot_db - _mode_sec)
                    # `_wk is not None` — release only once a REPLACEMENT key is
                    # in place. Deleting a key does not lift MEXC's limit (it is
                    # on the account), and the pool keeps the old credentials
                    # cached for seconds afterwards, so releasing on the delete
                    # half of a swap just re-probes a still-limited account and
                    # earns a fresh 6h latch — measured 12:17:38 -> 12:17:39 on
                    # 2026-07-29. With no key the slot must not trade anyway, so
                    # holding the latch costs nothing.
                    _released = False
                    if _wk is not None and (self._open_rl_wk_seen.get(sid, _wk) != _wk
                                            or _cleared_in_db or _key_is_newer):
                        _released = True
                        if time.monotonic() < self._open_rl_mode_until.get(sid, 0.0):
                            logger.info(
                                "[OPEN THROTTLE] slot=%d new webkey in place — "
                                "latch dropped with the old account", sid)
                        self._open_rl_mode_until.pop(sid, None)
                        self._open_rl_code.pop(sid, None)
                        self._open_rl_persisted.pop(sid, None)
                        self._slot_cooldown_until.pop(sid, None)
                        # Clear the STORED deadline too. Dropping only the
                        # in-memory copy let the next restart restore the very
                        # latch we just decided was not ours.
                        if _ot_db > 0:
                            try:
                                await self.live_pool.webkey_store\
                                    .set_open_throttle_until(sid, None)
                            except Exception:
                                logger.exception(
                                    "[OPEN THROTTLE] could not clear stored "
                                    "latch slot=%d", sid)
                    self._open_rl_wk_seen[sid] = _wk

                    # Restore a throttled latch that outlived the process. Stored
                    # as an epoch, used as monotonic — convert, never compare
                    # the two clocks directly.
                    # `not _released` — slot_cfg was read BEFORE the release
                    # wrote NULL, so without this the stale deadline re-arms the
                    # latch we just dropped (seen 2026-07-29 13:54:28).
                    _ot = 0 if _released else (slot_cfg.get("open_throttle_until") or 0)
                    _now_w = time.time()
                    if (_ot > _now_w
                            and time.monotonic() >= self._open_rl_mode_until.get(sid, 0.0)):
                        self._open_rl_mode_until[sid] = time.monotonic() + (_ot - _now_w)
                        logger.info(
                            "[OPEN THROTTLE] slot=%d restored from DB — %.0f min left",
                            sid, (_ot - _now_w) / 60)
                if slot_cfg is None:
                    if _skip_reason is None:
                        _skip_reason, _skip_sid = "no_slot_config", sid
                    continue

                # Sizing (margin/leverage) comes from the SAME yaml source as
                # shadow (cfg = PairExecConfig from ConfigLoader) — single source
                # of truth. Randomized per trade within the configured range.
                # Per-slot sizing OVERRIDE (slot_cfg) wins over the pair YAML —
                # lets two accounts on the same pair use different margin/leverage.
                # None fields fall through to cfg (exact prior behaviour).
                _s_mmin = slot_cfg.get("slot_margin_min_usdt")
                _s_mmax = slot_cfg.get("slot_margin_max_usdt")
                _s_lmin = slot_cfg.get("slot_leverage_min")
                _s_lmax = slot_cfg.get("slot_leverage_max")
                # sorted() so an inverted override (min>max) reaching the DB via a
                # raw edit degrades gracefully instead of random.randint raising
                # ValueError and silently dropping the open. The Telegram wizard
                # already validates min<=max; this is defence in depth.
                _m_lo, _m_hi = sorted((
                    _s_mmin if _s_mmin is not None else cfg.margin_min_usdt,
                    _s_mmax if _s_mmax is not None else cfg.margin_max_usdt,
                ))
                _l_lo, _l_hi = sorted((
                    _s_lmin if _s_lmin is not None else cfg.leverage_min,
                    _s_lmax if _s_lmax is not None else cfg.leverage_max,
                ))
                live_margin = round(random.uniform(_m_lo, _m_hi), 2)
                live_leverage = random.randint(_l_lo, _l_hi)
                live_notional = live_margin * live_leverage

                allowed, reason = safety.can_open_live(
                    symbol=signal.symbol,
                    margin_usdt=live_margin,
                )
                if not allowed:
                    if _skip_reason is None:
                        _skip_reason, _skip_sid = f"safety_blocked: {reason}", sid
                    logger.debug(
                        "[LIVE SLOT %d] not available for %s: %s",
                        sid, signal.symbol, reason,
                    )
                    continue
                # NOTE: chosen_slot_id is set only AFTER the lock is taken —
                # setting it here made the "no slot was even attempted"
                # repair below unreachable for a busy slot, so the operator
                # got "LIVE FAILED — Unknown error / Raw: ?".
                # Convert symbol format: ZECUSDT → ZEC_USDT
                mexc_symbol = to_mexc(signal.symbol)
                _mexc_ob_for_open = self.ob_manager.get("mexc", signal.symbol)
                # IOC LIMIT entry — preserves 0% maker fee economics.
                # If all attempts expire, treat as skipped signal (no market fallback).
                # Profiling: pass t_signal_created for end-to-end latency measurement.
                t_sig = signal.metadata.get("t_signal_created", 0.0) if signal.metadata else 0.0
                # Serialize slot access: if another pair is already submitting
                # on this slot, skip — don't queue stale signals.
                _slot_lock = self.live_pool.get_slot_lock(sid)
                if _slot_lock.locked():
                    logger.debug("[SLOT BUSY] slot=%d busy, skipping %s", sid, signal.symbol)
                    if _skip_reason is None:
                        _skip_reason, _skip_sid = "slot_busy", sid
                    continue
                chosen_slot_id = sid
                async with _slot_lock:
                    live_result = await executor.place_ioc_open(
                        symbol=mexc_symbol,
                        direction=signal.direction,
                        notional_usdt=live_notional,
                        leverage=live_leverage,
                        mexc_ob=_mexc_ob_for_open,
                        offset_ticks=cfg.ioc_offset_ticks,  # 0=at-touch, N>0=cross N ticks (per-pair)
                        # Throttled: force a SINGLE request per pass. Retries
                        # spend extra /order/create calls inside one gate pass,
                        # which is exactly the quota we are trying to ration —
                        # and a 10014 on the last attempt masks the hold for the
                        # earlier one that did reach MEXC.
                        max_attempts=(
                            1 if time.monotonic() < self._open_rl_mode_until.get(sid, 0.0)
                            else cfg.ioc_max_attempts),
                        retry_delay_ms=cfg.ioc_attempt_interval_ms,  # per-pair (was global env IOC_RETRY_DELAY_MS)
                        t_signal_created=t_sig,
                    )
                if live_result.success:
                    # MEASUREMENT: gap since this slot's previous accepted open.
                    # This is the number that reveals the imposed ceiling — read
                    # it straight from the log, no self-imposed pacing involved.
                    _prev = self._last_open_ts.get(sid)
                    _nowt = time.monotonic()
                    self._last_open_ts[sid] = _nowt
                    logger.info(
                        "[OPEN RATE] slot=%d accepted, gap_since_prev=%s",
                        sid, f"{_nowt - _prev:.1f}s" if _prev else "first")
                    # Throttled mode: the MEXC window runs from the ACCEPTED open,
                    # so pace from here. Slight upward jitter only — going under
                    # the ceiling would just earn a rejection.
                    if _nowt < self._open_rl_mode_until.get(sid, 0.0):
                        _h = self._env_float("OPEN_THROTTLE_HOLD_SEC", 65.0)
                        self._arm_open_hold(sid, self._humanize(_h, 1.0, 1.15),
                                            "throttled: 1 open per window")
                    pos.mode = "live"
                    pos.live_order_id = live_result.order_id
                    pos.live_open_latency_ms = live_result.latency_ms
                    # breakdown for diagnostics
                    pos.live_open_submit_ms = live_result.submit_latency_ms
                    pos.live_open_response_ms = live_result.response_latency_ms
                    pos.live_open_fill_poll_ms = live_result.fill_poll_latency_ms
                    pos.account_label = f"slot{sid}"
                    # Update position with LIVE values (not shadow's)
                    pos.margin_usdt = live_margin
                    pos.leverage = live_leverage
                    # use REAL filled notional from MEXC if available.
                    # IOC LIMIT can partial-fill if liquidity at limit price is shallow.
                    # Falls back to planned notional only if MEXC didn't return fill data.
                    if live_result.notional_usdt > 0:
                        pos.notional_usdt = live_result.notional_usdt
                    else:
                        pos.notional_usdt = live_notional
                    # Record the ACTUAL fill fraction/status. The optimistic stub
                    # set filled_pct=1.0/status="filled"; a partial IOC fill must
                    # NOT be persisted as a full fill — that poisons the fill-rate
                    # / slippage analytics used to pick live tokens.
                    if live_notional > 0:
                        pos.entry_filled_pct = min(1.0, pos.notional_usdt / live_notional)
                        pos.entry_status = (
                            "partial" if pos.notional_usdt < live_notional * 0.99
                            else "filled"
                        )
                    # use REAL fill price for accurate PnL (matches MEXC)
                    if live_result.fill_price > 0:
                        pos.entry_price = live_result.fill_price
                        # Recalculate qty based on real fill notional (not planned)
                        if pos.entry_price > 0:
                            pos.qty = pos.notional_usdt / pos.entry_price
                    safety.record_open(signal.symbol)
                    # Log both planned and actual when there's a partial fill
                    if pos.notional_usdt < live_notional * 0.99:
                        fill_pct = 100 * pos.notional_usdt / live_notional
                        logger.info(
                            "[LIVE OPEN OK] slot=%d %s %s margin=$%.2f lev=%dx "
                            "target=$%.0f filled=$%.0f (%.0f%% partial) "
                            "orderId=%s latency=%dms",
                            sid, signal.symbol, signal.direction,
                            live_margin, live_leverage, live_notional,
                            pos.notional_usdt, fill_pct,
                            live_result.order_id, live_result.latency_ms,
                        )
                    else:
                        logger.info(
                            "[LIVE OPEN OK] slot=%d %s %s margin=$%.2f lev=%dx "
                            "notional=$%.0f orderId=%s latency=%dms",
                            sid, signal.symbol, signal.direction,
                            live_margin, live_leverage, pos.notional_usdt,
                            live_result.order_id, live_result.latency_ms,
                        )
                    # explicit latency breakdown log
                    logger.info(
                        "[LATENCY] %s entry: total=%dms (pickup=%d submit=%d "
                        "response=%d fill_poll=%d)",
                        pos.symbol,
                        pos.signal_to_pickup_ms + live_result.latency_ms,
                        pos.signal_to_pickup_ms,
                        live_result.submit_latency_ms,
                        live_result.response_latency_ms,
                        live_result.fill_poll_latency_ms,
                    )
                else:
                    pos.live_open_error = live_result.error_msg
                    logger.warning(
                        "[LIVE OPEN FAIL] slot=%d %s %s: %s — keeping as shadow",
                        sid, signal.symbol, signal.direction, live_result.error_msg,
                    )
                    # Persist the miss so fill-rate is queryable from the DB,
                    # not just grep-able from logs. live_trades holds only
                    # opens that filled; without this the DB is blind to
                    # ioc_expired_no_fill / rejects (the "де не встиг").
                    # Controlled probe (2026-07-25, 3 tiny unfillable IOCs): request
                    # #1 was refused by VALIDATION (code 2003, no order created, no
                    # position) and request #2 three seconds later still came back
                    # 10014 — while #4, sent 70s later, passed again. So the quota is
                    # consumed by the REQUEST to /order/create itself, not by a fill
                    # and not even by an order existing. Hold after ANY outcome
                    # except a 10014 refusal (those are blocked before counting).
                    _err = live_result.error_msg or ""
                    # Failures that never reached MEXC spend no quota, so they must
                    # not trigger any pacing.
                    _local_only = any(x in _err for x in (
                        "empty_orderbook", "no_bbo", "orderbook_not_synced",
                        "vol calc", "fee_guard", "not in pool", "invalid direction"))
                    _spent_quota = bool(_err) and "10014" not in _err and not _local_only
                    if _spent_quota:
                        if time.monotonic() < self._open_rl_mode_until.get(sid, 0.0):
                            _hc = self._env_float("OPEN_THROTTLE_HOLD_SEC", 65.0)
                            self._arm_open_hold(sid, self._humanize(_hc, 1.0, 1.15),
                                                "throttled: request spent")
                    # Recorded AFTER the hold is armed: this await used to sit
                    # between the spent request and the hold, leaving a window for a
                    # second request inside the same MEXC window.
                    await self._record_live_miss(
                        signal, sid, live_result.error_msg, cfg,
                    )
                    if live_result.error_msg and "api_error_510" in live_result.error_msg:
                        # Through _arm_open_hold: writing the deadline directly
                        # SHORTENED the 65s hold taken a few lines above to 15s.
                        self._arm_open_hold(sid, 15.0, "510 rate-limit")
                    elif open_freq_limit_code(_err):
                        # The first open-rate refusal latches this slot into
                        # throttled mode; from then on it holds after every
                        # accepted open instead of firing hundreds of doomed
                        # requests between fills.
                        _lim_code = open_freq_limit_code(_err)
                        _now = time.monotonic()
                        # Probe interval. The hold below only arms after a request
                        # that MEXC actually processed — so while every request is
                        # refused there was nothing to hold from and the bot fired
                        # ~790 rejects/hour. A 10014 does NOT consume quota (proven
                        # by the probe), so we lose nothing by retrying every ~20s
                        # instead of continuously: the window is still caught within
                        # 20s of reopening.
                        _probe = self._env_float("OPEN_THROTTLE_PROBE_SEC", 20.0)
                        self._arm_open_hold(sid, self._humanize(_probe, 0.8, 1.3),
                                            "throttled: probe interval")
                        _mode = self._env_float("OPEN_THROTTLE_MODE_SEC", 21600.0)
                        _was_on = _now < self._open_rl_mode_until.get(sid, 0.0)
                        self._open_rl_mode_until[sid] = _now + _mode
                        # A different limit class means a different problem and a
                        # different remedy (pace vs pair), so it alerts again even
                        # while the latch is already held.
                        _new_class = self._open_rl_code.get(sid) != _lim_code
                        self._open_rl_code[sid] = _lim_code
                        _deadline_epoch = int(time.time() + _mode)
                        # Refresh the stored deadline as the window keeps extending.
                        # Persisting only on the FIRST refusal let the DB copy expire
                        # under a still-active latch, so a restart resumed at full
                        # speed into a limited account.
                        if (_deadline_epoch
                                - self._open_rl_persisted.get(sid, 0) > 300):
                            try:
                                await self.live_pool.webkey_store.set_open_throttle_until(
                                    sid, _deadline_epoch)
                                self._open_rl_persisted[sid] = _deadline_epoch
                            except Exception:
                                logger.exception(
                                    "[OPEN THROTTLE] could not persist latch slot=%d", sid)
                        if not _was_on or _new_class:
                            _hold = self._env_float("OPEN_THROTTLE_HOLD_SEC", 65.0)
                            logger.warning(
                                "[OPEN THROTTLE] slot=%d limited by MEXC — holding "
                                "%.0fs after each open for the next %.0fh",
                                sid, _hold, _mode / 3600)
                            if self.alerts is not None:
                                try:
                                    await self.alerts.send(
                                        self._al_text(sid, _lim_code, _hold,
                                                      _mode, signal.symbol),
                                        category=f"open_throttle_{_lim_code}:{sid}",
                                        throttle_sec=1800,
                                    )
                                except Exception:
                                    logger.exception("Failed to send 10014 alert")
                break  # one slot tried — don't cascade through others

        # skip shadow for live pair when live execution failed.
        # If pair is in live state machine but pos.mode=="shadow" (live API failed
        # or no slot available), don't fall back to shadow simulation — return
        # without opening shadow position. User wanted live; we tried and failed.
        # This avoids:
        #   1. Double TG alerts that look like live trades
        #   2. Shadow stats polluted by live failures
        #   3. Confusion in PnL tracking
        # No slot was even attempted (all skipped: cooldown / no config /
        # safety) → record WHY so the alert reflects the real reason instead
        # of a bare "Raw: ?". The dominant case is an active 510 slot-cooldown.
        if chosen_slot_id is None and pos.live_open_error is None and _skip_reason is not None:
            pos.live_open_error = _skip_reason
            # Attribute it: these skips (slot cooldown, throttle hold, soft
            # start break, safety block, busy slot) left no trace anywhere,
            # so a silent slot looked identical to an idle market.
            self._note_slot_skip(_skip_sid, _skip_reason.split(':')[0])

        try:
            ps = self.state_manager.get_state(signal.symbol)
            pair_in_live_state = (ps is not None and ps.state == "live")
        except Exception:
            pair_in_live_state = False

        if pair_in_live_state and pos.mode != "live":
            logger.warning(
                "[SKIP SHADOW] %s in live state but live execution failed — "
                "skipping shadow simulation to avoid confusion",
                signal.symbol,
            )
            # Classify the failure so the alert is actionable instead of
            # a generic "check webkey? balance? MEXC API?" guess list.
            # Each category gets its own emoji, message, and throttle policy.
            err_msg = (pos.live_open_error or "").lower()
            sym = signal.symbol
            direction = signal.direction.upper()

            # ─── Classification table ────────────────────────────────
            # Each entry: (matcher, kind, emoji, title, hint, throttle_sec, auto_pause)
            # Matched in order — first hit wins. Add new ones to the top of
            # the list so they take precedence over the catch-all.
            # Benign cases (gap closed during IOC) come first and short-circuit
            # WITHOUT alerting at all — they happen routinely on fast markets
            # and would drown real alerts.
            _BENIGN = (
                "ioc_expired_no_fill",
                "ioc_all_expired",
                "gtc_cancelled_no_fill",
                "empty_orderbook",
                "no_bbo",
                "orderbook_not_synced",
                "no_slot_config",       # pair in live state but slot not yet assigned
                # 10014 already sends its own rate-limit alert (see _al_text)
                # from the throttle handler (with the slot, the hold and the
                # repeat count) — the generic "Unknown error" copy of the very
                # same event is pure duplication.
                "api_error_10014",
                "api_error_9082",
                "api_error_2036",
                "number of orders has exceeded",   # err_msg is lowercased above
                "position-opening frequency",
                # Our OWN deliberate pacing, not an exchange failure: soft-start
                # warm-up and the 10014/510 cooldowns skip the signal on purpose.
                # The underlying error alerts once when it happens; every later
                # paced skip must stay silent or it spams every few seconds.
                "slot_open_cooldown",
                "slot_cooldown",
                "slot_busy",              # another pair is submitting on this slot

            )
            # No reason recorded at all (no live_pool / no executor / slot
            # skipped before anything was tried) — nothing actionable to say,
            # and it used to render as "Unknown error / Raw: ?" every 300s.
            if not err_msg:
                return
            if any(b in err_msg for b in _BENIGN):
                # Silent — bot's normal logs/metrics still capture them.
                return

            # Default classification (matches anything that wasn't benign)
            kind = "unknown"
            emoji = "⚠️"
            title = "LIVE FAILED — Unknown error"
            hint = f"Raw: <code>{(pos.live_open_error or '?')[:120]}</code>"
            throttle = 300
            auto_pause = False

            # api_error_6026 — MEXC face verification / risk control
            if "api_error_6026" in err_msg:
                kind = "risk_control"
                emoji = "🛑"
                title = "MEXC РИЗИК-КОНТРОЛЬ"
                hint = (
                    "MEXC вимагає face verification або інші перевірки.\n"
                    "Залогінься на MEXC web → пройди перевірку.\n"
                    "Пара авто-поставлена на pause."
                )
                throttle = 3600
                auto_pause = True


            # Insufficient balance / margin
            elif any(s in err_msg for s in (
                "insufficient", "api_error_6017",
                "api_error_3003", "api_error_3005",
            )):
                kind = "insufficient_balance"
                emoji = "💸"
                title = "Недостатньо коштів"
                hint = (
                    "Не вистачає USDT на slot для відкриття позиції.\n"
                    "Поповни баланс на MEXC або зменш margin "
                    f"(<code>/menu</code> → 💰 Sizing → <code>{sym}</code>)."
                )
                throttle = 600

            # Webkey expired / auth issues
            elif any(s in err_msg for s in (
                "401", "403", "webkey", "unauthor",
                "api_error_1002", "invalid token",
                "session expired",
            )):
                kind = "webkey_invalid"
                emoji = "🔑"
                title = "Webkey не валідний"
                hint = (
                    "Webkey expired або відкликаний MEXC.\n"
                    "Відкрий <code>/menu</code> → 🔑 Webkey → "
                    "вибери slot → /webkey_setup → встав свіжий webkey."
                )
                throttle = 1800  # 30 min — once user knows, they know

            # Symbol/contract not available
            elif any(s in err_msg for s in (
                "api_error_3008", "api_error_30000",
                "symbol not", "contract not",
            )):
                kind = "symbol_unavailable"
                emoji = "🚫"
                title = "Пара не доступна на MEXC"
                hint = (
                    f"Контракт {sym} призупинено або делістили на MEXC.\n"
                    f"Розглянь переключити slot на іншу пару "
                    "(<code>/menu</code> → 🔑 Webkey → slot → Pair)."
                )
                throttle = 3600

            # Rate limit — MEXC code 510 ("Requests are too frequent"),
            # generic 429 / "too many requests", and our own 510 slot-cooldown
            # skips. This is account/IP-wide, NOT a leverage problem.
            elif any(s in err_msg for s in (
                "rate limit", "rate_limit", "429", "too many requests",
                "api_error_429", "api_error_510", "too frequent",
            )):
                kind = "rate_limit"
                emoji = "🚦"
                title = "MEXC rate-limit"
                hint = ""  # title + pair say enough; 510 is self-healing
                throttle = 600

            # Network / timeout
            elif any(s in err_msg for s in (
                "timeout", "connection", "network",
                "name resolution", "ssl",
            )):
                kind = "network"
                emoji = "📡"
                title = "Network / MEXC unreachable"
                hint = (
                    "Bot не може дістатися MEXC API.\n"
                    "Перевір з'єднання сервера, або зачекай — "
                    "MEXC може бути overloaded."
                )
                throttle = 600

            # Exception in bot's own code (very rare, indicates a bug)
            elif "exception" in err_msg:
                kind = "internal_exception"
                emoji = "💥"
                title = "Внутрішня помилка боту"
                hint = (
                    "Bot спіймав exception при відправці ордеру.\n"
                    "Дивись логи: <code>docker compose logs --tail=200 stakan-bot</code>"
                )
                throttle = 300

            # Auto-pause logic — applied BEFORE alert, so message reflects state
            if auto_pause:
                try:
                    await self.state_manager.manual_pause(
                        sym,
                        duration_sec=None,  # persistent
                        reason=f"auto:{kind}",
                    )
                    logger.warning(
                        "[AUTO-PAUSE] %s paused (reason=%s)", sym, kind,
                    )
                except Exception:
                    logger.exception("[AUTO-PAUSE] Failed for %s", sym)

            # Send the classified alert
            if self.alerts is not None:
                try:
                    _slot_id = (
                        chosen_slot_id if chosen_slot_id is not None
                        else (_skip_sid if _skip_sid is not None else _attempt_sid)
                    )
                    _slot_txt = f" · <b>SLOT{_slot_id}</b>" if _slot_id is not None else ""
                    alert_text = (
                        f"{emoji} <b>{title}</b>{_slot_txt}\n"
                        f"Pair: <code>{sym}</code> · {direction}"
                    )
                    if hint:
                        alert_text += f"\n\n{hint}"
                    await self.alerts.send(
                        alert_text,
                        category=f"live_fail_{kind}:{sym}:{_slot_id}",
                        throttle_sec=throttle,
                    )
                except Exception:
                    logger.exception("Failed to send live_fail alert")
            return

        self._open_positions[signal.symbol].append(pos)

        logger.info(
            "[OPEN] %s %s @ %.6f (target %.6f, slip %+.4f%%) qty=%.4f notional=$%.0f conf=%.2f mode=%s",
            pos.symbol, pos.direction.upper(),
            pos.entry_price, pos.entry_target_price, pos.entry_slippage_pct,
            pos.qty, pos.notional_usdt, pos.confidence, pos.mode,
        )

        # Event the OB listener sets on each book update; the watch loop awaits
        # it (event-driven exits) instead of a fixed 20ms poll. Created before
        # the task/listener so both observe the same instance.
        pos._book_event = asyncio.Event()  # type: ignore[attr-defined]

        # Start watcher
        task = asyncio.create_task(
            self._watch_position(pos, cfg),
            name=f"watch_{signal.symbol}_{int(time.time()*1000)}",
        )
        self._watcher_tasks[id(pos)] = task

        # ─── peak-listener patch ─────────────────────────────
        # Register a synchronous OrderBook listener that updates
        # peak_price_favorable at full WS resolution (~1-5ms), rather than
        # waiting for the 20ms watch-loop poll. This catches peaks that
        # live <20ms which the polling path misses.
        # The listener is intentionally minimal: just compute the exit
        # price for our direction and call pos.update_peak_only(). It does
        # NOT trigger exit decisions — those stay in the watch loop, which
        # keeps time-based safety nets (max_hold, sl_grace, phase windows)
        # robust against WS stalls.
        try:
            mexc_ob = self.ob_manager.get("mexc", signal.symbol)
            if mexc_ob is not None:
                _listener = self._make_position_listener(pos)
                pos._price_listener = _listener  # type: ignore[attr-defined]
                mexc_ob.add_listener(_listener)
                logger.debug(
                    "[peak-listener] registered for %s (mexc ob listeners=%d)",
                    pos.symbol, mexc_ob.listener_count(),
                )
            else:
                # Shouldn't happen — we already entered, so mexc_ob existed.
                # Defensive: log, continue without listener. Polling still
                # tracks peak at 20ms resolution.
                logger.warning(
                    "[peak-listener] mexc orderbook missing for %s at open — "
                    "falling back to polling-only peak tracking",
                    pos.symbol,
                )
        except Exception:
            # Never break position open due to listener registration failure.
            logger.exception(
                "[peak-listener] failed to register for %s — falling back to polling",
                pos.symbol,
            )

        # The caller announces THIS position. Returning it is the only
        # unambiguous answer once two slots can open concurrently on one
        # signal — reading the list afterwards cannot tell whose is whose.
        return pos

    # ============================================================
    # Position watcher
    # ============================================================

    def _make_position_listener(self, pos):
        """Build the per-position OrderBook listener (sync, WS-resolution).

        Two cheap, isolated jobs (a raising listener can't break the WS apply
        path — see OrderBook._notify_listeners):
          1. update peak_price_favorable at full WS resolution (~1-5ms) so we
             catch sub-interval peaks the timer path would miss;
          2. set pos._book_event so the watch loop wakes immediately on a price
             move (event-driven exits). Idempotent set() is O(1).
        Exit DECISIONS stay in the watch loop (keeps time-based safety nets
        robust); this just lets the loop react on book updates, not only on
        the timer.
        """
        def _listener(ob, _pos=pos) -> None:
            # _pos bound by default-arg to avoid late-binding bugs.
            if not _pos.is_open or _pos.is_closing:
                return
            price = ob.executable_exit_price(_pos.direction)
            if price is not None and price > 0:
                _pos.update_peak_only(price)
            ev = getattr(_pos, "_book_event", None)
            if ev is not None:
                ev.set()
        _listener.__name__ = f"pos_listener[{pos.symbol}]"
        return _listener

    async def _watch_position(self, pos: ShadowPosition, cfg: PairExecConfig) -> None:
        """Monitor pos every 20ms (was 100ms), exit when conditions met.

        Polling interval is 20ms to
        catch micro-impulse peaks. Original 100ms missed peaks that lived
        20-50ms, causing trailing stops and phase3 profit reversal to fire
        on already-decayed prices instead of actual MFE.

        Trade-off: 5x more orderbook reads + state comparisons per second
        per open position. With max_concurrent_positions=1 and current pair
        count, CPU overhead is negligible (~5-10% additional load).
        """
        try:
            while pos.is_open and not self._stop.is_set():
                # Event-driven (OPT #1): wake on a MEXC book update for instant
                # price-exit reaction; the timeout floor still drives time-based
                # exits + the staleness guard when the book is quiet.
                await _wait_for_book_event(
                    getattr(pos, "_book_event", None), _WATCH_TIME_TICK_SEC
                )

                # Hard ceiling on position age, INDEPENDENT of strategy
                # phase exits. If a position lives longer than this, force
                # close regardless of strategy state. Catches:
                #   - phase exit logic broken / desync'd
                #   - WS price feed stalled (no price → no exit triggered)
                #   - any "forgotten" position scenarios
                # Env override: STAKAN_ABSOLUTE_MAX_HOLD_SEC (default 600s).
                # 600s = 10 min is far beyond any normal trade duration
                # (phase exits typically fire within seconds). False
                # positives are acceptable: forcing close on a "stuck for
                # 10 min" position is correct behavior.
                # _close_position will route through the safety-patched
                # close chain (IOC → IOC retry → MARKET fallback).
                if pos.elapsed_sec > _ABSOLUTE_MAX_HOLD_SEC:
                    logger.warning(
                        "[ABSOLUTE MAX_HOLD] %s force-closing after %.1fs "
                        "(limit=%ds) — strategy exits did not fire in time",
                        pos.symbol, pos.elapsed_sec, _ABSOLUTE_MAX_HOLD_SEC,
                    )
                    await self._close_position(pos, "absolute_max_hold")
                    break

                mexc_ob = self.ob_manager.get("mexc", pos.symbol)
                if mexc_ob is None or not mexc_ob.is_synced:
                    continue

                # Staleness guard (OPT #3): a frozen MEXC feed means a frozen
                # price → no price-exit can fire → the position rides blind
                # until the 600s absolute ceiling. Force-close instead if the
                # book has been idle beyond _FEED_STALE_MS.
                now_ms = int(time.time() * 1000)
                if _feed_is_stale(mexc_ob.last_update_ts_ms, now_ms, _FEED_STALE_MS):
                    logger.warning(
                        "[FEED STALE] %s — MEXC book idle %dms (>%dms), force-closing",
                        pos.symbol, now_ms - mexc_ob.last_update_ts_ms, _FEED_STALE_MS,
                    )
                    await self._close_position(pos, "feed_stalled")
                    break

                # use executable exit price (best_bid for long,
                # best_ask for short) instead of mid_price. This matches what
                # MEXC UI shows as unrealized PnL — the price at which the
                # position would actually close if hit MARKET right now.
                # mid_price was systematically optimistic for SHORT (used a
                # price between bid and ask, but real exit was at bid which
                # is lower — i.e. less favorable for short).
                current_price = mexc_ob.executable_exit_price(pos.direction)
                if not current_price:
                    continue

                pos.update_price(current_price)

                # Compute live mid_gap_ticks (Binance mid - MEXC mid, in ticks
                # in MEXC scaled domain). Used by phase-exit logic to detect
                # "death of cause" — when the arbitrage opportunity collapses.
                # If we can't compute (book not ready), pass None — the exit
                # check handles that as "unknown gap, don't fire phase-1 rule".
                # same tick_scaled is reused to compute peak_ticks
                # at fixed-time milestones (500/1000/1500/2000ms) for warmup
                # hypothesis validation. See ShadowPosition.record_peak_snapshot.
                # Also capture binance bid/ask to
                # feed the new Binance reversal stop (replaces micro_stop).
                mid_gap_ticks: float | None = None
                # Use cached tick_scaled set once at position open. Falls
                # back to recompute only if cache is empty (cold-start across
                # restart). The previous version recomputed every 20ms.
                tick_scaled: float = pos.tick_scaled
                binance_best_bid: float | None = None
                binance_best_ask: float | None = None
                try:
                    if tick_scaled <= 0:
                        # Cold-start backfill (rare; pre-existing position).
                        mexc_symbol = to_mexc(pos.symbol)
                        tick = get_tick_size(mexc_symbol)
                        scale = get_binance_scale(mexc_symbol)
                        tick_scaled = tick * scale if scale > 0 else tick
                        pos.tick_scaled = tick_scaled

                    # OPT #2: the Binance book is consumed ONLY by the
                    # binance_reversal stop (binance_reversal_ticks). When it's
                    # disabled (0), skip the read + mid_gap entirely — it was
                    # dead work every tick + a needless dependency on the
                    # Binance feed in the exit path.
                    if _needs_binance_book(cfg):
                        binance_ob = self.ob_manager.get("binance", pos.symbol)
                        # *_price() accessors return float directly, avoiding
                        # OrderBookLevel allocations. 0.0 = empty / not synced.
                        if binance_ob is not None and binance_ob.is_synced:
                            b_bid = binance_ob.best_bid_price()
                            b_ask = binance_ob.best_ask_price()
                            m_bid = mexc_ob.best_bid_price()
                            m_ask = mexc_ob.best_ask_price()
                            if b_bid > 0 and b_ask > 0 and m_bid > 0 and m_ask > 0:
                                binance_best_bid = b_bid
                                binance_best_ask = b_ask
                                if tick_scaled > 0:
                                    mid_gap_ticks = (
                                        (b_bid + b_ask) - (m_bid + m_ask)
                                    ) / 2 / tick_scaled
                except Exception:
                    mid_gap_ticks = None  # gracefully degrade
                    binance_best_bid = None
                    binance_best_ask = None

                # peak_ticks snapshot at fixed milestones.
                # peak_ticks = best price improvement (in ticks) since entry.
                # >0 = position has been in profit at this moment; 0 = never.
                # Used post-hoc to evaluate warmup-exit hypothesis.
                if tick_scaled > 0 and pos.entry_price > 0 and pos.peak_price_favorable > 0:
                    if pos.direction == "long":
                        peak_ticks_now = (pos.peak_price_favorable - pos.entry_price) / tick_scaled
                    else:
                        peak_ticks_now = (pos.entry_price - pos.peak_price_favorable) / tick_scaled
                    # Instantaneous adverse at the same instant, in ticks and
                    # positive when against us — the exact quantity nevergreen_cut
                    # compares. Terminal mae_pct cannot answer for it.
                    if pos.direction == "long":
                        adverse_ticks_now = (pos.entry_price - current_price) / tick_scaled
                    else:
                        adverse_ticks_now = (current_price - pos.entry_price) / tick_scaled
                    elapsed_ms = int(pos.elapsed_sec * 1000)
                    pos.record_peak_snapshot(elapsed_ms, peak_ticks_now,
                                             adverse_ticks_now)

                exit_reason = self._check_exit(
                    pos, cfg, mid_gap_ticks,
                    binance_best_bid=binance_best_bid,
                    binance_best_ask=binance_best_ask,
                    tick_scaled=tick_scaled,
                )
                if exit_reason:
                    await self._close_position(pos, exit_reason)
                    break

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception("Watcher error for %s: %s", pos.symbol, e)
            try:
                await self._close_position(pos, "watcher_error")
            except Exception:
                logger.exception("Watcher: failed to close %s after error", pos.symbol)

    def _check_exit(
        self, pos: ShadowPosition, cfg: PairExecConfig,
        mid_gap_ticks: float | None = None,
        binance_best_bid: float | None = None,
        binance_best_ask: float | None = None,
        tick_scaled: float = 0.0,
    ) -> str | None:
        """
        Exit logic — single unified check.

        Priority (first match wins):
          1. time_limit  — safety net, always active
          2. stop_loss   — tick-based catastrophic protection (after sl_grace_sec)
          3. simple_trail — trailing / adverse / breakeven exit

        binance_best_bid/ask + tick_scaled (passed-through from _watch_position):
        used by the Binance reversal stop. If callers don't provide them, it
        silently no-ops.
        """
        # ── 1. TIME LIMIT — always active, safety net
        if pos.elapsed_sec >= cfg.max_hold_sec:
            return "time_limit"

        # ── 2. STOP LOSS — tick-based only (leverage-invariant).
        # stop_loss_ticks=0 effectively disables SL (no fallback).
        # sl_grace_sec blocks SL during first N seconds (entry noise window).
        in_sl_grace = pos.elapsed_sec < cfg.sl_grace_sec
        if not in_sl_grace and cfg.stop_loss_ticks and cfg.stop_loss_ticks > 0:
            # Prefer passed-in (already in scope), then cached on pos, then
            # full recompute as last resort. The cache (pos.tick_scaled) is
            # filled at position open and is the cheapest source.
            sl_tick_scaled = tick_scaled
            if sl_tick_scaled <= 0:
                sl_tick_scaled = pos.tick_scaled
            if sl_tick_scaled <= 0:
                mexc_symbol = to_mexc(pos.symbol)
                tick = get_tick_size(mexc_symbol)
                scale = get_binance_scale(mexc_symbol)
                sl_tick_scaled = tick * scale if scale > 0 else tick

            if sl_tick_scaled > 0 and pos.entry_price > 0 and pos.current_price > 0:
                if pos.direction == "long":
                    adverse_price = pos.entry_price - pos.current_price
                else:  # short
                    adverse_price = pos.current_price - pos.entry_price
                adverse_ticks = adverse_price / sl_tick_scaled
                if adverse_ticks >= cfg.stop_loss_ticks - 1e-9:
                    logger.info(
                        "[SL ticks %.1f/%d] %s %s elapsed=%.2fs roi=%.4f%% "
                        "price=%.8f entry=%.8f leverage=%d",
                        adverse_ticks, cfg.stop_loss_ticks,
                        getattr(pos, 'symbol', '?'),
                        getattr(pos, 'direction', '?').upper() if getattr(pos, 'direction', None) else '?',
                        pos.elapsed_sec,
                        pos.current_roi_pct,
                        getattr(pos, 'current_price', 0.0),
                        getattr(pos, 'entry_price', 0.0),
                        getattr(pos, 'leverage', 0),
                    )
                    return "stop_loss"

        # ── 3. BINANCE REVERSAL ──────
        # Always-on safety net (independent of the trailing exit model).
        # The thesis is "Binance leads, MEXC catches up." If Binance has
        # already reversed past entry by binance_reversal_ticks, the
        # thesis is broken — exit immediately regardless of which exit
        # strategy is active.
        # binance_reversal_ticks doubles as the Binance reversal threshold
        # Set to 0
        # to disable.
        # Time window (binance_reversal_max_ms): only fires during first N ms.
        # After that, MEXC is already moving and Binance micro-dips are
        # normal volatility — not a broken thesis. Set to 0 = always check.
        binance_reversal_threshold = cfg.binance_reversal_ticks
        gap_retrace_frac = getattr(cfg, "gap_retrace_frac", 0.0)
        _gr_trigger = _gap_relative_trigger_scaled(
            pos.direction, pos.entry_price,
            getattr(pos, "gap_ticks", 0.0), tick_scaled, gap_retrace_frac,
        )
        if ((binance_reversal_threshold > 0 or _gr_trigger > 0)
                and binance_best_bid is not None
                and binance_best_ask is not None
                and tick_scaled > 0
                and pos.entry_price > 0):
            elapsed_ms_br = int(pos.elapsed_sec * 1000)
            # Read cached value (set once at position open in _try_enter).
            # Used to do loader.get(pos.symbol).exit_strategy.binance_reversal_max_ms
            # inside try/except on EVERY 20ms tick — pure waste since the
            # value is immutable per-position. Fall back to 0 (no limit) if
            # cache missed (pre-existing positions, loader unavailable).
            _br_max_ms = getattr(pos, "binance_reversal_max_ms_cached", 0)
            # Skip if outside time window (0 = no limit, old behavior)
            if _br_max_ms <= 0 or elapsed_ms_br <= _br_max_ms:
                if _gr_trigger > 0:
                    # GAP-RELATIVE: cut when Binance gives back retrace_frac of
                    # the entry gap. Watches Binance's own retrace, so a MEXC dip
                    # while Binance holds the gap does NOT fire (we hold the
                    # recoverable dip); only a real leader reversal fires.
                    if pos.direction == "long":
                        if binance_best_bid <= _gr_trigger + 1e-12:
                            logger.info(
                                "[BINANCE_REVERSAL gap] %s LONG elapsed=%dms binance_bid=%.8f trigger=%.8f entry=%.8f gap=%.1ft frac=%.2f roi=%.4f%%",
                                pos.symbol, elapsed_ms_br, binance_best_bid, _gr_trigger,
                                pos.entry_price, getattr(pos, "gap_ticks", 0.0),
                                gap_retrace_frac, pos.current_roi_pct,
                            )
                            return "binance_reversal"
                    else:
                        if binance_best_ask >= _gr_trigger - 1e-12:
                            logger.info(
                                "[BINANCE_REVERSAL gap] %s SHORT elapsed=%dms binance_ask=%.8f trigger=%.8f entry=%.8f gap=%.1ft frac=%.2f roi=%.4f%%",
                                pos.symbol, elapsed_ms_br, binance_best_ask, _gr_trigger,
                                pos.entry_price, getattr(pos, "gap_ticks", 0.0),
                                gap_retrace_frac, pos.current_roi_pct,
                            )
                            return "binance_reversal"
                elif pos.direction == "long":
                    stop_price = pos.entry_price - binance_reversal_threshold * tick_scaled
                    if binance_best_bid <= stop_price + 1e-12:
                        logger.info(
                            "[BINANCE_REVERSAL] %s LONG elapsed=%dms "
                            "binance_bid=%.8f stop_price=%.8f entry=%.8f "
                            "(threshold=%.1ft) roi=%.4f%%",
                            pos.symbol, elapsed_ms_br,
                            binance_best_bid, stop_price, pos.entry_price,
                            binance_reversal_threshold, pos.current_roi_pct,
                        )
                        return "binance_reversal"
                else:  # short
                    stop_price = pos.entry_price + binance_reversal_threshold * tick_scaled
                    if binance_best_ask >= stop_price - 1e-12:
                        logger.info(
                            "[BINANCE_REVERSAL] %s SHORT elapsed=%dms "
                            "binance_ask=%.8f stop_price=%.8f entry=%.8f "
                            "(threshold=%.1ft) roi=%.4f%%",
                            pos.symbol, elapsed_ms_br,
                            binance_best_ask, stop_price, pos.entry_price,
                            binance_reversal_threshold, pos.current_roi_pct,
                        )
                        return "binance_reversal"

        # ── 4. TRAILING EXIT — simple_trail (the only exit model).
        # Respects time_limit, stop_loss, and binance_reversal checked above.
        # (The legacy 3-phase model was removed; every pair uses simple_trail.)
        return self._check_simple_trail_exit(pos)

    def _check_simple_trail_exit(self, pos: ShadowPosition) -> str | None:
        """Simple trail exit with 5 rules.

        Priority (first match wins):
          1. simple_adverse        — price moved N ticks against entry
          2. simple_breakeven      — peak reached trigger, current ≤0.5t
          3. simple_trail          — pullback M ticks from running peak
          4. simple_stalled        — in profit but peak hasn't improved
          5. simple_dead_on_arrival — never reached profit, time's up

        Order rationale:
          - adverse is catastrophic → fires first
          - breakeven protects achieved profit → before trail (tighter)
          - trail fires on active reversal from peak → before stall
          - stall catches trades that progressed then stopped
          - dead_on_arrival catches trades that never showed life

        Parameters from pair YAML exit_strategy section:
          stop_adverse_ticks         (default 2)
          trail_distance_ticks       (default 1)
          min_hold_ms                (default 300)
          breakeven_trigger_ticks    (default 0 = disabled)
          stall_timeout_ms           (default 0 = disabled)
          dead_on_arrival_timeout_ms (default 0 = disabled)

        Safety nets time_limit, stop_loss, and binance_reversal live
        ABOVE this in _check_exit and fire regardless.
        """
        # Read cached static values set once at position open (in _try_enter).
        # The previous code recomputed tick_scaled and re-resolved
        # exit_strategy from the loader on EVERY 20ms tick — pure waste,
        # since both are constant per-position. Cold-start fallback: if a
        # position pre-dates the caching patch, recompute on demand once
        # (rare, only for in-flight positions across a restart).
        tick_scaled = pos.tick_scaled
        if tick_scaled <= 0:
            mexc_symbol = to_mexc(pos.symbol)
            tick = get_tick_size(mexc_symbol)
            scale = get_binance_scale(mexc_symbol)
            tick_scaled = tick * scale if scale > 0 else tick
            pos.tick_scaled = tick_scaled  # backfill the cache
        if tick_scaled <= 0 or pos.entry_price <= 0 or pos.current_price <= 0:
            return None

        es = pos.exit_strategy_cached
        if es is None:
            # Cold-start fallback (pre-existing position from before caching).
            loader = getattr(self, "_config_loader", None)
            if loader is None:
                return None
            try:
                es = loader.get(pos.symbol).exit_strategy
                pos.exit_strategy_cached = es
            except Exception:
                logger.debug("No exit_strategy config for %s — skipping trail check", pos.symbol)
                return None

        elapsed_ms = int(pos.elapsed_sec * 1000)
        if elapsed_ms < es.min_hold_ms:
            return None

        # Direction-normalised metrics. "favorable_ticks" >0 means in profit.
        if pos.direction == "long":
            current_favorable_ticks = (pos.current_price - pos.entry_price) / tick_scaled
            peak_favorable_ticks = (
                (pos.peak_price_favorable - pos.entry_price) / tick_scaled
                if pos.peak_price_favorable > 0 else 0.0
            )
        else:  # short
            current_favorable_ticks = (pos.entry_price - pos.current_price) / tick_scaled
            peak_favorable_ticks = (
                (pos.entry_price - pos.peak_price_favorable) / tick_scaled
                if pos.peak_price_favorable > 0 else 0.0
            )

        adverse_ticks = -current_favorable_ticks  # positive in drawdown

        # Resolve thresholds — bps overrides ticks when set.
        eff_stop_adverse = es.effective_stop_adverse_ticks(pos.entry_price, tick_scaled)
        eff_trail_distance = es.effective_trail_distance_ticks(pos.entry_price, tick_scaled)
        eff_breakeven_trigger = es.effective_breakeven_trigger_ticks(pos.entry_price, tick_scaled)

        # ── Rule 1: Adverse stop from ENTRY ───────────────────────────
        if adverse_ticks >= eff_stop_adverse - 1e-9:
            logger.info(
                "[SIMPLE_TRAIL adverse] %s %s elapsed=%dms adverse=%.2ft "
                "(threshold=%.1ft%s) entry=%.6f current=%.6f roi=%.4f%%",
                pos.symbol, pos.direction.upper(), elapsed_ms,
                adverse_ticks, eff_stop_adverse,
                f" [{es.stop_adverse_bps}bps]" if es.stop_adverse_bps > 0 else "",
                pos.entry_price, pos.current_price, pos.current_roi_pct,
            )
            return "simple_adverse"

        # Rule 1b: Never-green deep-dip cut (dead-from-entry killer).
        # Cut a position still never-green AND past nevergreen_adverse_ticks
        # once elapsed >= nevergreen_cut_ms. (ported from primary 2026-06-20)
        ng_ms = getattr(es, "nevergreen_cut_ms", 0)
        if (ng_ms > 0
                and elapsed_ms >= ng_ms
                and peak_favorable_ticks <= getattr(es, "nevergreen_peak_ticks", 1.0) + 1e-9
                and adverse_ticks >= getattr(es, "nevergreen_adverse_ticks", 4.0) - 1e-9):
            logger.info(
                "[NEVERGREEN_CUT] %s %s elapsed=%dms peak=%.2ft adverse=%.2ft never-green",
                pos.symbol, pos.direction.upper(), elapsed_ms,
                peak_favorable_ticks, adverse_ticks,
            )
            return "nevergreen_cut"

        # ── Rule 2: Breakeven lock ────────────────────────────────────
        # Once peak hit trigger, don't let profit fall below 0.5t.
        # Catches the common case "peak +2t → drift back → loss".
        if (eff_breakeven_trigger > 0
                and peak_favorable_ticks >= eff_breakeven_trigger - 1e-9
                and current_favorable_ticks <= 0.5):
            logger.info(
                "[SIMPLE_TRAIL breakeven] %s %s elapsed=%dms "
                "peak=%.2ft current=%.2ft (trigger=%.1ft%s) roi=%.4f%%",
                pos.symbol, pos.direction.upper(), elapsed_ms,
                peak_favorable_ticks, current_favorable_ticks,
                eff_breakeven_trigger,
                f" [{es.breakeven_trigger_bps}bps]" if es.breakeven_trigger_bps > 0 else "",
                pos.current_roi_pct,
            )
            return "simple_breakeven"

        # ── Rule 3: Trail from PEAK ───────────────────────────────────
        # Only active when in profit. Catches active reversals.
        if peak_favorable_ticks > 0:
            pullback_ticks = peak_favorable_ticks - current_favorable_ticks
            if pullback_ticks >= eff_trail_distance - 1e-9:
                logger.info(
                    "[SIMPLE_TRAIL trail] %s %s elapsed=%dms peak=%.2ft "
                    "current=%.2ft pullback=%.2ft (threshold=%.1ft%s) roi=%.4f%%",
                    pos.symbol, pos.direction.upper(), elapsed_ms,
                    peak_favorable_ticks, current_favorable_ticks,
                    pullback_ticks, eff_trail_distance,
                    f" [{es.trail_distance_bps}bps]" if es.trail_distance_bps > 0 else "",
                    pos.current_roi_pct,
                )
                return "simple_trail"

        # ── Rule 4: Stall detection ───────────────────────────────────
        # Trade showed SOME life (peak > 0) but stopped progressing.
        # last_progress_ms is updated whenever peak improves; if it
        # hasn't moved for stall_timeout_ms, impulse is dead.
        if (es.stall_timeout_ms > 0
                and peak_favorable_ticks > 0
                and pos.last_progress_ms > 0):
            now_ms = int(time.time() * 1000)
            ms_since_progress = now_ms - pos.last_progress_ms
            if ms_since_progress >= es.stall_timeout_ms:
                logger.info(
                    "[SIMPLE_TRAIL stall] %s %s elapsed=%dms "
                    "peak=%.2ft stalled=%dms (threshold=%dms) roi=%.4f%%",
                    pos.symbol, pos.direction.upper(), elapsed_ms,
                    peak_favorable_ticks, ms_since_progress,
                    es.stall_timeout_ms, pos.current_roi_pct,
                )
                return "simple_stalled"

        # ── Rule 5: Dead-on-arrival cutoff ────────────────────────────
        # Peak NEVER exceeded entry (mfe ~ 0) after timeout.
        # Data: 841/864 such trades end as losers (-$0.18 avg, 2.7% WR).
        # Cuts the longest drain in the system.
        if (es.dead_on_arrival_timeout_ms > 0
                and peak_favorable_ticks <= 0
                and elapsed_ms >= es.dead_on_arrival_timeout_ms):
            logger.info(
                "[SIMPLE_TRAIL dead_on_arrival] %s %s elapsed=%dms "
                "never reached profit (peak=%.2ft current=%.2ft) roi=%.4f%%",
                pos.symbol, pos.direction.upper(), elapsed_ms,
                peak_favorable_ticks, current_favorable_ticks,
                pos.current_roi_pct,
            )
            return "simple_dead_on_arrival"

        return None

    # ============================================================
    # Close
    # ============================================================

    @staticmethod
    def _humanize(seconds: float, lo: float, hi: float) -> float:
        """Jitter a hold so the cadence does not look machine-generated.

        A dead-exact 300s rhythm is itself a fingerprint. `lo`/`hi` are
        multipliers: soft start jitters both ways (mean unchanged, so the
        opens/hour target still holds), while a throttle hold only jitters
        UP — dipping below the discovered ceiling would just earn a rejection.
        """
        return seconds * random.uniform(lo, hi)

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        """Never let a typo in the environment raise inside the trading loop."""
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default




    def _arm_open_hold(self, sid: int, seconds: float, why: str) -> None:
        """Hold OPENS on this slot for `seconds`, never shortening an
        existing hold (a 60s throttle hold must not cancel a 300s soft-start
        one, and vice versa). Exits are unaffected — the gate this feeds sits
        before order submit only."""
        import time as _t
        # Clamp: a hand-set OPEN_THROTTLE_*=0 would otherwise log a hold of
        # 0s and pace nothing at all.
        seconds = max(1.0, float(seconds))
        deadline = _t.monotonic() + seconds
        if deadline > self._slot_cooldown_until.get(sid, 0.0):
            self._slot_cooldown_until[sid] = deadline
            logger.info("[OPEN HOLD] slot=%d %.0fs (%s)", sid, seconds, why)

    async def _close_position(self, pos: ShadowPosition, reason: str) -> None:
        # Defensive race guard:
        # Two checks combined atomically (no await between):
        #   1. is_open — position not already closed (existing guard)
        #   2. is_closing — close not already in-flight (new defensive flag)
        # Without is_closing, concurrent _close_position calls (e.g.,
        # engine_shutdown + watcher exit at same tick) could both pass
        # the is_open check (since exit_reason isn't set until the end
        # of close), then both await on MEXC close — sending duplicate
        # close orders.
        # Python GIL guarantees that the read-check-write pattern below
        # is atomic relative to coroutine scheduling. No yield point.
        if not pos.is_open or getattr(pos, 'is_closing', False):
            return
        pos.is_closing = True

        # Record when the exit DECISION was made — before MEXC close latency.
        # duration_ms will use this instead of closed_at_ms so that duration
        # reflects actual trade lifetime, not trade lifetime + MEXC roundtrip.
        pos.exit_decided_at_ms = int(time.time() * 1000)

        # === LIVE CLOSE BRANCH ===
        # If position was opened in live mode, close it on MEXC first.
        # The shadow simulation runs after, for comparison.
        if pos.mode == "live":
            # Find correct executor based on slot used during open
            executor_for_close = None
            slot_id_for_close: int | None = None
            if pos.account_label and pos.account_label.startswith("slot"):
                # Multi-slot mode: parse slot_id from "slot2"
                try:
                    slot_id_for_close = int(pos.account_label[4:])
                except ValueError:
                    pass
                if self.live_pool is not None and slot_id_for_close is not None:
                    executor_for_close = self.live_pool.get_executor(slot_id_for_close)

            if executor_for_close is not None:
                mexc_symbol = to_mexc(pos.symbol)

                # prefer EXCHANGE-AUTHORITATIVE close fill
                # data from /position/list/history_positions (closeAvgPrice +
                # realised). Falls back to local mexc_ob.mid_price() if the
                # history endpoint is slow/unavailable. mid is approximate
                # (half-spread bias) but consistent with the rest of the bot.
                pre_close_exit_price = 0.0
                _mexc_ob_for_close = self.ob_manager.get("mexc", pos.symbol)
                if _mexc_ob_for_close is not None and _mexc_ob_for_close.is_synced:
                    pre_close_exit_price = _mexc_ob_for_close.mid_price() or 0.0

                # Reversal closes fire into a GAPPING book — route them through
                # the capped IOC-limit close (bounded cross + guaranteed market
                # fallback) so they can't sweep the whole gap (the −13bps fills).
                # Every other exit keeps the plain market close (calm book →
                # clean fill). Dormant until binance_reversal is re-enabled.
                if (
                    reason == "binance_reversal"
                    and not _REVERSAL_FAST_CLOSE
                    and _mexc_ob_for_close is not None
                    and _mexc_ob_for_close.is_synced
                    and pos.tick_scaled > 0
                ):
                    _lim_scaled = _capped_close_limit_scaled(
                        pos.direction,
                        _mexc_ob_for_close.best_bid_price(),
                        _mexc_ob_for_close.best_ask_price(),
                        _REVERSAL_CLOSE_CAP_TICKS, pos.tick_scaled,
                    )
                    _scale = get_binance_scale(to_mexc(pos.symbol))
                    _lim_raw = (_lim_scaled / _scale) if (_lim_scaled > 0 and _scale > 0) else 0.0
                    if _lim_raw > 0:
                        close_result = await executor_for_close.close_position_capped(
                            symbol=mexc_symbol, direction=pos.direction,
                            qty_contracts=int(pos.qty), leverage=pos.leverage,
                            limit_price_raw=_lim_raw,
                        )
                    else:
                        close_result = await executor_for_close.close_position(symbol=mexc_symbol)
                else:
                    close_result = await executor_for_close.close_position(symbol=mexc_symbol)
                pos.live_close_latency_ms = close_result.latency_ms

                # PRIMARY PATH: real exit + realized from MEXC history_positions
                # (set inside executor.close_position via _poll_close_fill).
                if (
                    close_result.success
                    and getattr(close_result, "exit_price", 0) > 0
                ):
                    pos.live_exit_price_real = close_result.exit_price
                    pos.live_realized_pnl_usdt = close_result.realized_pnl_usdt
                    # also override pos.entry_price with
                    # MEXC's openAvgPrice. Otherwise Telegram shows stale
                    # entry from initial submit (not actual fill).
                    real_entry = getattr(close_result, "entry_price_confirmed", 0)
                    if real_entry > 0 and abs(real_entry - pos.entry_price) > 0:
                        logger.info(
                            "[LIVE ENTRY OVERRIDE] %s old_entry=%.6f → real_entry=%.6f",
                            pos.symbol, pos.entry_price, real_entry,
                        )
                        pos.entry_price = real_entry
                    logger.info(
                        "[LIVE CLOSE PNL] %s entry=%.6f exit=%.6f qty=%.4f "
                        "pnl=$%+.4f (from MEXC history_positions, AUTHORITATIVE)",
                        pos.symbol, pos.entry_price, close_result.exit_price,
                        pos.qty, close_result.realized_pnl_usdt,
                    )
                # FALLBACK: real exit unavailable — use local mid_price
                elif (
                    close_result.success
                    and pre_close_exit_price > 0
                    and pos.entry_price > 0
                    and pos.qty > 0
                ):
                    real_pnl = calc_pnl_usdt(
                        direction=pos.direction,
                        entry=pos.entry_price,
                        exit=pre_close_exit_price,
                        qty=pos.qty,
                    )
                    pos.live_exit_price_real = pre_close_exit_price
                    pos.live_realized_pnl_usdt = real_pnl
                    logger.info(
                        "[LIVE CLOSE PNL] %s entry=%.6f exit=%.6f qty=%.4f "
                        "pnl=$%+.4f (FALLBACK to mexc_ob mid — history poll missed)",
                        pos.symbol, pos.entry_price, pre_close_exit_price,
                        pos.qty, real_pnl,
                    )

                # retry once on failure with 1s backoff
                # before declaring orphan. Many transient errors (rate limit, 502)
                # resolve on retry.
                if not close_result.success:
                    logger.warning(
                        "[LIVE CLOSE RETRY] slot=%s %s: first attempt failed (%s) — retrying in 1s",
                        slot_id_for_close, pos.symbol, close_result.error_msg,
                    )
                    await asyncio.sleep(1.0)
                    # capture fresh mid for retry — 1s passed,
                    # original pre_close_exit_price is stale.
                    retry_exit_price = 0.0
                    _ob = self.ob_manager.get("mexc", pos.symbol)
                    if _ob is not None and _ob.is_synced:
                        retry_exit_price = _ob.mid_price() or 0.0
                    try:
                        close_result = await executor_for_close.close_position(symbol=mexc_symbol)
                        pos.live_close_latency_ms += close_result.latency_ms
                    except Exception as retry_err:
                        logger.exception("[LIVE CLOSE RETRY] exception during retry: %s", retry_err)
                    # Apply real PnL from retry. Prefer history_positions data
                    # again; fall back to mid if missing.
                    if (
                        close_result.success
                        and pos.live_exit_price_real == 0
                    ):
                        if getattr(close_result, "exit_price", 0) > 0:
                            pos.live_exit_price_real = close_result.exit_price
                            pos.live_realized_pnl_usdt = close_result.realized_pnl_usdt
                            # Same entry override logic as primary path
                            real_entry = getattr(close_result, "entry_price_confirmed", 0)
                            if real_entry > 0 and abs(real_entry - pos.entry_price) > 0:
                                pos.entry_price = real_entry
                            logger.info(
                                "[LIVE CLOSE PNL RETRY] %s entry=%.6f exit=%.6f qty=%.4f "
                                "pnl=$%+.4f (from MEXC history_positions, retry)",
                                pos.symbol, pos.entry_price, close_result.exit_price,
                                pos.qty, close_result.realized_pnl_usdt,
                            )
                        elif (
                            retry_exit_price > 0
                            and pos.entry_price > 0
                            and pos.qty > 0
                        ):
                            real_pnl = calc_pnl_usdt(
                                direction=pos.direction,
                                entry=pos.entry_price,
                                exit=retry_exit_price,
                                qty=pos.qty,
                            )
                            pos.live_exit_price_real = retry_exit_price
                            pos.live_realized_pnl_usdt = real_pnl
                            logger.info(
                                "[LIVE CLOSE PNL RETRY] %s entry=%.6f exit=%.6f qty=%.4f "
                                "pnl=$%+.4f (FALLBACK to mexc_ob mid, retry)",
                                pos.symbol, pos.entry_price, retry_exit_price,
                                pos.qty, real_pnl,
                            )

                if not close_result.success:
                    # Both regular close attempts failed (Fix A in
                    # live_executor.close_position now correctly returns
                    # success=False when position remains open). Before
                    # marking orphan, try TRUE MARKET ORDER — type=5,
                    # accepts worse slippage but is guaranteed to fill.
                    # Orphan position drift scenarios:
                    # 36 minutes for -$7.93 because the bot accepted close
                    # failure and marked the position "closed" internally.
                    # Market fallback would have closed within seconds at
                    # whatever the touch price was at that moment.
                    mexc_symbol_for_market = to_mexc(pos.symbol)
                    logger.warning(
                        "[LIVE CLOSE ESCALATE] slot=%s %s: 2x regular close failed, "
                        "attempting MARKET CLOSE as last resort",
                        slot_id_for_close, pos.symbol,
                    )
                    try:
                        market_result = await executor_for_close.market_close_position(
                            symbol=mexc_symbol_for_market,
                            direction=pos.direction,
                            # round() instead of int() to avoid
                            # truncation residuals (99.7 → 99 leaves 0.7
                            # contract orphaned). The live_executor itself
                            # also overrides from MEXC holdVol, but better
                            # to send a correct number upstream.
                            qty_contracts=int(round(pos.qty)),
                            leverage=pos.leverage,
                        )
                        pos.live_close_latency_ms += market_result.latency_ms
                    except Exception as market_err:
                        logger.exception("[MARKET CLOSE] exception: %s", market_err)
                        market_result = None

                    if market_result is not None and market_result.success:
                        # Market close succeeded — apply real PnL and continue normally.
                        close_result = market_result
                        if market_result.exit_price > 0:
                            pos.live_exit_price_real = market_result.exit_price
                            pos.live_realized_pnl_usdt = market_result.realized_pnl_usdt
                            real_entry = getattr(market_result, "entry_price_confirmed", 0)
                            if real_entry > 0 and abs(real_entry - pos.entry_price) > 0:
                                pos.entry_price = real_entry
                            logger.info(
                                "[LIVE CLOSE PNL MARKET] %s entry=%.6f exit=%.6f "
                                "qty=%.4f pnl=$%+.4f (FORCE MARKET CLOSE)",
                                pos.symbol, pos.entry_price, market_result.exit_price,
                                pos.qty, market_result.realized_pnl_usdt,
                            )
                        # else: position closed but no history row yet —
                        # downstream code falls back to mid_price below.

                if not close_result.success:
                    # CRITICAL: ALL close attempts failed (2x regular + 1x market).
                    # Position may still be open on MEXC! Alert user immediately.
                    pos.live_close_error = close_result.error_msg
                    logger.error(
                        "🚨 [LIVE CLOSE FAILED ALL PATHS] slot=%s %s: %s — "
                        "POSITION MAY STILL BE OPEN! Manual intervention required.",
                        slot_id_for_close, pos.symbol, close_result.error_msg,
                    )
                    # send critical Telegram alert (no throttle)
                    # so user can manually close on MEXC UI immediately.
                    if getattr(self, "alerts", None) is not None:
                        try:
                            await self.alerts.send(
                                text=(
                                    f"🚨 <b>ORPHAN POSITION (market fallback failed)</b>\n\n"
                                    f"<b>Slot:</b> {slot_id_for_close}\n"
                                    f"<b>Pair:</b> {pos.symbol} {pos.direction.upper()}\n"
                                    f"<b>Margin:</b> ${pos.margin_usdt:.2f} × {pos.leverage}x\n"
                                    f"<b>Entry:</b> {pos.entry_price:.6f}\n"
                                    f"<b>Qty:</b> {pos.qty}\n"
                                    f"<b>Error:</b> <i>{close_result.error_msg}</i>\n\n"
                                    f"⚠️ <b>2x IOC close + 1x MARKET close all failed</b>\n"
                                    f"This is unusual — check MEXC immediately."
                                ),
                                category=f"orphan_{pos.symbol}_{int(time.time())}",
                                throttle_sec=0,
                                suppress_during_quiet=False,
                            )
                        except Exception:
                            logger.exception("Failed to send orphan position alert")
                    # We still record the shadow close so the bot doesn't get stuck.
                    # User should check MEXC UI manually.

        # === REALISM: latency before close order reaches MEXC ===
        # Close is faster than open (reused TLS connection, no warmup),
        # measured ~60ms in micro_trade test.
        # Skip extra latency for live since we already ate it in real call above.
        if pos.mode == "shadow" and self.realism.close_signal_to_order_latency_ms > 0:
            await asyncio.sleep(self.realism.close_signal_to_order_latency_ms / 1000)

        mexc_ob = self.ob_manager.get("mexc", pos.symbol)
        exit_price = 0.0
        slippage_pct = 0.0
        fee_usdt = 0.0

        if mexc_ob and mexc_ob.is_synced:
            result = self.market_executor.simulate_market_exit(
                mexc_ob=mexc_ob,
                direction=pos.direction,
                notional_usdt=pos.notional_usdt,
                # book size is in CONTRACTS — same contractSize the entry sim/live use
                contract_size=CONTRACT_SIZES.get(to_mexc(pos.symbol), 1.0),
            )
            if result:
                exit_price = result.exit_price
                slippage_pct = result.slippage_pct
                fee_usdt = result.fee_usdt

                # === REALISM Level 7: spread blowout simulation ===
                # Rare news/breakout events cause spread to widen dramatically.
                # When this happens during our exit, we eat extra slippage.
                blowout_mult = check_and_apply_spread_blowout(
                    pos.symbol, time.time(), self.realism,
                )
                if blowout_mult > 1.0:
                    # Apply the multiplier to slippage and recalc exit_price
                    extra_slip_pct = slippage_pct * (blowout_mult - 1.0)
                    if pos.direction == 'long':
                        # Selling — bigger slippage means lower exit price
                        exit_price = exit_price * (1 - extra_slip_pct / 100)
                    else:
                        # Buying back — bigger slippage means higher exit price
                        exit_price = exit_price * (1 + extra_slip_pct / 100)
                    slippage_pct = slippage_pct * blowout_mult
                    logger.info(
                        "[SPREAD_BLOWOUT] %s slip×%.1f → %.4f%%",
                        pos.symbol, blowout_mult, slippage_pct,
                    )

        if exit_price <= 0:
            # Fallback: use current mid (orderbook might be desynced briefly)
            exit_price = pos.current_price or pos.entry_price
            slippage_pct = 0.0
            fee_usdt = pos.notional_usdt * 0.0004

        # === REALISM: apply per-pair fee model ===
        # Some pairs have non-zero fees even on MEXC promo (e.g. majors).
        # Realism profile defaults to zero fee for alt-alts (verified).
        realism_fee_pct = fee_pct_for_pair(pos.symbol, self.realism, side='taker')
        if realism_fee_pct > 0:
            fee_usdt = pos.notional_usdt * realism_fee_pct

        # === REALISM: funding cost if position crossed funding window ===
        funding_cost = 0.0
        if self.realism.enable_funding_cost:
            closed_now_ms = int(time.time() * 1000)
            if crosses_funding_window(pos.opened_at_ms, closed_now_ms, self.realism):
                funding_cost = calculate_funding_cost(
                    notional_usdt=pos.notional_usdt,
                    direction=pos.direction,
                    profile=self.realism,
                )
                # Funding can be credit (negative) for short positions when rate is positive
                if funding_cost > 0:
                    fee_usdt += funding_cost
                    self.funding_cost_paid += funding_cost

        pos.close(
            exit_price=exit_price,
            exit_slippage_pct=slippage_pct,
            exit_fees_usdt=fee_usdt,
            exit_reason=reason,
        )

        # Remove from open positions
        if pos in self._open_positions[pos.symbol]:
            self._open_positions[pos.symbol].remove(pos)

        # Find safety controller for this slot (multi-slot only) — used below
        # AFTER real PnL override so kill switch sees authoritative PnL.
        safety_for_close = None
        slot_for_close: int | None = None
        if pos.mode == "live":
            if pos.account_label and pos.account_label.startswith("slot"):
                try:
                    slot_for_close = int(pos.account_label[4:])
                except ValueError:
                    pass
                if self.live_pool is not None and slot_for_close is not None:
                    safety_for_close = self.live_pool.get_safety(slot_for_close)

        # for live positions with real MEXC data, override
        # the shadow-simulated exit_price + net_pnl_usdt with real values.
        # Falls back to shadow values if real data unavailable.
        if pos.mode == "live" and getattr(pos, "live_exit_price_real", 0) > 0:
            real_exit = pos.live_exit_price_real
            real_pnl = pos.live_realized_pnl_usdt
            pos.exit_price = real_exit
            pos.net_pnl_usdt = real_pnl
            # Recalculate ROI based on real PnL
            if pos.margin_usdt > 0:
                pos.current_roi_pct = (real_pnl / pos.margin_usdt) * 100.0
            # Reset entry/exit fees since real fees are captured by realized_pnl
            pos.entry_fees_usdt = 0.0
            pos.exit_fees_usdt = 0.0
            pos.pnl_usdt = real_pnl
            logger.info(
                "[LIVE EXIT REAL] %s exit=%.6f pnl=$%+.4f roi=%+.2f%% (overrode shadow)",
                pos.symbol, real_exit, real_pnl, pos.current_roi_pct,
            )

        # Apply cooldown AFTER live PnL override — if it ran before the
        # override, a live LOSS that the shadow simulator thought was a
        # "win" would trigger cooldown_after_win_sec (shorter) instead
        # of cooldown_after_loss_sec, causing too-quick re-entry after
        # losing live money.
        cfg = self._pair_configs.get(pos.symbol) or PairExecConfig()
        cooldown_sec = cfg.cooldown_after_win_sec if pos.net_pnl_usdt > 0 else cfg.cooldown_after_loss_sec
        _cd_slot = None
        if pos.account_label and pos.account_label.startswith("slot"):
            try:
                _cd_slot = int(pos.account_label[4:])
            except ValueError:
                _cd_slot = None
        self._cooldown_until[(_cd_slot, pos.symbol)] = int(time.time()) + cooldown_sec

        # Notify safety controller AFTER real-PnL override. Using the
        # shadow-simulated net_pnl_usdt would accumulate fake simulated
        # fees in daily_pnl and trip the kill switch with real PnL
        # nowhere near the threshold.
        if safety_for_close is not None:
            safety_for_close.record_close(
                pos.symbol, pos.net_pnl_usdt,
                notional_usdt=(pos.margin_usdt or 0) * (pos.leverage or 0))
            ss = safety_for_close.state_summary()
            if ss["kill_active"]:
                logger.warning(
                    "🚨 KILL SWITCH NOW ACTIVE after live close (slot=%s): %s "
                    "(daily_pnl=$%.2f, consec_losses=%d)",
                    slot_for_close, ss["kill_reason"],
                    ss["today_pnl"], ss["consecutive_losses"],
                )
                # notify owner via Telegram
                if self.alerts is not None:
                    try:
                        await self.alerts.send(
                            f"🚨 <b>KILL SWITCH ACTIVE</b>\n"
                            f"Slot: <code>{slot_for_close}</code>\n"
                            f"Reason: {ss['kill_reason']}\n"
                            f"Daily PnL: ${ss['today_pnl']:+.2f}\n"
                            f"Consec losses: {ss['consecutive_losses']}\n"
                            f"Until: {ss.get('kill_until_human', 'indefinite')}",
                            category=f"kill_switch:{slot_for_close}",
                            throttle_sec=60,  # don't spam if multiple closes hit at once
                        )
                    except Exception:
                        logger.exception("Failed to send kill_switch alert")

        # Persist to DB
        await self._persist_trade(pos)

        self.positions_closed += 1

        logger.info(
            "[CLOSE] %s %s @ %.6f (entry %.6f, slip %.4f%%) | reason=%s "
            "duration=%.2fs close_latency=%dms ROI=%+.2f%% PnL=$%+.4f net=$%+.4f MFE=%.3f%% MAE=%.3f%%",
            pos.symbol, pos.direction.upper(),
            pos.exit_price, pos.entry_price, pos.exit_slippage_pct,
            reason, pos.duration_ms / 1000.0,
            max(0, pos.closed_at_ms - pos.exit_decided_at_ms) if pos.exit_decided_at_ms > 0 else 0,
            pos.current_roi_pct,
            pos.pnl_usdt, pos.net_pnl_usdt, pos.mfe_pct, pos.mae_pct,
        )

        # Memory leak fix:
        # _watcher_tasks dict was leaking references — never cleaned up after
        # position close. Each closed position left a dead Task in the dict
        # until process restart. With 1000+ trades/day this accumulates.
        # Discard here (idempotent — won't error if already removed).
        # getattr(default={}) keeps tests that mock-construct ShadowEngine
        # without invoking __init__ working — they don't have _watcher_tasks.
        getattr(self, '_watcher_tasks', {}).pop(id(pos), None)

        # ─── peak-listener patch ─────────────────────────────
        # Unregister OrderBook price listener if one was attached at open.
        # Same memory-leak class as _watcher_tasks: a forgotten listener
        # holds a closure that references `pos`, blocking GC and firing
        # update_peak_only() on a closed position forever.
        # update_peak_only() does check is_open/is_closing and early-returns,
        # but the function call itself still wastes WS cycles. Remove
        # explicitly. Idempotent — no-op if listener was never set.
        listener = getattr(pos, '_price_listener', None)
        if listener is not None:
            try:
                mexc_ob = self.ob_manager.get("mexc", pos.symbol)
                if mexc_ob is not None:
                    mexc_ob.remove_listener(listener)
            except Exception:
                logger.exception(
                    "[peak-listener] failed to unregister for %s — "
                    "listener will be a no-op (is_closing guard) until GC",
                    pos.symbol,
                )
            finally:
                # Clear ref regardless so GC can reclaim the closure once
                # the position itself is collected.
                pos._price_listener = None  # type: ignore[attr-defined]

    async def mark_position_closed_externally(
        self,
        pos: ShadowPosition,
        reason: str,
        exit_price_hint: float = 0.0,
    ) -> None:
        """Mark a live position as closed WITHOUT submitting close to MEXC.

        Used by the reconciliation system when MEXC confirms a position is
        already gone (closed externally — liquidation, manual UI close, or
        any path that bypassed our close machinery). Bot needs to clean up
        its in-memory state and DB row, but submitting a close to MEXC
        would error (no position to close).

        Args:
            pos: ShadowPosition to mark closed.
            reason: exit_reason string to record (e.g. "reconciliation_external_close").
            exit_price_hint: optional exit price. If 0, uses current MEXC
                executable_exit_price as best estimate (PnL will not match
                exchange-real value if there's been drift).

        Idempotent: no-op if pos is already closed or is_closing.
        Safe to call concurrently with _close_position thanks to is_closing
        flag (same defense as in _close_position).
        """
        if not pos.is_open or getattr(pos, "is_closing", False):
            return
        pos.is_closing = True

        # Determine exit price: hint > current MEXC mid > entry as last resort.
        if exit_price_hint <= 0:
            ob = self.ob_manager.get("mexc", pos.symbol)
            if ob is not None and getattr(ob, "is_synced", False):
                exit_price_hint = ob.executable_exit_price(pos.direction) or 0.0
            if exit_price_hint <= 0 and ob is not None:
                exit_price_hint = ob.mid_price() or 0.0
            if exit_price_hint <= 0:
                # Last resort: entry price (PnL = 0 will be wrong, but
                # better than crashing on a divide-by-zero downstream).
                exit_price_hint = pos.entry_price

        # Set exit fields. Mirrors what ShadowPosition.close() does but
        # without the strict slippage calc (which assumes our own submission).
        pos.exit_price = exit_price_hint
        pos.exit_slippage_pct = 0.0  # not our submission, slippage not meaningful
        pos.exit_fees_usdt = 0.0
        pos.exit_reason = reason
        pos.closed_at_ms = int(time.time() * 1000)
        if pos.exit_decided_at_ms <= 0:
            pos.exit_decided_at_ms = pos.closed_at_ms
        decision_ms = pos.exit_decided_at_ms
        pos.duration_ms = decision_ms - pos.opened_at_ms
        pos.duration_sec = int(pos.duration_ms / 1000)

        if pos.entry_price > 0 and pos.qty > 0:
            pos.pnl_usdt = calc_pnl_usdt(
                direction=pos.direction,
                entry=pos.entry_price,
                exit=exit_price_hint,
                qty=pos.qty,
            )
        pos.net_pnl_usdt = pos.pnl_usdt - pos.entry_fees_usdt - pos.exit_fees_usdt

        logger.warning(
            "[EXTERNAL CLOSE] %s %s entry=%.6f exit≈%.6f qty=%.4f "
            "pnl=$%+.4f duration=%.1fs reason=%s",
            pos.symbol, pos.direction.upper(),
            pos.entry_price, exit_price_hint, pos.qty,
            pos.net_pnl_usdt, pos.duration_ms / 1000.0, reason,
        )

        # Persist to DB
        try:
            await self._persist_trade(pos)
        except Exception:
            logger.exception(
                "[EXTERNAL CLOSE] _persist_trade failed for %s — state still "
                "marked closed in engine but DB row may be missing",
                pos.symbol,
            )

        # notify the per-slot LiveSafetyController
        # that this live position is gone. Without this, the safety counter
        # `open_live_positions[symbol]` stays elevated forever — every future
        # `can_open_live(symbol)` returns False with reason
        # "max_concurrent_per_symbol reached", silently bricking the slot
        # from trading this symbol until the bot restarts.
        # This mirrors the same record_close logic in _close_position (the
        # normal-close path). PnL is the position's net_pnl_usdt computed
        # above from a best-effort exit_price_hint — it WILL diverge from
        # the real exchange PnL (we didn't submit the close, we don't know
        # what realised was). That's accepted: the kill-switch counter using
        # approximate PnL is still better than no decrement at all.
        if pos.mode == "live" and pos.account_label \
                and pos.account_label.startswith("slot") \
                and self.live_pool is not None:
            try:
                slot_for_close = int(pos.account_label[4:])
            except ValueError:
                slot_for_close = None
            if slot_for_close is not None:
                safety_for_close = self.live_pool.get_safety(slot_for_close)
                if safety_for_close is not None:
                    try:
                        safety_for_close.record_close(
                            pos.symbol, pos.net_pnl_usdt or 0.0,
                            notional_usdt=(pos.margin_usdt or 0) * (pos.leverage or 0),
                        )
                        logger.info(
                            "[EXTERNAL CLOSE] safety counter decremented "
                            "slot=%d symbol=%s approx_pnl=$%+.4f",
                            slot_for_close, pos.symbol, pos.net_pnl_usdt or 0.0,
                        )
                    except Exception:
                        logger.exception(
                            "[EXTERNAL CLOSE] safety.record_close failed for "
                            "%s — slot may stay blocked until restart",
                            pos.symbol,
                        )

        self.positions_closed += 1

        # Cleanup: same as _close_position end. Remove watcher reference,
        # unregister OrderBook listener, remove from _open_positions.
        getattr(self, "_watcher_tasks", {}).pop(id(pos), None)

        listener = getattr(pos, "_price_listener", None)
        if listener is not None:
            try:
                mexc_ob = self.ob_manager.get("mexc", pos.symbol)
                if mexc_ob is not None:
                    mexc_ob.remove_listener(listener)
            except Exception:
                logger.exception(
                    "[EXTERNAL CLOSE] listener unregister failed for %s",
                    pos.symbol,
                )
            finally:
                pos._price_listener = None  # type: ignore[attr-defined]

        if pos in self._open_positions[pos.symbol]:
            self._open_positions[pos.symbol].remove(pos)

    async def _record_live_miss(self, signal, slot_id, reason, cfg) -> None:
        """Record a failed/expired live-open attempt into live_open_misses.

        live_trades stores only opens that FILLED; misses (ioc_expired_no_fill,
        rejects, 510s) used to vanish into logs, leaving the DB blind to the
        true fill rate. fill_rate = filled / (filled + misses) over a window.
        Best-effort: a logging failure must never disrupt trading.
        """
        if self.live_db is None:
            return
        # Don't count an attempt as a "miss" while a position is already open
        # for this symbol. With max_positions_per_symbol=1 the bot could not
        # have opened a second one anyway, so it isn't a genuine missed
        # opportunity — counting it would understate the real fill rate
        # (fill_rate = filled / (filled + misses)). 2026-05-31.
        _lbl = f"slot{slot_id}" if slot_id is not None else None
        if any((p.account_label or None) == _lbl
               for p in self._open_positions.get(signal.symbol, ())):
            return
        try:
            await self.live_db.execute(
                "INSERT INTO live_open_misses "
                "(ts, symbol, direction, slot_id, reason, confidence, ioc_offset_ticks) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    signal.symbol,
                    signal.direction,
                    slot_id,
                    reason or "unknown",
                    getattr(signal, "confidence", None),
                    getattr(cfg, "ioc_offset_ticks", None),
                ),
            )
        except Exception as e:
            logger.warning("Failed to record live miss for %s: %s", signal.symbol, e)

    async def _persist_trade(self, pos: ShadowPosition) -> None:
        """Insert closed trade into DB. Live → live_db.live_trades, shadow → db.shadow_trades."""
        # Per-trade context line: entry spread + gap + outcome on one durable log
        # row, so we can later test whether adverse stops cluster on a wide book
        # (spread-gate) without a DB-schema migration. Grep "[TRADE-CTX]".
        _net_bps = (pos.net_pnl_usdt / pos.notional_usdt * 1e4) if (pos.net_pnl_usdt is not None and pos.notional_usdt) else 0.0
        logger.info(
            "[TRADE-CTX] %s %s spread=%.1fbps gap=%.1ft conf=%.2f -> %s "
            "net=%+.2fbps mfe=%+.1f mae=%+.1f",
            pos.symbol, pos.direction, pos.entry_spread_bps, pos.gap_ticks,
            pos.confidence, pos.exit_reason, _net_bps,
            (pos.mfe_pct or 0.0) * 100, (pos.mae_pct or 0.0) * 100,
        )
        # Route by mode. Live trades go to isolated live_db so shadow analytics stays clean.
        if pos.mode == "live" and self.live_db is not None:
            target_db = self.live_db
            target_table = "live_trades"
        else:
            target_db = self.db
            target_table = "shadow_trades"
        try:
            await target_db.execute(
                f"""INSERT INTO {target_table}
                   (signal_id, signal_uid, symbol, direction, leverage, margin_usdt, notional_usdt,
                    entry_price, entry_slippage_pct, opened_at,
                    exit_price, exit_slippage_pct, closed_at, exit_reason,
                    pnl_usdt, roi_pct, duration_sec, duration_ms,
                    mfe_pct, mae_pct, peak_roi_pct, trough_roi_pct,
                    entry_target_price, entry_filled_pct, entry_status, entry_attempted_at,
                    detector_source, confidence,
                    binance_price_at_entry, mexc_price_at_entry, mexc_lag_at_entry_pct,
                    entry_fees_usdt, exit_fees_usdt, net_pnl_usdt,
                    time_to_max_favorable_sec, mode, account_label,
                    mexc_order_id_open, mexc_order_id_close,
                    real_entry_latency_ms, real_close_latency_ms,
                    latency_signal_to_pickup_ms, latency_submit_ms, latency_response_ms,
                    latency_fill_poll_ms, latency_close_submit_ms, latency_close_response_ms,
                    peak_ticks_at_500ms, peak_ticks_at_1000ms,
                    peak_ticks_at_1500ms, peak_ticks_at_2000ms,
                    adverse_ticks_at_1000ms)
                   VALUES (?,?,?,?,?,?,?, ?,?,?, ?,?,?,?,
                           ?,?,?,?, ?,?,?,?, ?,?,?,?,
                           ?,?, ?,?,?,
                           ?,?,?, ?,?,?,
                           ?,?,
                           ?,?,
                           ?,?,?,
                           ?,?,?,
                           ?,?,?,?,?)""",
                (
                    pos.signal_id, pos.signal_uid, pos.symbol, pos.direction, pos.leverage,
                    pos.margin_usdt, pos.notional_usdt,
                    pos.entry_price, pos.entry_slippage_pct, pos.opened_at_ms // 1000,
                    pos.exit_price, pos.exit_slippage_pct, pos.closed_at_ms // 1000,
                    pos.exit_reason,
                    pos.pnl_usdt, pos.current_roi_pct, pos.duration_sec, pos.duration_ms,
                    pos.mfe_pct, pos.mae_pct, pos.peak_roi_pct, pos.trough_roi_pct,
                    pos.entry_target_price, pos.entry_filled_pct, pos.entry_status,
                    pos.entry_attempted_at_ms // 1000,
                    pos.detector_source, pos.confidence,
                    pos.binance_price_at_entry, pos.mexc_price_at_entry,
                    pos.mexc_lag_at_entry_pct,
                    pos.entry_fees_usdt, pos.exit_fees_usdt, pos.net_pnl_usdt,
                    pos.time_to_max_favorable_sec, pos.mode, pos.account_label,
                    # latency tracking (live only; shadow stores NULL)
                    pos.live_order_id, None,  # mexc_order_id_close (close-side TBD)
                    (pos.signal_to_pickup_ms + pos.live_open_latency_ms) or None,
                    pos.live_close_latency_ms or None,
                    pos.signal_to_pickup_ms or None,
                    pos.live_open_submit_ms or None,
                    pos.live_open_response_ms or None,
                    pos.live_open_fill_poll_ms or None,
                    pos.live_close_submit_ms or None,
                    pos.live_close_response_ms or None,
                    # peak_ticks snapshots (None if trade closed early
                    # or tick lookup failed in watch loop)
                    pos.peak_ticks_at_500ms,
                    pos.peak_ticks_at_1000ms,
                    pos.peak_ticks_at_1500ms,
                    pos.peak_ticks_at_2000ms,
                    pos.adverse_ticks_at_1000ms,
                ),
            )
            if pos.mode == "live":
                logger.info(
                    "Persisted LIVE trade to %s: %s %s pnl=$%.2f",
                    target_table, pos.symbol, pos.direction, pos.net_pnl_usdt or 0.0,
                )
        except Exception as e:
            logger.exception("Failed to persist trade (mode=%s) for %s: %s",
                             pos.mode, pos.symbol, e)

    # ============================================================
    # Pair config caching
    # ============================================================

    async def _get_pair_config(self, symbol: str) -> PairExecConfig:
        # TTL check is synchronous (no await on the hot path). When TTL
        # expires, reload runs as a background task; we return the stale
        # cache immediately. Config values change on the order of hours,
        # so TTL staleness is irrelevant for trade decisions.
        now = int(time.time())
        if now - self._configs_loaded_at >= self._configs_ttl_sec:
            # Single-flight: only spawn if no reload is currently running.
            if self._config_reload_task is None or self._config_reload_task.done():
                self._config_reload_task = asyncio.create_task(
                    self._background_reload_configs(),
                    name="pair_config_reload",
                )
        return self._pair_configs.get(symbol, PairExecConfig())

    async def _background_reload_configs(self) -> None:
        """Reload pair configs without blocking on_signal."""
        try:
            await self._reload_pair_configs()
        except Exception as e:
            logger.warning("Background pair_config reload failed: %s", e)

    def _sizing_should_warn(self, symbol: str, slot_id: int, fingerprint: str) -> bool:
        """True on a new or changed mismatch, otherwise at most twice an hour."""
        key = (symbol, slot_id)
        prev = self._sizing_warned.get(key)
        now = time.time()
        if prev is not None and prev[0] == fingerprint and now - prev[1] < 1800:
            return False
        self._sizing_warned[key] = (fingerprint, now)
        return True

    def _log_stop_units(self) -> None:
        """Який стоп в'яже зараз і за якого руху ціни це перемкнеться.

        stop_loss_ticks і stop_adverse_bps — одна величина у двох одиницях, у
        різних блоках YAML, з різним пріоритетом перевірки. Хто з них головний,
        залежить від ЦІНИ, тому це не можна записати коментарем у файл: на PENGU
        сьогодні різниця лише 1.2x, і рух ціни на ~19% міняє відповідь.
        """
        loader = getattr(self, "_config_loader", None)
        if loader is None or self.state_manager is None:
            return
        seen = getattr(self, "_stop_units_seen", None)
        if seen is None:
            seen = self._stop_units_seen = {}
        for symbol, cfg in list(self._pair_configs.items()):
            try:
                if not self.state_manager.is_in_live(symbol):
                    continue
                sl_t = float(getattr(cfg, "stop_loss_ticks", 0) or 0)
                es = loader.get(symbol).exit_strategy
                adv_bps = float(getattr(es, "stop_adverse_bps", 0) or 0)
                if sl_t <= 0 or adv_bps <= 0:
                    continue
                ob = self.ob_manager.get("mexc", symbol)
                bb = ob.best_bid() if ob else None
                ba = ob.best_ask() if ob else None
                if bb is None or ba is None:
                    continue
                mid = (float(bb.price) + float(ba.price)) / 2
                mxsym = to_mexc(symbol)
                tick = get_tick_size(mxsym) * get_binance_scale(mxsym)
                if mid <= 0 or tick <= 0:
                    continue
                tick_bps = tick / mid * 1e4
                adv_t = adv_bps / tick_bps          # bps-стоп у тіках
                binds = "ticks" if sl_t <= adv_t else "bps"
                # ціна, на якій два стопи зрівняються
                flip = sl_t * tick * 1e4 / adv_bps
                key = (binds, round(adv_t, 1))
                if seen.get(symbol) == key:
                    continue
                seen[symbol] = key
                logger.info(
                    "[STOPS] %s: в'яже %s | stop_loss_ticks=%.0f (=%.2f bps) "
                    "після sl_grace %.1fс; stop_adverse_bps=%.1f (=%.1ft) діє "
                    "лише у вікні min_hold..sl_grace. Порівняються при ціні "
                    "%.8g (%+.1f%% від %.8g).",
                    symbol, "ТІКИ" if binds == "ticks" else "BPS",
                    sl_t, sl_t * tick_bps, float(getattr(cfg, "sl_grace_sec", 0) or 0),
                    adv_bps, adv_t, flip, (flip / mid - 1) * 100, mid,
                )
            except Exception:
                continue

    async def _warn_on_sizing_drift(self) -> None:
        """Shout when a live pair's YAML sizing is not what its slots use.

        The pair YAML is the real fallback for any slot without a
        slot_pair_sizing row, but nobody re-reads it once per-slot overrides
        exist: on 2026-07-28 PEPE declared $5,231 while its slots ran
        $2,491-$2,755, so deleting one override would have silently doubled the
        position. Freezing the YAML to today's number would only move the
        staleness — the operator retunes size from Telegram — so instead make
        the divergence impossible to miss on every config reload.
        """
        if self.live_pool is None or self.state_manager is None:
            return
        try:
            for symbol, cfg in list(self._pair_configs.items()):
                if not self.state_manager.is_in_live(symbol):
                    continue
                yaml_n = ((cfg.margin_min_usdt + cfg.margin_max_usdt) / 2
                          * (cfg.leverage_min + cfg.leverage_max) / 2)
                for sid in self.live_pool.find_slots_for_pair(symbol):
                    slot_cfg = await self.live_pool.get_slot_config(sid, symbol=symbol)
                    if slot_cfg is None:
                        continue
                    mmin = slot_cfg.get("slot_margin_min_usdt")
                    mmax = slot_cfg.get("slot_margin_max_usdt")
                    lmin = slot_cfg.get("slot_leverage_min")
                    lmax = slot_cfg.get("slot_leverage_max")
                    if mmin is None and mmax is None and lmin is None and lmax is None:
                        if self._sizing_should_warn(symbol, sid, f"noovr:{yaml_n:.0f}"):
                            logger.warning(
                                "[SIZING] %s slot=%d has NO per-slot override — it "
                                "sizes straight from the YAML ($%.0f notional). "
                                "Check that is intended.",
                                symbol, sid, yaml_n)
                        continue
                    _em0 = mmin if mmin is not None else cfg.margin_min_usdt
                    _em1 = mmax if mmax is not None else cfg.margin_max_usdt
                    _el0 = lmin if lmin is not None else cfg.leverage_min
                    _el1 = lmax if lmax is not None else cfg.leverage_max
                    eff = (_em0 + _em1) / 2 * (_el0 + _el1) / 2
                    # ПОЛЕ В ПОЛЕ, не середній нотіонал з допуском 25%.
                    # Маржа вгору + плече вниз дають майже той самий нотіонал і
                    # гасять одне одного, хоча дистанція до ліквідації міняється:
                    # 97.5x47.5 проти ямлових 77.5x67.5 — це 11.5% по нотіоналу
                    # (сторож мовчав) і 1.42x по плечу.
                    _diff = [d for d in (
                        ("margin_min", _em0, cfg.margin_min_usdt),
                        ("margin_max", _em1, cfg.margin_max_usdt),
                        ("leverage_min", _el0, cfg.leverage_min),
                        ("leverage_max", _el1, cfg.leverage_max),
                    ) if abs(float(d[1]) - float(d[2])) > 1e-9]
                    if _diff and self._sizing_should_warn(
                            symbol, sid, "|".join(f"{n}{a}/{b}" for n, a, b in _diff)):
                        logger.warning(
                            "[SIZING] %s slot=%d: БД slot_pair_sizing розходиться з "
                            "YAML по %s. Торгує $%.0f нотіоналу, YAML каже $%.0f. "
                            "Видалення оверрайду (кнопка ↺ reset) перекине слот на "
                            "ямлові числа.",
                            symbol, sid,
                            ", ".join(f"{n} {a:g}≠{b:g}" for n, a, b in _diff),
                            eff, yaml_n)
        except Exception:
            logger.exception("[SIZING] drift check failed")

    async def _reload_pair_configs(self) -> None:
        # The DB pair_configs table defines only WHICH symbols exist (+ a few
        # metadata cols). The live authority is pair_states.state (is_in_live);
        # all STATIC TUNING (sizing, ioc, filters, stops, cooldowns) comes from
        # YAML via ConfigLoader — ONE place to edit a pair.
        rows = await self.db.fetchall("SELECT * FROM pair_configs")
        loader = getattr(self, "_config_loader", None)
        if loader is None:
            # ConfigLoader is ALWAYS wired in prod (main.py:545); absent only in
            # unit tests. The legacy DB tuning columns were dropped 2026-06-15, so
            # every field resolves to its PairExecConfig default (mode="shadow").
            self._pair_configs = {r["symbol"]: PairExecConfig() for r in rows}
            self._configs_loaded_at = int(time.time())
            return
        try:
            loader.maybe_reload()  # pick up YAML edits (hot-reload)
        except Exception as e:
            logger.warning("ConfigLoader.maybe_reload failed: %s", e)
        cache: dict[str, PairExecConfig] = {}
        for r in rows:
            symbol = r["symbol"]
            # Tuning from YAML (single source of truth). mode is a legacy label
            # (pair_configs.mode dropped) — unused by trading logic.
            e = loader.get(symbol).execution
            cache[symbol] = PairExecConfig(
                ioc_offset_ticks=e.ioc_offset_ticks,
                ioc_max_attempts=e.ioc_max_attempts,
                ioc_attempt_interval_ms=e.ioc_attempt_interval_ms,
                margin_min_usdt=e.margin_min_usdt,
                margin_max_usdt=e.margin_max_usdt,
                leverage_min=e.leverage_min,
                leverage_max=e.leverage_max,
                stop_loss_ticks=e.stop_loss_ticks,
                sl_grace_sec=e.sl_grace_sec,
                max_hold_sec=e.max_hold_sec,
                cooldown_after_loss_sec=e.cooldown_after_loss_sec,
                cooldown_after_win_sec=e.cooldown_after_win_sec,
                mode="shadow",
                binance_reversal_ticks=e.binance_reversal_ticks,
                gap_retrace_frac=e.gap_retrace_frac,
                momentum_filter=e.momentum_filter,
                momentum_tau_sec=e.momentum_tau_sec,
                momentum_threshold_bps=e.momentum_threshold_bps,
                min_mexc_lag_pct=e.min_mexc_lag_pct,
                max_mexc_lag_pct=e.max_mexc_lag_pct,
            )
        self._pair_configs = cache
        self._configs_loaded_at = int(time.time())
        # Best-effort: a stale YAML sizing must never be able to fail a reload.
        try:
            await self._warn_on_sizing_drift()
        except Exception:
            logger.exception("[SIZING] drift check raised")
        try:
            self._log_stop_units()
        except Exception:
            logger.exception("[STOPS] unit check raised")

    # ============================================================
    # Public introspection (for stats logger)
    # ============================================================

    def diagnostics(self) -> dict:
        return {
            "received": self.signals_received,
            "attempted": self.entries_attempted,
            "filled": self.entries_filled,
            "partial": self.entries_partial,
            "expired": self.entries_expired,
            "skip_not_tradeable": self.signals_skipped_not_tradeable,
            "skip_cooldown": self.signals_skipped_cooldown,
            "skip_funding": self.signals_skipped_funding,
            "skip_low_conf": self.signals_skipped_low_confidence,
            "skip_lag": self.signals_skipped_lag_out_of_range,
            "skip_max_pos": self.signals_skipped_max_positions,
            "skip_momentum": self.signals_skipped_momentum,
            "skip_no_book": self.signals_skipped_no_book,
            "skip_latency_drift": self.signals_skipped_latency_drift,
            "open_count": sum(len(v) for v in self._open_positions.values()),
            "closed_count": self.positions_closed,
        }
