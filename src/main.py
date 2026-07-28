"""
Entry point for stakan-bot.

Whitelist-only trading bot: trades exactly the symbols listed under `universe:`
in config.yaml. No discovery, no periodic scanner refresh.
  - Loads config and env
  - Initializes DB
  - On startup: reads universe from config → subscribes Binance + MEXC WebSockets
  - Logs orderbook + trade health every 10 seconds
  - Writes heartbeat for Docker healthcheck

"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from loguru import logger as loguru_logger

from src.config import load_env, load_yaml
from src.config_loader import ConfigLoader
from src.exchanges.binance_ws import BinanceWSClient, Trade as BinanceTrade
from src.exchanges.mexc_ws import MexcWSClient, Trade as MexcTrade
from src.exchanges.orderbook import OrderBookManager
from src.execution.funding_guard import FundingGuard
from src.execution.ioc_executor import IOCExecutor
from src.execution.market_executor import MarketExecutor
from src.state.universe import UniverseProvider, UniverseResult
from src.state.pair_state_manager import PairStateManager
from src.state.transitions import TransitionCriteria
from src.storage.db import Database, init_db
from src.storage.db_live import LiveDatabase, init_live_db
from src.strategy.static_gap_detector import StaticGapDetector, StaticGapConf
from src.strategy.shadow_engine import ShadowEngine
from src.strategy.signal import SignalWriter
from src.telegram_bot.alerts import TelegramAlerts
from src.telegram_bot.bot import StakanTelegramBot


HEARTBEAT_PATH = Path("/app/data/.heartbeat")

logger = logging.getLogger(__name__)


# ---- Logging ----
def setup_logging(env_log_level: str, log_file: str) -> None:
    loguru_logger.remove()
    loguru_logger.add(
        sys.stderr,
        level=env_log_level.upper(),
        format=(
            "<green>{time:HH:mm:ss.SSS}</green> "
            "<level>{level: <8}</level> "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
    )
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    loguru_logger.add(
        log_file,
        level="INFO",
        rotation="50 MB",
        retention="14 days",
        compression="gz",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{line} | {message}",
    )

    class InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                level = loguru_logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            frame, depth = logging.currentframe(), 2
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1
            loguru_logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # Silence per-message library spam. websockets logs EVERY frame at DEBUG;
    # with root level=0 the InterceptHandler runs getMessage() (frame __str__)
    # on each one BEFORE loguru drops it at INFO — pure CPU waste on the hot WS
    # path, bursting exactly when order submission needs the loop. Filter at source.
    for _noisy in ("websockets", "websockets.client", "websockets.protocol",
                   "websockets.server", "asyncio", "urllib3"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)


# ---- Heartbeat ----
async def heartbeat_loop(interval_sec: int) -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    while True:
        HEARTBEAT_PATH.write_text(str(int(time.time())))
        await asyncio.sleep(interval_sec)


async def live_pool_rebuild_loop(live_pool, interval_sec: int = 30) -> None:
    """
    Periodically resync live_pool with DB state.

    Picks up changes when user assigns/unassigns pairs via Telegram.
    """
    while True:
        await asyncio.sleep(interval_sec)
        try:
            await live_pool.rebuild_from_store()
        except Exception:
            import traceback
            traceback.print_exc()


async def db_prune_loop(db_path: str, live_db_path: str, interval_sec: int = 86400) -> None:
    """Periodic DB prune — keeps stakan.db from growing unbounded as new
    signals / orderbook-snapshots accumulate.

    DELETE only — no VACUUM, since VACUUM holds an exclusive lock for tens of
    seconds and is too disruptive at a daily cadence. Freed pages get reused
    by subsequent inserts, so file size stabilises at a steady state set by
    the per-table retention windows. If true shrinkage is ever needed (e.g.
    after a one-off bulk delete), stop the bot and `VACUUM` manually.

    Retention values are deliberately tight on the high-rate tables so a $10
    VPS with ~25-40 GB disk stays comfortable indefinitely.
    """
    import sqlite3
    RET_SHADOW = [
        # (table, age_column, retention_seconds) — orderbook snapshots are
        # diagnostic only, 3 days is plenty.
        ("live_orderbook_snapshots", "ts_ms",       3 * 86400),
        ("signals",                  "created_at",  7 * 86400),
        ("historical_candles",       "open_time",  30 * 86400),
        ("shadow_trades",            "opened_at",   3 * 86400),  # 3-day shadow retention (user)
        ("shadow_open_misses",       "ts",          3 * 86400),  # IOC-expiry rows, 3-day
        ("state_transitions",        "created_at", 30 * 86400),
    ]
    RET_LIVE = [
        ("live_trades", "opened_at", 90 * 86400),
    ]
    while True:
        try:
            now = int(time.time())
            for db_p, ret in ((db_path, RET_SHADOW), (live_db_path, RET_LIVE)):
                c = None
                try:
                    c = sqlite3.connect(db_p)
                    deleted_total = 0
                    for tbl, col, sec in ret:
                        try:
                            mx = c.execute(f"SELECT MAX({col}) FROM {tbl}").fetchone()[0]
                            if not mx:
                                continue
                            scale = 1000 if mx > 1e12 else 1
                            cutoff = (now - sec) * scale
                            cur = c.execute(
                                f"DELETE FROM {tbl} WHERE {col} < ?", (cutoff,)
                            )
                            deleted_total += cur.rowcount or 0
                        except sqlite3.OperationalError as oe:
                            # Table absent on this instance (e.g. historical_candles
                            # on the clone) — skip quietly instead of a daily traceback.
                            if "no such table" in str(oe):
                                continue
                            import traceback; traceback.print_exc()
                        except Exception:
                            import traceback; traceback.print_exc()
                    c.commit()
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    if deleted_total > 0:
                        loguru_logger.info(
                            "DB prune: deleted {} rows from {}",
                            deleted_total, db_p,
                        )
                except Exception:
                    import traceback; traceback.print_exc()
                finally:
                    # Always close — commit()/PRAGMA can raise "database is locked"
                    # (the bot holds WAL connections to these same files), which
                    # would otherwise leak the sqlite3.Connection every day.
                    if c is not None:
                        try:
                            c.close()
                        except Exception:
                            pass
        except asyncio.CancelledError:
            return
        except Exception:
            import traceback; traceback.print_exc()
        await asyncio.sleep(interval_sec)


async def slot_balance_refresh_loop(webkey_store, webkey_client_pool, interval_sec: int = 60) -> None:
    """Periodically refresh `last_balance_usdt` (and latency/error) for every
    configured slot, so the web panel always shows fresh balances for ALL
    slots without manual /webkey_test calls.

    Without this loop, `update_health` is only invoked by the IOC executor on
    slot-level error/recovery events, always with `balance_usdt=None` — which
    leaves the cache NULL for any slot the operator never tested via Telegram.
    Per-slot errors are isolated; the loop itself never dies on transient
    failures (network blip, MEXC 5xx). 2-5 slots × ~0.7s each is trivial load
    at the default 60s cadence.
    """
    while True:
        try:
            for s in await webkey_store.list_all():
                if not s.is_complete:
                    continue
                try:
                    client = await webkey_client_pool.get(s.slot_id)
                    report = await client.health_check()
                    bal = report.get("balance")
                    # Prefer TOTAL account value (equity → cashBalance) over
                    # availableBalance — `availableBalance` drops by the locked
                    # margin when a position is open, so the panel was showing
                    # $56 during a $32-margin trade instead of the actual $88
                    # wallet. Operators expect wallet balance, not free margin.
                    # Fall back through the chain in case MEXC renames fields.
                    balance_str = (
                        str(
                            bal.get("equity")
                            or bal.get("cashBalance")
                            or bal.get("balance")
                            or bal.get("availableBalance")
                            or ""
                        )
                        if isinstance(bal, dict) else None
                    )
                    await webkey_store.update_health(
                        slot_id=s.slot_id,
                        latency_ms=report.get("latency_ms"),
                        balance_usdt=balance_str,
                        error=None if report.get("valid") else (report.get("error") or "n/a"),
                    )
                except Exception:
                    import traceback
                    traceback.print_exc()
        except asyncio.CancelledError:
            return
        except Exception:
            import traceback
            traceback.print_exc()
        await asyncio.sleep(interval_sec)


async def _recovery_stop(webkey_store, state_manager, db, slot_id: int, pair: str) -> list[int]:
    """Stop live on a slot + demote its pair to shadow + reset recovery arming.

    ACCOUNT-WIDE: the recovery signal (360d realized PnL) is the WHOLE account's,
    not one pair's. So if a slot's account recovered/hit cap, EVERY live slot that
    shares the SAME MEXC account (identical decrypted webkey) must stop together —
    otherwise a second pair running on the same account would keep trading a
    recovered/capped account. Returns the list of slot_ids actually stopped (the
    caller skips them for the rest of the cycle so we don't act twice)."""
    import hashlib
    # Build the stop set: this slot + any siblings sharing the same account.
    targets: list[tuple[int, str]] = [(slot_id, pair)]
    try:
        all_slots = await webkey_store.list_all()
        me = next((x for x in all_slots if x.slot_id == slot_id), None)
        my_wk = getattr(me, "webkey", None) if me else None
        if my_wk:
            my_id = hashlib.sha256(my_wk.encode()).hexdigest()
            for x in all_slots:
                if x.slot_id == slot_id or not getattr(x, "live_enabled", 0):
                    continue
                xwk = getattr(x, "webkey", None)
                xpair = getattr(x, "assigned_pair", None)
                if xwk and xpair and hashlib.sha256(xwk.encode()).hexdigest() == my_id:
                    targets.append((x.slot_id, xpair))
    except Exception:
        logger.warning("recovery: sibling lookup failed for slot %d — stopping only it", slot_id)

    if len(targets) > 1:
        logger.warning(
            "[RECOVERY] ACCOUNT-WIDE stop: slots %s share one MEXC account — stopping ALL together",
            [t[0] for t in targets],
        )

    stopped: list[int] = []
    for sid, p in targets:
        try:
            await webkey_store.set_live_enabled(sid, False)
        except Exception:
            logger.warning("recovery: set_live_enabled(%d, False) failed", sid)
        try:
            if state_manager is not None:
                await state_manager.manual_promote(p, target="shadow", reason="recovery: target/cap")
        except Exception:
            logger.warning("recovery: demote %s -> shadow failed", p)
        try:
            # state demote above (manual_promote) is the authority; the pair_configs.mode
            # mirror was removed. Just clear the recovery markers here.
            await db.execute(
                "UPDATE webkey_slots SET recovery_baseline_ts=NULL, recovery_target_usdt=NULL "
                "WHERE slot_id=?", (sid,),
            )
        except Exception:
            logger.warning("recovery: db cleanup failed slot %d", sid)
        stopped.append(sid)
    return stopped


# Two reads farther apart than this (USD) are treated as unstable → no action.
# The analysis page can spuriously spike: on 2026-06-18 a single read returned
# +$92.82 while the account was really ~-$199, which false-fired a "recovered"
# stop. Realized PnL over a fixed window is deterministic, so two back-to-back
# reads of the true value agree within a couple $ (only an in-between close
# moves it). 25 is generous headroom; a 290-wide disagreement = a bad read.
_RECOVERY_CONFIRM_TOL = 25.0
# A recovery account's 360d realized PnL cannot move by more than this in one
# ~15s monitor cycle. A read that jumps further is a bad account-PAGE value (the
# 2-read confirm can't catch a persistent glitch — false-fired a "recovered
# $476.20" stop 2026-07-17 while the account was actually -$658.45). Reject it.
_RECOVERY_MAX_CYCLE_DELTA = 200.0


async def _pnl_confirms(client, first: float, tol: float = _RECOVERY_CONFIRM_TOL):
    """Confirm a consequential recovery/cap trigger with a 2nd account-PnL read.

    A single bad page read must not stop a live slot. Returns the confirming
    value only if a fresh read agrees with `first` within `tol`; else None
    (caller skips — never acts on an unstable read). Mirrors the existing
    "never guess on None" guard, extended to "never guess on an outlier".
    """
    try:
        await asyncio.sleep(1.5)  # space the reads; avoid a cached/rate-limited echo
        second = await client.get_account_pnl_usdt(window_days=359)
    except Exception as e:
        logger.warning("[RECOVERY] confirm read failed: %s", e)
        return None
    if second is None:
        return None
    if abs(second - first) > tol:
        logger.warning(
            "[RECOVERY] PnL reads disagree ($%.2f vs $%.2f, >$%.0f) — treating as bad read, no action",
            first, second, tol,
        )
        return None
    return second


async def recovery_monitor_loop(
    webkey_store, webkey_client_pool, state_manager, db,
    interval_sec: int = 15,
) -> None:
    """Per-slot account recovery controls. For a live-enabled slot with
    recovery_mode=1: ARM by reading the account's accumulated realized PnL (sum
    of 360d closed-position PnL). If deeper than the cap -> refuse (stop live +
    shadow + risk alert). Else set target = |loss| - buffer; when the account
    PAGE re-read climbs back to within buffer of breakeven (pnl >= -buffer) ->
    auto-stop (live off + shadow). Arm and stop both read the SAME page number
    (get_account_pnl_usdt), never the bot's live_trades ledger, which overcounts
    and stops prematurely. Each slot isolated; loop never dies on transient
    errors."""
    import time as _t
    last_pnl = {}  # slot_id -> last plausible account-PnL read (spurious-jump guard)
    while True:
        try:
            # Slots stopped account-wide this cycle — skip them so a sibling's
            # own pass doesn't re-fire on the already-stopped account.
            stopped_this_cycle: set[int] = set()
            for s in await webkey_store.list_all():
                if s.slot_id in stopped_this_cycle:
                    continue
                if not (getattr(s, "is_complete", False) and getattr(s, "live_enabled", 0)):
                    continue
                pair = getattr(s, "assigned_pair", None)
                if not pair:
                    continue
                row = await db.fetchone(
                    "SELECT recovery_mode, recovery_cap_usdt, recovery_buffer_usdt, "
                    "recovery_baseline_ts, recovery_target_usdt FROM webkey_slots WHERE slot_id=?",
                    (s.slot_id,),
                )
                if not row or not row["recovery_mode"]:
                    continue
                cap = float(row["recovery_cap_usdt"] or 105)
                buf = float(row["recovery_buffer_usdt"] or 1)
                baseline = row["recovery_baseline_ts"]
                target = row["recovery_target_usdt"]

                if baseline is None:
                    # ARM — read the account's accumulated realized PnL
                    try:
                        client = await webkey_client_pool.get(s.slot_id)
                        pnl = await client.get_account_pnl_usdt(window_days=359)
                    except Exception as e:
                        logger.warning("[RECOVERY] slot %d PnL read failed: %s", s.slot_id, e)
                        continue
                    if pnl is None:
                        continue  # transient read failure — retry next cycle (never guess)
                    _lp = last_pnl.get(s.slot_id)
                    # ALWAYS advance the baseline, even when we skip acting on this
                    # read. If we froze the baseline on a rejected read, a GENUINE
                    # persistent step >$DELTA (a large real close, a webkey swap, or
                    # an old trade rolling out of the 359d window) would leave every
                    # later read >$DELTA away from the stale baseline -> rejected
                    # forever -> the cap/buf recovery stop permanently disabled until
                    # restart. Advancing lets a persistent move self-heal next cycle
                    # (a lone transient spike still costs only a skipped cycle, and
                    # consequential stops are re-confirmed via _pnl_confirms anyway).
                    if _lp is not None and abs(pnl - _lp) > _RECOVERY_MAX_CYCLE_DELTA:
                        logger.warning(
                            "[RECOVERY] %s IMPLAUSIBLE PnL read $%.2f (last $%.2f, jump $%.2f > $%.0f) "
                            "— spurious, no action this cycle", pair, pnl, _lp, abs(pnl - _lp), _RECOVERY_MAX_CYCLE_DELTA,
                        )
                        last_pnl[s.slot_id] = pnl
                        continue
                    last_pnl[s.slot_id] = pnl
                    if pnl <= -cap:
                        # Confirm with a 2nd read (same guard as the stop-check
                        # below) so a lone spurious page spike can't false-demote.
                        pnl = await _pnl_confirms(client, pnl)
                        if pnl is None or pnl > -cap:
                            continue
                        stopped_this_cycle.update(await _recovery_stop(webkey_store, state_manager, db, s.slot_id, pair))
                        logger.warning(
                            "[RECOVERY] %s account down $%.2f > cap $%.0f — LIVE REFUSED -> shadow",
                            pair, -pnl, cap,
                        )
                        continue
                    if pnl >= -buf:
                        # Already within $buf of breakeven at arm — nothing to
                        # recover, so stop live immediately (recovered IS the goal).
                        # Fixes the old "armed not-underwater -> no auto-stop" dead
                        # state that left the slot permanently unmonitored.
                        # Confirm (2nd read) before acting, like the stop-check.
                        pnl = await _pnl_confirms(client, pnl)
                        if pnl is None or pnl < -buf:
                            continue
                        stopped_this_cycle.update(await _recovery_stop(webkey_store, state_manager, db, s.slot_id, pair))
                        logger.warning(
                            "[RECOVERY] %s account $%.2f already within $%.2f of breakeven "
                            "— LIVE STOPPED -> shadow", pair, pnl, buf,
                        )
                        continue
                    tgt = round((-pnl) - buf, 4)
                    await db.execute(
                        "UPDATE webkey_slots SET recovery_baseline_ts=?, recovery_target_usdt=? "
                        "WHERE slot_id=?", (int(_t.time()), tgt, s.slot_id),
                    )
                    logger.info("[RECOVERY] %s armed: account $%.2f -> target +$%.2f (stop at -$%.2f)", pair, pnl, tgt, buf)
                else:
                    # STOP CHECK — re-read the account PAGE (same source as
                    # arming via get_account_pnl_usdt), NOT the bot's live_trades
                    # ledger. The ledger overcounts vs the real account (rolling
                    # 360d window shifts, funding, fill/slippage) and stops
                    # prematurely — a high-volume pair's ledger instantly exceeds
                    # the target while the page is still deep underwater (the
                    # PENGU misfire 2026-06-06). Recovered = the page itself
                    # climbed to within `buf` of breakeven. Cap-guard still
                    # applies on the way down. `target` stays the armed sentinel.
                    try:
                        client = await webkey_client_pool.get(s.slot_id)
                        pnl = await client.get_account_pnl_usdt(window_days=359)
                    except Exception as e:
                        logger.warning("[RECOVERY] slot %d stop-check PnL read failed: %s", s.slot_id, e)
                        continue
                    if pnl is None:
                        continue  # transient read failure — retry next cycle (never guess)
                    _lp = last_pnl.get(s.slot_id)
                    # ALWAYS advance the baseline, even when we skip acting on this
                    # read. If we froze the baseline on a rejected read, a GENUINE
                    # persistent step >$DELTA (a large real close, a webkey swap, or
                    # an old trade rolling out of the 359d window) would leave every
                    # later read >$DELTA away from the stale baseline -> rejected
                    # forever -> the cap/buf recovery stop permanently disabled until
                    # restart. Advancing lets a persistent move self-heal next cycle
                    # (a lone transient spike still costs only a skipped cycle, and
                    # consequential stops are re-confirmed via _pnl_confirms anyway).
                    if _lp is not None and abs(pnl - _lp) > _RECOVERY_MAX_CYCLE_DELTA:
                        logger.warning(
                            "[RECOVERY] %s IMPLAUSIBLE PnL read $%.2f (last $%.2f, jump $%.2f > $%.0f) "
                            "— spurious, no action this cycle", pair, pnl, _lp, abs(pnl - _lp), _RECOVERY_MAX_CYCLE_DELTA,
                        )
                        last_pnl[s.slot_id] = pnl
                        continue
                    last_pnl[s.slot_id] = pnl
                    # A stop is consequential and the page read can spuriously
                    # spike (false-fired a "recovered" stop 2026-06-18: read
                    # +$92.82 while the account was ~-$199). Confirm with a 2nd
                    # read before acting; an unstable/unconfirmed read = no action.
                    if pnl <= -cap:
                        pnl = await _pnl_confirms(client, pnl)
                        if pnl is None or pnl > -cap:
                            continue
                        stopped_this_cycle.update(await _recovery_stop(webkey_store, state_manager, db, s.slot_id, pair))
                        logger.warning(
                            "[RECOVERY] %s account down $%.2f > cap $%.0f — LIVE STOPPED -> shadow",
                            pair, -pnl, cap,
                        )
                        continue
                    if pnl >= -buf:
                        pnl = await _pnl_confirms(client, pnl)
                        if pnl is None or pnl < -buf:
                            continue
                        stopped_this_cycle.update(await _recovery_stop(webkey_store, state_manager, db, s.slot_id, pair))
                        logger.warning(
                            "[RECOVERY] %s recovered to $%.2f (within $%.2f of breakeven, target was +$%.2f) "
                            "— LIVE STOPPED -> shadow",
                            pair, pnl, buf, (target or 0.0),
                        )
        except asyncio.CancelledError:
            return
        except Exception:
            import traceback
            traceback.print_exc()
        await asyncio.sleep(interval_sec)


# ---- Trade counters ----
class TradeCounter:
    def __init__(self, exchange: str) -> None:
        self.exchange = exchange
        self.count_total = 0
        self.count_per_symbol: dict[str, int] = {}
        self._last_log_count = 0

    async def __call__(self, trade: BinanceTrade | MexcTrade) -> None:
        self.count_total += 1
        self.count_per_symbol[trade.symbol] = self.count_per_symbol.get(trade.symbol, 0) + 1

    def delta_since_last_log(self) -> int:
        d = self.count_total - self._last_log_count
        self._last_log_count = self.count_total
        return d


# ---- Stats loop ----
async def _warmup_webkey_pool(pool) -> None:
    """Background task: warm up webkey clients without blocking startup."""
    try:
        # Small delay to let main startup finish first
        await asyncio.sleep(2.0)
        report = await pool.start(eager_warmup=True)
        loguru_logger.info("Webkey client pool warmup: {}", report)
    except Exception as e:
        loguru_logger.warning("Webkey client pool warmup failed: {}", e)


async def stats_loop(
    binance_client: BinanceWSClient,
    mexc_client: MexcWSClient,
    ob_manager: OrderBookManager,
    binance_counter: TradeCounter,
    mexc_counter: TradeCounter,
    universe_holder: dict,
    signal_writer: SignalWriter | None = None,
    shadow_engine: ShadowEngine | None = None,
    state_manager: PairStateManager | None = None,
) -> None:
    """Log diagnostics every 10s."""
    while True:
        await asyncio.sleep(10)
        bs = binance_client.stats()
        ms = mexc_client.stats()
        b_delta = binance_counter.delta_since_last_log()
        m_delta = mexc_counter.delta_since_last_log()

        universe = universe_holder.get("symbols", [])

        # Per-symbol mid prices on both exchanges
        for sym in universe:
            b_ob = ob_manager.get("binance", sym)
            m_ob = ob_manager.get("mexc", sym)
            b_mid = b_ob.mid_price() if b_ob and b_ob.is_synced else None
            m_mid = m_ob.mid_price() if m_ob and m_ob.is_synced else None
            b_str = f"{b_mid:.6f}" if b_mid else "-"
            m_str = f"{m_mid:.6f}" if m_mid else "-"
            divergence = ""
            if b_mid and m_mid:
                pct = (m_mid - b_mid) / b_mid * 100
                divergence = f" Δ={pct:+.4f}%"
            loguru_logger.info("{:<10} bin={} mex={}{}", sym, b_str, m_str, divergence)

        b_conn_d = "✓" if bs.get("depth_connected") else "✗"
        b_conn_t = "✓" if bs.get("trades_connected") else "✗"
        m_conn = "✓" if ms.get("connected") else "✗"
        loguru_logger.info(
            "BIN: depth={}({}) trades={}({}) synced={}/{} resyncs={} | "
            "MEX: msgs={} synced={}/{} conn={} resyncs={} | "
            "trades_10s: bin={} mex={}",
            bs["depth_messages"], b_conn_d,
            bs["trade_messages"], b_conn_t,
            bs["synced_books"], bs["subscribed_symbols"], bs["resync_count"],
            ms["messages_received"], ms["synced_books"], ms["subscribed_symbols"], m_conn, ms["resync_count"],
            b_delta, m_delta,
        )

        # Static gap detector stats (only detector we use)
        if signal_writer:
            loguru_logger.info(
                "DET: signals_written={}",
                signal_writer.total_written,
            )

        # Shadow engine + state machine
        if shadow_engine and state_manager:
            sd = shadow_engine.diagnostics()
            states = state_manager.all_states()
            n_shadow = sum(1 for s in states.values() if s.state == "shadow")
            n_live = sum(1 for s in states.values() if s.state == "live")
            n_paused = sum(1 for s in states.values() if s.state == "paused")
            n_disc = sum(1 for s in states.values() if s.state == "discovered")
            loguru_logger.info(
                "SHADOW: rec={} fill={}/{}/{} (filled/partial/expired) | "
                "skip(notrade={} cd={} fund={} conf={} lag={} maxpos={}) | "
                "open={} closed={} | "
                "STATES live={} shadow={} discovered={} paused={}",
                sd["received"], sd["filled"], sd["partial"], sd["expired"],
                sd["skip_not_tradeable"], sd["skip_cooldown"], sd["skip_funding"],
                sd["skip_low_conf"], sd["skip_lag"], sd["skip_max_pos"],
                sd["open_count"], sd["closed_count"],
                n_live, n_shadow, n_disc, n_paused,
            )


# ---- Universe orchestration ----
async def apply_universe(
    result: UniverseResult,
    binance_client: BinanceWSClient,
    mexc_client: MexcWSClient,
    universe_holder: dict,
) -> None:
    """
    One-shot universe application at startup.

    Builds {symbol: binance_scale} for cross-exchange notation differences
    (1000PEPE is the only known case where MEXC raw price ≠ Binance price),
    subscribes both WS clients to the universe, and stores the symbol list
    in universe_holder for downstream consumers (stats loop, etc.).

    No periodic refresh — the universe is static for the bot's lifetime.
    To change pairs: edit config.yaml, rebuild, restart.
    """
    scales = {
        c.symbol: c.binance_scale
        for c in result.candidates_full
        if c.symbol in result.proposed_universe and c.binance_scale != 1.0
    }

    target = list(result.proposed_universe)
    loguru_logger.info("Universe: subscribing to {} symbols", len(target))
    if target:
        await binance_client.subscribe(target)
        await mexc_client.subscribe(target, scales=scales or None)

    universe_holder["symbols"] = target

    loguru_logger.info(
        "Universe ready: {} pairs ({})",
        len(target),
        ", ".join(target),
    )


# ---- main ----
async def main() -> None:
    env = load_env()
    cfg = load_yaml()

    setup_logging(env.log_level, env.log_file)
    loguru_logger.info("=" * 60)
    loguru_logger.info("stakan-bot starting")
    loguru_logger.info("=" * 60)

    # DB
    await init_db(env.db_path)
    db = Database(env.db_path)
    await db.connect()

    # Live trading DB — fully isolated from shadow DB.
    # Path defaults to /app/data/stakan-live.db (sibling to stakan.db).
    live_db_path = os.environ.get(
        "LIVE_DB_PATH",
        env.db_path.replace("stakan.db", "stakan-live.db"),
    )
    live_db = LiveDatabase(live_db_path)
    await live_db.connect()
    await init_live_db(live_db)
    loguru_logger.info("Live DB initialized at {}", live_db_path)

    # ---- ConfigLoader ----
    # Reads config/global.yaml + config/pairs/<SYMBOL>.yaml.
    # Replaces the old pattern of STATIC_GAP_* env vars + pair_configs.gap_*
    # DB columns. Hot-reloads on file mtime change within reload_ttl_sec.
    # If config dir is missing, falls back to built-in defaults (preserving
    # current behaviour for installs that haven't migrated yet).
    config_dir = os.environ.get("STAKAN_CONFIG_DIR", "/app/config")
    config_loader = ConfigLoader(config_dir, reload_ttl_sec=30.0)
    config_loader.load()
    loguru_logger.info(
        "ConfigLoader watching {} ({} pair configs loaded)",
        config_dir, len(config_loader.list_pairs()),
    )

    # ---- Webkey store (Phase 2 multi-slot): encrypted credentials per-account ----
    # Up to MAX_SLOTS slots in DB. Each slot has its own webkey + dolos + proxy.
    # All managed via Telegram (/webkey_setup, /webkey_test, etc).
    from src.execution.webkey import WebkeyClientPool, WebkeyStore
    webkey_store = WebkeyStore(db, env.master_key)
    await webkey_store.ensure_slots_seeded()
    loguru_logger.info("Webkey store initialized (5 slots, manage via /webkey commands)")

    # Persistent client pool — one MexcWebClient per slot, kept alive for the
    # whole bot lifetime. This eliminates ~50-100ms TLS handshake overhead
    # per request (matters for /webkey_test and, later, for live trading).
    # Eagerly warm up clients in background so first user request is fast.
    webkey_client_pool = WebkeyClientPool(webkey_store)
    asyncio.create_task(_warmup_webkey_pool(webkey_client_pool))

    # Periodically refresh `last_balance_usdt` for ALL configured slots so the
    # web panel (which reads from this cache) always shows fresh balances —
    # without needing manual /webkey_test calls per slot.
    asyncio.create_task(
        slot_balance_refresh_loop(webkey_store, webkey_client_pool, 60),
        name="slot_balance_refresh",
    )

    # Daily DB prune so stakan.db doesn't grow forever — orderbook snapshots
    # and signals accumulate at ~50K/hour combined. See db_prune_loop docstring
    # for retention windows.
    _live_db_path = os.environ.get("LIVE_DB_PATH", "/app/data/stakan-live.db")
    asyncio.create_task(
        db_prune_loop(env.db_path, _live_db_path, 86400),
        name="db_prune",
    )

    # ---- Components ----
    ob_manager = OrderBookManager()
    binance_counter = TradeCounter("binance")
    mexc_counter = TradeCounter("mexc")

    # Signal storage (writes to BD in batches)
    signal_writer = SignalWriter(db, flush_interval_sec=1.0, max_batch=100)
    await signal_writer.start()

    # Reference-only set: pairs in universe (WS subscribed) but detectors don't trade on them
    reference_only = set(cfg.universe.reference_only_symbols)
    if reference_only:
        loguru_logger.info(
            "Reference-only symbols (no signals will be emitted): %s",
            sorted(reference_only),
        )

    # ---- Core components ----
    state_manager = PairStateManager(
        db=db,
        criteria=TransitionCriteria(),  # uses defaults from transitions.py
        evaluation_interval_sec=60,
        summary_interval_sec=300,
        live_db=live_db,
    )

    funding_guard = FundingGuard(
        funding_intervals_utc=cfg.mexc.funding_intervals_utc,
        cutoff_sec=cfg.mexc.funding_cutoff_sec,
    )

    ioc_executor = IOCExecutor()
    market_executor = MarketExecutor()

    # === Live trading components (multi-slot pool architecture) ===
    # Each webkey slot can be assigned a single pair to trade live.
    # Multiple slots can be assigned to the same pair for parallel positions.
    # Configure via Telegram: /slot N → pick pair from whitelist → enable.
    # Master switch: LIVE_TRADING_ENABLED=1 in .env
    # If 0 (default), no live components are loaded — bot runs shadow-only.
    live_pool = None
    private_ws_pool = None
    if os.environ.get('LIVE_TRADING_ENABLED', '0') == '1':
        from src.execution.live_pool import LiveExecutorPool

        # Private WS pool: push-based IOC fills (push.personal.order over the
        # webkey-authed wss://contract.mexc.com/edge). Replaces the post-submit
        # REST fill poll on confirmed fills → saves ~fill_poll latency and cuts
        # REST GET volume (fewer 510 rate-limits). Lazy per-slot; the fill path
        # falls back to REST polling whenever a push is down/missed. Disable
        # with PRIVATE_WS_FILL=0.
        if os.environ.get('PRIVATE_WS_FILL', '1') == '1':
            from src.exchanges.mexc_private_ws import MexcPrivateWSPool
            private_ws_pool = MexcPrivateWSPool(webkey_client_pool)

        live_pool = LiveExecutorPool(
            client_pool=webkey_client_pool,
            webkey_store=webkey_store,
            alerts=None,  # wired below after alerts is created
            private_ws_pool=private_ws_pool,
            default_max_drawdown_usdt=float(os.environ.get('LIVE_MAX_DRAWDOWN', '20.0')),
            default_max_per_symbol=int(os.environ.get('LIVE_MAX_PER_SYMBOL', '1')),
            default_max_total=int(os.environ.get('LIVE_MAX_TOTAL', '1')),
            default_max_margin_usdt=float(os.environ.get('LIVE_MAX_MARGIN', '30.0')),
        )
        # Initial sync from DB
        await live_pool.rebuild_from_store()
        # Per-slot account-recovery monitor: read the account's accumulated
        # drawdown on enable, refuse if deeper than cap, auto-stop (live off +
        # shadow) once the loss is recovered to within `recovery_buffer_usdt`.
        asyncio.create_task(
            recovery_monitor_loop(
                webkey_store, webkey_client_pool, state_manager, db, 15
            ),
            name="recovery_monitor",
        )
        # Eagerly warm the private WS for live-active slots so the first fill is
        # already push-served (otherwise the first fill per slot falls back to
        # REST while the WS connects in the background).
        if private_ws_pool is not None:
            try:
                _live_slots = [s.slot_id for s in await webkey_store.list_live_active()]
                asyncio.create_task(
                    private_ws_pool.start(_live_slots), name="private_ws_warmup"
                )
                loguru_logger.info(
                    "Private-WS fill push enabled — warming slots {}", _live_slots
                )
            except Exception:
                loguru_logger.exception("private_ws warmup scheduling failed")
        loguru_logger.warning(
            "🔴 LIVE TRADING ENABLED (multi-slot mode) — "
            "default max_margin=${:.2f}, max_drawdown=${:.2f}. "
            "Configure slots via Telegram: tap 🔑 Webkey → slot → assign pair.",
            float(os.environ.get('LIVE_MAX_MARGIN', '30.0')),
            float(os.environ.get('LIVE_MAX_DRAWDOWN', '20.0')),
        )
    else:
        loguru_logger.info("Live trading disabled (set LIVE_TRADING_ENABLED=1 to activate)")

    shadow_engine = ShadowEngine(
        cfg=cfg.shadow,
        db=db,
        ob_manager=ob_manager,
        state_manager=state_manager,
        funding_guard=funding_guard,
        ioc_executor=ioc_executor,
        market_executor=market_executor,
        max_positions_per_symbol=cfg.risk.max_positions_per_symbol,
        realism_profile=os.environ.get('SHADOW_REALISM_PROFILE', 'realistic'),
        live_pool=live_pool,
        live_db=live_db,
        config_loader=config_loader,  # per-pair exit_strategy lookup
    )

    # ---- Telegram bot ----
    alerts = TelegramAlerts(
        bot_token=env.telegram_bot_token,
        owner_id=env.telegram_owner_id,
        quiet_hours=None,
        alert_min_pnl_usdt=0.0,
    )

    # Wire alerts to ShadowEngine for orphan position notifications
    shadow_engine.alerts = alerts

    # Wire alerts to live pool (for slot-level error notifications)
    if live_pool is not None:
        live_pool.alerts = alerts
        # Re-create executors so they get the alerts ref
        # (executors are created lazily when slots become live-active)
        for sid, executor in live_pool._executors.items():
            executor.alerts = alerts

    telegram_bot = StakanTelegramBot(
        token=env.telegram_bot_token,
        owner_id=env.telegram_owner_id,
        db=db,
        state_manager=state_manager,
        shadow_engine=shadow_engine,
        alerts=alerts,
        daily_report_hour_utc=6,  # 09:00 Kyiv summer
        webkey_store=webkey_store,
        webkey_client_pool=webkey_client_pool,
        live_pool=live_pool,
        live_db=live_db,
    )

    # Wire alerts into state machine — fired on every state transition
    async def _on_state_change(symbol: str, from_state: str, to_state: str, reason: str) -> None:
        await alerts.state_change(symbol, from_state, to_state, reason)

    state_manager.on_state_change = _on_state_change

    # Wire alerts into shadow_engine — patch _open_position and _close_position to fire alerts
    _original_open = shadow_engine._open_position
    _original_close = shadow_engine._close_position

    async def _open_position_with_alert(*args, **kwargs):
        # The engine returns the position IT created, or None. Reading
        # _open_positions[symbol][-1] instead announced the previous, still-open
        # position whenever this call opened nothing — two identical
        # [LIVE OPEN] messages for one trade — and could not be made correct
        # anyway once two slots open concurrently on the same signal.
        opened = await _original_open(*args, **kwargs)
        try:
            if opened is not None:
                pos = opened
                # Telegram alerts limited to LIVE trades only.
                # Shadow can fire 50+ trades/min on bursty markets which
                # exceeds Telegram's per-chat rate limit (~20 msg/min),
                # eventually triggering Flood Control with hours-long
                # RetryAfter penalties. Shadow PnL is still fully tracked
                # in shadow_trades table — accessed via the "🧪 Shadow PnL"
                # button — only the per-trade push notifications are
                # suppressed.
                if cfg.telegram.send_realtime_open and pos.mode == "live":
                    await alerts.trade_open(
                        symbol=pos.symbol,
                        direction=pos.direction,
                        entry_price=pos.entry_price,
                        leverage=pos.leverage,
                        margin_usdt=pos.margin_usdt,
                        notional_usdt=pos.notional_usdt,
                        gap_ticks=pos.gap_ticks,
                        detector_source=pos.detector_source,
                        mode=pos.mode,  # pass mode for [LIVE]/[SHADOW] prefix
                        account_label=getattr(pos, "account_label", None),
                    )
        except Exception as e:
            logger.warning("trade_open alert failed: %s", e)

    async def _close_position_with_alert(pos, reason):
        await _original_close(pos, reason)
        try:
            # Telegram alerts limited to LIVE trades only (see trade_open
            # comment above). Shadow close PnL still recorded in DB and
            # exposed via the "🧪 Shadow PnL" button.
            if cfg.telegram.send_realtime_close and pos.mode == "live":
                await alerts.trade_close(
                    symbol=pos.symbol,
                    direction=pos.direction,
                    entry_price=pos.entry_price,
                    exit_price=pos.exit_price,
                    exit_reason=pos.exit_reason or reason,
                    roi_pct=pos.current_roi_pct,
                    net_pnl_usdt=pos.net_pnl_usdt,
                    duration_sec=pos.duration_sec,
                    duration_ms=pos.duration_ms,
                    mfe_pct=pos.mfe_pct,
                    mae_pct=pos.mae_pct,
                    mode=pos.mode,
                    account_label=getattr(pos, "account_label", None),
                )
        except Exception as e:
            logger.warning("trade_close alert failed: %s", e)

    shadow_engine._open_position = _open_position_with_alert  # type: ignore[method-assign]
    shadow_engine._close_position = _close_position_with_alert  # type: ignore[method-assign]

    # Wrap signal_writer.write to fan-out to shadow_engine as well
    _original_write = signal_writer.write

    # Strong reference set for background tasks. Without this, asyncio's
    # garbage collector can destroy in-flight on_signal coroutines before
    # they complete, since create_task() returns a weakly-referenced task.
    # Pattern: add to set on create, remove via done_callback.
    _signal_bg_tasks: set[asyncio.Task] = set()

    def _on_signal_task_done(task: asyncio.Task) -> None:
        _signal_bg_tasks.discard(task)
        # Surface exceptions that would otherwise be silently swallowed.
        # asyncio logs the warning for unhandled task exceptions, but we
        # want them in our own logger with context.
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            logger.error(
                "ShadowEngine.on_signal background task failed: %s: %s",
                type(exc).__name__, exc,
                exc_info=exc,
            )

    async def signal_write_with_shadow(sig):
        # 1. Persist signal to DB synchronously (cheap, must complete before
        #    on_signal observes it).
        await _original_write(sig)

        # 2. Fire-and-forget on_signal — the IOC order submit + fill poll
        #    can take 500-2000ms, but the detector's scan loop MUST keep
        #    cycling through orderbooks at SCAN_INTERVAL_SEC. Blocking
        #    here would freeze gap detection across ALL pairs while one
        #    pair's order is in-flight.
        task = asyncio.create_task(
            shadow_engine.on_signal(sig, signal_id=None),
            name=f"on_signal:{sig.symbol}:{sig.direction}",
        )
        _signal_bg_tasks.add(task)
        task.add_done_callback(_on_signal_task_done)

    signal_writer.write = signal_write_with_shadow  # type: ignore[method-assign]

    # Static gap detector — the only detector wired.
    # Scans orderbooks periodically for arbitrage gaps between Binance and MEXC.
    # configuration source priority:
    #   1. config/global.yaml (via ConfigLoader)            ← human-edited
    #   2. STATIC_GAP_* env vars (legacy, still supported)  ← backward compat
    #   3. Built-in defaults from DetectorConfig dataclass  ← last resort
    # Per-pair overrides live in config/pairs/<SYMBOL>.yaml and are resolved
    # by the detector itself (see StaticGapDetector._reload_pair_overrides_from_yaml).
    gd = config_loader.global_defaults()  # built-in defaults if no global.yaml
    static_gap_cfg = StaticGapConf(
        enabled=(
            os.environ.get("STATIC_GAP_ENABLED", str(int(gd.enabled))) == "1"
        ),
        scan_interval_sec=float(
            os.environ.get("STATIC_GAP_INTERVAL_SEC", str(gd.scan_interval_sec))
        ),
        cooldown_sec=float(
            os.environ.get("STATIC_GAP_COOLDOWN_SEC", str(gd.cooldown_sec))
        ),
        min_gap_ticks=gd.min_ticks,  # global default from global.yaml; per-pair via pair yaml
    )
    static_gap = StaticGapDetector(
        static_gap_cfg, ob_manager, signal_writer,
        reference_only_symbols=reference_only,
        db=db,                          # DB fallback
        config_loader=config_loader,    # preferred source
    )

    # Fan-out for Binance trades: counter only
    async def binance_trade_fanout(trade: BinanceTrade) -> None:
        await binance_counter(trade)

    # Fan-out for MEXC trades: counter only
    async def mexc_trade_fanout(trade: MexcTrade) -> None:
        await mexc_counter(trade)

    binance_client = BinanceWSClient(cfg.binance, ob_manager, on_trade=binance_trade_fanout)
    mexc_client = MexcWSClient(cfg.mexc, ob_manager, on_trade=mexc_trade_fanout)
    universe_provider = UniverseProvider(cfg.universe, db)
    universe_holder: dict = {"symbols": []}

    # Build universe from whitelist + subscribe WS (one-shot, no periodic refresh)
    loguru_logger.info("Building universe from config whitelist...")
    universe_result = await universe_provider.build()
    await apply_universe(universe_result, binance_client, mexc_client, universe_holder)

    # Initialize pair state machine AFTER universe is built
    await state_manager.initialize(universe_holder["symbols"])
    await shadow_engine.start()

    # ─── reconcile patch ─────────────────────────────────────
    # Startup reconciliation: find any positions still open on MEXC from
    # before this process started (bot crash mid-trade, manual restart
    # without graceful shutdown, etc.) and close them via MARKET order.
    # Fail-soft: errors are logged but don't block bot startup.
    try:
        from src.safety.reconciliation import startup_reconcile
        await startup_reconcile(live_pool, alerts)
    except Exception:
        loguru_logger.exception("startup_reconcile failed — bot will continue")

    # Start static gap detector (own scan loop)
    await static_gap.start()

    # Signal handling
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        loguru_logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    # Tasks
    await state_manager.start()  # creates internal task

    # Start Telegram bot (long-polling + report scheduler) — NON-BLOCKING.
    # A Telegram API timeout during init/get_me must NEVER block the exchange
    # feeds + trading started below (it used to: `await telegram_bot.start()`
    # ran first, and a transient Telegram TimedOut on restart hung startup so
    # the feeds never started → bot unhealthy, no signals). Start it in the
    # background with retry; trading runs regardless of Telegram availability.
    async def _start_telegram_resilient():
        while not stop_event.is_set():
            try:
                await telegram_bot.start()
                logger.info("Telegram bot started")
                return
            except Exception:
                logger.warning(
                    "telegram_bot.start failed (Telegram unreachable?) — retrying "
                    "in 10s; feeds/trading unaffected", exc_info=True,
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
    asyncio.create_task(_start_telegram_resilient(), name="telegram_start")

    tasks = [
        asyncio.create_task(binance_client.run(), name="binance_ws"),
        asyncio.create_task(mexc_client.run(), name="mexc_ws"),
        asyncio.create_task(heartbeat_loop(cfg.app.heartbeat_interval_sec), name="heartbeat"),
        asyncio.create_task(
            stats_loop(
                binance_client, mexc_client, ob_manager,
                binance_counter, mexc_counter, universe_holder,
                signal_writer=signal_writer,
                shadow_engine=shadow_engine,
                state_manager=state_manager,
            ),
            name="stats",
        ),
    ]

    # Spread monitor — Telegram alert when the MEXC book spread widens beyond
    # the tight-book threshold (the lead-lag edge needs ~1-tick books).
    # Report-only: does NOT gate trading. Off via SPREAD_MONITOR=0.
    if alerts is not None and os.environ.get("SPREAD_MONITOR", "1") == "1":
        from src.strategy.spread_monitor import SpreadMonitor
        tasks.append(
            asyncio.create_task(
                SpreadMonitor(ob_manager, alerts).run(),
                name="spread_monitor",
            )
        )

    # Add periodic live_pool rebuild if multi-slot mode
    if live_pool is not None:
        tasks.append(
            asyncio.create_task(
                live_pool_rebuild_loop(live_pool, interval_sec=30),
                name="live_pool_rebuild",
            )
        )

    # ─── reconcile patch ─────────────────────────────────────
    # Periodic reconciliation: every 60s compare MEXC reality vs engine
    # state. Catches orphans (MEXC has position bot doesn't know about)
    # and stale engine state (engine thinks open but MEXC says closed).
    # Race protection: skips positions younger than 15s on both sides.
    if live_pool is not None:
        from src.safety.reconciliation import periodic_reconcile_loop
        tasks.append(
            asyncio.create_task(
                periodic_reconcile_loop(
                    shadow_engine=shadow_engine,
                    live_pool=live_pool,
                    alerts=alerts,
                    interval_sec=60.0,
                ),
                name="periodic_reconcile",
            )
        )

    # Raw data collector — orderbook snapshots for deep per-pair analysis.
    # Always running (lightweight), feeds analyzer.
    from src.storage.raw_data_collector import raw_data_collector_loop
    tasks.append(
        asyncio.create_task(
            raw_data_collector_loop(db, ob_manager, interval_sec=5.0),
            name="raw_data_collector",
        )
    )

    stop_waiter = None
    try:
        stop_waiter = asyncio.create_task(stop_event.wait(), name="stop_waiter")
        done, pending = await asyncio.wait(
            [*tasks, stop_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            if t.get_name() != "stop_waiter" and t.exception():
                loguru_logger.error("Task {} failed: {}", t.get_name(), t.exception())
    finally:
        loguru_logger.info("Shutting down...")
        await telegram_bot.stop()
        await binance_client.stop()
        await mexc_client.stop()
        await static_gap.stop()
        await shadow_engine.stop()
        await state_manager.stop()
        await signal_writer.stop()
        await webkey_client_pool.close_all()
        for t in tasks:
            t.cancel()
        if stop_waiter is not None:
            stop_waiter.cancel()  # ad-hoc waiter isn't in `tasks`
        await asyncio.gather(*tasks, *( [stop_waiter] if stop_waiter is not None else [] ),
                             return_exceptions=True)
        await db.close()
        loguru_logger.info("Goodbye.")


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass

    asyncio.run(main())
