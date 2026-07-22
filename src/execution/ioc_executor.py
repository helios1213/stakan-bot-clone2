"""
IOC Executor — simulates IMMEDIATE_OR_CANCEL limit entries on MEXC.

Maker-style IOC entry model:
  - Place a LIMIT order with IOC time-in-force
  - Order fills immediately at limit-or-better, or expires (canceled)
  - Maker fee on MEXC futures = 0% → entry has zero fee cost

Why this matters for edge:
  - Market entry pays full taker fee (~0.04% × leverage = real cost)
  - Slippage on market = whatever the book gives you
  - IOC limit AT TOUCH gives zero fee and filters trades where price moved

Simulation logic:
  1. Decide entry_target_price = best_ask (LONG) or best_bid (SHORT) — at touch
  2. Apply network latency: snapshot orderbook ~150ms in the future (from `now`)
  3. Walk through the book up to entry_target_price
  4. If we get any fill → record fill_price, fill_pct
  5. If price moved away → status='expired', no fill
  6. Optionally retry up to ioc_max_attempts (each attempt simulates fresh latency)

Latency model:
  - signal_to_order_ms: time for our code to react (default 150ms)
  - order_to_fill_ms: time for MEXC to process (default 50ms)
  - Total = 200ms typical
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.exchanges.orderbook import OrderBook

logger = logging.getLogger(__name__)


@dataclass
class IOCAttemptResult:
    """Result of a single IOC fill attempt."""
    status: str                      # 'filled' | 'partial' | 'expired'
    target_price: float
    avg_fill_price: float = 0.0
    filled_qty: float = 0.0
    filled_notional_usdt: float = 0.0
    filled_pct: float = 0.0          # 0.0..1.0
    expired_reason: str = ""


class IOCExecutor:
    """
    Pure-function IOC simulator. State-free — every call is independent.

    Usage:
        executor = IOCExecutor()
        result = executor.simulate_ioc_entry(
            mexc_ob=mexc_orderbook,
            direction="long",
            notional_usdt=1250,
        )
        if result.status == "filled":
            # We entered at result.avg_fill_price
            ...
    """

    def simulate_ioc_entry(
        self,
        mexc_ob: OrderBook,
        direction: str,                   # 'long' | 'short'
        notional_usdt: float,
        limit_price: float | None = None,
        contract_size: float = 1.0,       # base-asset units per contract (MEXC contractSize)
    ) -> IOCAttemptResult:
        """
        Simulate one IOC limit attempt against `mexc_ob`.

        limit_price = the IOC limit FIXED at submit time (signal-time touch ±
        per-pair offset). The caller passes `mexc_ob` as the POST-latency book,
        so if price moved past the fixed limit during latency the ladder walk
        finds no liquidity and the IOC EXPIRES — exactly like a real IOC.
        If limit_price is None, fall back to the current touch (no-latency mode).
        """
        if not mexc_ob.is_synced:
            return IOCAttemptResult(status="expired", target_price=0.0,
                                    expired_reason="orderbook_not_synced")

        best_bid = mexc_ob.best_bid()
        best_ask = mexc_ob.best_ask()
        if not best_bid or not best_ask:
            return IOCAttemptResult(status="expired", target_price=0.0,
                                    expired_reason="empty_orderbook")

        if direction == "long":
            lp = limit_price if limit_price is not None else best_ask.price
            return self._simulate_buy_ioc(mexc_ob, lp, notional_usdt, contract_size)
        elif direction == "short":
            lp = limit_price if limit_price is not None else best_bid.price
            return self._simulate_sell_ioc(mexc_ob, lp, notional_usdt, contract_size)
        else:
            raise ValueError(f"Invalid direction: {direction}")

    def _simulate_buy_ioc(
        self,
        ob: OrderBook,
        limit_price: float,
        notional_usdt: float,
        contract_size: float = 1.0,
    ) -> IOCAttemptResult:
        """Walk the asks ladder, fill up to limit_price.

        OrderBook level `size` is the raw MEXC depth in CONTRACTS; the USDT
        value of a level is price × size × contract_size (1 contract =
        contract_size base-asset units). Omitting contract_size mis-sized
        shadow fills by the contractSize factor (e.g. 10x small for PENGU,
        100x big for ZEC/BCH). filled_qty stays in base-asset units.
        """
        levels = sorted(ob._asks.items())  # ascending price
        if not levels:
            return IOCAttemptResult(status="expired", target_price=limit_price,
                                    expired_reason="no_asks")

        remaining = notional_usdt
        spent_usdt = 0.0
        filled_qty = 0.0

        for price, size in levels:
            if price > limit_price:
                # Past our limit — IOC stops here
                break
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
            return IOCAttemptResult(status="expired", target_price=limit_price,
                                    expired_reason="no_liquidity_at_or_below_limit")

        avg_price = spent_usdt / filled_qty
        filled_pct = spent_usdt / notional_usdt
        status = "filled" if filled_pct >= 0.99 else "partial"
        return IOCAttemptResult(
            status=status,
            target_price=limit_price,
            avg_fill_price=avg_price,
            filled_qty=filled_qty,
            filled_notional_usdt=spent_usdt,
            filled_pct=filled_pct,
        )

    def _simulate_sell_ioc(
        self,
        ob: OrderBook,
        limit_price: float,
        notional_usdt: float,
        contract_size: float = 1.0,
    ) -> IOCAttemptResult:
        """Walk the bids ladder, fill down to limit_price.

        Level `size` is in CONTRACTS; USDT value = price × size × contract_size
        (see _simulate_buy_ioc). filled_qty stays in base-asset units.
        """
        levels = sorted(ob._bids.items(), reverse=True)  # descending price
        if not levels:
            return IOCAttemptResult(status="expired", target_price=limit_price,
                                    expired_reason="no_bids")

        remaining = notional_usdt
        received_usdt = 0.0
        filled_qty = 0.0

        for price, size in levels:
            if price < limit_price:
                break
            level_notional = price * size * contract_size
            if remaining <= level_notional:
                qty = remaining / price
                filled_qty += qty
                received_usdt += remaining
                remaining = 0
                break
            else:
                filled_qty += size * contract_size
                received_usdt += level_notional
                remaining -= level_notional

        if filled_qty == 0:
            return IOCAttemptResult(status="expired", target_price=limit_price,
                                    expired_reason="no_liquidity_at_or_above_limit")

        avg_price = received_usdt / filled_qty
        filled_pct = received_usdt / notional_usdt
        status = "filled" if filled_pct >= 0.99 else "partial"
        return IOCAttemptResult(
            status=status,
            target_price=limit_price,
            avg_fill_price=avg_price,
            filled_qty=filled_qty,
            filled_notional_usdt=received_usdt,
            filled_pct=filled_pct,
        )
