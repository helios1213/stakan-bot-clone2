"""
Market Exit Executor — simulates market sell/buy to close position.

For exit we use MARKET orders since:
  - We need guaranteed fill at exit (no risk of "stuck position")
  - Slippage on small notional ($1k-2k) is minimal
  - On MEXC promo accounts (0% maker/0% taker for selected pairs)
    market exit has ZERO fee cost — only slippage matters

MEXC futures: 0% maker / 0% taker
This applies to the specific account/pair set we trade.

Returns:
  - exit_price: weighted average fill price (after slippage)
  - slippage_pct: how far avg_price deviated from mid (this IS our cost)
  - fee_usdt: 0 (zero fee schedule)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.exchanges.orderbook import OrderBook

logger = logging.getLogger(__name__)


# MEXC futures fee — 0% on our account/pair set (taker AND maker)
MEXC_TAKER_FEE_PCT = 0.0  # was 0.0004, now zero per user's MEXC promo


@dataclass
class MarketExitResult:
    exit_price: float
    slippage_pct: float
    fee_usdt: float
    notional_usdt: float


class MarketExecutor:
    """Pure-function market exit simulator."""

    def simulate_market_exit(
        self,
        mexc_ob: OrderBook,
        direction: str,           # original position direction ('long'/'short')
        notional_usdt: float,
        contract_size: float = 1.0,
    ) -> MarketExitResult | None:
        """
        Close a position with a MARKET order.

        For LONG position → we SELL (eat bids).
        For SHORT position → we BUY (eat asks).

        Cost model: ZERO fees, slippage only.
        Slippage is captured in `exit_price` already (avg fill price).
        """
        if not mexc_ob.is_synced:
            return None

        side = "sell" if direction == "long" else "buy"
        avg_price, slippage_pct = mexc_ob.simulate_market_fill(side, notional_usdt, contract_size)
        if avg_price <= 0:
            return None

        # Fee is zero on our account schedule
        fee_usdt = notional_usdt * MEXC_TAKER_FEE_PCT  # = 0

        return MarketExitResult(
            exit_price=avg_price,
            slippage_pct=slippage_pct,
            fee_usdt=fee_usdt,
            notional_usdt=notional_usdt,
        )
