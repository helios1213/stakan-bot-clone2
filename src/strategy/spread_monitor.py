"""Spread monitor — watches the MEXC book spread per pair and pushes a Telegram
alert when it widens beyond the tight-book threshold.

The lead-lag edge needs a tight MEXC book (~1 tick: e.g. bid 1.05 / ask 1.06).
When the bid/ask spread widens, entries fill worse and the strategy loses to the
spread. This monitor REPORTS that condition — it does NOT gate trading.

Spread-in-ticks uses the SAME scaling as the detector's gap math: the MEXC book
stores binance-scaled prices, so
    spread_ticks = (best_ask - best_bid) / (tick_size * binance_scale)
A tight book = 1.0 tick.

Hysteresis (avoid flapping): per pair state tight<->wide with a debounce window.
  tight -> WIDE   when spread_ticks >= wide_ticks   sustained >= debounce_sec
  wide  -> tight  when spread_ticks <= recover_ticks sustained >= debounce_sec
The deadband (recover_ticks < wide_ticks) prevents alert spam at the boundary.
One alert per transition; no repeats while a pair stays wide.
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from src.exchanges.mexc_rest import to_mexc, get_binance_scale
from src.execution.live_executor import get_tick_size


class SpreadMonitor:
    def __init__(
        self,
        ob_manager,
        alerts,
        *,
        wide_ticks: float = 3.0,
        recover_ticks: float = 2.0,
        debounce_sec: float = 3.0,
        interval_sec: float = 2.0,
    ) -> None:
        self.ob_manager = ob_manager
        self.alerts = alerts
        self.wide_ticks = wide_ticks
        self.recover_ticks = recover_ticks
        self.debounce_sec = debounce_sec
        self.interval_sec = interval_sec
        self._stop = asyncio.Event()
        self._state: dict[str, str] = {}            # symbol -> "tight" | "wide"
        self._pending_since: dict[str, float] = {}  # symbol -> monotonic ts of pending flip

    def stop(self) -> None:
        self._stop.set()

    def spread_ticks(self, symbol: str) -> tuple[float, float, float] | None:
        """(spread_ticks, bid, ask) for the MEXC book, or None if not ready."""
        ob = self.ob_manager.get("mexc", symbol)
        if ob is None or not getattr(ob, "is_synced", False):
            return None
        bid = ob.best_bid_price()
        ask = ob.best_ask_price()
        if bid <= 0 or ask <= 0:
            return None
        msym = to_mexc(symbol)
        tick = get_tick_size(msym)
        scale = get_binance_scale(msym)
        tick_scaled = tick * scale if scale > 0 else tick
        if tick_scaled <= 0:
            return None
        return ((ask - bid) / tick_scaled, bid, ask)

    def _evaluate(self, symbol: str, st_now: float, now: float) -> str | None:
        """Pure hysteresis+debounce. Returns 'wide'/'tight' on a transition
        (alert should fire), else None. `now` is a monotonic timestamp."""
        state = self._state.get(symbol, "tight")
        if state == "tight":
            if st_now >= self.wide_ticks:
                since = self._pending_since.get(symbol)
                if since is None:
                    self._pending_since[symbol] = now
                elif now - since >= self.debounce_sec:
                    self._state[symbol] = "wide"
                    self._pending_since.pop(symbol, None)
                    return "wide"
            else:
                self._pending_since.pop(symbol, None)
        else:  # wide
            if st_now <= self.recover_ticks:
                since = self._pending_since.get(symbol)
                if since is None:
                    self._pending_since[symbol] = now
                elif now - since >= self.debounce_sec:
                    self._state[symbol] = "tight"
                    self._pending_since.pop(symbol, None)
                    return "tight"
            else:
                self._pending_since.pop(symbol, None)
        return None

    async def _send(self, emoji: str, head: str, symbol: str, st: float, bid: float, ask: float, tail: str) -> None:
        await self.alerts.send(
            text=(f"{emoji} <b>{head} {symbol}</b>: {st:.1f} тіка "
                  f"(bid {bid:.6g} / ask {ask:.6g}){tail}"),
            category=f"spread_{symbol}",
            throttle_sec=0,
        )

    async def run(self) -> None:
        logger.info(
            f"SpreadMonitor started (wide>={self.wide_ticks:.1f}t "
            f"recover<={self.recover_ticks:.1f}t debounce={self.debounce_sec:.0f}s "
            f"interval={self.interval_sec:.0f}s)"
        )
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                for symbol in self.ob_manager.all_symbols("mexc"):
                    res = self.spread_ticks(symbol)
                    if res is None:
                        continue
                    st, bid, ask = res
                    fired = self._evaluate(symbol, st, now)
                    if fired == "wide":
                        logger.warning(f"[SPREAD] {symbol} WIDE {st:.1f}t (bid={bid:.6g} ask={ask:.6g})")
                        await self._send("🔴", "СПРЕД ШИРОКИЙ", symbol, st, bid, ask,
                                         "\nЕдж зник — входи втрачають на спреді.")
                    elif fired == "tight":
                        logger.info(f"[SPREAD] {symbol} recovered {st:.1f}t")
                        await self._send("🟢", "спред нормалізувався", symbol, st, bid, ask, "")
            except Exception:
                logger.exception("SpreadMonitor loop error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_sec)
            except asyncio.TimeoutError:
                pass
