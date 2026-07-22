"""
MexcPrivateWS — per-slot private WebSocket to MEXC contract for push-based
fill / position / asset updates, authenticated by the web-session token
(webkey). Lets us learn IOC fills from a server PUSH instead of polling
/order/deal_details over REST.

Endpoint: wss://contract.mexc.com/edge   (SAME host the public mexc_ws.py uses)
Login:    {"method":"login","param":{"token":"<webkey>"}}  → {"channel":"rs.login","data":"success"}
          (the WEB05... value = MexcWebClient.webkey / the Authorization header;
           NO apiKey/secret/signature — the web-session token is accepted directly)
After a successful login the server AUTO-PUSHES personal channels — there is NO
explicit sub for them ("sub.personal.*" is a no-op; pushes are server→client):
  - push.personal.order       {orderId, dealAvgPrice(raw), dealVol, remainVol,
                               vol, state, makerFee, takerFee, ...}  ← THE FILL
  - push.personal.order.deal  per-execution record (= REST deal_details, pushed)
  - push.personal.position    {positionId, holdVol, openAvgPrice, realised, state}
  - push.personal.asset       {availableBalance, frozenBalance, positionMargin}
Heartbeat: client must send {"method":"ping"} every ~20s or the server drops us.

Scope (deliberate): this module ONLY exposes fill lookup by orderId for the
entry path. Position/asset push consumption can be layered on later. The REST
poll (_poll_fill_price) stays as the fallback, so if this WS is down / a push
is missed, behaviour is identical to before (just slower for that one fill).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import orjson
import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://contract.mexc.com/edge"

# MEXC contract order states: 1=new, 2=partially-filled/in-progress,
# 3=completed (filled, or IOC partial-then-cancelled-remainder), 4=cancelled,
# 5=invalid. We treat 3/4 as terminal: dealVol/dealAvgPrice are final.
_TERMINAL_STATES = {3, 4}

# How long an order push is retained for lookup before being pruned. Generous
# vs the ~hundreds-of-ms entry path; just bounds memory.
_ORDER_TTL_SEC = 60.0
_PING_INTERVAL_SEC = 20.0


@dataclass
class OrderFill:
    """Final fill of an order, as pushed on push.personal.order.

    price is RAW MEXC price (caller multiplies by get_binance_scale to get the
    bot's internal scaled domain — matching _poll_fill_price's return contract).
    """
    deal_avg_price_raw: float
    deal_vol: int
    fee: float
    state: int
    ts: float

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


@dataclass
class PositionClose:
    """A position close, as pushed on push.personal.position (state=3).

    Prices are RAW MEXC (caller ×get_binance_scale to reach the bot's scaled
    domain — matching _poll_close_fill's return contract). realised is USDT
    absolute (NOT scaled).
    """
    close_avg_price_raw: float
    open_avg_price_raw: float
    realised: float
    state: int
    ts: float  # time.monotonic() at receipt — used to reject stale closes


class MexcPrivateWS:
    """One logged-in private WS per slot (each slot has its own webkey)."""

    def __init__(self, slot_id: int, webkey: str, url: str = WS_URL) -> None:
        self.slot_id = slot_id
        self._webkey = webkey
        self._url = url
        self._ws = None
        self._task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.connected = False  # True once logged in
        # orderId → OrderFill (latest push for that order)
        self._orders: dict[str, OrderFill] = {}
        # orderId → Event, set when a TERMINAL push for that order arrives
        self._events: dict[str, asyncio.Event] = {}
        # symbol → PositionClose (latest close push for that symbol). Keyed by
        # SYMBOL (max_positions_per_symbol=1, so unambiguous) — positionId from
        # the pre-close snapshot can be None, so symbol+after_ts is the robust key.
        self._closes: dict[str, PositionClose] = {}
        # symbol → Event, set when a state=3 close push for that symbol arrives
        self._close_events: dict[str, asyncio.Event] = {}

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(
                self._run(), name=f"mexc_private_ws_slot{self.slot_id}"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        for t in (self._ping_task, self._task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    # ---------------- connect / receive loop ----------------
    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self._url,
                    ping_interval=None,   # MEXC uses its own application ping
                    close_timeout=5,
                    max_size=8 * 1024 * 1024,
                    compression=None,
                ) as ws:
                    self._ws = ws
                    await ws.send(
                        orjson.dumps(
                            {"method": "login", "param": {"token": self._webkey}}
                        ).decode()
                    )
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))
                    backoff = 1.0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            self._dispatch(orjson.loads(raw))
                        except Exception:
                            logger.exception(
                                "[priv-ws slot %d] dispatch error", self.slot_id
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    "[priv-ws slot %d] connection error: %s — reconnecting in %.1fs",
                    self.slot_id, e, backoff,
                )
            finally:
                self.connected = False
                self._ws = None
                if self._ping_task is not None:
                    self._ping_task.cancel()
                    self._ping_task = None
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _ping_loop(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL_SEC)
                try:
                    await ws.send(orjson.dumps({"method": "ping"}).decode())
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    def _dispatch(self, msg: dict) -> None:
        ch = msg.get("channel", "")
        if ch == "rs.login":
            if str(msg.get("data", "")).lower() == "success":
                self.connected = True
                logger.info("[priv-ws slot %d] login OK", self.slot_id)
            else:
                logger.warning(
                    "[priv-ws slot %d] login failed: %s", self.slot_id, msg.get("data")
                )
            return
        if ch == "push.personal.order":
            self._on_order(msg.get("data") or {})
            return
        if ch == "push.personal.position":
            self._on_position(msg.get("data") or {})
            return
        # push.personal.{order.deal,asset,risk.limit,...} — not consumed here.

    def _on_order(self, d: dict) -> None:
        oid = d.get("orderId")
        if oid is None:
            return
        oid = str(oid)
        try:
            state = int(d.get("state", 0) or 0)
            price = float(d.get("dealAvgPrice", 0) or 0)
            vol = int(d.get("dealVol", 0) or 0)
            fee = float(d.get("makerFee", 0) or 0) + float(d.get("takerFee", 0) or 0)
        except (TypeError, ValueError):
            return
        self._orders[oid] = OrderFill(
            deal_avg_price_raw=price, deal_vol=vol, fee=fee,
            state=state, ts=time.monotonic(),
        )
        if state in _TERMINAL_STATES:
            ev = self._events.get(oid)
            if ev is not None:
                ev.set()
        self._prune()

    def _on_position(self, d: dict) -> None:
        """Record a position CLOSE (state=3) for close-fill confirmation."""
        sym = d.get("symbol")
        if sym is None:
            return
        try:
            state = int(d.get("state", 0) or 0)
        except (TypeError, ValueError):
            return
        # Only a closed position (state=3) confirms a close fill. state=1 (open)
        # / other are ignored — they don't carry a finished close.
        if state != 3:
            return
        try:
            close_avg = float(d.get("closeAvgPrice", 0) or 0)
            open_avg = float(d.get("openAvgPrice", 0) or 0)
            realised = float(d.get("realised", 0) or 0)
        except (TypeError, ValueError):
            return
        self._closes[sym] = PositionClose(
            close_avg_price_raw=close_avg, open_avg_price_raw=open_avg,
            realised=realised, state=state, ts=time.monotonic(),
        )
        ev = self._close_events.get(sym)
        if ev is not None:
            ev.set()

    def _prune(self) -> None:
        if len(self._orders) < 256:
            return
        cutoff = time.monotonic() - _ORDER_TTL_SEC
        stale = [k for k, v in self._orders.items() if v.ts < cutoff]
        for k in stale:
            self._orders.pop(k, None)
            self._events.pop(k, None)

    # ---------------- public lookup ----------------
    async def wait_fill(self, order_id: str, timeout_sec: float) -> OrderFill | None:
        """Return the terminal OrderFill for order_id, waiting up to timeout_sec.

        Returns the fill (deal_vol may be 0 → IOC expired, definitive no-fill)
        once a TERMINAL push arrives, or None if none arrived in time (caller
        should then fall back to the REST poll). Handles the race where the
        push arrives before the caller knows the orderId (buffer is checked
        first).
        """
        if not self.connected:
            return None
        existing = self._orders.get(order_id)
        if existing is not None and existing.terminal:
            return existing
        ev = self._events.setdefault(order_id, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            return None
        finally:
            # Drop the waiter so an order that never receives a push (private-WS
            # stale / reconnect) can't leak an Event forever: _prune() only reaps
            # keys present in self._orders, which a never-pushed order never is.
            # wait_fill runs once per order, so no concurrent waiter to disturb.
            self._events.pop(order_id, None)
        fill = self._orders.get(order_id)
        return fill if (fill is not None and fill.terminal) else None

    async def wait_close(
        self, symbol: str, after_ts: float, timeout_sec: float
    ) -> PositionClose | None:
        """Return the state=3 close for `symbol` that arrived at/after `after_ts`
        (a time.monotonic() captured at close-submit), waiting up to timeout_sec.

        `after_ts` rejects a STALE prior close for the same symbol — we only
        accept a close push that landed after we submitted ours (max 1 pos/
        symbol → unambiguous). Returns None on timeout → caller falls back to
        the REST _poll_close_fill (so a missed push never strands a position).
        """
        if not self.connected:
            return None
        def _fresh():
            c = self._closes.get(symbol)
            if c is not None and c.ts >= after_ts and c.close_avg_price_raw > 0:
                return c
            return None
        deadline = time.monotonic() + timeout_sec
        while True:
            c = _fresh()
            if c is not None:
                return c
            ev = self._close_events.setdefault(symbol, asyncio.Event())
            ev.clear()
            # re-check after clear so a push landing during the clear isn't lost
            c = _fresh()
            if c is not None:
                return c
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(ev.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None


class MexcPrivateWSPool:
    """slot_id → MexcPrivateWS. Pulls the per-slot webkey from the client pool."""

    def __init__(self, client_pool) -> None:
        self._client_pool = client_pool
        self._instances: dict[int, MexcPrivateWS] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def ensure(self, slot_id: int) -> MexcPrivateWS | None:
        """Get-or-create a started private WS for slot_id. Returns None if the
        slot's client/webkey isn't available (caller falls back to REST)."""
        ws = self._instances.get(slot_id)
        if ws is not None:
            return ws
        lock = self._locks.setdefault(slot_id, asyncio.Lock())
        async with lock:
            ws = self._instances.get(slot_id)
            if ws is not None:
                return ws
            try:
                client = await self._client_pool.get(slot_id)
                if client is None or not client.webkey:
                    return None
                ws = MexcPrivateWS(slot_id, client.webkey)
                await ws.start()
                self._instances[slot_id] = ws
                return ws
            except Exception as e:
                logger.warning(
                    "[priv-ws] ensure slot %d failed: %s", slot_id, e
                )
                return None

    async def start(self, slot_ids: list[int]) -> None:
        for sid in slot_ids:
            await self.ensure(sid)

    async def wait_fill(
        self, slot_id: int, order_id: str, timeout_sec: float
    ) -> OrderFill | None:
        """Fill lookup with lazy warmup: if the slot's WS isn't up yet, kick off
        its creation in the background and return None (this fill uses the REST
        fallback); subsequent fills use the live push."""
        ws = self._instances.get(slot_id)
        if ws is None or not ws.connected:
            asyncio.create_task(self.ensure(slot_id))
            return None
        return await ws.wait_fill(order_id, timeout_sec)

    async def wait_close(
        self, slot_id: int, symbol: str, after_ts: float, timeout_sec: float
    ) -> PositionClose | None:
        """Close-confirmation lookup; lazy-warms the slot WS, returns None (→ REST
        fallback) if not up yet."""
        ws = self._instances.get(slot_id)
        if ws is None or not ws.connected:
            asyncio.create_task(self.ensure(slot_id))
            return None
        return await ws.wait_close(symbol, after_ts, timeout_sec)

    async def stop(self) -> None:
        for ws in self._instances.values():
            await ws.stop()
        self._instances.clear()
