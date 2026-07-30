"""
Position reconciliation between MEXC exchange state and bot state.

This module addresses the **orphan position** failure class:
  - Bot crashes mid-trade → MEXC still has position, bot loses tracking
  - Watcher task dies silently → position abandoned, drifts unbounded
  - External close (liquidation, manual) → bot state goes stale

Two entry points:

  startup_reconcile(live_pool, alerts):
      Run ONCE at bot startup, after live_pool is initialized but BEFORE
      any new trading begins. Closes any MEXC position the bot has no
      in-memory state for. Safe to fail (logs + continues).

  periodic_reconcile_loop(shadow_engine, live_pool, alerts, interval_sec):
      Background asyncio task. Every `interval_sec` seconds, compares
      MEXC reality with bot's in-memory engine state. Handles two cases:
        A. MEXC has position bot doesn't track → ORPHAN → market-close
        B. Engine tracks position MEXC says is gone → STALE → mark closed
      Both with race protection (15s grace window) to avoid acting on
      transient just-opened / just-closed transitions.

Design choice: **close orphans rather than reconstruct**. Bot doesn't
remember strategy phase / peak_ticks / exit hypothesis for an orphan,
so attempting to "continue managing" it would lead to wrong decisions.
Closing immediately = predictable small slippage cost, vs unbounded
drift risk if we leave it untracked.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


# Race protection: ignore positions that were just opened/closed within
# this window. Prevents false-positive reconciliation actions on positions
# that are in the process of being opened by `on_signal` but haven't yet
# been added to engine state, or just closed but state not yet propagated.
_RECONCILE_GRACE_SEC = 15


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

async def _fetch_mexc_positions_for_slot(
    executor,
    slot_id: int,
    timeout_sec: float = 5.0,
) -> list[dict]:
    """Fetch open positions for a single slot. Returns empty list on failure."""
    try:
        client = await executor.client_pool.get(slot_id)
        if client is None:
            return []
        resp = await asyncio.wait_for(
            client.get_open_positions(), timeout=timeout_sec,
        )
        if resp.get("code") != 0:
            logger.warning(
                "[RECONCILE] slot=%d get_open_positions returned code=%s msg=%s",
                slot_id, resp.get("code"), resp.get("msg"),
            )
            return []
        return resp.get("data", []) or []
    except asyncio.TimeoutError:
        logger.warning("[RECONCILE] slot=%d get_open_positions TIMEOUT", slot_id)
        return []
    except Exception:
        logger.exception("[RECONCILE] slot=%d get_open_positions failed", slot_id)
        return []


def _num(v) -> float:
    """Coerce a possibly-None / non-numeric attr to float, defaulting 0.0.
    Shared by both orphan-close log paths (was duplicated inline twice)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _mexc_pos_to_close_args(mexc_pos: dict) -> tuple[str, str, int, int]:
    """Extract (symbol, direction, qty, leverage) from MEXC position dict.

    MEXC positionType: 1 = LONG, 2 = SHORT.
    holdVol is contracts (integer).
    """
    symbol = mexc_pos.get("symbol", "")
    pos_type = int(mexc_pos.get("positionType", 1) or 1)
    direction = "long" if pos_type == 1 else "short"
    qty = int(float(mexc_pos.get("holdVol", 0) or 0))
    leverage = int(mexc_pos.get("leverage", 50) or 50)
    return symbol, direction, qty, leverage


def _mexc_pos_age_sec(mexc_pos: dict) -> float:
    """Age of MEXC position in seconds. 0 if createTime unknown."""
    create_time = int(mexc_pos.get("createTime", 0) or 0)
    if create_time <= 0:
        return 0.0
    return (time.time() * 1000 - create_time) / 1000


async def _send_orphan_alert(alerts, action: str, mexc_pos: dict, result_msg: str) -> None:
    """Send Telegram alert about an orphan event. Best-effort, never raises."""
    if alerts is None:
        return
    try:
        symbol, direction, qty, lev = _mexc_pos_to_close_args(mexc_pos)
        await alerts.send(
            text=(
                f"🚨 <b>RECONCILE — {action}</b>\n\n"
                f"<b>Pair:</b> {symbol} {direction.upper()}\n"
                f"<b>Qty:</b> {qty} contracts\n"
                f"<b>Leverage:</b> {lev}x\n"
                f"<b>Result:</b> <i>{result_msg}</i>"
            ),
            category=f"reconcile_{symbol}_{int(time.time())}",
            throttle_sec=0,
            suppress_during_quiet=False,
        )
    except Exception:
        logger.exception("Failed to send reconcile alert")


# ────────────────────────────────────────────────────────────────────────
# Startup reconciliation
# ────────────────────────────────────────────────────────────────────────

async def startup_reconcile(live_pool, alerts) -> dict:
    """At bot startup: close any MEXC position bot has no record of.

    Fail-soft: if reconciliation can't complete (MEXC unreachable, etc.),
    log + continue. Bot remains usable in shadow mode at minimum.

    Returns dict with summary counts (for logging / tests):
        {
            "checked_slots": N,
            "found_positions": N,
            "closed_successfully": N,
            "close_failures": N,
        }
    """
    summary = {
        "checked_slots": 0,
        "found_positions": 0,
        "closed_successfully": 0,
        "close_failures": 0,
    }

    if live_pool is None:
        logger.info("[RECONCILE STARTUP] live_pool is None — skipping (shadow-only mode)")
        return summary

    executors = getattr(live_pool, "_executors", {})
    if not executors:
        logger.info("[RECONCILE STARTUP] no live executors configured — skipping")
        return summary

    logger.info("[RECONCILE STARTUP] checking %d slot(s) for unmanaged MEXC positions",
                len(executors))

    # Parallel fetch — each call is a ~600ms HTTP roundtrip. With 3 slots
    # that's 1.8s sequential vs ~600ms parallel.
    slot_items = list(executors.items())
    fetch_results = await asyncio.gather(
        *(_fetch_mexc_positions_for_slot(executor, slot_id)
          for slot_id, executor in slot_items),
        return_exceptions=False,
    )

    # Close orphans serially — close is account-state-mutating and small
    # in count (typically 0–1 per slot at startup), so parallelizing this
    # phase brings little benefit and adds reasoning overhead.
    for (slot_id, executor), mexc_positions in zip(slot_items, fetch_results):
        summary["checked_slots"] += 1
        if not mexc_positions:
            continue

        for mexc_pos in mexc_positions:
            summary["found_positions"] += 1
            symbol, direction, qty, lev = _mexc_pos_to_close_args(mexc_pos)
            age_sec = _mexc_pos_age_sec(mexc_pos)

            # On STARTUP, there's no race protection — bot has no in-memory
            # state regardless of position age. Every position found here is
            # by definition not managed by this fresh process.
            logger.error(
                "🚨 [RECONCILE STARTUP] slot=%d found unmanaged position: "
                "%s %s qty=%d lev=%dx age=%.0fs — closing via MARKET",
                slot_id, symbol, direction.upper(), qty, lev, age_sec,
            )

            if qty <= 0:
                logger.warning(
                    "[RECONCILE STARTUP] %s has zero qty — skipping market close",
                    symbol,
                )
                continue

            try:
                # Last check before touching the account. rebuild_from_store runs
                # on a timer, so a key deleted seconds ago may still look active in
                # the pool. Closing a position on an account the operator has
                # deliberately disconnected is the one outcome worth an extra
                # round-trip to avoid.
                _keyed = True
                _chk = getattr(live_pool, "slot_has_key", None)
                if callable(_chk):
                    try:
                        _r = _chk(slot_id)
                        if inspect.isawaitable(_r):
                            _keyed = bool(await _r)
                    except Exception:
                        # Cannot verify. active_executors() already vetted this
                        # slot, so trust that rather than stranding a real orphan.
                        _keyed = True
                if not _keyed:
                    logger.warning(
                        "[RECONCILE] slot=%s без вебкея — залишаю %s недоторканим "
                        "(позиція відкрита вручну, не наша)", slot_id, symbol)
                    continue
                result = await executor.market_close_position(
                    symbol=symbol,
                    direction=direction,
                    qty_contracts=qty,
                    leverage=lev,
                )
            except Exception:
                logger.exception(
                    "[RECONCILE STARTUP] market_close_position threw for %s",
                    symbol,
                )
                summary["close_failures"] += 1
                await _send_orphan_alert(alerts, "STARTUP close FAILED", mexc_pos, "exception")
                continue

            if result.success:
                summary["closed_successfully"] += 1
                exit_price = _num(getattr(result, "exit_price", 0))
                pnl = _num(getattr(result, "realized_pnl_usdt", 0))
                logger.info(
                    "[RECONCILE STARTUP] %s CLOSED: exit=%.6f pnl=$%+.4f",
                    symbol, exit_price, pnl,
                )
                _msg = (f"exit=${exit_price:.6f} realised=${pnl:+.4f}" if exit_price > 0
                        else "realised: n/a (MEXC history not settled — check account)")
                await _send_orphan_alert(alerts, "STARTUP close OK", mexc_pos, _msg)
            else:
                summary["close_failures"] += 1
                logger.error(
                    "[RECONCILE STARTUP] %s FAILED to close: %s — MANUAL ACTION NEEDED",
                    symbol, result.error_msg,
                )
                await _send_orphan_alert(
                    alerts, "STARTUP close FAILED", mexc_pos, str(result.error_msg),
                )

    logger.info("[RECONCILE STARTUP] complete: %s", summary)
    return summary


# ────────────────────────────────────────────────────────────────────────
# Periodic reconciliation
# ────────────────────────────────────────────────────────────────────────

def _slot_of(pos) -> int | None:
    """Slot a live position belongs to, or None when it cannot be read.

    The engine writes account_label as f"slot{sid}" immediately after a live
    open succeeds. Anything else (shadow positions, a malformed label) means we
    must not attribute the position to a slot — callers treat that as "unknown"
    and fall back to the safer symbol-wide behaviour.
    """
    label = getattr(pos, "account_label", None)
    if not isinstance(label, str) or not label.startswith("slot"):
        return None
    try:
        return int(label[4:])
    except ValueError:
        return None


async def reconcile_once(shadow_engine, live_pool, alerts) -> dict:
    """Single reconciliation pass. Called both from periodic loop and tests.

    Compares MEXC open positions vs engine's in-memory live positions.
    Handles two divergence cases with race protection.

    MEXC reality is keyed by (slot_id, symbol) instead of symbol alone.
    If two slots happen to have positions in the same symbol (which
    live_pool's docstring explicitly allows: "Multiple slots can be
    assigned to the same pair for parallel positions"), keying by symbol
    alone would let dict overwrite silently lose the earlier slot's
    orphan. The (slot_id, symbol) key preserves each slot's view
    independently.
    """
    summary = {
        "mexc_positions": 0,
        "engine_positions": 0,
        "orphans_closed": 0,
        "orphans_failed": 0,
        "stale_engine_marked": 0,
    }

    if live_pool is None:
        return summary

    # active_executors(), NOT _executors: the latter keeps deactivated slots for
    # their stats, and using it meant a slot whose webkey had been deleted was
    # still reconciled — closing positions the operator had opened by hand.
    executors = getattr(live_pool, "_executors", {})
    _active = getattr(live_pool, "active_executors", None)
    if callable(_active):
        try:
            _res = _active()
            # Only a real mapping counts. A test double answers callable() and
            # returns something dict-shaped only by accident.
            if isinstance(_res, dict):
                executors = _res
        except Exception:
            logger.exception("[RECONCILE] active_executors() кинуло — "
                             "працюю зі старим списком")
    if not executors:
        return summary

    # key by (slot_id, mexc_symbol). Each tuple is unique per
    # slot, so multiple slots holding positions in the same symbol no longer
    # collide. Both keys are needed downstream: slot_id to route the close
    # via the correct executor, symbol for the engine_by_mexc_symbol lookup.
    #
    # Parallelize the fetch fan-out: each _fetch_mexc_positions_for_slot
    # is a ~600ms HTTP roundtrip to MEXC. With 3 slots that's 1.8s
    # sequential; asyncio.gather brings it back to ~600ms wall-clock.
    # _fetch_* swallows all exceptions internally (returns []), so
    # return_exceptions=False is safe.
    mexc_by_key: dict[tuple[int, str], dict] = {}
    slot_items = list(executors.items())
    fetch_results = await asyncio.gather(
        *(_fetch_mexc_positions_for_slot(executor, slot_id)
          for slot_id, executor in slot_items),
        return_exceptions=False,
    )
    for (slot_id, _executor), positions in zip(slot_items, fetch_results):
        for mexc_pos in positions:
            sym = mexc_pos.get("symbol", "")
            if sym:
                mexc_by_key[(slot_id, sym)] = mexc_pos
    summary["mexc_positions"] = len(mexc_by_key)

    # Build engine reality. Multiple positions per binance symbol could in
    # theory exist if max_positions_per_symbol > 1; with current default of
    # 1 the list is always 0..1 elements. We collect ALL live-mode positions
    # into a multi-map keyed by mexc-symbol so Case A can match by symbol
    # alone (we don't always know which slot opened a given engine position
    # — pos.account_label encodes it but parsing is best-effort).
    # Defer import to avoid circular dependency
    from src.exchanges.mexc_rest import to_mexc

    # Set of mexc-symbols the engine considers live-active. Used by Case A
    # to decide "orphan vs known".
    engine_live_by_key: set[tuple] = set()
    # Symbols whose live position could NOT be attributed to a slot. Those keep
    # the old symbol-wide match: never close a position we cannot account for.
    engine_unattributed_syms: set[str] = set()
    # Map (slot | None, mexc_symbol) → list of ShadowPosition (Case B iteration).
    engine_positions_by_key: dict[tuple, list] = {}
    open_positions = getattr(shadow_engine, "_open_positions", {})
    for sym, poss in open_positions.items():
        for pos in poss:
            if (pos.mode == "live"
                    and pos.is_open
                    and not getattr(pos, "is_closing", False)):
                mexc_sym = to_mexc(sym)
                pos_slot = _slot_of(pos)
                if pos_slot is None:
                    engine_unattributed_syms.add(mexc_sym)
                else:
                    engine_live_by_key.add((pos_slot, mexc_sym))
                engine_positions_by_key.setdefault(
                    (pos_slot, mexc_sym), []).append(pos)
    summary["engine_positions"] = sum(
        len(v) for v in engine_positions_by_key.values()
    )

    # ── Case A: MEXC has it, engine doesn't → ORPHAN
    # Iterate per-(slot, symbol) so multi-slot orphans on the same symbol
    # are each handled independently.
    for (slot_id, mexc_sym), mexc_pos in mexc_by_key.items():
        if (slot_id, mexc_sym) in engine_live_by_key:
            # This very slot has a tracked position here — not an orphan.
            continue
        if mexc_sym in engine_unattributed_syms:
            # A live position on this symbol whose slot we could not read.
            # Fail safe: leave it alone rather than risk closing a real one.
            logger.warning(
                "[RECONCILE] slot=%d %s — a live position on this symbol has "
                "no readable slot label; skipping orphan check",
                slot_id, mexc_sym,
            )
            continue
        # race protection now treats age==0
        # ("createTime unknown") as "apply grace, not bypass it". The old
        # condition `if 0 < age < _RECONCILE_GRACE_SEC` skipped grace when
        # age was 0, force-closing positions of unknown age. Unknown age
        # = maximum caution: assume it might be in-flight from on_signal.
        age = _mexc_pos_age_sec(mexc_pos)
        if age < _RECONCILE_GRACE_SEC:
            logger.debug(
                "[RECONCILE] slot=%d %s age=%.1fs < %ds grace — skipping",
                slot_id, mexc_sym, age, _RECONCILE_GRACE_SEC,
            )
            continue

        symbol, direction, qty, lev = _mexc_pos_to_close_args(mexc_pos)
        logger.error(
            "🚨 [RECONCILE ORPHAN] slot=%d %s %s qty=%d age=%.0fs — "
            "bot has no record. Closing via MARKET.",
            slot_id, symbol, direction.upper(), qty, age,
        )

        if qty <= 0:
            logger.warning("[RECONCILE] %s zero qty, skipping", symbol)
            continue

        executor = live_pool.get_executor(slot_id)
        if executor is None:
            summary["orphans_failed"] += 1
            continue

        try:
            # Last check before touching the account. rebuild_from_store runs
            # on a timer, so a key deleted seconds ago may still look active in
            # the pool. Closing a position on an account the operator has
            # deliberately disconnected is the one outcome worth an extra
            # round-trip to avoid.
            _keyed = True
            _chk = getattr(live_pool, "slot_has_key", None)
            if callable(_chk):
                try:
                    _r = _chk(slot_id)
                    if inspect.isawaitable(_r):
                        _keyed = bool(await _r)
                except Exception:
                    # Cannot verify. active_executors() already vetted this
                    # slot, so trust that rather than stranding a real orphan.
                    _keyed = True
            if not _keyed:
                logger.warning(
                    "[RECONCILE] slot=%s без вебкея — залишаю %s недоторканим "
                    "(позиція відкрита вручну, не наша)", slot_id, symbol)
                continue
            result = await executor.market_close_position(
                symbol=symbol,
                direction=direction,
                qty_contracts=qty,
                leverage=lev,
            )
        except Exception:
            logger.exception("[RECONCILE] market_close threw for %s", symbol)
            summary["orphans_failed"] += 1
            await _send_orphan_alert(alerts, "ORPHAN close FAILED", mexc_pos, "exception")
            continue

        if result.success:
            summary["orphans_closed"] += 1
            # MEXC's history_positions row can lag the close, so realised may
            # not be readable yet (exit_price==0). Say "n/a" instead of printing
            # a misleading $0.0000 — the real PnL is in the MEXC account.
            _pnl = _num(getattr(result, "realized_pnl_usdt", 0))
            _exit = _num(getattr(result, "exit_price", 0))
            _msg = (f"realised=${_pnl:+.4f}" if _exit > 0
                    else "realised: n/a (MEXC history not settled — check account)")
            await _send_orphan_alert(alerts, "ORPHAN closed", mexc_pos, _msg)
            # ── Persist the orphan close to live_trades so its realised PnL
            # COUNTS in the per-pair "trades today" SUM(net_pnl_usdt) and every
            # other ledger reader (per-slot, webpanel, metrics). The reconcile
            # path never wrote a row -> orphan PnL was silently dropped.
            # Fully wrapped: a write failure can NEVER break the reconcile loop
            # (the close already succeeded above).
            try:
                live_db = getattr(shadow_engine, "live_db", None)
                if live_db is None:
                    logger.warning(
                        "[RECONCILE] no live_db; orphan %s PnL not persisted", symbol,
                    )
                elif _exit <= 0:
                    # MEXC history not settled -> realised/exit unknown. Skip the
                    # insert (a $0 row would CORRUPT the daily total) and log so
                    # the operator can reconcile manually from the account.
                    logger.warning(
                        "[RECONCILE] orphan %s closed but realised n/a "
                        "(exit_price=0) - NOT writing live_trades row; "
                        "check MEXC account for actual PnL", symbol,
                    )
                else:
                    from src.exchanges.mexc_rest import to_binance
                    from src.execution.live_executor import CONTRACT_SIZES

                    internal_sym = to_binance(mexc_sym)            # row key MUST be INTERNAL fmt to merge
                    contract_size = CONTRACT_SIZES.get(symbol, 1.0)  # CONTRACT_SIZES keyed by MEXC fmt
                    _entry = _num(getattr(result, "entry_price_confirmed", 0))
                    if _entry <= 0:
                        _entry = _exit  # sane non-null; entry_price feeds no PnL sum

                    notional = _exit * qty * contract_size        # scaled price domain
                    margin = (notional / lev) if lev else notional
                    now_s = int(time.time())

                    await live_db.execute(
                        """INSERT INTO live_trades
                           (symbol, direction, leverage, margin_usdt, notional_usdt,
                            entry_price, entry_slippage_pct, opened_at,
                            exit_price, closed_at, exit_reason,
                            pnl_usdt, net_pnl_usdt,
                            entry_fees_usdt, exit_fees_usdt,
                            mode, account_label, detector_source)
                           VALUES (?,?,?,?,?, ?,?,?, ?,?,?, ?,?, ?,?, ?,?,?)""",
                        (
                            internal_sym, direction, int(lev), margin, notional,
                            _entry, 0.0, now_s,
                            _exit, now_s, "reconcile_orphan",
                            _pnl, _pnl,
                            0.0, 0.0,
                            "live", f"slot{slot_id}", "reconcile",
                        ),
                    )
                    logger.info(
                        "[RECONCILE] persisted orphan %s (%s) net_pnl=%+.4f to live_trades",
                        internal_sym, symbol, _pnl,
                    )
            except Exception:
                logger.exception(
                    "[RECONCILE] failed to persist orphan %s to live_trades", symbol,
                )

            # Count the orphan toward the per-slot safety state (in-memory).
            # record_close adds to today_pnl and re-checks the peak-drawdown
            # kill; the open-position decrement is a no-op for an untracked
            # orphan. Gated on _exit>0 (realised known).
            if _exit > 0:
                try:
                    _safety = live_pool.get_safety(slot_id)
                    if _safety is not None:
                        # INTERNAL format: record_open keyed the counter with
                        # signal.symbol ("1000PEPEUSDT"), so passing the MEXC
                        # form here decremented nothing and left the pair stuck
                        # on "max_concurrent_per_symbol reached" until a restart.
                        # Converted locally — internal_sym above is only bound
                        # inside the live_db branch.
                        from src.exchanges.mexc_rest import to_binance as _to_int
                        # Computed here, not reused from the live_db branch
                        # above: `notional` is bound only inside it, and the
                        # NameError would be swallowed by the except below.
                        from src.execution.live_executor import CONTRACT_SIZES as _CS
                        _notional = _exit * qty * _CS.get(symbol, 1.0)
                        _safety.record_close(_to_int(symbol), _pnl,
                                             notional_usdt=_notional)
                except Exception:
                    logger.exception(
                        "[RECONCILE] failed to record orphan %s in safety", symbol,
                    )
        else:
            summary["orphans_failed"] += 1
            await _send_orphan_alert(
                alerts, "ORPHAN close FAILED", mexc_pos, str(result.error_msg),
            )

    # ── Case B: Engine has it, MEXC doesn't → STALE engine state.
    # Build the set of mexc-symbols present anywhere in MEXC across slots
    # for quick membership tests.
    mexc_symbols_present: set[str] = {sym for (_sid, sym) in mexc_by_key.keys()}
    for (pos_slot, mexc_sym), positions in engine_positions_by_key.items():
        if pos_slot is None:
            # Unattributed: fall back to the symbol-wide test.
            if mexc_sym in mexc_symbols_present:
                continue
        elif (pos_slot, mexc_sym) in mexc_by_key:
            continue
        for eng_pos in positions:
            # Race protection: skip just-opened engine positions
            if eng_pos.elapsed_sec < _RECONCILE_GRACE_SEC:
                continue

            logger.warning(
                "[RECONCILE STALE] engine tracks %s (age=%.0fs) but MEXC has "
                "no position — was it closed externally? Marking closed in engine.",
                eng_pos.symbol, eng_pos.elapsed_sec,
            )
            try:
                await shadow_engine.mark_position_closed_externally(
                    eng_pos, reason="reconciliation_external_close",
                )
                summary["stale_engine_marked"] += 1
            except Exception:
                logger.exception(
                    "[RECONCILE STALE] mark_position_closed_externally failed for %s",
                    eng_pos.symbol,
                )

    return summary


async def periodic_reconcile_loop(
    shadow_engine,
    live_pool,
    alerts,
    interval_sec: float = 60.0,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Background task: every `interval_sec` seconds, run reconcile_once().

    Designed to be wrapped in asyncio.create_task at bot startup. Exits
    cleanly when `stop_event` is set (or shadow_engine._stop is set).
    """
    # Use shadow_engine's stop event if no explicit one provided
    if stop_event is None:
        stop_event = getattr(shadow_engine, "_stop", None) or asyncio.Event()

    logger.info(
        "[RECONCILE PERIODIC] starting loop (interval=%.0fs)",
        interval_sec,
    )

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
            break  # stop signalled
        except asyncio.TimeoutError:
            pass  # normal — time to run another pass

        try:
            summary = await reconcile_once(shadow_engine, live_pool, alerts)
            if (summary["orphans_closed"] + summary["orphans_failed"]
                    + summary["stale_engine_marked"]) > 0:
                logger.warning("[RECONCILE PERIODIC] action taken: %s", summary)
            else:
                logger.debug("[RECONCILE PERIODIC] clean: %s", summary)
        except Exception:
            logger.exception("[RECONCILE PERIODIC] reconcile_once threw")

    logger.info("[RECONCILE PERIODIC] loop exited cleanly")
