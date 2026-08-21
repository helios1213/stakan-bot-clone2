"""Operator's manual kill-release must survive a restart.

The complaint (2026-08-21): "I clear the kill switch and after a restart it
comes back." It did, and it was not a mystery — `release_kill` re-baselines the
peak in MEMORY only, while `_hydrate_safety` replays the whole day from
`live_trades` on every rebuild. The replay restored the PRE-release peak, the
drawdown was breached again, and `hydrate_session` re-engaged the kill by
design ("otherwise a restart is a way to buy yourself another trade").

Both halves are right on their own; together they made the button last exactly
until the next restart. The fix persists WHEN the operator released, so the
replay re-bases the peak at that same point in history.

The safety net itself is untouched: a fresh bleed AFTER the release still kills.
"""
from __future__ import annotations

import pytest

from src.execution.live_safety import LiveSafetyController


def ctl(max_dd=25.0):
    return LiveSafetyController(max_drawdown_usdt=max_dd, kill_pause_sec=3600)


# (pnl, notional, closed_at) — a day that peaks at +30 then bleeds past the limit
DAY = [(30.0, 4000.0, 1000), (-26.0, 4000.0, 2000), (-1.0, 4000.0, 3000)]


def test_without_a_release_marker_the_kill_still_re_engages():
    """The original protection must stay intact — this is the control case."""
    c = ctl()
    c.hydrate_session(DAY)
    assert c.is_killed(), "a breached drawdown must survive a restart"


# The realistic sequence: the bleed at t=2000 is what fired the kill, so the
# operator clears it AFTER that — at 2500.
RELEASED_AT = 2500


def test_release_after_the_bleed_is_honoured_after_restart():
    c = ctl()
    c.hydrate_session(DAY, released_at=RELEASED_AT)
    assert not c.is_killed(), "manual release must survive the restart"


def test_peak_is_rebased_to_where_the_slot_stood_at_release():
    c = ctl()
    c.hydrate_session(DAY, released_at=RELEASED_AT)
    # at 2500 the day stood at +30-26 = +4; that is the new high-water mark,
    # NOT the pre-bleed +30 — otherwise the drawdown is instantly breached again
    assert c.state.peak_pnl == pytest.approx(4.0)
    assert c.state.today_pnl == pytest.approx(3.0)


def test_release_does_not_erase_the_days_pnl():
    """release_kill deliberately keeps today_pnl on the record."""
    c = ctl()
    c.hydrate_session(DAY, released_at=RELEASED_AT)
    assert c.state.today_pnl == pytest.approx(3.0)
    assert c.state.today_trades == 3


def test_a_fresh_bleed_after_the_release_still_kills():
    """The override must not become a permanent free pass."""
    day = DAY + [(-40.0, 4000.0, 4000)]
    c = ctl()
    c.hydrate_session(day, released_at=RELEASED_AT)
    assert c.is_killed(), "a NEW drawdown past the limit must still halt the slot"


def test_release_after_the_last_close_is_not_lost():
    """Operator cleared it after the day's final trade — the marker still applies."""
    c = ctl()
    c.hydrate_session(DAY, released_at=9999)
    assert not c.is_killed()
    assert c.state.peak_pnl == pytest.approx(c.state.today_pnl)


def test_release_marker_of_zero_means_no_release():
    c = ctl()
    c.hydrate_session(DAY, released_at=0)
    assert c.is_killed()


def test_rebasing_happens_once_then_the_peak_tracks_normally():
    day = [(30.0, 4000.0, 1000), (-26.0, 4000.0, 2000),
           (1.0, 4000.0, 3000), (1.0, 4000.0, 4000)]
    c = ctl()
    c.hydrate_session(day, released_at=RELEASED_AT)
    # rebased once to +4, then the two winners lift it to +6
    assert c.state.peak_pnl == pytest.approx(6.0)


# ---- wiring: the UI must go through the pool, which persists -------------

def test_ui_paths_do_not_release_directly_on_the_controller():
    """Two UI paths used to call the controller directly and persist nothing."""
    from pathlib import Path
    for src in ("src/telegram_bot/bot.py", "src/telegram_bot/cmd_slot.py"):
        text = Path(src).read_text()
        assert ".release_kill()" not in text, (
            f"{src} bypasses live_pool.release_kill and loses the marker"
        )
        assert "live_pool.release_kill(" in text


def test_pool_persists_the_marker_and_scopes_it_to_the_day():
    from pathlib import Path
    pool = Path("src/execution/live_pool.py").read_text()
    assert "kill_released_at:slot" in pool
    assert "INSERT OR REPLACE INTO live_state" in pool
    # yesterday's release must not silence today's guard
    assert "ctl.session_start_ts()" in pool
