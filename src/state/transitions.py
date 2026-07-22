"""
State transition rules.

Defines criteria for pair lifecycle progression:
  discovered → shadow      : enough signals to test
  shadow → live            : proven edge over 48h+
  live → paused            : recent performance degraded
  paused → shadow          : auto-resume after cooldown
  shadow → rejected        : 7 days without edge

All criteria are pure functions on PairState + PairMetrics + config.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from src.state.metrics_calculator import PairMetrics
from src.state.pair_state import PairState, DISCOVERED, SHADOW, LIVE, PAUSED, REJECTED

logger = logging.getLogger(__name__)


@dataclass
class TransitionCriteria:
    """Configurable thresholds for state transitions."""

    # shadow → live
    promotion_min_trades: int = 100
    promotion_min_days_in_shadow: float = 2.0
    promotion_min_winrate: float = 0.55
    promotion_min_profit_factor: float = 1.5
    promotion_min_edge_pct: float = 0.02       # avg roi after slippage
    promotion_max_drawdown_6h_pct: float = 8.0

    # live → paused
    # DISABLED 2026-07-21 (user request): auto-demotion pauses a live pair on a
    # slightly-negative 24h (pnl<0 OR WR<48% after 20 trades). For scalpers this
    # fires on normal variance and interrupts live tuning/data-collection (ONDO
    # got paused at −$0.26 / WR 47.4%). Both triggers off; re-enable by setting
    # demotion_24h_pnl_negative=True and demotion_min_winrate_24h back to 0.48.
    demotion_24h_pnl_negative: bool = False
    demotion_min_winrate_24h: float = 0.0
    demotion_max_drawdown_6h_pct: float = 20.0

    # paused → shadow (auto-resume)
    pause_duration_sec: int = 12 * 3600        # 12h

    # shadow → rejected
    # DISABLED 2026-06-07 (user request): the auto-reject demoted any pair
    # that sat in shadow >=7d with PF24h<1.0 to a TERMINAL rejected state
    # (e.g. ZEC on clone). Off by default now — pairs stay in shadow and keep
    # being evaluated for promotion. Set reject_enabled=True to restore.
    reject_enabled: bool = False
    reject_after_days: float = 7.0
    reject_max_profit_factor: float = 1.0


@dataclass
class TransitionDecision:
    """Result of evaluating one transition."""
    should_transition: bool
    target_state: str | None = None
    reason: str = ""


def evaluate_discovered(
    state: PairState,
    metrics: PairMetrics,
    crit: TransitionCriteria,
) -> TransitionDecision:
    """
    Legacy state — DISCOVERED is no longer entered by new pairs after the
    Note: new pairs are seeded directly into
    SHADOW). This function remains as a no-op so any existing DB rows in
    `discovered` state simply remain there until manually transitioned;
    they don't auto-promote anymore (the old logic depended on signal
    counts that no longer flow through a discovery pipeline).

    To clear lingering rows, run scripts/migrations/scanner_removal.sql
    or manually: UPDATE pair_states SET state='shadow' WHERE state='discovered'.
    """
    return TransitionDecision(False)


def evaluate_shadow(
    state: PairState,
    metrics: PairMetrics,
    crit: TransitionCriteria,
    now: int | None = None,
    auto_promotion_enabled: bool = False,
) -> TransitionDecision:
    """
    shadow → live (if criteria met) or shadow → rejected (if 7d without edge).
    """
    now = now or int(time.time())
    started = state.shadow_started_at or state.state_since
    days_in_shadow = (now - started) / 86400

    # Check rejection first (7 days without proven edge) — DISABLED by default
    # (crit.reject_enabled=False); pairs never auto-reject, they stay in shadow.
    if (crit.reject_enabled
            and days_in_shadow >= crit.reject_after_days
            and metrics.profit_factor_24h < crit.reject_max_profit_factor):
        return TransitionDecision(
            should_transition=True,
            target_state=REJECTED,
            reason=(
                f"shadow_days={days_in_shadow:.1f}>={crit.reject_after_days} "
                f"and PF24h={metrics.profit_factor_24h:.2f}<{crit.reject_max_profit_factor}"
            ),
        )

    # Promotion check — ALL criteria must pass
    checks = [
        (metrics.trades_24h >= crit.promotion_min_trades,
         f"trades={metrics.trades_24h}>={crit.promotion_min_trades}"),
        (days_in_shadow >= crit.promotion_min_days_in_shadow,
         f"days={days_in_shadow:.1f}>={crit.promotion_min_days_in_shadow}"),
        (metrics.winrate_24h >= crit.promotion_min_winrate,
         f"WR={metrics.winrate_24h*100:.1f}%>={crit.promotion_min_winrate*100:.0f}%"),
        (metrics.profit_factor_24h >= crit.promotion_min_profit_factor,
         f"PF={metrics.profit_factor_24h:.2f}>={crit.promotion_min_profit_factor}"),
        (metrics.avg_edge_pct_24h >= crit.promotion_min_edge_pct,
         f"edge={metrics.avg_edge_pct_24h:.3f}%>={crit.promotion_min_edge_pct}%"),
        # DD6h check removed — formula was producing false positives.
        # 24h PnL/winrate/PF are stronger signals.
    ]

    all_pass = all(passed for passed, _ in checks)
    if all_pass and auto_promotion_enabled:
        reason = " | ".join(desc for _, desc in checks)
        return TransitionDecision(
            should_transition=True,
            target_state=LIVE,
            reason=f"promotion: {reason}",
        )
    # auto-promotion disabled → shadow pairs stay in shadow until a manual
    # /promote. (Auto-promotion ran on SHADOW metrics, which over-estimate
    # live for spread-crossing pairs — adverse selection — so promotion is
    # now an explicit human decision.)

    return TransitionDecision(False)


def evaluate_live(
    state: PairState,
    metrics: PairMetrics,
    crit: TransitionCriteria,
    auto_demotion_enabled: bool = True,
) -> TransitionDecision:
    """live → paused: any single demotion criterion triggers.

    auto_demotion_enabled=False bypasses all demotion checks. Used when a pair
    is being actively tested with a new config and we don't want stale 24h
    metrics (from old config) to demote it before new config produces enough
    trades for fair evaluation.
    """
    # Per-pair override: skip demotion entirely when explicitly disabled
    if not auto_demotion_enabled:
        return TransitionDecision(False)

    triggers = []

    if crit.demotion_24h_pnl_negative and metrics.trades_24h >= 20 and metrics.pnl_24h < 0:
        # Only demote on PnL with meaningful sample size — for scalpers,
        # a 35% loss rate is normal but profitable in aggregate, so a
        # single loss should not pause a pair.
        triggers.append(f"24h_pnl={metrics.pnl_24h:.2f}<0 (after {metrics.trades_24h} trades)")

    if metrics.trades_24h >= 20 and metrics.winrate_24h < crit.demotion_min_winrate_24h:
        # Only demote on WR if we have enough sample size
        triggers.append(f"24h_WR={metrics.winrate_24h*100:.1f}%<{crit.demotion_min_winrate_24h*100:.0f}%")

    # DD6h check removed — formula produced false positives like DD=244% on
    # profitable pairs. 24h PnL<0 + winrate<48% (above) cover real degradation.

    if triggers:
        return TransitionDecision(
            should_transition=True,
            target_state=PAUSED,
            reason="demotion: " + " | ".join(triggers),
        )

    return TransitionDecision(False)


def evaluate_paused(
    state: PairState,
    metrics: PairMetrics,
    crit: TransitionCriteria,
    now: int | None = None,
) -> TransitionDecision:
    """paused → shadow: auto-resume after pause_duration.

    Manual pauses (pause_reason starts with 'manual:') NEVER auto-resume,
    regardless of paused_until value.  The user must explicitly resume via
    Telegram (Shadow ON, /mode, or /slot reassign).

    This fixes a bug where manual pauses with a non-NULL paused_until
    (set by older code paths or default fallback) would silently auto-resume
    after the timestamp expired — especially visible after bot restarts
    where the expired timestamp triggers immediate resume on first
    _evaluate_all cycle.
    """
    # Manual pauses are sacred — never auto-resume
    if state.pause_reason and state.pause_reason.startswith("manual:"):
        return TransitionDecision(False)

    # Persistent pause (paused_until=None) — never auto-resume
    if not state.paused_until:
        return TransitionDecision(False)

    # Timed pause with expiry — auto-resume when expired
    now = now or int(time.time())
    if now >= state.paused_until:
        return TransitionDecision(
            should_transition=True,
            target_state=SHADOW,
            reason="auto-resume after pause cooldown",
        )
    return TransitionDecision(False)


def evaluate_transition(
    state: PairState,
    metrics: PairMetrics,
    crit: TransitionCriteria,
    now: int | None = None,
    auto_demotion_enabled: bool = True,
    auto_promotion_enabled: bool = False,
) -> TransitionDecision:
    """Dispatch on current state — returns the action (if any) to take.

    auto_demotion_enabled is forwarded only to evaluate_live; other states
    don't have a demotion concept. auto_promotion_enabled (default False)
    gates shadow→live auto-promotion — disabled because it ran on shadow
    metrics that over-estimate live (adverse selection); promote manually.
    """
    if state.state == DISCOVERED:
        return evaluate_discovered(state, metrics, crit)
    if state.state == SHADOW:
        return evaluate_shadow(state, metrics, crit, now,
                               auto_promotion_enabled=auto_promotion_enabled)
    if state.state == LIVE:
        return evaluate_live(state, metrics, crit, auto_demotion_enabled)
    if state.state == PAUSED:
        return evaluate_paused(state, metrics, crit, now)
    # REJECTED is terminal — no transitions
    return TransitionDecision(False)
