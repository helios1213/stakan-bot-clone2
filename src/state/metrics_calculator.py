"""
Metrics calculator — computes rolling per-pair statistics from shadow_trades.

Called by PairStateManager every cycle (1-5 minutes typical).
Updates `pair_states` rolling fields used for promotion/demotion decisions.

Metrics computed:
  - last_24h_signals: count from `signals` table
  - last_24h_trades: count from `shadow_trades` (closed only)
  - last_24h_winrate: % of trades with net_pnl > 0
  - last_24h_pnl: sum of net_pnl_usdt
  - last_24h_profit_factor: gross_profit / abs(gross_loss)
  - last_24h_avg_edge_pct: avg roi_pct
  - last_6h_drawdown_pct: worst 6h rolling drawdown
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from src.storage.db import Database

logger = logging.getLogger(__name__)


@dataclass
class PairMetrics:
    symbol: str
    signals_24h: int = 0
    trades_24h: int = 0
    wins_24h: int = 0
    losses_24h: int = 0
    pnl_24h: float = 0.0
    gross_profit_24h: float = 0.0
    gross_loss_24h: float = 0.0
    avg_edge_pct_24h: float = 0.0
    drawdown_6h_pct: float = 0.0

    @property
    def winrate_24h(self) -> float:
        n = self.wins_24h + self.losses_24h
        if n == 0:
            return 0.0
        return self.wins_24h / n

    @property
    def profit_factor_24h(self) -> float:
        if self.gross_loss_24h <= 0:
            # No losses — define as gross_profit (avoids inf when small sample)
            return self.gross_profit_24h if self.gross_profit_24h > 0 else 0.0
        return self.gross_profit_24h / self.gross_loss_24h


class MetricsCalculator:
    """Computes rolling metrics for pairs from raw trade and signal data."""

    def __init__(self, db: Database, live_db=None) -> None:
        self.db = db
        self.live_db = live_db  # optional LiveDatabase for live-mode pairs

    async def compute_for_symbol(self, symbol: str, state: str = "shadow") -> PairMetrics:
        """Compute current 24h/6h metrics for a single symbol.

        If state=='live' and self.live_db is configured, reads from live_trades.
        Otherwise reads from shadow_trades. Signal counts always read from shadow db
        (signals table is shared since detectors are shared).
        """
        # Pick which DB and table to query for trades
        if state == "live" and self.live_db is not None:
            trades_db = self.live_db
            trades_table = "live_trades"
        else:
            trades_db = self.db
            trades_table = "shadow_trades"
        now = int(time.time())
        cutoff_24h = now - 24 * 3600

        m = PairMetrics(symbol=symbol)

        # Signals 24h
        row = await self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM signals WHERE symbol=? AND created_at>=?",
            (symbol, cutoff_24h),
        )
        m.signals_24h = row["cnt"] if row else 0

        # Trades 24h (closed only)
        rows = await trades_db.fetchall(
            f"""SELECT net_pnl_usdt, roi_pct FROM {trades_table}
               WHERE symbol=? AND closed_at IS NOT NULL AND closed_at>=?""",
            (symbol, cutoff_24h),
        )
        roi_sum = 0.0
        for row in rows:
            pnl = row["net_pnl_usdt"] if row["net_pnl_usdt"] is not None else 0.0
            roi = row["roi_pct"] if row["roi_pct"] is not None else 0.0
            m.trades_24h += 1
            m.pnl_24h += pnl
            roi_sum += roi
            if pnl > 0:
                m.wins_24h += 1
                m.gross_profit_24h += pnl
            elif pnl < 0:
                m.losses_24h += 1
                m.gross_loss_24h += abs(pnl)

        if m.trades_24h > 0:
            m.avg_edge_pct_24h = roi_sum / m.trades_24h

        # Drawdown 6h: DEPRECATED — formula was buggy, removed from transition logic.
        # Field still exists for backwards compat with DB schema, but always 0.
        m.drawdown_6h_pct = 0.0

        return m

    async def compute_for_all_active(
        self,
        symbols: list[str],
        states: dict[str, str] | None = None,
    ) -> dict[str, PairMetrics]:
        """Compute metrics for multiple symbols. Sequential — fast enough for 15 pairs.

        states: optional mapping symbol → current state ("live"|"shadow"|...).
                When provided, live pairs query live_db.live_trades.
        """
        states = states or {}
        result: dict[str, PairMetrics] = {}
        for sym in symbols:
            try:
                state = states.get(sym, "shadow")
                result[sym] = await self.compute_for_symbol(sym, state=state)
            except Exception as e:
                logger.exception("Metrics compute failed for %s: %s", sym, e)
        return result
