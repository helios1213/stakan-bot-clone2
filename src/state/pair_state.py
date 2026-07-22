"""
Pair state model — pure data class.

Represents one row of the `pair_states` table. The state machine logic
lives in PairStateManager; this module is just the data shape.
"""
from __future__ import annotations

from dataclasses import dataclass


# ---- States ----
DISCOVERED = "discovered"
SHADOW = "shadow"
LIVE = "live"
PAUSED = "paused"
REJECTED = "rejected"

TRADEABLE_STATES = (SHADOW, LIVE)        # detectors emit signals but trades only if in these


@dataclass
class PairState:
    symbol: str
    state: str
    state_since: int                          # unix ts
    discovered_at: int | None = None
    shadow_started_at: int | None = None
    live_started_at: int | None = None
    paused_at: int | None = None
    rejected_at: int | None = None
    paused_until: int | None = None

    # Rolling 24h
    last_24h_signals: int = 0
    last_24h_trades: int = 0
    last_24h_winrate: float = 0.0
    last_24h_pnl: float = 0.0
    last_24h_profit_factor: float = 0.0
    last_24h_avg_edge_pct: float = 0.0
    last_6h_drawdown_pct: float = 0.0

    # Lifetime
    total_shadow_trades: int = 0
    total_shadow_pnl: float = 0.0
    total_live_trades: int = 0
    total_live_pnl: float = 0.0

    last_state_change_reason: str | None = None
    pause_reason: str | None = None
    updated_at: int = 0
