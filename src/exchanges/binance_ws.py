"""
Binance USD-M Futures WebSocket client.

After Binance's 2026-04-23 endpoint reorganization, streams are split
across routed paths. A single connection only delivers data from streams
that belong to its routed path. We run TWO parallel WebSocket
connections:

  Connection A — wss://fstream.binance.com/stream
                 streams: <symbol>@depth@100ms        (legacy, /public)
  Connection B — wss://fstream.binance.com/market/stream
                 streams: <symbol>@aggTrade           (/market)

Both feed the same OrderBookManager and the same on_trade callback.
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

# If the resnap rate-limit (3/min/symbol) is hit during a SUSTAINED sequence-break
# storm, the book can otherwise freeze while is_synced stays True (consumers gate
# only on is_synced). Force one resnap past the cap once the book is this stale,
# so a frozen-but-"live" Binance reference price can't exceed ~this bound.
_RESNAP_FORCE_STALE_MS = 3000

# Як часто перевіряти книги, що лишились без знімка (дзеркало mexc_ws:277).
# 30с — той самий крок, що в MEXC; вибір не переміряний, узятий заради
# однаковості двох фідів.
SELF_HEAL_INTERVAL_SEC = 30.0
from websockets.client import WebSocketClientProtocol

from src.config import BinanceConf
from src.exchanges.orderbook import OrderBookManager

logger = logging.getLogger(__name__)

# Скільки секунд діагностика bookTicker дивиться назад. Раніше вікна не було
# зовсім — дек жив від старту процесу, тож `mean` не міг відновитись після
# жодного шторму і брехав добами.
BT_DIAG_WINDOW_SEC = 600


@dataclass
class Trade:
    symbol: str
    exchange: str
    price: float
    size: float
    is_buyer_maker: bool   # True = aggressive seller hit a bid
    timestamp_ms: int


TradeCallback = Callable[[Trade], Awaitable[None]]


class BinanceWSClient:
    """
    Binance Futures WebSocket client with snapshot+diff sync.
    Maintains two WS connections in parallel:
      - depth on /stream (public path)
      - aggTrade on /market/stream
    """

    DEPTH_PATH = "/stream"
    TRADES_PATH = "/market/stream"

    @staticmethod
    def _normalize_base(ws_base: str) -> str:
        """Strip trailing path segments — we add /stream or /market/stream ourselves."""
        base = ws_base.rstrip("/")
        # Common misconfigurations: trailing /stream, /market/stream, /ws
        for suffix in ("/market/stream", "/stream", "/ws"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        return base.rstrip("/")

    def __init__(
        self,
        cfg: BinanceConf,
        ob_manager: OrderBookManager,
        on_trade: TradeCallback | None = None,
    ) -> None:
        self.cfg = cfg
        self.ob_manager = ob_manager
        self.on_trade = on_trade

        self._symbols: set[str] = set()
        self._buffered_diffs: dict[str, deque[dict]] = {}
        self._snapshot_ready: dict[str, bool] = {}
        self._previous_final_id: dict[str, int] = {}
        # Re-snapshot rate-limit (mirror of mexc_ws): a burst of sequence breaks
        # (packet loss/reorder) must not fire unbounded concurrent REST snapshots.
        self._resnap_history: dict[str, deque[float]] = {}

        self._ws_depth: WebSocketClientProtocol | None = None
        self._ws_trades: WebSocketClientProtocol | None = None
        self._http: aiohttp.ClientSession | None = None
        self._stop_event = asyncio.Event()

        # Stats
        self.depth_messages = 0
        self.trade_messages = 0
        self.last_message_ts: float = 0
        self.resync_count = 0

        # ─── bookTicker (optional 3rd WS subscription) ─────────────────
        # Real-time top-of-book stream — Binance pushes on every change
        # (not throttled to 100ms like @depth). Two modes:
        #   - diagnostic only (book_ticker_enabled): logs how much earlier
        #     bookTicker sees a top-of-book change than @depth@100ms
        #   - feed mode (book_ticker_feed_enabled): bookTicker also updates
        #     the OrderBook's cached top-of-book directly, so the detector
        #     wakes earlier; full depth diffs still maintain the deep book
        self._ws_book_ticker: WebSocketClientProtocol | None = None
        self._book_ticker_enabled: bool = bool(
            getattr(cfg, "book_ticker_enabled", False)
        )
        self._book_ticker_feed: bool = bool(
            getattr(cfg, "book_ticker_feed_enabled", False)
        ) and self._book_ticker_enabled
        # Last bookTicker top-of-book observation per symbol:
        #   (bid_price, ask_price, observed_at_ms)
        self._bt_last: dict[str, tuple[float, float, int]] = {}
        # Latency advantage samples in ms (bookTicker saw change N ms earlier
        # than depth diff). Bounded to avoid unbounded memory.
        # ВІКНО ЗА ЧАСОМ, не за кількістю. `deque(maxlen=2000)` ніколи не
        # обертався (найбільше спостережене n=592 за 12 годин), тож `mean`
        # був КУМУЛЯТИВНИЙ від старту процесу: один шторм отруював діагностику
        # на добу вперед, а рядок друкувався ідентичним годинами.
        # Тепер зберігаємо (ts_ms, advantage_ms) і ріжемо за віком.
        self._bt_latency_advantage_ms: deque[tuple[int, int]] = deque(maxlen=20000)
        self.book_ticker_messages = 0
        # How many bookTicker frames actually MOVED the top (feed mode). The
        # gap between this and book_ticker_messages is the throttle we get for
        # free: most frames only restate a top we already have.
        self.book_ticker_top_updates = 0
        # Number of depth diffs where a matching bookTicker observation
        # was found (and thus a latency sample was recorded).
        self.book_ticker_matched = 0

    # ---------------- public API ----------------
    async def subscribe(self, symbols: list[str]) -> None:
        """Add symbols. Re-subscribes both connections if already running."""
        new_symbols = {s.upper() for s in symbols} - self._symbols
        self._symbols.update(new_symbols)
        for sym in new_symbols:
            self._buffered_diffs[sym] = deque(maxlen=1000)
            self._snapshot_ready[sym] = False
            self.ob_manager.get_or_create("binance", sym, max_levels=self.cfg.depth_levels)

        if self._ws_depth is not None and new_symbols:
            await self._send_subscribe_depth(list(new_symbols))
            for sym in new_symbols:
                await self._fetch_snapshot(sym)
        if self._ws_trades is not None and new_symbols:
            await self._send_subscribe_trades(list(new_symbols))
        if self._ws_book_ticker is not None and new_symbols:
            await self._send_subscribe_book_ticker(list(new_symbols))

    async def unsubscribe(self, symbols: list[str]) -> None:
        gone = {s.upper() for s in symbols} & self._symbols
        for sym in gone:
            self._symbols.discard(sym)
            self._buffered_diffs.pop(sym, None)
            self._snapshot_ready.pop(sym, None)
            self._previous_final_id.pop(sym, None)
            self._bt_last.pop(sym, None)
            self._resnap_history.pop(sym, None)  # was leaked (only per-symbol deque)
            self.ob_manager.remove("binance", sym)
        if self._ws_depth is not None and gone:
            await self._send_unsubscribe_depth(list(gone))
        if self._ws_trades is not None and gone:
            await self._send_unsubscribe_trades(list(gone))
        if self._ws_book_ticker is not None and gone:
            await self._send_unsubscribe_book_ticker(list(gone))

    async def run(self) -> None:
        """Run both connections until stop() is called. Auto-reconnect on errors."""
        self._http = aiohttp.ClientSession()
        try:
            coros = [
                self._run_loop("depth", self.DEPTH_PATH, self._depth_streams_for_url, self._handle_depth_msg),
                self._run_loop("trades", self.TRADES_PATH, self._trade_streams_for_url, self._handle_trade_msg),
            ]
            if self._book_ticker_enabled:
                logger.info(
                    "Binance bookTicker diagnostic stream ENABLED — "
                    "subscribing to <symbol>@bookTicker for latency comparison"
                    "%s",
                    " + FEED MODE (updating OB top-of-book)" if self._book_ticker_feed else "",
                )
                coros.append(
                    self._run_loop(
                        "book_ticker",
                        self.DEPTH_PATH,  # bookTicker lives on /stream
                        self._book_ticker_streams_for_url,
                        self._handle_book_ticker_msg,
                    )
                )
                interval = getattr(self.cfg, "book_ticker_log_interval_sec", 60)
                if interval > 0:
                    coros.append(self._book_ticker_summary_loop(interval))
            coros.append(self._self_heal_loop())
            await asyncio.gather(*coros, return_exceptions=False)
        finally:
            if self._http:
                await self._http.close()

    async def stop(self) -> None:
        self._stop_event.set()
        for ws in (self._ws_depth, self._ws_trades, self._ws_book_ticker):
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

    # ---------------- internals: connection loop ----------------
    async def _run_loop(
        self,
        name: str,
        path: str,
        url_builder: Callable[[], str],
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        delay = self.cfg.reconnect_delay_sec
        while not self._stop_event.is_set():
            try:
                await self._connect_once(name, path, url_builder(), handler)
                delay = self.cfg.reconnect_delay_sec
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Binance WS [%s] error: %s — reconnecting in %ds", name, e, delay)
                if name == "book_ticker":
                    # ОБОВʼЯЗКОВО СКИНУТИ ЗНІМКИ.
                    #
                    # `_bt_last` тримає (bid, ask, seen_at_ms) на символ і до
                    # 2026-09-04 попався ЛИШЕ в `unsubscribe()` — на реконекті
                    # ніколи. Через це поки стрім лежав у бекофі, перший же
                    # depth-рух топу писав у `advantage_ms` не фору фіду, а
                    # ВІК ОБРИВУ: спостережені 59951/59683/59418 мс проти
                    # `max_reconnect_delay_sec: 60`, тобто метрика впиралась
                    # рівно в стелю бекофу. Це згенерувало хибну тривогу
                    # «фід деградував у 600 разів» 04.09.
                    self._bt_last.clear()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.cfg.max_reconnect_delay_sec)

    async def _connect_once(
        self,
        name: str,
        path: str,
        url: str,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        logger.info("Connecting Binance WS [%s]: %d streams via %s", name, len(self._symbols), path)
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=10 * 1024 * 1024,
            compression=None,  # disable permessage-deflate: cuts decode CPU on hot WS path; data identical, +bandwidth (fine on VPS)
        ) as ws:
            if name == "depth":
                self._ws_depth = ws
                # On (re)connect — re-fetch all snapshots so the buffer→snapshot handshake works
                self._snapshot_ready = {s: False for s in self._symbols}
                self._previous_final_id.clear()
                for s in list(self._symbols):
                    self._buffered_diffs[s] = deque(maxlen=1000)
                await asyncio.gather(
                    *(self._fetch_snapshot(sym) for sym in self._symbols),
                    return_exceptions=True,
                )
            elif name == "book_ticker":
                self._ws_book_ticker = ws
                # No snapshot needed — bookTicker is stateless top-of-book stream.
            else:
                self._ws_trades = ws

            async for raw in ws:
                if self._stop_event.is_set():
                    break
                self.last_message_ts = time.time()
                try:
                    msg = orjson.loads(raw)
                    if "stream" not in msg or "data" not in msg:
                        continue  # subscription ack or error
                    await handler(msg)
                except Exception as e:
                    logger.exception("Binance [%s] dispatch error: %s", name, e)

        if name == "depth":
            self._ws_depth = None
        elif name == "book_ticker":
            self._ws_book_ticker = None
        else:
            self._ws_trades = None

    # ---------------- URL builders ----------------
    def _depth_streams_for_url(self) -> str:
        base = self._normalize_base(self.cfg.ws_base)
        if not self._symbols:
            return f"{base}{self.DEPTH_PATH}?streams=btcusdt@depth@{self.cfg.depth_update_speed_ms}ms"
        streams = [f"{s.lower()}@depth@{self.cfg.depth_update_speed_ms}ms" for s in self._symbols]
        return f"{base}{self.DEPTH_PATH}?streams={'/'.join(streams)}"

    def _trade_streams_for_url(self) -> str:
        base = self._normalize_base(self.cfg.ws_base)
        if not self._symbols:
            return f"{base}{self.TRADES_PATH}?streams=btcusdt@aggTrade"
        streams = [f"{s.lower()}@aggTrade" for s in self._symbols]
        return f"{base}{self.TRADES_PATH}?streams={'/'.join(streams)}"

    def _book_ticker_streams_for_url(self) -> str:
        """bookTicker lives on /stream like @depth. Real-time push on every
        top-of-book change (no throttling). Format: <symbol>@bookTicker.
        """
        base = self._normalize_base(self.cfg.ws_base)
        if not self._symbols:
            return f"{base}{self.DEPTH_PATH}?streams=btcusdt@bookTicker"
        streams = [f"{s.lower()}@bookTicker" for s in self._symbols]
        return f"{base}{self.DEPTH_PATH}?streams={'/'.join(streams)}"

    # ---------------- SUBSCRIBE / UNSUBSCRIBE messages ----------------
    async def _send_subscribe_depth(self, symbols: list[str]) -> None:
        if not self._ws_depth:
            return
        params = [f"{s.lower()}@depth@{self.cfg.depth_update_speed_ms}ms" for s in symbols]
        msg = {"method": "SUBSCRIBE", "params": params, "id": int(time.time())}
        await self._ws_depth.send(orjson.dumps(msg).decode())

    async def _send_unsubscribe_depth(self, symbols: list[str]) -> None:
        if not self._ws_depth:
            return
        params = [f"{s.lower()}@depth@{self.cfg.depth_update_speed_ms}ms" for s in symbols]
        msg = {"method": "UNSUBSCRIBE", "params": params, "id": int(time.time())}
        await self._ws_depth.send(orjson.dumps(msg).decode())

    async def _send_subscribe_trades(self, symbols: list[str]) -> None:
        if not self._ws_trades:
            return
        params = [f"{s.lower()}@aggTrade" for s in symbols]
        msg = {"method": "SUBSCRIBE", "params": params, "id": int(time.time())}
        await self._ws_trades.send(orjson.dumps(msg).decode())

    async def _send_unsubscribe_trades(self, symbols: list[str]) -> None:
        if not self._ws_trades:
            return
        params = [f"{s.lower()}@aggTrade" for s in symbols]
        msg = {"method": "UNSUBSCRIBE", "params": params, "id": int(time.time())}
        await self._ws_trades.send(orjson.dumps(msg).decode())

    async def _send_subscribe_book_ticker(self, symbols: list[str]) -> None:
        if not self._ws_book_ticker:
            return
        params = [f"{s.lower()}@bookTicker" for s in symbols]
        msg = {"method": "SUBSCRIBE", "params": params, "id": int(time.time())}
        await self._ws_book_ticker.send(orjson.dumps(msg).decode())

    async def _send_unsubscribe_book_ticker(self, symbols: list[str]) -> None:
        if not self._ws_book_ticker:
            return
        params = [f"{s.lower()}@bookTicker" for s in symbols]
        msg = {"method": "UNSUBSCRIBE", "params": params, "id": int(time.time())}
        await self._ws_book_ticker.send(orjson.dumps(msg).decode())

    # ---------------- snapshot ----------------
    async def _fetch_snapshot(self, symbol: str) -> None:
        assert self._http is not None
        url = f"{self.cfg.rest_base}/fapi/v1/depth"
        params = {"symbol": symbol, "limit": min(1000, max(self.cfg.depth_levels * 2, 100))}
        try:
            async with self._http.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as e:
            logger.error("Binance snapshot failed for %s: %s", symbol, e)
            self._snapshot_ready[symbol] = False
            return

        last_update_id = int(data["lastUpdateId"])
        bids = [(float(p), float(s)) for p, s in data.get("bids", [])]
        asks = [(float(p), float(s)) for p, s in data.get("asks", [])]

        ob = self.ob_manager.get_or_create("binance", symbol, max_levels=self.cfg.depth_levels)
        ob.apply_snapshot(bids=bids, asks=asks, update_id=last_update_id)

        buf = self._buffered_diffs[symbol]
        applied = 0
        while buf:
            diff = buf.popleft()
            U = int(diff["U"])
            u = int(diff["u"])
            if u <= last_update_id:
                continue
            if U <= last_update_id + 1 <= u:
                self._apply_depth_diff(symbol, diff, expect_pu=False)
                applied += 1
                self._previous_final_id[symbol] = u
                break
            else:
                logger.warning("%s snapshot stale (U=%d, lastUpdateId=%d), re-fetching", symbol, U, last_update_id)
                self._buffered_diffs[symbol].clear()
                self._snapshot_ready[symbol] = False
                self.resync_count += 1
                return
        while buf:
            diff = buf.popleft()
            self._apply_depth_diff(symbol, diff, expect_pu=True)
            applied += 1

        self._snapshot_ready[symbol] = True
        logger.info("Binance %s synced (snapshot id=%d, applied %d buffered)", symbol, last_update_id, applied)

    # ---------------- handlers ----------------
    async def _handle_depth_msg(self, msg: dict) -> None:
        self.depth_messages += 1
        data = msg["data"]
        symbol = data.get("s")
        if symbol not in self._symbols:
            return

        if not self._snapshot_ready.get(symbol, False):
            self._buffered_diffs[symbol].append(data)
            return

        self._apply_depth_diff(symbol, data, expect_pu=True)

    async def _self_heal_loop(self) -> None:
        """Періодично перезабирати знімок для книг, які лишились без нього.

        НАВІЩО — це був ТЕРМІНАЛЬНИЙ стан, а не транзієнт. `_fetch_snapshot`
        на невдачі HTTP (:383) і на протухлому знімку (:409) ставить
        `_snapshot_ready=False` і виходить, а більше ніхто його не кличе:
        `_handle_depth_msg` при `not ready` лише БУФЕРИЗУЄ диф і повертається
        (:430), тож `_apply_depth_diff` не виконується — і наявний self-heal
        по розриву послідовності (:462) не спрацьовує НІКОЛИ. Книга мертва до
        рестарту процесу.

        ВИМІРЯНО 2026-09-10 16:10 UTC, через 2 год після рестарту primary:
            primary  BIN synced=7/23   resyncs=297    MEX 23/23
            клон     BIN synced=22/23  resyncs=332    MEX 23/23
        MEXC на обох цілий саме тому, що в `mexc_ws` цей цикл є з самого
        початку; це його дзеркало.

        ЧОМУ ЦЕ БУЛО ТИХО. Детектор гейтить на `OrderBook.is_synced`, який
        виставляється в `apply_snapshot` і НІКОЛИ не скидається назад, а топ
        книги тримає свіжим bookTicker-фід. Тому сигнали йшли як ні в чому не
        бувало (виміряно: розподіл по символах на обох боксах збігається), і
        псувалась лише ДРАБИНА нижче топу — тобто фічі глибини в
        `signal_features`, shadow-симуляція і захист від схрещеної книги.
        """
        try:
            while True:
                await asyncio.sleep(SELF_HEAL_INTERVAL_SEC)
                if self._stop_event.is_set():
                    return
                # Поки depth-сокет не піднято, «немає знімка» означає «ще не
                # стартували», а не «зламалось». Знімок без живого потоку дифів
                # однаково протухне, тож не смикаємо біржу дарма.
                if self._ws_depth is None:
                    continue
                unsynced = [s for s in list(self._symbols)
                            if not self._snapshot_ready.get(s, False)]
                if not unsynced:
                    continue
                logger.warning(
                    "Binance self-heal: %d книг без знімка → перезабираю: %s",
                    len(unsynced), sorted(unsynced))
                for sym in unsynced:
                    if self._stop_event.is_set():
                        return
                    # Той самий тротл 3/хв/символ, що й у решти шляхів resnap —
                    # інакше 16 мертвих книг дали б чергу запитів до /fapi.
                    if self._can_resnap(sym):
                        await self._fetch_snapshot(sym)
                        await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        except Exception:
            # Цикл-лікар не має вбивати WS-задачі: `run()` збирає їх через
            # gather(return_exceptions=False), тож виняток тут поклав би ВЕСЬ
            # фід Binance — рівно те, що ми лікуємо.
            logger.exception("Binance self-heal loop error")

    def _can_resnap(self, symbol: str) -> bool:
        """Rate-limit re-snapshots: at most 3 per minute per symbol (mirror of
        mexc_ws._can_resnap). Prevents a storm of sequence breaks from firing
        unbounded concurrent /fapi/v1/depth requests."""
        now = time.time()
        history = self._resnap_history.setdefault(symbol, deque())
        cutoff = now - 60
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) >= 3:
            return False
        history.append(now)
        return True

    def _apply_depth_diff(self, symbol: str, data: dict, expect_pu: bool) -> None:
        U = int(data["U"])
        u = int(data["u"])
        pu = int(data.get("pu", 0))

        if expect_pu:
            prev = self._previous_final_id.get(symbol)
            if prev is not None and pu != prev:
                # Gate FIRST and, when rate-limited, return WITHOUT clearing
                # `prev` so the next diff re-detects the break and retries once
                # the 60s window frees — clearing prev here would leave the book
                # stuck not-ready forever (mirror of mexc_ws ordering).
                if not self._can_resnap(symbol):
                    # Rate-limited: normally return and let the next diff retry
                    # once the window frees. BUT don't keep serving a FROZEN book
                    # as live — if it has gone stale past the bound (a sustained
                    # storm where every diff is out-of-sequence, so apply_diff
                    # never refreshes it), force one resnap past the cap. Clearing
                    # `prev` below then gates further forces until the snapshot lands.
                    ob_cur = self.ob_manager.get("binance", symbol)
                    stale_ms = (
                        int(time.time() * 1000) - ob_cur.last_update_ts_ms
                        if ob_cur is not None and ob_cur.last_update_ts_ms else 0
                    )
                    if stale_ms < _RESNAP_FORCE_STALE_MS:
                        logger.warning("%s sequence break: pu=%d != prev_u=%d → resnap RATE-LIMITED (>3/min), retry later", symbol, pu, prev)
                        return
                    logger.warning("%s sequence break: book stale %dms → FORCING resnap past rate-limit", symbol, stale_ms)
                else:
                    logger.warning("%s sequence break: pu=%d != prev_u=%d → re-snapshot", symbol, pu, prev)
                self._snapshot_ready[symbol] = False
                self._buffered_diffs[symbol].clear()
                self._previous_final_id.pop(symbol, None)
                self.resync_count += 1
                asyncio.create_task(self._fetch_snapshot(symbol))
                return

        # Crossed-book self-heal — the Binance mirror of the MEXC check.
        # A stale top level can be stranded with the SEQUENCE FULLY INTACT, so
        # the break-detector above never fires and the book stays inverted
        # indefinitely. 2026-07-27: the PEPE ask froze at 0.0029230 while the
        # bid tracked up to 0.0029600 (370 ticks crossed) -> the detector saw a
        # ~370-tick phantom gap (normal is 5-8) and the bot opened 28 real
        # SHORTs in a row, about -$4.8 in 35 minutes. Re-snapshot from REST
        # (rate-limited 3/min/symbol, same as sequence breaks).
        _ob_x = self.ob_manager.get("binance", symbol)
        if _ob_x is not None and _ob_x.is_crossed() and self._can_resnap(symbol):
            logger.warning(
                "BINANCE %s crossed book (bid=%.8f >= ask=%.8f) -> re-snapshot",
                symbol, _ob_x._top_bid_price, _ob_x._top_ask_price)
            self._snapshot_ready[symbol] = False
            self._buffered_diffs[symbol].clear()
            self._previous_final_id.pop(symbol, None)
            self.resync_count += 1
            asyncio.create_task(self._fetch_snapshot(symbol))
            return

        bids = [(float(p), float(s)) for p, s in data.get("b", [])]
        asks = [(float(p), float(s)) for p, s in data.get("a", [])]

        ob = self.ob_manager.get_or_create("binance", symbol, max_levels=self.cfg.depth_levels)
        # Capture top-of-book BEFORE applying diff so we can detect whether
        # this diff actually changed it (bookTicker comparison is meaningful
        # only when top-of-book moved).
        prev_top = None
        if self._book_ticker_enabled:
            bb = ob.best_bid()
            ba = ob.best_ask()
            prev_top = (bb.price if bb else 0.0, ba.price if ba else 0.0)

        ob.apply_diff(bids=bids, asks=asks, first_update_id=U, final_update_id=u)
        self._previous_final_id[symbol] = u

        # bookTicker latency comparison (diagnostic-only, no production effect)
        if self._book_ticker_enabled and prev_top is not None:
            self._record_book_ticker_latency_sample(symbol, prev_top, ob)

    async def _handle_trade_msg(self, msg: dict) -> None:
        self.trade_messages += 1
        if self.on_trade is None:
            return
        data = msg["data"]
        symbol = data.get("s")
        if symbol not in self._symbols:
            return
        trade = Trade(
            symbol=symbol,
            exchange="binance",
            price=float(data["p"]),
            size=float(data["q"]),
            is_buyer_maker=bool(data.get("m", False)),
            timestamp_ms=int(data["T"]),
        )
        try:
            await self.on_trade(trade)
        except Exception as e:
            logger.exception("on_trade callback error: %s", e)

    # ─── bookTicker handlers ────────────────────────────────────────────
    async def _handle_book_ticker_msg(self, msg: dict) -> None:
        """Record latest top-of-book observation per symbol with arrival ts.

        bookTicker payload (futures):
          {"stream": "btcusdt@bookTicker", "data": {
              "e": "bookTicker", "u": 17, "s": "BTCUSDT",
              "b": "40000.0", "B": "1.0", "a": "40001.0", "A": "2.0",
              "T": <event ts ms>, "E": <transaction ts ms>
          }}

        Diagnostic path: stores (bid, ask, seen_at_ms) in self._bt_last
        for the depth handler to compare against.

        Feed path (when _book_ticker_feed is True): also pushes top-of-book
        into the OrderBook's cached _top_bid_price / _top_ask_price so the
        detector wakes earlier. _bids/_asks dicts stay owned by the depth
        stream; we only update the top price entry + fire listeners.
        """
        self.book_ticker_messages += 1
        data = msg.get("data") or {}
        symbol = data.get("s")
        if symbol not in self._symbols:
            return
        try:
            bid_p = float(data["b"])
            ask_p = float(data["a"])
        except (KeyError, ValueError, TypeError):
            return
        # int(time.time()*1000) is the only timebase used across this file
        # for "now in ms" — keep consistent so deltas line up.
        seen_at_ms = int(time.time() * 1000)
        self._bt_last[symbol] = (bid_p, ask_p, seen_at_ms)

        if getattr(self, '_book_ticker_feed', False):
            ob = self.ob_manager.get("binance", symbol)
            if ob is not None and ob.is_synced:
                try:
                    bid_size = float(data.get("B", 0))
                    ask_size = float(data.get("A", 0))
                except (ValueError, TypeError):
                    return
                try:
                    upd_id = int(data.get("u") or 0)
                except (TypeError, ValueError):
                    upd_id = 0
                try:
                    event_ts = int(data.get("T") or 0)
                except (TypeError, ValueError):
                    event_ts = 0
                # The book owns its own invariants: bookTicker is authoritative
                # for the TOP, so applying it must also PRUNE the levels it
                # supersedes. Poking _top_*/_bids from out here is what left a
                # stale higher bid in the ladder and crossed the book.
                if ob.apply_top_of_book(
                    bid_p, bid_size, ask_p, ask_size,
                    update_id=upd_id, event_ts_ms=event_ts,
                    seen_at_ms=seen_at_ms,
                ):
                    self.book_ticker_top_updates += 1
                    ob._notify_listeners()

    def _record_book_ticker_latency_sample(
        self, symbol: str, prev_top: tuple[float, float], ob,
    ) -> None:
        """Compare current depth top-of-book with last bookTicker observation.

        If depth just moved top-of-book to (new_bid, new_ask) AND bookTicker
        already saw the SAME (bid, ask) earlier, record (now - bt_seen_at)
        as the latency advantage of bookTicker over throttled depth@100ms.

        Skips when:
          - bookTicker hasn't observed this symbol yet
          - depth diff didn't change top-of-book (no comparison possible)
          - bookTicker's recorded top doesn't match new depth top (stale)
        """
        bt = self._bt_last.get(symbol)
        if bt is None:
            return
        new_bb = ob.best_bid()
        new_ba = ob.best_ask()
        if new_bb is None or new_ba is None:
            return
        new_top = (new_bb.price, new_ba.price)
        if new_top == prev_top:
            return  # depth didn't move top; nothing to compare
        bt_bid, bt_ask, bt_seen_ms = bt
        if (bt_bid, bt_ask) != new_top:
            # bookTicker has a different top — either it moved again since,
            # or depth just caught up to a state bookTicker passed through.
            # Either way, this isn't a clean A-vs-B sample. Skip.
            return
        now_ms = int(time.time() * 1000)
        advantage_ms = now_ms - bt_seen_ms
        # Negative would mean depth saw it first — should be extremely rare
        # (clock jitter); clamp at 0 for clean stats.
        if advantage_ms < 0:
            advantage_ms = 0
        self._bt_latency_advantage_ms.append((now_ms, advantage_ms))
        self.book_ticker_matched += 1

    async def _book_ticker_summary_loop(self, interval_sec: int) -> None:
        """Periodically log latency advantage stats for bookTicker vs depth."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(interval_sec)
                _cut = int(time.time() * 1000) - BT_DIAG_WINDOW_SEC * 1000
                samples = [a for (t_, a) in self._bt_latency_advantage_ms
                           if t_ >= _cut]
                if not samples:
                    logger.info(
                        "[BOOK_TICKER_DIAG] no samples yet "
                        "(bt_msgs=%d depth_msgs=%d)",
                        self.book_ticker_messages, self.depth_messages,
                    )
                    continue
                samples.sort()
                n = len(samples)
                p50 = samples[n // 2]
                p90 = samples[min(n - 1, int(n * 0.9))]
                p99 = samples[min(n - 1, int(n * 0.99))]
                mean = sum(samples) / n
                logger.info(
                    "[BOOK_TICKER_DIAG] advantage_ms n=%d mean=%.1f "
                    "p50=%d p90=%d p99=%d max=%d "
                    "(bt_msgs=%d matched=%d depth_msgs=%d top_moves=%d feed=%s)",
                    n, mean, p50, p90, p99, samples[-1],
                    self.book_ticker_messages,
                    self.book_ticker_matched,
                    self.depth_messages,
                    # top_moves = frames that actually MOVED the top, i.e. the
                    # real extra detector load the feed costs. bt_msgs alone
                    # overstates it (most frames only restate a size).
                    self.book_ticker_top_updates,
                    "ON" if self._book_ticker_feed else "off",
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.exception("book_ticker summary loop error: %s", e)

    # ---- diagnostics ----
    def stats(self) -> dict:
        return {
            "subscribed_symbols": len(self._symbols),
            "depth_messages": self.depth_messages,
            "trade_messages": self.trade_messages,
            "book_ticker_messages": self.book_ticker_messages,
            "book_ticker_matched": self.book_ticker_matched,
            "book_ticker_advantage_samples": len(self._bt_latency_advantage_ms),
            "messages_received": self.depth_messages + self.trade_messages,
            "last_message_age_sec": time.time() - self.last_message_ts if self.last_message_ts else None,
            "resync_count": self.resync_count,
            "synced_books": sum(1 for ready in self._snapshot_ready.values() if ready),
            "depth_connected": self._ws_depth is not None,
            "trades_connected": self._ws_trades is not None,
            "book_ticker_connected": self._ws_book_ticker is not None,
        }
