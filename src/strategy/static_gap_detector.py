"""
Static Gap Detector — emits signals when MEXC lags Binance on same side
of the book.

Strategy:
  Continuously compute SAME-SIDE lag between Binance and MEXC books for
  each symbol. When MEXC bid lags Binance bid by N ticks → LONG (MEXC
  will catch up). When MEXC ask lags Binance ask by N ticks → SHORT.

Signal generation (event-driven):
  For each symbol with a synced Binance + MEXC orderbook, on any book update:
    long_gap_ticks  = (binance_bid - mexc_bid) / tick_scaled
    short_gap_ticks = (mexc_ask    - binance_ask) / tick_scaled

    LONG  if long_gap_ticks  >= min_gap_ticks  (MEXC bid behind Binance bid)
    SHORT if short_gap_ticks >= min_gap_ticks  (MEXC ask behind Binance ask)

  Execution: IOC LIMIT into the OPPOSITE side at the first level (top of book).

  Cooldown per symbol prevents spamming when gap remains open across
  multiple scans — we emit ONCE per gap-opening, then wait for it to
  close before emitting again.

Output: a Signal pushed to SignalWriter with source='static_gap'.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from src.exchanges.orderbook import OrderBookManager
from src.execution.live_executor import get_tick_size
from src.exchanges.mexc_rest import to_mexc, get_binance_scale
from src.strategy.signal import Signal, SignalWriter

logger = logging.getLogger(__name__)




@dataclass
class StaticGapConf:
    """Config for the static gap detector. Loaded from environment."""
    enabled: bool = False
    scan_interval_sec: float = 0.2     # how often we scan all books (safety-net)
    cooldown_sec: float = 5.0          # min seconds between signals on same symbol

    # Minimum gap in tick units to consider a signal.
    min_gap_ticks: int = 1


@dataclass
class PerPairDetectorOverride:
    """Per-pair detector parameter overrides.

    All fields are Optional (None = use global default). Resolution order:
        1. Per-pair value (if not None)
        2. Global value from StaticGapConf
    """
    min_gap_ticks: int | None = None
    cooldown_sec: float | None = None
    long_only: bool = False
    short_only: bool = False
    max_spread_bps: float = 0.0
    min_mid_gap_ticks: float = 0.0
    min_exec_ticks: float = 0.0


@dataclass
class _SymbolFastPath:
    """Precomputed per-symbol constants for the hot detection loop.

    Avoids repeated to_mexc() + dict lookups + float multiplies on every
    _check_symbol call. Rebuilt on pair_overrides reload.
    """
    tick_scaled: float       # tick_raw * binance_scale
    min_gap_ticks: int       # effective (pair override or global)
    cooldown_ms: int         # effective cooldown in milliseconds
    gap_eps: float = 1e-9    # FP epsilon for tick comparison


class StaticGapDetector:
    """
    Emits a signal when MEXC lags Binance on the SAME side of the book by
    at least min_gap_ticks:
        long_gap_ticks  = (binance_bid - mexc_bid) / tick_scaled   → LONG
        short_gap_ticks = (mexc_ask    - binance_ask) / tick_scaled → SHORT
    The tick gap is the ONLY entry condition (no mid_gap / exec_edge filter).
    Runs event-driven on order-book updates (with a periodic safety-net
    rescan) and applies a per-symbol cooldown so it emits once per
    gap-opening rather than on every book tick.
    """

    def __init__(
        self,
        cfg: StaticGapConf,
        ob_manager: OrderBookManager,
        signal_writer: SignalWriter,
        reference_only_symbols: set[str] | None = None,
        db = None,           # DB per-pair config fallback (used when config_loader has no value; wired from main.py)
        config_loader = None,  # YAML-based per-pair config (preferred)
        pair_config_ttl_sec: float = 30.0,
    ) -> None:
        self.cfg = cfg
        self.ob_manager = ob_manager
        self.signal_writer = signal_writer
        self.reference_only = reference_only_symbols or set()

        # Cooldown: symbol → next-allowed-emit timestamp (ms)
        self._cooldown_until: dict[str, int] = {}

        # Direction last emitted per symbol (so we don't re-emit same direction
        # while gap stays open). Cleared when gap closes.
        self._last_emitted_direction: dict[str, str] = {}

        # FILLWATCH instrumentation (env FILLWATCH=1, log-only): after an
        # at-touch signal, measure how long the MEXC touch price stays
        # fillable before it moves past our limit. Answers "how many ms am I
        # short to fill" = our_submit_latency - touch_survival_ms.
        # symbol → (armed_perf_ns, limit_price, direction)
        self._fillwatch_enabled = os.environ.get("FILLWATCH", "0") == "1"
        self._fillwatch: dict[str, tuple[int, float, str]] = {}

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        # Diagnostics
        self.scans_run = 0
        self.signals_emitted = 0
        self.signals_skip_cooldown = 0
        self.signals_skip_no_ob = 0
        self.signals_skip_below_threshold = 0
        self.signals_skip_wide_spread = 0
        self.signals_skip_narrow_mid_gap = 0
        self.signals_skip_no_exec_edge = 0
        self.signals_skip_reference_only = 0

        # ─── event-driven scan loop ─────────────────────────────────────
        # OrderBook listeners on both Binance and MEXC books mark symbols
        # dirty when they update; scan loop wakes on _book_dirty_event,
        # drains the dirty set, checks only those symbols.
        # scan_interval_sec is a SAFETY-NET wakeup (periodic full scan in
        # case listeners are missed or detector starts before WS subscribes).
        #
        # Stored symbols are in BINANCE format (e.g. "PENGUUSDT"), matching
        # the rest of the detector. MEXC listener converts via to_binance()
        # before adding to the dirty set.
        self._book_dirty_event: asyncio.Event | None = None  # lazy-init in start()
        self._dirty_symbols: set[str] = set()
        # (exchange, native_symbol) — tracks which OBs already have a listener
        # registered, so we don't double-register on safety scans.
        self._registered_books: set[tuple[str, str]] = set()
        self.event_driven_checks = 0   # symbols processed via dirty-set path
        self.safety_scan_count = 0     # times the timeout fallback fired

        # ─── detection-latency metrics ──────────────────────────────────
        # Per-symbol timestamp of when it FIRST became dirty within the
        # current burst (setdefault — first writer wins, so a flood of
        # updates from one burst yields a single measurement against the
        # earliest update). Popped atomically with the drain in _scan_loop.
        # Units: nanoseconds from time.perf_counter_ns().
        self._dirty_timestamps: dict[str, int] = {}
        # Ring buffer of latency samples in microseconds. No mutex needed
        # (single-threaded asyncio).
        self._latency_samples: deque = deque(maxlen=1000)
        self._metric_log_task: asyncio.Task | None = None

        # Per-pair detector config cache. Refreshes every pair_config_ttl_sec.
        # Empty dict means "no overrides" (use global cfg for everything).
        # Prefers config_loader (YAML) when provided; falls back to DB.
        self._db = db
        self._config_loader = config_loader
        self._pair_overrides: dict[str, PerPairDetectorOverride] = {}
        self._pair_overrides_loaded_at: float = 0.0
        self._pair_config_ttl_sec = pair_config_ttl_sec

        # Precomputed per-symbol constants. Rebuilt on each pair_overrides
        # reload. Avoids to_mexc() + get_tick_size() + get_binance_scale() +
        # float multiply on every _check_symbol call.
        self._fast_paths: dict[str, _SymbolFastPath] = {}

    async def start(self) -> None:
        if not self.cfg.enabled:
            logger.info("StaticGapDetector disabled in config")
            return
        try:
            await self._reload_pair_overrides()
            self._rebuild_fast_paths()
        except Exception as e:
            # Don't block startup if initial reload fails — TTL refresh
            # in scan loop will retry. Just log.
            logger.warning("Initial pair_overrides load failed: %s", e)
        self._task = asyncio.create_task(self._scan_loop(), name="static_gap_detector")
        logger.info(
            "StaticGapDetector started (min_gap=%dt interval=%.2fs cooldown=%.1fs)",
            self.cfg.min_gap_ticks,
            self.cfg.scan_interval_sec,
            self.cfg.cooldown_sec,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._metric_log_task:
            self._metric_log_task.cancel()
            try:
                await self._metric_log_task
            except (asyncio.CancelledError, Exception):
                pass

    # ─── per-pair config cache ──────────────────────────────────────────
    async def _reload_pair_overrides(self) -> None:
        """Reload per-pair detector overrides from YAML (via ConfigLoader).

        ConfigLoader is ALWAYS wired in prod (main.py). It is absent only in
        unit tests, in which case this is a no-op: the legacy pair_configs.gap_*
        DB override columns were dropped 2026-06-15, so there is nothing to load.
        """
        if self._config_loader is not None:
            self._reload_pair_overrides_from_yaml()
            return
        # No ConfigLoader (tests only): nothing to load. No-op.
        self._pair_overrides_loaded_at = time.monotonic()

    def _reload_pair_overrides_from_yaml(self) -> None:
        """Per-pair overrides from YAML files via ConfigLoader.

        Unlike the DB path, ConfigLoader resolves global+pair inheritance
        internally and returns fully-resolved PairConfig objects. We extract
        only the fields the detector cares about.
        """
        # Refresh loader's view of disk
        try:
            self._config_loader.maybe_reload()
        except Exception as e:
            logger.warning("ConfigLoader.maybe_reload failed: %s", e)

        new_overrides: dict[str, PerPairDetectorOverride] = {}
        for symbol in self._config_loader.list_pairs():
            pc = self._config_loader.get(symbol)
            d = pc.detector
            ovr = PerPairDetectorOverride(
                min_gap_ticks=d.min_ticks,
                cooldown_sec=d.cooldown_sec,
                long_only=d.long_only,
                short_only=d.short_only,
                max_spread_bps=d.max_spread_bps,
                min_mid_gap_ticks=d.min_mid_gap_ticks,
                min_exec_ticks=d.min_exec_ticks,
            )
            new_overrides[symbol] = ovr

        self._pair_overrides = new_overrides
        self._pair_overrides_loaded_at = time.monotonic()

    async def _maybe_reload_overrides(self) -> None:
        """Refresh per-pair cache if TTL expired. Bails when neither DB
        nor config_loader is present.
        """
        if self._db is None and self._config_loader is None:
            return
        if (time.monotonic() - self._pair_overrides_loaded_at) >= self._pair_config_ttl_sec:
            await self._reload_pair_overrides()
            self._rebuild_fast_paths()

    def _eff_min_gap_ticks(self, symbol: str) -> int:
        o = self._pair_overrides.get(symbol)
        if o and o.min_gap_ticks is not None:
            return o.min_gap_ticks
        return self.cfg.min_gap_ticks

    def _eff_cooldown_sec(self, symbol: str) -> float:
        o = self._pair_overrides.get(symbol)
        if o and o.cooldown_sec is not None:
            return o.cooldown_sec
        return self.cfg.cooldown_sec

    def _get_fast_path(self, symbol: str) -> _SymbolFastPath | None:
        """O(1) lookup of precomputed per-symbol constants.

        Returns None if symbol isn't in the fast-path cache (e.g. new pair
        just appeared, cache not yet rebuilt). Caller falls back to the
        slow path (to_mexc + dict lookups) on None.
        """
        return self._fast_paths.get(symbol)

    def _rebuild_fast_paths(self) -> None:
        """Precompute per-symbol constants after overrides reload.

        Called from _maybe_reload_overrides (on TTL refresh) and from
        start(). Avoids to_mexc() + get_tick_size() + get_binance_scale()
        + float multiply on every _check_symbol invocation.
        """
        new: dict[str, _SymbolFastPath] = {}
        for binance_symbol in self.ob_manager.all_symbols("binance"):
            mexc_symbol = to_mexc(binance_symbol)
            tick = get_tick_size(mexc_symbol)
            if tick <= 0:
                continue
            scale = get_binance_scale(mexc_symbol)
            tick_scaled = tick * scale if scale > 0 else tick
            new[binance_symbol] = _SymbolFastPath(
                tick_scaled=tick_scaled,
                min_gap_ticks=self._eff_min_gap_ticks(binance_symbol),
                cooldown_ms=int(self._eff_cooldown_sec(binance_symbol) * 1000),
            )
        self._fast_paths = new

    async def _scan_loop(self) -> None:
        """Event-driven main loop.

        Wakes on either:
          (a) self._book_dirty_event being set by an OB listener (fast path),
          (b) cfg.scan_interval_sec timeout (safety-net path).

        Drains _dirty_symbols (event path) or the full symbol universe
        (safety path) into per-symbol checks. Listener registration is
        lazy: each iteration ensures every currently-known symbol has
        listeners on both its Binance and MEXC books.

        The safety-net wakeup guarantees correctness if:
          - WS subscribed books after detector start (no listener yet)
          - a listener was somehow missed
          - new pairs were added at runtime

        Detection-latency metrics: timestamps are popped atomically with
        the drain so listener firing between drain and check leaves a
        clean slate for the next iteration. Latency is measured only for
        the event-driven path (safety-net has no trigger baseline).
        """
        # Lazy-init event in the current event loop (don't capture wrong loop
        # if __init__ ran in a different thread/loop context).
        self._book_dirty_event = asyncio.Event()
        # Start periodic metric log task.
        self._metric_log_task = asyncio.create_task(
            self._metric_log_loop(), name="static_gap_metric_log"
        )
        # Bootstrap listeners once up-front; thereafter re-scan only on the
        # safety-net (timeout) tick. New books appear via universe changes,
        # which the full safety scan picks up within scan_interval_sec — so
        # keeping registration off the per-wakeup path removes an
        # O(universe) set-scan from every book-update batch (the hot path).
        self._register_book_listeners_if_needed()
        try:
            while not self._stop.is_set():
                # Wait for either a book update or the safety-net timeout.
                try:
                    await asyncio.wait_for(
                        self._book_dirty_event.wait(),
                        timeout=self.cfg.scan_interval_sec,
                    )
                    # Event-driven wakeup — process only dirty symbols.
                    self._book_dirty_event.clear()
                    symbols_to_check = self._dirty_symbols.copy()
                    # Pop timestamps atomically — listener firing between
                    # now and check_symbol leaves a clean slate for next iter.
                    timestamps_to_check = {
                        sym: self._dirty_timestamps.pop(sym, None)
                        for sym in symbols_to_check
                    }
                    self._dirty_symbols.clear()
                    safety_mode = False
                except asyncio.TimeoutError:
                    # Safety-net wakeup — scan the full universe. Also the
                    # cheap place to register listeners for any newly-appeared
                    # books (off the per-batch event-driven hot path).
                    self._register_book_listeners_if_needed()
                    self.safety_scan_count += 1
                    symbols_to_check = set(self.ob_manager.all_symbols("binance"))
                    # Don't bypass dirty set — process any pending event-driven
                    # symbols in the same iteration to avoid losing them.
                    symbols_to_check |= self._dirty_symbols
                    # Only event-driven symbols carry timestamps; pull them.
                    # No need to build an intersection set — pop(sym, None)
                    # already tolerates absence, and `if sym in dict` is O(1).
                    timestamps_to_check = {
                        sym: self._dirty_timestamps.pop(sym, None)
                        for sym in symbols_to_check
                        if sym in self._dirty_timestamps
                    }
                    self._dirty_symbols.clear()
                    self._book_dirty_event.clear()
                    safety_mode = True

                if not symbols_to_check:
                    continue

                try:
                    await self._maybe_reload_overrides()
                    now_ms = int(time.time() * 1000)
                    for symbol in symbols_to_check:
                        # Detection latency: from listener-stamped dirty
                        # timestamp to the moment we begin checking. Only
                        # measured for event-driven path (safety-net has
                        # no trigger timestamp by definition).
                        t_dirty = timestamps_to_check.get(symbol)
                        if t_dirty is not None and not safety_mode:
                            latency_us = (time.perf_counter_ns() - t_dirty) / 1000.0
                            self._latency_samples.append(latency_us)
                        await self._check_symbol(symbol, now_ms)
                        if not safety_mode:
                            self.event_driven_checks += 1
                    if safety_mode:
                        # scans_run counts full passes only
                        self.scans_run += 1
                except Exception as e:
                    logger.exception("Static gap scan iteration error: %s", e)
        except asyncio.CancelledError:
            return

    def _register_book_listeners_if_needed(self) -> None:
        """Lazy-register OB listeners for any symbol we've not seen before.

        Listeners are sync (per OrderBook contract), fast, and isolated by
        per-listener try/except in _notify_listeners — a broken listener
        cannot break the WS apply path.

        NOTE: MEXC books in this codebase are keyed by BINANCE-FORMAT symbol
        ("ZECUSDT"), not MEXC native ("ZEC_USDT"). See mexc_ws.py:322 where
        get_or_create("mexc", binance_sym, ...) is called. We follow the same
        convention here.
        """
        for binance_symbol in self.ob_manager.all_symbols("binance"):
            key_b = ("binance", binance_symbol)
            if key_b not in self._registered_books:
                b_ob = self.ob_manager.get("binance", binance_symbol)
                if b_ob is not None:
                    b_ob.add_listener(self._make_listener(binance_symbol))
                    self._registered_books.add(key_b)
            key_m = ("mexc", binance_symbol)
            if key_m not in self._registered_books:
                m_ob = self.ob_manager.get("mexc", binance_symbol)
                if m_ob is not None:
                    m_ob.add_listener(self._make_listener(binance_symbol))
                    self._registered_books.add(key_m)

    def _make_listener(self, binance_symbol: str):
        """Build a sync listener bound to a specific binance-format symbol.

        Returned closure marks the symbol dirty and signals the scan loop.
        Idempotent: set() on an already-set event is a no-op, add() on a
        set member is a no-op. Both are O(1).

        Records first-dirty timestamp via setdefault — preserves the
        earliest stamp within a burst (multiple updates between drains),
        so the measured latency reflects the worst-case staleness of any
        information acted on, not the most recent.
        """
        def _listener(_ob) -> None:
            self._dirty_symbols.add(binance_symbol)
            self._dirty_timestamps.setdefault(binance_symbol, time.perf_counter_ns())
            if self._book_dirty_event is not None:
                self._book_dirty_event.set()
        _listener.__name__ = f"static_gap_listener[{binance_symbol}]"
        return _listener

    async def _check_symbol(self, symbol: str, now_ms: int) -> None:
        """Run gap-detection logic for a single symbol.

        Called from both the event-driven path (one symbol) and the
        safety-net path (full universe).
        """
        # FP epsilon: float math on tiny tick sizes (e.g. 1e-6) can
        # produce 2.9999...95 when the true gap is exactly 3 ticks.
        # 1e-9 is six orders of magnitude smaller than one tick, so it
        # only catches FP-induced near-misses, never broadens real ranges.
        gap_eps = 1e-9

        if symbol in self.reference_only:
            self.signals_skip_reference_only += 1
            return

        binance_ob = self.ob_manager.get("binance", symbol)
        mexc_ob = self.ob_manager.get("mexc", symbol)
        if (
            binance_ob is None or not binance_ob.is_synced or
            mexc_ob is None or not mexc_ob.is_synced
        ):
            self.signals_skip_no_ob += 1
            return

        # Hot path: avoid the OrderBookLevel allocations that best_bid()/
        # best_ask() do. The *_price() accessors return float directly. This
        # runs on every OB update — for N pairs at 50–200 Hz each, the saved
        # allocations add up.
        b_bid_p = binance_ob.best_bid_price()
        b_ask_p = binance_ob.best_ask_price()
        m_bid_p = mexc_ob.best_bid_price()
        m_ask_p = mexc_ob.best_ask_price()
        if b_bid_p == 0.0 or b_ask_p == 0.0 or m_bid_p == 0.0 or m_ask_p == 0.0:
            self.signals_skip_no_ob += 1
            return

        # Corrupt-book guard: a crossed MEXC book (bid >= ask) is impossible
        # real data — a stale top level was stranded by the diff stream (see
        # OrderBook.is_crossed). Signalling off it manufactures a huge phantom
        # gap (HYPE: frozen 68.843 bid vs live 66.3 ask -> endless fake shorts,
        # +3.7% per shadow trade). Skip until the book self-heals.
        if m_bid_p >= m_ask_p:
            self.signals_skip_no_ob += 1
            return

        # The SAME guard for the Binance side. It was missing, and a crossed
        # Binance book is exactly as impossible: on 2026-07-27 01:33 Binance
        # showed bid=0.0029540 above ask=0.0029230 (310 ticks crossed) while
        # MEXC was sane, so short_gap = m_bid - b_ask manufactured a 311-tick
        # phantom signal — 60x a normal PEPE gap — and a real $2977 live SHORT
        # was opened on it. Skip until the feed self-heals.
        if b_bid_p >= b_ask_p:
            self.signals_skip_no_ob += 1
            if not hasattr(self, "_bx_last") or time.time() - self._bx_last > 60:
                self._bx_last = time.time()
                logger.warning(
                    "[CROSSED BOOK] binance %s bid=%.8f >= ask=%.8f — skipping signals",
                    symbol, b_bid_p, b_ask_p,
                )
            return

        # FILLWATCH: if armed for this symbol, check whether the MEXC touch
        # price has moved past our at-touch limit (= no longer fillable).
        # Logs touch_survival_ms — how long we had to fill before the move.
        if self._fillwatch_enabled and symbol in self._fillwatch:
            _t0, _limit, _dir = self._fillwatch[symbol]
            _elapsed_ms = (time.perf_counter_ns() - _t0) / 1e6
            _breached = (m_ask_p > _limit) if _dir == "long" else (m_bid_p < _limit)
            if _breached:
                logger.info(
                    "[FILLWATCH] %s %s touch_survival=%.0fms (breached)",
                    symbol, _dir, _elapsed_ms,
                )
                del self._fillwatch[symbol]
            elif _elapsed_ms >= 500.0:
                logger.info(
                    "[FILLWATCH] %s %s touch_survival>=500ms (would_fill)",
                    symbol, _dir,
                )
                del self._fillwatch[symbol]

        # Precomputed fast-path (normal case) — falls back to per-call
        # computation only for symbols not yet in cache (e.g. pair just
        # appeared, cache rebuilds in ≤TTL).
        fp = self._get_fast_path(symbol)
        if fp is not None:
            tick_scaled = fp.tick_scaled
            eff_min_gap_ticks = fp.min_gap_ticks
            eff_cooldown_ms = fp.cooldown_ms
            gap_eps = fp.gap_eps
        else:
            mexc_symbol = to_mexc(symbol)
            tick = get_tick_size(mexc_symbol)
            if tick <= 0:
                return
            scale = get_binance_scale(mexc_symbol)
            tick_scaled = tick * scale if scale > 0 else tick
            eff_min_gap_ticks = self._eff_min_gap_ticks(symbol)
            eff_cooldown_ms = int(self._eff_cooldown_sec(symbol) * 1000)

        # ─── SAME-SIDE LAG FORMULA ─────────────────────────────────────
        #   long_gap_ticks  = (binance_bid - mexc_bid) / tick
        #     >0 → MEXC bid is behind Binance bid → bid will catch up → LONG
        #   short_gap_ticks = (mexc_ask - binance_ask) / tick
        #     >0 → MEXC ask is behind Binance ask → ask will catch up → SHORT
        #
        # Execution: IOC LIMIT into the OPPOSITE side at the first level
        # (top of book), crosses spread, eats the top resting order:
        #   LONG  → IOC BUY  @ mexc_ask (best ask)
        #   SHORT → IOC SELL @ mexc_bid (best bid)
        # Order placement controlled per-pair by pair_configs.ioc_offset_ticks:
        #   0  = at touch (taker fill at current best)
        #   +N = cross N ticks deeper into book (aggressive)
        #   -N = N ticks inside spread (maker-style passive limit)
        #
        # Filters: cooldown only (anti-duplicate).

        # Midprice computations are kept for downstream logging only;
        # they are NOT used in any decision.
        b_mid = (b_bid_p + b_ask_p) / 2
        m_mid = (m_bid_p + m_ask_p) / 2

        long_gap_ticks  = (b_bid_p - m_bid_p) / tick_scaled
        short_gap_ticks = (m_ask_p - b_ask_p) / tick_scaled

        # exec_buy/sell kept for logging (informational).
        exec_buy_ticks  = (b_bid_p - m_ask_p) / tick_scaled
        exec_sell_ticks = (m_bid_p - b_ask_p) / tick_scaled

        # Direction selection. Both directions could theoretically fire
        # at once if both sides of MEXC lag Binance simultaneously
        # (rare, possible during fast Binance moves). Pick the larger lag.
        direction: str | None = None
        gap_ticks = 0.0

        long_qualifies  = long_gap_ticks  >= eff_min_gap_ticks - gap_eps
        short_qualifies = short_gap_ticks >= eff_min_gap_ticks - gap_eps

        if long_qualifies and short_qualifies:
            if long_gap_ticks >= short_gap_ticks:
                direction = "long"
                gap_ticks = long_gap_ticks
            else:
                direction = "short"
                gap_ticks = short_gap_ticks
        elif long_qualifies:
            direction = "long"
            gap_ticks = long_gap_ticks
        elif short_qualifies:
            direction = "short"
            gap_ticks = short_gap_ticks

        if direction is None:
            self.signals_skip_below_threshold += 1
            self._last_emitted_direction.pop(symbol, None)
            return

        # Per-pair direction filter: long_only suppresses short signals.
        if direction == "short":
            ovr = self._pair_overrides.get(symbol)
            if ovr is not None and ovr.long_only:
                self.signals_skip_below_threshold += 1
                self._last_emitted_direction.pop(symbol, None)
                return

        # short_only suppresses long signals (mirror of long_only).
        if direction == "long":
            ovr = self._pair_overrides.get(symbol)
            if ovr is not None and getattr(ovr, "short_only", False):
                self.signals_skip_below_threshold += 1
                self._last_emitted_direction.pop(symbol, None)
                return

        # Spread-cap gate: reject when the MEXC book is too wide to fill
        # cleanly (ported from primary 2026-06-21). Per-pair max_spread_bps; 0=off.
        _ovr_sp = self._pair_overrides.get(symbol)
        if _ovr_sp is not None and _ovr_sp.max_spread_bps > 0 and m_mid > 0:
            _spread_bps = (m_ask_p - m_bid_p) / m_mid * 1e4
            if _spread_bps > _ovr_sp.max_spread_bps:
                self.signals_skip_wide_spread += 1
                self._last_emitted_direction.pop(symbol, None)
                return

        # Mid-gap floor, in TICKS. min_ticks above reads the same-side quote
        # gap, which equals this plus half the excess MEXC spread — so it also
        # admits signals whose real dislocation is a tick smaller than it looks.
        # Measured on 2,831 TAO trades: the 4.0-tick mid cohort is 18.2% of the
        # flow, loses $29.80 at a 38% win rate and dies on arrival 51% of the
        # time, while every wider cohort earns and DOA falls monotonically to
        # 27%. Ticks, not bps: the mid lands on a half-tick grid and a bps
        # threshold would cut a different cohort as the price drifts.
        # 0 = off (default; behaviour-preserving).
        _ovr_mg = self._pair_overrides.get(symbol)
        if _ovr_mg is not None and _ovr_mg.min_mid_gap_ticks > 0 and tick_scaled > 0:
            if abs(b_mid - m_mid) / tick_scaled < _ovr_mg.min_mid_gap_ticks - gap_eps:
                self.signals_skip_narrow_mid_gap += 1
                self._last_emitted_direction.pop(symbol, None)
                return

        # Executable-edge floor, in TICKS. exec_buy/sell are computed above
        # and were previously logged only. They are the dislocation net of BOTH
        # spreads — what remains after crossing to the price we actually fill
        # at — so a genuine gap through a wide book passes min_ticks and
        # min_mid_gap_ticks and still has nothing left to catch.
        # Measured on PENGU (231 primary / 115 clone fills joined to signals):
        # exec -1 = -7.16/-8.07 bps, exec 0 = -1.74/-1.66, exec 1 = +1.35/-0.42,
        # exec 2 = +2.26/+0.74. The distribution is strictly integer-tick, so a
        # 0.5 floor sits in empty space (zero trades within +/-0.26 of it).
        # Ticks, not bps: a bps threshold slides off its target as the price
        # moves, which has already cost us twice.
        # 0 = off (default; behaviour-preserving).
        _ovr_ex = self._pair_overrides.get(symbol)
        if _ovr_ex is not None and _ovr_ex.min_exec_ticks > 0:
            _exec_t = exec_buy_ticks if direction == "long" else exec_sell_ticks
            if _exec_t < _ovr_ex.min_exec_ticks - gap_eps:
                self.signals_skip_no_exec_edge += 1
                self._last_emitted_direction.pop(symbol, None)
                return

        # ─── ONLY FILTER: cooldown (anti-duplicate guard) ──────────────
        cooldown_until = self._cooldown_until.get(symbol, 0)
        last_dir = self._last_emitted_direction.get(symbol)
        if now_ms < cooldown_until and last_dir == direction:
            self.signals_skip_cooldown += 1
            return

        # Confidence: scales linearly with how far above threshold.
        # gap == min_gap → 0.5, gap == 2*min_gap → 1.0
        confidence = min(1.0, abs(gap_ticks) / (eff_min_gap_ticks * 2))

        # mexc_lag_pct kept for downstream compatibility (signals/PnL log).
        # Informational only — not used as a filter.
        mexc_lag_pct = (b_mid - m_mid) / b_mid * 100 if b_mid > 0 else 0.0

        # Stamp signal creation time for end-to-end latency measurement
        # (signal → IOC submit overhead).
        t_signal_created = time.perf_counter()

        sig = Signal(
            symbol=symbol,
            direction=direction,
            source="static_gap",
            confidence=confidence,
            binance_price=b_mid,
            mexc_price=m_mid,
            binance_impulse_pct=0.0,
            mexc_lag_pct=mexc_lag_pct,
            metadata={
                "gap_ticks": round(gap_ticks, 2),
                "long_gap_ticks":  round(long_gap_ticks, 2),
                "short_gap_ticks": round(short_gap_ticks, 2),
                "min_gap_ticks": eff_min_gap_ticks,
                "mid_gap_ticks": round(abs(b_mid - m_mid) / tick_scaled, 2)
                                 if tick_scaled > 0 else 0.0,
                "binance_bid": b_bid_p,
                "binance_ask": b_ask_p,
                "mexc_bid":    m_bid_p,
                "mexc_ask":    m_ask_p,
                "binance_mid": b_mid,
                "mexc_mid":    m_mid,
                "exec_ticks":      round(exec_buy_ticks if direction == "long"
                                         else exec_sell_ticks, 2),
                "exec_buy_ticks":  round(exec_buy_ticks, 2),
                "exec_sell_ticks": round(exec_sell_ticks, 2),
                "t_signal_created": t_signal_created,
            },
        )
        await self.signal_writer.write(sig)
        self.signals_emitted += 1
        self._cooldown_until[symbol] = now_ms + eff_cooldown_ms
        self._last_emitted_direction[symbol] = direction

        # FILLWATCH: arm the touch-survival measurement for this at-touch
        # signal. limit = the price an at-touch IOC would rest at (long→ask,
        # short→bid). Measured against subsequent MEXC book updates above.
        if self._fillwatch_enabled:
            _lim = m_ask_p if direction == "long" else m_bid_p
            self._fillwatch[symbol] = (time.perf_counter_ns(), _lim, direction)

        logger.info(
            "[STATIC_GAP] %s %s gap=%+.2ft long_gap=%+.2ft short_gap=%+.2ft "
            "exec_buy=%+.2ft exec_sell=%+.2ft "
            "(b_bid=%.6f b_ask=%.6f m_bid=%.6f m_ask=%.6f) conf=%.2f",
            symbol, direction.upper(),
            gap_ticks, long_gap_ticks, short_gap_ticks,
            exec_buy_ticks, exec_sell_ticks,
            b_bid_p, b_ask_p, m_bid_p, m_ask_p,
            confidence,
        )

    async def _scan_once(self) -> None:
        """Back-compat wrapper for external callers. The main loop drives
        _check_symbol() directly per dirty symbol.
        """
        self.scans_run += 1
        await self._maybe_reload_overrides()
        now_ms = int(time.time() * 1000)
        for symbol in self.ob_manager.all_symbols("binance"):
            await self._check_symbol(symbol, now_ms)

    @staticmethod
    def _percentile(sorted_samples: list[float], pct: float) -> float:
        """Nearest-rank percentile on a pre-sorted list. pct in [0, 100]."""
        if not sorted_samples:
            return 0.0
        if pct <= 0:
            return sorted_samples[0]
        if pct >= 100:
            return sorted_samples[-1]
        idx = int(len(sorted_samples) * pct / 100.0)
        if idx >= len(sorted_samples):
            idx = len(sorted_samples) - 1
        return sorted_samples[idx]

    def _latency_summary(self) -> dict[str, float]:
        """Compute p50/p99/mean from current ring buffer. Returns 0s if empty."""
        if not self._latency_samples:
            return {"p50_us": 0.0, "p99_us": 0.0, "mean_us": 0.0, "count": 0}
        # Snapshot before sort (deque can be modified by listener but we
        # snapshot via list() under single-threaded asyncio).
        samples = sorted(self._latency_samples)
        n = len(samples)
        return {
            "p50_us":  self._percentile(samples, 50),
            "p99_us":  self._percentile(samples, 99),
            "mean_us": sum(samples) / n,
            "count":   n,
        }

    async def _metric_log_loop(self) -> None:
        """Periodic emission of detection latency metrics.

        Logs every 30s as `[DET_METRICS]`:
          - p50_us: median time between OB update and detector check
          - p99_us: worst case across recent window
          - safety vs event ratio: if safety >>> event_driven_checks, the
            listener path isn't carrying load — investigate registration
        """
        try:
            while not self._stop.is_set():
                await asyncio.sleep(30.0)
                summary = self._latency_summary()
                if summary["count"] == 0:
                    logger.info(
                        "[DET_METRICS] event=%d safety=%d samples=0 "
                        "(no latency data yet — listeners not firing?)",
                        self.event_driven_checks, self.safety_scan_count,
                    )
                    continue
                logger.info(
                    "[DET_METRICS] event=%d safety=%d samples=%d "
                    "latency_us p50=%.1f p99=%.1f mean=%.1f",
                    self.event_driven_checks, self.safety_scan_count,
                    summary["count"], summary["p50_us"],
                    summary["p99_us"], summary["mean_us"],
                )
        except asyncio.CancelledError:
            return

    def stats(self) -> dict[str, Any]:
        return {
            "scans_run": self.scans_run,
            "signals_emitted": self.signals_emitted,
            "signals_skip_cooldown": self.signals_skip_cooldown,
            "signals_skip_no_ob": self.signals_skip_no_ob,
            "signals_skip_below_threshold": self.signals_skip_below_threshold,
            "signals_skip_reference_only": self.signals_skip_reference_only,
            "event_driven_checks":    self.event_driven_checks,
            "safety_scan_count":      self.safety_scan_count,
            "registered_book_count":  len(self._registered_books),
            "detection_latency":      self._latency_summary(),
        }
