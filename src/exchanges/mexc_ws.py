"""
MEXC Futures WebSocket client.

Endpoint: wss://contract.mexc.com/edge

Channels we subscribe to:
  - sub.depth   — orderbook diffs (need REST snapshot first)
  - sub.deal    — trade ticks

MEXC uses JSON commands for subscriptions:
  {"method": "sub.depth", "param": {"symbol": "BTC_USDT"}}
  {"method": "sub.deal",  "param": {"symbol": "BTC_USDT"}}

Push messages have format:
  {"channel": "push.depth", "data": {...}, "symbol": "BTC_USDT"}
  {"channel": "push.deal",  "data": {...}, "symbol": "BTC_USDT"}

Heartbeat: client must send {"method": "ping"} every 30s, server replies "pong".

Snapshot+diff sync (per MEXC docs):
  1. Connect WS, subscribe to sub.depth.
  2. Buffer push.depth messages.
  3. Fetch GET /api/v1/contract/depth/{symbol}?limit=1000, save its `version`.
  4. For each buffered diff, drop those with version <= snapshot_version.
  5. Apply the first diff with version > snapshot_version. Δv > 1 is NORMAL
     here — MEXC's `version` is a global change-counter and each push is
     self-contained (absolute levels, vol=0 = delete), so a forward jump is
     coalescing, not lost data. Only Δv >= _MAX_VERSION_GAP means we really
     missed messages -> re-snapshot.
  6. Steady state uses the SAME rule (see `_handle_depth`).
  ВИПРАВЛЕНО 2026-09-10: пункти 5-6 описували правило BINANCE (строге +1).
  Через нього активні символи (ZEC/MUSTOCK/SOXL) зависали незасинхронізованими
  на хвилини, а детектор увесь цей час читав ЗАМОРОЖЕНУ книгу MEXC.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp
import orjson
import websockets
from websockets.client import WebSocketClientProtocol

from src.config import MexcConf
from src.exchanges.mexc_rest import MexcRestClient, to_binance, to_mexc
from src.exchanges.orderbook import OrderBookManager

logger = logging.getLogger(__name__)

# Стеля правдоподібного розриву версій MEXC. Δv між сусідніми пушами штатно
# 9..300 (коалесценція, кожен пуш самодостатній); більше за це — ми, найпевніше,
# пропустили повідомлення, і книгу треба перезабрати.
# ОДНА константа на ОБИДВА вживання — усталений режим (`_handle_depth`) і злив
# буфера після знімка (`_fetch_snapshot`). Вони вже розійшлись одного разу:
# у зливі стояло суворе `v == version + 1`, і активні символи через це
# зависали незасинхронізованими на хвилини.
_MAX_VERSION_GAP = 10000


@dataclass
class Trade:
    symbol: str             # Binance-style (BTCUSDT) for unified pipeline
    exchange: str
    price: float
    size: float
    is_buyer_maker: bool
    timestamp_ms: int


TradeCallback = Callable[[Trade], Awaitable[None]]


class MexcWSClient:
    """
    MEXC Futures WebSocket client.

    Single connection serves both depth and trades.
    All symbols are stored internally in MEXC format (BTC_USDT)
    but we expose them in Binance format (BTCUSDT) externally
    so callers don't have to think about conversion.
    """

    def __init__(
        self,
        cfg: MexcConf,
        ob_manager: OrderBookManager,
        on_trade: TradeCallback | None = None,
    ) -> None:
        self.cfg = cfg
        self.ob_manager = ob_manager
        self.on_trade = on_trade

        # All internal storage in MEXC format
        self._symbols_mexc: set[str] = set()
        self._buffered_diffs: dict[str, deque[dict]] = {}
        self._snapshot_ready: dict[str, bool] = {}
        self._last_version: dict[str, int] = {}
        # deque for O(1) popleft (was list with pop(0) which is O(n))
        self._resnap_history: dict[str, deque[float]] = {}
        # Per-symbol price scale: multiply MEXC raw price by this to get
        # Binance-equivalent price. Default 1.0 (no scaling).
        # Used for tokens like 1000PEPE where Binance/MEXC price scales differ.
        self._scale_map: dict[str, float] = {}

        self._ws: WebSocketClientProtocol | None = None
        self._http: aiohttp.ClientSession | None = None
        self._stop_event = asyncio.Event()
        self._ping_task: asyncio.Task | None = None
        self._heal_task: asyncio.Task | None = None

        # Stats
        self.depth_messages = 0
        self.trade_messages = 0
        self.last_message_ts: float = 0
        self.resync_count = 0

    # ---------------- public API (Binance-style symbols) ----------------
    async def subscribe(
        self,
        symbols: list[str],
        scales: dict[str, float] | None = None,
    ) -> None:
        """
        symbols in Binance format (BTCUSDT). Internally stored as MEXC format.

        scales: optional map {binance_symbol: scale_factor}. Multiplied into
        all MEXC raw prices (orderbook, trades) before they reach downstream.
        Defaults to 1.0 (no scaling) for any symbol not in the map.

        Auto-fallback: if scales is None or doesn't contain a given symbol,
        we try the static SYMBOL_SCALE_TO_BINANCE table by MEXC symbol.
        """
        from src.exchanges.mexc_rest import SYMBOL_SCALE_TO_BINANCE

        new_mexc = {to_mexc(s.upper()) for s in symbols} - self._symbols_mexc
        self._symbols_mexc.update(new_mexc)

        for sym in new_mexc:
            self._buffered_diffs[sym] = deque(maxlen=1000)
            self._snapshot_ready[sym] = False
            # In OrderBookManager we use Binance-style symbol for unified access
            binance_sym = to_binance(sym)
            self.ob_manager.get_or_create("mexc", binance_sym, max_levels=50)

            # Determine price scale: prefer caller-provided, fallback to static table
            scale = 1.0
            if scales is not None and binance_sym in scales:
                scale = float(scales[binance_sym])
            elif sym in SYMBOL_SCALE_TO_BINANCE:
                scale = SYMBOL_SCALE_TO_BINANCE[sym]
            self._scale_map[sym] = scale
            if scale != 1.0:
                logger.info("MEXC %s using binance_scale=%g", sym, scale)

        if self._ws is not None and new_mexc:
            for sym in new_mexc:
                await self._send_sub("sub.depth", sym)
                await self._send_sub("sub.deal", sym)
                await self._fetch_snapshot(sym)

    async def unsubscribe(self, symbols: list[str]) -> None:
        gone_mexc = {to_mexc(s.upper()) for s in symbols} & self._symbols_mexc
        for sym in gone_mexc:
            self._symbols_mexc.discard(sym)
            self._buffered_diffs.pop(sym, None)
            self._snapshot_ready.pop(sym, None)
            self._last_version.pop(sym, None)
            self._scale_map.pop(sym, None)
            self._resnap_history.pop(sym, None)  # was leaked (only per-symbol deque)
            self.ob_manager.remove("mexc", to_binance(sym))
        if self._ws is not None and gone_mexc:
            for sym in gone_mexc:
                await self._send_sub("unsub.depth", sym)
                await self._send_sub("unsub.deal", sym)

    async def run(self) -> None:
        """Run until stop() is called. Auto-reconnects."""
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        delay = self.cfg.reconnect_delay_sec
        try:
            while not self._stop_event.is_set():
                try:
                    await self._connect_once()
                    delay = self.cfg.reconnect_delay_sec
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("MEXC WS error: %s — reconnecting in %ds", e, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.cfg.max_reconnect_delay_sec)
        finally:
            if self._http:
                await self._http.close()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ping_task:
            self._ping_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ---------------- internals ----------------
    async def _connect_once(self) -> None:
        url = self.cfg.ws_base
        logger.info("Connecting MEXC WS: %d symbols", len(self._symbols_mexc))
        # Той самий helper, що й у приватного каналу. Публічний фід іде на ТОЙ
        # САМИЙ хост contract.mexc.com цілодобово, тож лишити тут
        # `User-Agent: Python/3.11 websockets/13.1` означало б знецінити фікс
        # приватного каналу: два зʼєднання з однієї IP, одне «браузерне», друге
        # ні, — це гірше, ніж два однакових.
        from src.exchanges.mexc_private_ws import _ws_header_kwargs
        async with websockets.connect(
            url,
            ping_interval=None,    # MEXC has its own ping
            close_timeout=5,
            max_size=10 * 1024 * 1024,
            compression=None,  # disable permessage-deflate: cuts decode CPU on hot WS path; data identical, +bandwidth (fine on VPS)
            **_ws_header_kwargs(None),
        ) as ws:
            self._ws = ws
            # On (re)connect — reset state
            for s in self._symbols_mexc:
                self._snapshot_ready[s] = False
                self._buffered_diffs[s] = deque(maxlen=1000)
                self._last_version.pop(s, None)

            # Subscribe to all symbols
            for sym in self._symbols_mexc:
                await self._send_sub("sub.depth", sym)
                await self._send_sub("sub.deal", sym)

            # Fetch snapshots SEQUENTIALLY with throttling — MEXC rate-limits code 510.
            # 11 of 15 worked when parallel; safer to do one at a time with 250ms gap.
            for sym in list(self._symbols_mexc):
                await self._fetch_snapshot(sym)
                await asyncio.sleep(0.25)  # 4 req/sec is well within MEXC limits

            # Start ping loop
            self._ping_task = asyncio.create_task(self._ping_loop(ws))

            # Start self-heal loop — re-fetches snapshots that failed initially
            self._heal_task = asyncio.create_task(self._self_heal_loop())

            try:
                async for raw in ws:
                    if self._stop_event.is_set():
                        break
                    self.last_message_ts = time.time()
                    try:
                        msg = orjson.loads(raw)
                        await self._dispatch(msg)
                    except Exception as e:
                        logger.exception("MEXC dispatch error: %s", e)
            finally:
                if self._ping_task:
                    self._ping_task.cancel()
                    try:
                        await self._ping_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    self._ping_task = None
                if self._heal_task:
                    self._heal_task.cancel()
                    try:
                        await self._heal_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    self._heal_task = None

        self._ws = None

    async def _ping_loop(self, ws: WebSocketClientProtocol) -> None:
        """Send ping every 25s — MEXC requires application-level ping."""
        try:
            while True:
                await asyncio.sleep(25)
                try:
                    await ws.send(orjson.dumps({"method": "ping"}).decode())
                except Exception as e:
                    logger.warning("MEXC ping failed: %s", e)
                    break
        except asyncio.CancelledError:
            return

    async def _self_heal_loop(self) -> None:
        """
        Periodically check for symbols that aren't synced and re-fetch their snapshot.
        Runs every 30 seconds. Throttles to 1 retry per symbol per minute.
        """
        try:
            while True:
                await asyncio.sleep(30)
                unsynced = [
                    sym for sym in self._symbols_mexc
                    if not self._snapshot_ready.get(sym, False)
                ]
                if not unsynced:
                    continue
                logger.info("MEXC self-heal: %d unsynced symbols → retrying: %s",
                            len(unsynced), unsynced)
                # Sequential with throttle — same logic as initial sync
                for sym in unsynced:
                    if self._can_resnap(sym):
                        await self._fetch_snapshot(sym)
                        await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception("MEXC self-heal loop error: %s", e)

    async def _send_sub(self, method: str, mexc_symbol: str) -> None:
        if not self._ws:
            return
        msg = {"method": method, "param": {"symbol": mexc_symbol}}
        await self._ws.send(orjson.dumps(msg).decode())

    # ---------------- snapshot ----------------
    async def _fetch_snapshot(self, mexc_symbol: str) -> None:
        assert self._http is not None
        try:
            async with MexcRestClient(self._http) as rest:
                snap = await rest.get_depth(mexc_symbol, limit=200)
        except Exception as e:
            logger.error("MEXC snapshot failed for %s: %s", mexc_symbol, e)
            self._snapshot_ready[mexc_symbol] = False
            return

        # MEXC depth format: {bids: [[price, vol, count], ...], asks: [...], version: int}
        version = int(snap.get("version", 0))
        bids_raw = snap.get("bids", []) or []
        asks_raw = snap.get("asks", []) or []
        # Scale MEXC raw prices to Binance-equivalent (e.g. PEPE: ×1000).
        scale = self._scale_map.get(mexc_symbol, 1.0)
        # MEXC bids/asks: [price, vol_in_contracts, num_orders]
        # We treat vol_in_contracts as size (downstream OB uses generic floats)
        bids = [(float(item[0]) * scale, float(item[1])) for item in bids_raw]
        asks = [(float(item[0]) * scale, float(item[1])) for item in asks_raw]

        binance_sym = to_binance(mexc_symbol)
        ob = self.ob_manager.get_or_create("mexc", binance_sym, max_levels=50)
        ob.apply_snapshot(bids=bids, asks=asks, update_id=version)

        # Drain buffered diffs
        buf = self._buffered_diffs[mexc_symbol]
        applied = 0
        while buf:
            diff = buf.popleft()
            v = int(diff.get("version", 0))
            if v <= version:
                continue
            # ТА САМА рамка, що і в усталеному режимі (`_handle_depth`), і це
            # НЕ косметика. Тут стояло `v == version + 1` — правило BINANCE,
            # де дифи інкрементні й строго послідовні. У MEXC `version` — це
            # ГЛОБАЛЬНИЙ лічильник змін, Δv між сусідніми пушами штатно 9..300
            # (див. коментар у `_handle_depth`), а кожен пуш самодостатній
            # (абсолютні рівні, vol=0 = видалення). Тому суворе `+1` оголошувало
            # `stale` будь-який АКТИВНИЙ символ.
            # ВИМІРЯНО на primary 2026-09-10: ZEC_USDT — 13 невдалих спроб
            # поспіль (розриви 2-86), книга не оновлювалась 6 хв 51 с, і за цей
            # час детектор випустив 403 сигнали ZECUSDT, порівнюючи свіжий
            # Binance із замороженою MEXC. Те саме з MUSTOCK_USDT і SOXL_USDT —
            # тобто рівно з найактивнішими символами.
            _gap = v - version - 1
            if _gap >= _MAX_VERSION_GAP:
                logger.warning(
                    "MEXC %s snapshot stale (diff version=%d, snapshot=%d, gap=%d)",
                    mexc_symbol, v, version, _gap,
                )
                self._buffered_diffs[mexc_symbol].clear()
                self._snapshot_ready[mexc_symbol] = False
                self.resync_count += 1
                return
            self._apply_depth_diff(mexc_symbol, diff)
            applied += 1
            self._last_version[mexc_symbol] = v
            break
        while buf:
            diff = buf.popleft()
            self._apply_depth_diff(mexc_symbol, diff)
            applied += 1

        self._snapshot_ready[mexc_symbol] = True
        logger.info("MEXC %s synced (snapshot version=%d, applied %d buffered)", mexc_symbol, version, applied)

    # ---------------- dispatch ----------------
    async def _dispatch(self, msg: dict) -> None:
        # Pong / sub-ack messages
        ch = msg.get("channel")
        if not ch:
            return
        if ch == "pong":
            return
        if ch.startswith("rs."):
            # rs.sub.depth, rs.error, rs.login etc — ack/error responses
            if ch == "rs.error":
                logger.warning("MEXC error response: %s", msg)
            return

        if ch == "push.depth":
            await self._handle_depth(msg)
        elif ch == "push.deal":
            await self._handle_trade(msg)
        # other channels we don't subscribe to — ignore

    async def _handle_depth(self, msg: dict) -> None:
        self.depth_messages += 1
        mexc_symbol = msg.get("symbol")
        if not mexc_symbol or mexc_symbol not in self._symbols_mexc:
            return
        data = msg.get("data", {})
        # data has: {bids, asks, version, ...} — same shape as snapshot
        if not self._snapshot_ready.get(mexc_symbol, False):
            self._buffered_diffs[mexc_symbol].append(data)
            return

        v = int(data.get("version", 0))
        prev = self._last_version.get(mexc_symbol)

        # Sequence handling. MEXC's `version` is a GLOBAL change-counter, NOT a
        # per-message id: each push.depth carries the NET changed levels for the
        # window (absolute per-level value; vol=0 = delete), coalesced since the
        # previous push, and `version` is the latest change-id included. So Δv
        # between consecutive messages is normally >1 (= number of coalesced
        # changes — confirmed live: Δv≈9..300) and is NOT lost data: every
        # message is self-contained, so applying them in arrival order keeps
        # full book coverage (deletes arrive as vol=0 entries within the push).
        #   - v <= prev      : stale / duplicate → skip
        #   - v  > prev      : apply (forward Δv is normal coalescing, see above)
        #   - Δv >= 10000    : implausibly large → we likely missed messages
        #                      (stall / dropped frames) → re-snapshot to be safe.
        if prev is not None:
            if v <= prev:
                return  # stale or duplicate
            gap = v - prev - 1
            if 0 < gap < _MAX_VERSION_GAP:
                pass  # normal coalescing — apply as-is (not a data gap)
            elif gap >= _MAX_VERSION_GAP:
                # huge gap — likely server reset or our side stalled
                if not self._can_resnap(mexc_symbol):
                    return  # rate-limited; skip and try later
                logger.warning("MEXC %s large gap: v=%d, prev=%d (gap=%d) → re-snapshot",
                               mexc_symbol, v, prev, gap)
                self._snapshot_ready[mexc_symbol] = False
                self._buffered_diffs[mexc_symbol].clear()
                self._last_version.pop(mexc_symbol, None)
                self.resync_count += 1
                asyncio.create_task(self._fetch_snapshot(mexc_symbol))
                return

        self._apply_depth_diff(mexc_symbol, data)
        self._last_version[mexc_symbol] = v

        # Content-corruption guard. The Delta-v check above only catches
        # SEQUENCE gaps; a limited-depth diff can strand a stale top level with
        # the sequence fully intact (MEXC HYPE: a 68.843 bid lingered for days
        # as the ask tracked 66.3 -> crossed book -> phantom shorts in shadow).
        # Detect the cross and re-snapshot from REST (rate-limited 3/min/sym).
        _ob = self.ob_manager.get("mexc", to_binance(mexc_symbol))
        if _ob is not None and _ob.is_crossed() and self._can_resnap(mexc_symbol):
            logger.warning("MEXC %s crossed book (bid=%.6f >= ask=%.6f) -> re-snapshot",
                           mexc_symbol, _ob._top_bid_price, _ob._top_ask_price)
            self._snapshot_ready[mexc_symbol] = False
            self._buffered_diffs[mexc_symbol].clear()
            self._last_version.pop(mexc_symbol, None)
            self.resync_count += 1
            asyncio.create_task(self._fetch_snapshot(mexc_symbol))

    def _can_resnap(self, mexc_symbol: str) -> bool:
        """Rate-limit re-snapshots: at most 3 per minute per symbol."""
        now = time.time()
        history = self._resnap_history.setdefault(mexc_symbol, deque())
        # purge older than 60s — deque.popleft is O(1), unlike list.pop(0)
        cutoff = now - 60
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) >= 3:
            return False
        history.append(now)
        return True

    def _apply_depth_diff(self, mexc_symbol: str, data: dict) -> None:
        bids_raw = data.get("bids", []) or []
        asks_raw = data.get("asks", []) or []
        # Scale MEXC raw prices to Binance-equivalent (e.g. PEPE: ×1000).
        # For non-aliased pairs scale=1.0 → no-op.
        scale = self._scale_map.get(mexc_symbol, 1.0)
        # MEXC sends absolute updates; vol=0 means delete
        bids = [(float(item[0]) * scale, float(item[1])) for item in bids_raw]
        asks = [(float(item[0]) * scale, float(item[1])) for item in asks_raw]
        v = int(data.get("version", 0))

        binance_sym = to_binance(mexc_symbol)
        ob = self.ob_manager.get_or_create("mexc", binance_sym, max_levels=50)
        ob.apply_diff(bids=bids, asks=asks, first_update_id=v, final_update_id=v)

    async def _handle_trade(self, msg: dict) -> None:
        self.trade_messages += 1
        if self.on_trade is None:
            return
        mexc_symbol = msg.get("symbol")
        if not mexc_symbol or mexc_symbol not in self._symbols_mexc:
            return

        # MEXC sends data as a LIST of trades (one push can carry many).
        # Per real-world payload:
        #   {'p': 77980.1, 'v': 167, 'T': 1, 'O': 3, 'M': 2, 't': ..., 'i': '...'}
        # Field meanings:
        #   p — price
        #   v — volume (in contracts; convert via contractSize for base coin qty)
        #   T — taker side: 1=buy, 2=sell
        #   t — timestamp ms
        data = msg.get("data", [])
        if isinstance(data, dict):
            # defensive: handle single-trade case if MEXC ever changes format
            data = [data]

        binance_sym = to_binance(mexc_symbol)
        # Scale MEXC raw price to Binance-equivalent (e.g. PEPE: ×1000).
        scale = self._scale_map.get(mexc_symbol, 1.0)
        for item in data:
            try:
                taker_side = int(item.get("T", 0))
                ts = int(item.get("t", time.time() * 1000))
                trade = Trade(
                    symbol=binance_sym,
                    exchange="mexc",
                    price=float(item["p"]) * scale,
                    size=float(item["v"]),
                    # is_buyer_maker = True when the buyer was a passive maker.
                    # If taker_side == 2 (sell), it means an aggressive seller hit a resting bid → buyer was maker.
                    is_buyer_maker=(taker_side == 2),
                    timestamp_ms=ts,
                )
                await self.on_trade(trade)
            except (KeyError, ValueError, TypeError) as e:
                logger.debug("MEXC trade parse error: %s item=%s", e, item)

    # ---- diagnostics ----
    def stats(self) -> dict:
        return {
            "subscribed_symbols": len(self._symbols_mexc),
            "depth_messages": self.depth_messages,
            "trade_messages": self.trade_messages,
            "messages_received": self.depth_messages + self.trade_messages,
            "last_message_age_sec": time.time() - self.last_message_ts if self.last_message_ts else None,
            "resync_count": self.resync_count,
            "synced_books": sum(1 for ready in self._snapshot_ready.values() if ready),
            "connected": self._ws is not None,
        }
