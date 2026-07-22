"""
Local L2 orderbook with snapshot + diff synchronization.

Maintains a sorted view of bids/asks per symbol. Designed for Binance
Futures protocol but generic enough to be reused for MEXC.

Synchronization rules (Binance Futures depth stream):
  1. Connect to WebSocket and start buffering updates.
  2. Fetch REST snapshot at fapi/v1/depth.
  3. Drop buffered updates where update_id < snapshot.lastUpdateId.
  4. First update must satisfy: U <= lastUpdateId+1 <= u
     (otherwise re-snapshot).
  5. Each subsequent update must satisfy: pu == previous_update.u
     (otherwise re-snapshot).

Price-update listeners: subscribers register synchronous callbacks via
`add_listener`. After every `apply_snapshot` / `apply_diff`, all
listeners are invoked with the OrderBook itself. Listeners MUST be
synchronous and fast (just read prices, update one float, etc).
Exceptions in any listener are caught and logged; they never break the
WS apply path.
"""
from __future__ import annotations

import heapq
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


@dataclass
class OrderBookLevel:
    price: float
    size: float


# Listener callback signature: receives the OrderBook itself. Listener
# decides what to read (best_bid, best_ask, executable_exit_price, ...).
PriceListener = Callable[["OrderBook"], None]


@dataclass
class OrderBook:
    """In-memory L2 orderbook for a single symbol on a single exchange."""

    symbol: str
    exchange: str
    max_levels: int = 50

    # Bids: highest price = best. Asks: lowest price = best.
    # Storage: dict for O(1) price-level updates. Sorted views built on demand.
    _bids: dict[float, float] = field(default_factory=dict)
    _asks: dict[float, float] = field(default_factory=dict)

    last_update_id: int = 0
    last_update_ts_ms: int = 0
    is_synced: bool = False

    # Sync price-update listeners. Invoked after each apply_snapshot /
    # apply_diff. Exceptions are isolated per-listener.
    _listeners: list[PriceListener] = field(default_factory=list)

    # Cached top-of-book prices. Recomputed once per apply_*, so
    # best_bid() / best_ask() are O(1) lookups instead of O(n) max/min.
    # 0.0 sentinel = empty side.
    _top_bid_price: float = 0.0
    _top_ask_price: float = 0.0

    def _recompute_top(self) -> None:
        """Refresh cached top-of-book prices. O(n) once per diff."""
        self._top_bid_price = max(self._bids) if self._bids else 0.0
        self._top_ask_price = min(self._asks) if self._asks else 0.0

    # ---- snapshot ----
    def apply_snapshot(
        self,
        bids: Iterable[tuple[float, float]],
        asks: Iterable[tuple[float, float]],
        update_id: int,
    ) -> None:
        self._bids = {p: s for p, s in bids if s > 0}
        self._asks = {p: s for p, s in asks if s > 0}
        self.last_update_id = update_id
        self.last_update_ts_ms = int(time.time() * 1000)
        self.is_synced = True
        self._recompute_top()
        logger.debug(
            "%s/%s snapshot applied: %d bids, %d asks, id=%d",
            self.exchange, self.symbol, len(self._bids), len(self._asks), update_id,
        )
        self._notify_listeners()

    # ---- diff ----
    def apply_diff(
        self,
        bids: Iterable[tuple[float, float]],
        asks: Iterable[tuple[float, float]],
        first_update_id: int,
        final_update_id: int,
    ) -> None:
        for price, size in bids:
            if size == 0:
                self._bids.pop(price, None)
            else:
                self._bids[price] = size
        for price, size in asks:
            if size == 0:
                self._asks.pop(price, None)
            else:
                self._asks[price] = size

        self.last_update_id = final_update_id
        self.last_update_ts_ms = int(time.time() * 1000)

        # Trim to max_levels — protect memory on very deep books.
        # heapq.nlargest/nsmallest for k << n run in O(n log k) which beats
        # the O(n log n) of a full sort when we only need the top-k.
        if len(self._bids) > self.max_levels * 4:
            top_prices = heapq.nlargest(self.max_levels * 2, self._bids.keys())
            self._bids = {p: self._bids[p] for p in top_prices}
        if len(self._asks) > self.max_levels * 4:
            top_prices = heapq.nsmallest(self.max_levels * 2, self._asks.keys())
            self._asks = {p: self._asks[p] for p in top_prices}

        self._recompute_top()
        self._notify_listeners()

    # ---- listener API ----
    def add_listener(self, listener: PriceListener) -> None:
        """Register a price-update listener. Idempotent."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: PriceListener) -> None:
        """Unregister a listener. No-op if not present."""
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def listener_count(self) -> int:
        return len(self._listeners)

    def _notify_listeners(self) -> None:
        """Invoke each listener. Exceptions are isolated per-listener.
        Iterates a copy so a listener may remove itself during iteration.
        """
        if not self._listeners:
            return
        for cb in list(self._listeners):
            try:
                cb(self)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "OrderBook listener raised for %s/%s — listener=%r",
                    self.exchange, self.symbol, cb,
                )

    # ---- accessors ----
    def best_bid(self) -> OrderBookLevel | None:
        p = self._top_bid_price
        if p == 0.0 and not self._bids:
            return None
        # Fast path: cached price is still in _bids. Cache stale only if
        # _bids was mutated externally (tests, etc) — fall back to recompute.
        if p in self._bids:
            return OrderBookLevel(price=p, size=self._bids[p])
        self._recompute_top()
        p = self._top_bid_price
        if p == 0.0:
            return None
        return OrderBookLevel(price=p, size=self._bids[p])

    def best_ask(self) -> OrderBookLevel | None:
        p = self._top_ask_price
        if p == 0.0 and not self._asks:
            return None
        if p in self._asks:
            return OrderBookLevel(price=p, size=self._asks[p])
        self._recompute_top()
        p = self._top_ask_price
        if p == 0.0:
            return None
        return OrderBookLevel(price=p, size=self._asks[p])

    # ---- alloc-free accessors for hot path ----
    # best_bid()/best_ask() return OrderBookLevel dataclasses — when the
    # caller only needs the price, that's a wasted allocation. In the
    # detector and 20ms watch loop these run 50–200 Hz × N pairs, so the
    # garbage adds up. The _price variants return float directly.
    def best_bid_price(self) -> float:
        """Top bid price as float. Returns 0.0 if book is empty.
        No object allocation — preferred in hot paths (detector, watcher).
        """
        p = self._top_bid_price
        if p == 0.0:
            if not self._bids:
                return 0.0
            self._recompute_top()
            return self._top_bid_price
        if p in self._bids:
            return p
        self._recompute_top()
        return self._top_bid_price

    def best_ask_price(self) -> float:
        """Top ask price as float. Returns 0.0 if book is empty.
        No object allocation — preferred in hot paths.
        """
        p = self._top_ask_price
        if p == 0.0:
            if not self._asks:
                return 0.0
            self._recompute_top()
            return self._top_ask_price
        if p in self._asks:
            return p
        self._recompute_top()
        return self._top_ask_price

    def mid_price(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None
        return (b.price + a.price) / 2

    def is_crossed(self) -> bool:
        """True if best_bid >= best_ask — an impossible (corrupt) book.
        A limited-depth diff stream can strand a stale top level when the
        market gaps past it and the delete for that level falls outside the
        streamed window; the version sequence stays intact so the seq-gap
        resync never fires. Consumers use this to reject the book / resync."""
        bb, ba = self._top_bid_price, self._top_ask_price
        return bb > 0.0 and ba > 0.0 and bb >= ba

    def executable_exit_price(self, direction: str) -> float | None:
        """
        Price at which an existing position would close NOW if hit MARKET.
        Matches what the exchange UI displays as unrealized PnL.

          - LONG  → sells at best_bid (give up size at the bid)
          - SHORT → buys back at best_ask
        Falls back to mid_price() if one side of book is missing.
        """
        if direction == "long":
            b = self.best_bid()
            return b.price if b is not None else self.mid_price()
        if direction == "short":
            a = self.best_ask()
            return a.price if a is not None else self.mid_price()
        return self.mid_price()

    def spread_pct(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None or b.price <= 0:
            return None
        return (a.price - b.price) / b.price * 100

    def top_bids(self, n: int = 20) -> list[OrderBookLevel]:
        prices = sorted(self._bids.keys(), reverse=True)[:n]
        return [OrderBookLevel(price=p, size=self._bids[p]) for p in prices]

    def top_asks(self, n: int = 20) -> list[OrderBookLevel]:
        prices = sorted(self._asks.keys())[:n]
        return [OrderBookLevel(price=p, size=self._asks[p]) for p in prices]

    def simulate_market_fill(self, side: str, notional_usdt: float,
                             contract_size: float = 1.0) -> tuple[float, float]:
        """Simulate a market order eating through the book.
        side: 'buy' (eats asks) or 'sell' (eats bids).
        Returns (avg_fill_price, slippage_pct vs best price).

        `size` from the book is raw MEXC depth in CONTRACTS; USDT value of a
        level is price*size*contract_size. Omitting contract_size mis-walks
        the ladder by the contractSize factor (same bug class as the IOC entry
        sim) → wrong slippage/exit price for pairs with contract_size != 1.
        """
        if side == "buy":
            levels = sorted(self._asks.items())
            best = levels[0][0] if levels else None
        elif side == "sell":
            levels = sorted(self._bids.items(), reverse=True)
            best = levels[0][0] if levels else None
        else:
            raise ValueError(f"Invalid side: {side}")

        if not levels or best is None:
            return 0.0, 0.0

        remaining = notional_usdt
        spent_usdt = 0.0
        filled_qty = 0.0
        for price, size in levels:
            level_notional = price * size * contract_size
            if remaining <= level_notional:
                qty = remaining / price
                filled_qty += qty
                spent_usdt += remaining
                remaining = 0
                break
            else:
                filled_qty += size * contract_size
                spent_usdt += level_notional
                remaining -= level_notional

        if filled_qty == 0:
            return 0.0, 0.0

        avg_price = spent_usdt / filled_qty
        slippage_pct = (avg_price - best) / best * 100
        if side == "sell":
            slippage_pct = -slippage_pct
        return avg_price, abs(slippage_pct)


class OrderBookManager:
    """Holds orderbooks for many (exchange, symbol) pairs."""

    def __init__(self) -> None:
        self._books: dict[tuple[str, str], OrderBook] = {}

    def get_or_create(self, exchange: str, symbol: str, max_levels: int = 50) -> OrderBook:
        key = (exchange, symbol)
        if key not in self._books:
            self._books[key] = OrderBook(symbol=symbol, exchange=exchange, max_levels=max_levels)
        return self._books[key]

    def get(self, exchange: str, symbol: str) -> OrderBook | None:
        return self._books.get((exchange, symbol))

    def remove(self, exchange: str, symbol: str) -> None:
        self._books.pop((exchange, symbol), None)

    def all_symbols(self, exchange: str) -> list[str]:
        return [sym for (ex, sym) in self._books.keys() if ex == exchange]
