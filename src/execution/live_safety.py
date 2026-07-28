"""
Live Safety Controller — protect real capital from runaway bugs and bad markets.

ONE automatic kill, on purpose: PEAK DRAWDOWN. Halt the slot when PnL falls
LIVE_MAX_DRAWDOWN below the session high-water mark. Measuring from the peak
rather than from zero is what lets it catch a real bleed inside a day that is
still net-positive.

A cumulative daily-loss threshold and a consecutive-loss pause used to sit
alongside it. Both were removed 2026-07-28: a profitable day masked the first
and the UTC-midnight reset split it, while the second fired on ordinary variance
(~19 times per 1,330 trades at the observed ~43% losing rate) and paused the
slot for an hour each time. Three kills writing one flag also made a halt hard
to attribute. Do not add them back without new evidence.

The remaining guards are not kills, they are admission checks:
  * per-symbol max position count — one live position per symbol at a time
  * margin sanity — refuse to open if margin exceeds the per-trade cap

All checks are evaluated BEFORE every live entry. State is in-memory
(no DB persistence — restart resets state, which is intentional for safety).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SafetyState:
    """Per-day live trading state."""
    today_pnl: float = 0.0
    peak_pnl: float = 0.0        # session high-water mark (for the drawdown kill)
    today_trades: int = 0
    consecutive_losses: int = 0
    # Smoothed notional of a position on this slot — the drawdown limit
    # is a percentage of it, so a resize retunes the kill automatically.
    avg_notional_usdt: float = 0.0
    _logged_dd_step: int = 0        # deepest quarter-of-limit already logged
    _logged_peak: float = 0.0
    _closes_since_log: int = 0
    _last_log_ts: float = 0.0
    kill_active: bool = False
    kill_reason: str = ""
    kill_until_ts: int = 0  # 0 = indefinite
    open_live_positions: dict[str, int] = field(default_factory=dict)  # symbol → count


class LiveSafetyController:
    """
    Gatekeeper for all live trades.

    Configure via constructor. Defaults are conservative — tighten further
    when starting live, loosen as confidence grows.
    """

    def __init__(
        self,
        # THE kill switch — peak drawdown. Halt if PnL falls this far BELOW
        # the session high-water mark, which catches a genuine bleed even inside
        # a net-positive day. SINGLE SOURCE = env LIVE_MAX_DRAWDOWN
        # (main.py → LivePool → here); this is only the unset-env fallback.
        max_drawdown_usdt: float = 20.0,       # fallback until size is known
        # Fraction of ONE position's notional. The worst drawdown in 21 days
        # of live trading was $29.72 at ~$1,400 notional = 2.1%; 2.5% sits
        # just above everything observed at any size.
        drawdown_pct_of_notional: float = 0.025,
        min_drawdown_usdt: float = 5.0,        # a bad notional must not
                                               # produce a limit of pennies
        kill_pause_sec: int = 14400,                    # 4h pause

        # Per-symbol limits
        max_concurrent_per_symbol: int = 1,
        max_concurrent_total: int = 2,                  # across ALL symbols

        # Margin sanity
        max_margin_per_trade_usdt: float = 10.0,        # never risk more than this
    ) -> None:
        self.max_drawdown_usdt = max_drawdown_usdt
        self.drawdown_pct_of_notional = drawdown_pct_of_notional
        self.min_drawdown_usdt = min_drawdown_usdt
        self.kill_pause_sec = kill_pause_sec
        self.max_concurrent_per_symbol = max_concurrent_per_symbol
        self.max_concurrent_total = max_concurrent_total
        self.max_margin_per_trade_usdt = max_margin_per_trade_usdt

        self.state = SafetyState()
        self._daily_reset_at_ts = self._next_reset_ts()

        logger.info(
            "LiveSafetyController initialized: max_drawdown=$%.2f (the only "
            "kill), max_concurrent=%d, max_margin/trade=$%.2f",
            max_drawdown_usdt,
            max_concurrent_total,
            max_margin_per_trade_usdt,
        )

    def _next_reset_ts(self) -> int:
        """Reset daily counters at next 00:00 UTC."""
        now = int(time.time())
        # Round to next 86400-second boundary (UTC midnight)
        return (now // 86400 + 1) * 86400

    def _maybe_reset_daily(self) -> None:
        """Reset daily PnL/counters at UTC midnight."""
        if int(time.time()) >= self._daily_reset_at_ts:
            logger.info(
                "Daily safety reset: previous PnL=$%.2f trades=%d",
                self.state.today_pnl, self.state.today_trades,
            )
            self.state.today_pnl = 0.0
            self.state.peak_pnl = 0.0
            self.state.today_trades = 0
            self.state.consecutive_losses = 0
            self.state._logged_dd_step = 0
            self.state._logged_peak = 0.0
            # Don't reset kill_active here — kill persists until kill_until_ts
            self._daily_reset_at_ts = self._next_reset_ts()

    def can_open_live(
        self,
        symbol: str,
        margin_usdt: float,
    ) -> tuple[bool, str]:
        """
        Check if we're allowed to open a live position right now.

        Returns (allowed, reason_if_blocked).
        """
        self._maybe_reset_daily()

        now = int(time.time())

        # Kill switch active
        if self.state.kill_active:
            if self.state.kill_until_ts == 0 or now < self.state.kill_until_ts:
                return False, f"kill_active: {self.state.kill_reason}"
            else:
                # Kill expired — auto-clear
                logger.info("Kill switch expired — resuming live trading")
                self.state.kill_active = False
                self.state.kill_reason = ""
                # Re-baseline the drawdown high-water mark to the resume point.
                # Otherwise peak_pnl still holds the pre-kill high, so the FIRST
                # losing close after resume instantly re-crosses the drawdown
                # limit and re-kills — turning a 4h pause into a day-long lockout
                # of a pair the thesis says is profitable. A genuinely NEW bleed
                # is then measured from where we actually resumed.
                self.state.peak_pnl = self.state.today_pnl

        # Margin sanity
        if margin_usdt > self.max_margin_per_trade_usdt:
            return False, (
                f"margin ${margin_usdt:.2f} exceeds max ${self.max_margin_per_trade_usdt:.2f}"
            )

        # Per-symbol concurrent limit
        sym_count = self.state.open_live_positions.get(symbol, 0)
        if sym_count >= self.max_concurrent_per_symbol:
            return False, f"max_concurrent_per_symbol reached for {symbol}"

        # Total concurrent limit
        total_open = sum(self.state.open_live_positions.values())
        if total_open >= self.max_concurrent_total:
            return False, f"max_concurrent_total ({self.max_concurrent_total}) reached"

        return True, ""

    def record_open(self, symbol: str) -> None:
        """Note that a live position was opened (after successful API call)."""
        self.state.open_live_positions[symbol] = (
            self.state.open_live_positions.get(symbol, 0) + 1
        )

    def drawdown_limit(self) -> float:
        """Dollars of drawdown this slot may take, at its CURRENT position size.

        A fixed dollar limit is stale the moment sizing changes — and the two
        slots differ ninefold today, so no single number fits both. Falls back
        to the configured dollar value until the first close reveals the size.
        """
        if self.state.avg_notional_usdt <= 0:
            return self.max_drawdown_usdt
        return max(self.min_drawdown_usdt,
                   self.drawdown_pct_of_notional * self.state.avg_notional_usdt)

    def record_close(self, symbol: str, pnl_usdt: float,
                     notional_usdt: float | None = None) -> None:
        """Note that a live position was closed.

        notional_usdt teaches the controller this slot's position size, which
        sets the drawdown limit. Omitting it leaves the previous estimate.
        """
        # Decrement counter
        if symbol in self.state.open_live_positions:
            self.state.open_live_positions[symbol] = max(
                0, self.state.open_live_positions[symbol] - 1
            )
            if self.state.open_live_positions[symbol] == 0:
                del self.state.open_live_positions[symbol]

        self.state.today_pnl += pnl_usdt
        self.state.today_trades += 1
        if self.state.today_pnl > self.state.peak_pnl:
            self.state.peak_pnl = self.state.today_pnl

        if pnl_usdt < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        # PRIMARY kill — PEAK DRAWDOWN: PnL fell >= limit below the session high.
        # The real "bleed" catch: fires even inside a net-positive day, is not
        # masked by earlier profit, and does not depend on the UTC-midnight reset.
        # Learn the size. EMA because margin and leverage are randomised per
        # trade by ~15% and the limit should not jitter with them.
        if notional_usdt and notional_usdt > 0:
            a = self.state.avg_notional_usdt
            self.state.avg_notional_usdt = (
                notional_usdt if a <= 0 else 0.9 * a + 0.1 * notional_usdt)

        dd_limit = self.drawdown_limit()
        drawdown = self.state.peak_pnl - self.state.today_pnl
        self._log_equity(drawdown, dd_limit)
        if dd_limit > 0 and drawdown >= dd_limit:
            self.engage_kill(
                reason=(f"drawdown ${drawdown:.2f} from session peak "
                        f"${self.state.peak_pnl:.2f} (limit ${dd_limit:.2f})"),
                duration_sec=self.kill_pause_sec,
            )

    def _log_equity(self, drawdown: float, dd_limit: float) -> None:
        """Make the peak and the drawdown visible without spamming the log.

        One line when the peak advances by a quarter of the limit, and one each
        time the drawdown deepens past another quarter of it. At ~2,000 trades a
        day a line per close would be unreadable.
        """
        # Heartbeat: the step thresholds below only fire at 25% of the limit,
        # which at ordinary PnL is once in hours. This keeps the peak visible.
        self.state._closes_since_log += 1
        _now = time.time()
        if self.state._last_log_ts <= 0:
            self.state._last_log_ts = _now
        if (self.state._closes_since_log >= 100
                or _now - self.state._last_log_ts >= 900):
            self.state._closes_since_log = 0
            self.state._last_log_ts = _now
            logger.info(
                "[EQUITY] %d угод: PnL $%+.2f, пік $%+.2f, просадка $%.2f з $%.2f "
                "(позиція ~$%.0f)",
                self.state.today_trades, self.state.today_pnl, self.state.peak_pnl,
                drawdown, dd_limit, self.state.avg_notional_usdt)

        step = int(drawdown / dd_limit * 4) if dd_limit > 0 else 0
        if step > self.state._logged_dd_step:
            self.state._logged_dd_step = step
            logger.info(
                "[EQUITY] slot PnL $%+.2f, peak $%+.2f, drawdown $%.2f of $%.2f "
                "(%.0f%%, position ~$%.0f)",
                self.state.today_pnl, self.state.peak_pnl, drawdown, dd_limit,
                100 * drawdown / dd_limit, self.state.avg_notional_usdt)
        elif self.state.peak_pnl >= self.state._logged_peak + dd_limit / 4:
            self.state._logged_peak = self.state.peak_pnl
            self.state._logged_dd_step = 0
            logger.info(
                "[EQUITY] slot new peak $%+.2f (limit $%.2f, position ~$%.0f)",
                self.state.peak_pnl, dd_limit, self.state.avg_notional_usdt)
        elif step == 0:
            self.state._logged_dd_step = 0

    def engage_kill(self, reason: str, duration_sec: int = 0) -> None:
        """
        Activate kill switch. duration_sec=0 means indefinite.

        Existing positions are NOT auto-closed by this. Caller must
        explicitly close them via close_all_live_positions().
        """
        now = int(time.time())
        candidate = (now + duration_sec) if duration_sec > 0 else 0  # 0 = indefinite
        # Only ever EXTEND an active halt, never shorten it. With one automatic
        # kill left nothing here collides today, but the rule is what makes a
        # second engage_kill during an active halt safe: a later, shorter
        # deadline must not resume trading early. Keep whichever reaches further
        # into the future (0 = indefinite always wins).
        if self.state.kill_active:
            cur = self.state.kill_until_ts
            if cur == 0:
                keep_existing = True                 # already indefinite
            elif candidate == 0:
                keep_existing = False                 # new indefinite beats finite
            else:
                keep_existing = cur >= candidate      # keep the longer deadline
            if keep_existing:
                logger.info(
                    "Kill already active with a longer/equal halt (until %s) — "
                    "keeping it; not shortening for '%s'",
                    cur or "indefinite", reason,
                )
                return
        self.state.kill_active = True
        self.state.kill_reason = reason
        self.state.kill_until_ts = candidate
        logger.warning(
            "🚨 KILL SWITCH ENGAGED: %s (duration=%ss)", reason, duration_sec or "indefinite"
        )

    def release_kill(self) -> tuple[bool, str]:
        """Operator override: lift the halt on this slot.

        Returns (was_active, the_reason_it_had).

        peak_pnl is re-baselined to the current PnL for the same reason the
        expiry path in can_open_live does it: leaving the pre-kill high-water
        mark standing means the next losing close instantly re-crosses the
        drawdown limit and kills again, so lifting the halt would buy exactly
        one trade. The slot gets its full LIVE_MAX_DRAWDOWN of room back from
        where it now stands.

        today_pnl and consecutive_losses are NOT reset — the day's real PnL
        stays on the record and in the halt alert. This is an override:
        pressing it repeatedly through a genuine bleed will keep the slot
        trading.
        """
        was_active = self.state.kill_active
        reason = self.state.kill_reason
        self.state.kill_active = False
        self.state.kill_reason = ""
        self.state.kill_until_ts = 0
        self.state.peak_pnl = self.state.today_pnl
        self.state._logged_dd_step = 0
        self.state._logged_peak = self.state.today_pnl
        if was_active:
            logger.warning(
                "Kill switch RELEASED by operator (was: %s) — drawdown baseline "
                "reset to $%.2f", reason, self.state.today_pnl)
        return was_active, reason

    def is_killed(self) -> bool:
        self._maybe_reset_daily()
        if not self.state.kill_active:
            return False
        if self.state.kill_until_ts == 0:
            return True
        return int(time.time()) < self.state.kill_until_ts

    def state_summary(self) -> dict:
        from datetime import datetime
        kill_until_human = "indefinite"
        if self.state.kill_until_ts > 0:
            kill_until_human = datetime.utcfromtimestamp(self.state.kill_until_ts).strftime("%H:%M:%S UTC")
        return {
            "kill_active": self.state.kill_active,
            "kill_reason": self.state.kill_reason,
            "kill_until_ts": self.state.kill_until_ts,
            "kill_until_human": kill_until_human,
            "today_pnl": round(self.state.today_pnl, 4),
            "peak_pnl": round(self.state.peak_pnl, 4),
            "drawdown": round(self.state.peak_pnl - self.state.today_pnl, 4),
            "today_trades": self.state.today_trades,
            "consecutive_losses": self.state.consecutive_losses,
            "open_live_positions": dict(self.state.open_live_positions),
        }
