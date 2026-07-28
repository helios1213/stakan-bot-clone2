"""Two live slots are two MEXC accounts and must trade a signal independently.

Before this, a signal produced at most ONE position across all slots even on the
same pair: the cooldown, the max-positions check and the in-flight guard were all
keyed by SYMBOL, and the slot loop broke after the first attempt. Measured
consequence on 27.07: slot 2 only ever traded what slot 1 happened to skip, and
went to zero the moment slot 1 stopped being held by its soft start.

These tests pin the guarantee: every live slot gets its own pass, and one
account's state never gates another's.
"""
from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.strategy.shadow_engine import ShadowEngine
from src.strategy.shadow_position import ShadowPosition

SYM = "1000PEPEUSDT"


def _engine(slots: list[int] | None, *, in_live: bool = True) -> ShadowEngine:
    """Minimal engine wired for the entry-gate path only."""
    eng = ShadowEngine.__new__(ShadowEngine)
    eng.cfg = SimpleNamespace(enabled=True)
    eng.signals_received = 0
    eng.signals_fanned_out = 0
    eng.signals_skipped_not_tradeable = 0
    eng.signals_skipped_momentum = 0
    eng.signals_skipped_lag_out_of_range = 0
    eng.signals_skipped_cooldown = 0
    eng.signals_skipped_funding = 0
    eng.signals_skipped_max_positions = 0
    eng.signals_skipped_pending_submit = 0
    eng._cooldown_until = {}
    eng._pending_submissions = set()
    eng._open_positions = defaultdict(list)
    eng.max_positions_per_symbol = 1
    eng.funding_guard = SimpleNamespace(is_too_close_for_entry=lambda: False)
    eng.state_manager = SimpleNamespace(
        is_tradeable=lambda s: True,
        is_in_live=lambda s: in_live,
    )
    eng.live_pool = (None if slots is None
                     else SimpleNamespace(find_slots_for_pair=lambda s: list(slots)))
    eng._momentum_ok = lambda *a, **k: True
    eng._get_pair_config = AsyncMock(return_value=SimpleNamespace(min_mexc_lag_pct=0))
    eng._try_enter = AsyncMock()
    return eng


def _sig():
    return SimpleNamespace(symbol=SYM, direction="long")


def _pos(label: str | None) -> ShadowPosition:
    p = ShadowPosition(
        symbol=SYM, direction="long", detector_source="static_gap",
        confidence=0.5, leverage=50, margin_usdt=5.0, notional_usdt=250.0,
        entry_price=0.003, entry_target_price=0.003,
    )
    p.account_label = label
    return p


# ── fan-out ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_signal_reaches_every_live_slot():
    eng = _engine([1, 2])
    eng._enter_for_slot = AsyncMock()
    await eng.on_signal(_sig())
    assert sorted(c.args[3] for c in eng._enter_for_slot.await_args_list) == [1, 2]


@pytest.mark.asyncio
async def test_one_slot_raising_does_not_cancel_the_other():
    eng = _engine([1, 2])

    async def boom(signal, signal_id, cfg, pin_slot):
        if pin_slot == 1:
            raise RuntimeError("slot 1 exploded")

    eng._enter_for_slot = AsyncMock(side_effect=boom)
    await eng.on_signal(_sig())          # must not propagate
    assert sorted(c.args[3] for c in eng._enter_for_slot.await_args_list) == [1, 2]


def test_shadow_pair_stays_a_single_unpinned_pass():
    assert _engine(None)._entry_slots(SYM) == [None]
    assert _engine([1, 2], in_live=False)._entry_slots(SYM) == [None]
    assert _engine([])._entry_slots(SYM) == [None]


# ── the three gates are per account ───────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_of_one_account_leaves_the_other_free():
    eng = _engine([1, 2])
    eng._cooldown_until[(1, SYM)] = 2 ** 31          # slot 1 paused for ever
    await eng.on_signal(_sig())
    assert [c.args[3] for c in eng._try_enter.await_args_list] == [2]
    assert eng.signals_skipped_cooldown == 1


@pytest.mark.asyncio
async def test_open_position_on_one_account_leaves_the_other_free():
    eng = _engine([1, 2])
    eng._open_positions[SYM].append(_pos("slot1"))    # slot 1 already in a trade
    await eng.on_signal(_sig())
    assert [c.args[3] for c in eng._try_enter.await_args_list] == [2]
    assert eng.signals_skipped_max_positions == 1


@pytest.mark.asyncio
async def test_in_flight_submit_of_one_account_leaves_the_other_free():
    eng = _engine([1, 2])
    eng._pending_submissions.add((1, SYM))            # slot 1 mid-submit
    await eng.on_signal(_sig())
    assert [c.args[3] for c in eng._try_enter.await_args_list] == [2]
    assert eng.signals_skipped_pending_submit == 1


@pytest.mark.asyncio
async def test_each_account_still_holds_at_most_one_position():
    """Independence must not become "unlimited positions per account"."""
    eng = _engine([1, 2])
    eng._open_positions[SYM].extend([_pos("slot1"), _pos("slot2")])
    await eng.on_signal(_sig())
    assert eng._try_enter.await_count == 0
    assert eng.signals_skipped_max_positions == 2


@pytest.mark.asyncio
async def test_in_flight_key_is_released_for_its_own_slot():
    eng = _engine([1, 2])
    await eng.on_signal(_sig())
    assert eng._pending_submissions == set()


# ── the funnel counts slot passes, not signals ────────────────────────

@pytest.mark.asyncio
async def test_slot_passes_counted_once_per_slot():
    """passed_pre went negative because per-slot drops were subtracted from a
    per-signal total. The gates now have their own denominator."""
    eng = _engine([1, 2])
    await eng.on_signal(_sig())
    assert eng.signals_received == 1
    assert eng.signals_fanned_out == 2


@pytest.mark.asyncio
async def test_slot_passes_counted_for_a_shadow_pair_too():
    eng = _engine(None)
    await eng.on_signal(_sig())
    assert eng.signals_fanned_out == 1


@pytest.mark.asyncio
async def test_gate_skips_never_exceed_slot_passes():
    eng = _engine([1, 2])
    eng._cooldown_until[(1, SYM)] = 2 ** 31
    eng._pending_submissions.add((2, SYM))
    await eng.on_signal(_sig())
    drops = (eng.signals_skipped_cooldown + eng.signals_skipped_funding
             + eng.signals_skipped_max_positions
             + eng.signals_skipped_pending_submit)
    assert drops <= eng.signals_fanned_out


# ── one open alert per position actually opened ───────────────────────

@pytest.mark.asyncio
async def test_open_position_returns_what_it_created():
    """The alert wrapper announces the RETURNED position.

    Reading _open_positions[symbol][-1] announced the previous, still-open
    position whenever a call opened nothing (clone, 28.07 08:20: two identical
    [LIVE OPEN] messages for one trade).
    """
    from src.strategy.shadow_engine import ShadowEngine
    import inspect

    src = inspect.getsource(ShadowEngine._open_position)
    assert "return pos" in src, "_open_position must return the position it created"
    # Every early exit must yield None so the wrapper stays silent.
    assert src.count("return pos") == 1


# ── observability: per-slot skips, adverse snapshot ───────────────────

@pytest.mark.asyncio
async def test_skips_are_attributed_to_their_slot():
    """The global counters mix two accounts; the per-slot map must not."""
    eng = _engine([1, 2])
    eng._slot_skips = {}
    eng._cooldown_until[(1, SYM)] = 2 ** 31
    eng._pending_submissions.add((2, SYM))
    await eng.on_signal(_sig())
    assert eng._slot_skips.get((1, "cooldown")) == 1
    assert eng._slot_skips.get((2, "pending_submit")) == 1


def test_adverse_snapshot_records_the_instant_not_the_maximum():
    """nevergreen_cut tests the instantaneous excursion, not terminal mae_pct."""
    from src.strategy.shadow_position import ShadowPosition
    p = ShadowPosition(
        symbol=SYM, direction="long", detector_source="static_gap",
        confidence=0.5, leverage=50, margin_usdt=5.0, notional_usdt=250.0,
        entry_price=0.003, entry_target_price=0.003,
    )
    assert p.adverse_ticks_at_1000ms is None
    p.record_peak_snapshot(500, 0.0, 9.0)       # too early — not recorded
    assert p.adverse_ticks_at_1000ms is None
    p.record_peak_snapshot(1000, 0.0, 3.5)
    assert p.adverse_ticks_at_1000ms == 3.5
    p.record_peak_snapshot(1500, 0.0, 7.0)      # first cross wins
    assert p.adverse_ticks_at_1000ms == 3.5


def test_adverse_snapshot_is_optional():
    """Callers that pass no adverse value must not crash or write garbage."""
    from src.strategy.shadow_position import ShadowPosition
    p = ShadowPosition(
        symbol=SYM, direction="short", detector_source="static_gap",
        confidence=0.5, leverage=50, margin_usdt=5.0, notional_usdt=250.0,
        entry_price=0.003, entry_target_price=0.003,
    )
    p.record_peak_snapshot(1000, 2.0)
    assert p.adverse_ticks_at_1000ms is None
    assert p.peak_ticks_at_1000ms == 2.0


# ── the open-rate throttle must survive the soft-start removal ─────────

def test_throttle_machinery_intact():
    """Soft start only CALLED these; removing it must not have taken them.

    Losing any of them silently disables the hold between opens, and the bot
    then hammers a rate-limited account at ~1,600 requests/hour — the path that
    has already cost two banned accounts.
    """
    from src.strategy import shadow_engine as se
    for name in ("_arm_open_hold", "_humanize", "_env_float", "open_freq_limit_code"):
        assert hasattr(se.ShadowEngine, name) or hasattr(se, name), name
    assert se.OPEN_FREQ_CODES == ("10014", "9082", "2036")
    assert not hasattr(se, "SOFT_START_HOURS")
    for gone in ("_soft_start_plan", "_soft_start_now", "_arm_soft_start"):
        assert not hasattr(se.ShadowEngine, gone), f"{gone} survived"


def test_slot_config_still_carries_the_throttle_deadline():
    """live_pool's soft-start comment headed THREE keys; the third is the latch."""
    import inspect
    from src.execution import live_pool
    src = inspect.getsource(live_pool.LiveExecutorPool.get_slot_config)
    assert '"open_throttle_until"' in src
    assert "soft_start" not in src


class TestKillSwitchReset:
    """One method lifts the halt; it must also hand the drawdown room back."""

    @staticmethod
    def _ctl():
        from src.execution.live_safety import LiveSafetyController
        return LiveSafetyController()

    def test_release_kill_reports_and_lifts(self):
        c = self._ctl()
        c.state.kill_active = True
        c.state.kill_reason = "drawdown $31.00 from session peak"
        c.state.kill_until_ts = 2 ** 31
        was, why = c.release_kill()
        assert was is True and "drawdown" in why
        assert c.state.kill_active is False and c.state.kill_until_ts == 0
        allowed, _ = c.can_open_live("1000PEPEUSDT", 10.0)
        assert allowed is True

    def test_release_kill_rebaselines_the_high_water_mark(self):
        """Without this the next losing close instantly re-kills."""
        c = self._ctl()
        c.state.peak_pnl = 25.0
        c.state.today_pnl = -8.0
        c.state.kill_active = True
        c.release_kill()
        assert c.state.peak_pnl == -8.0

    def test_release_kill_keeps_the_days_real_pnl_on_the_record(self):
        c = self._ctl()
        c.state.today_pnl = -9.5
        c.state.consecutive_losses = 4
        c.state.kill_active = True
        c.release_kill()
        assert c.state.today_pnl == -9.5
        assert c.state.consecutive_losses == 4

    def test_drawdown_is_the_only_kill(self):
        """A deep cumulative loss with no drawdown from the peak must NOT kill."""
        c = self._ctl()
        c.state.today_pnl = -50.0      # would have tripped the old -$10 backstop
        c.state.peak_pnl = -50.0       # but never fell below its own high-water mark
        c.record_close("1000PEPEUSDT", 0.0)
        assert c.state.kill_active is False

    def test_a_long_losing_streak_no_longer_kills(self):
        """5-in-a-row used to pause the slot for an hour on ordinary variance."""
        c = self._ctl()
        for _ in range(12):
            c.record_close("1000PEPEUSDT", -0.05)
        assert c.state.consecutive_losses == 12   # still counted, for the alert
        assert c.state.kill_active is False

    def test_drawdown_fires_at_twenty(self):
        c = self._ctl()
        c.record_close("1000PEPEUSDT", +5.0)      # peak = 5
        assert c.state.kill_active is False
        c.record_close("1000PEPEUSDT", -14.0)     # -9 total, 14 below peak
        assert c.state.kill_active is False
        c.record_close("1000PEPEUSDT", -6.0)      # -15 total, 20 below peak
        assert c.state.kill_active is True

    def test_release_kill_gives_the_full_room_back(self):
        c = self._ctl()
        c.state.peak_pnl = 5.0
        c.state.today_pnl = -16.0
        c.state.kill_active = True
        c.release_kill()
        assert c.state.peak_pnl == -16.0
        c.record_close("1000PEPEUSDT", -1.0)      # only $1 below the new mark
        assert c.state.kill_active is False

    def test_release_kill_is_safe_when_nothing_is_active(self):
        c = self._ctl()
        was, why = c.release_kill()
        assert was is False and why == ""

    def test_release_kill_returns_a_pair_not_a_bool(self):
        """bot.py used `if safety.release_kill():` — a tuple is always truthy."""
        c = self._ctl()
        got = c.release_kill()
        assert isinstance(got, tuple) and len(got) == 2
        assert got[0] is False


class TestDrawdownScalesWithSize:
    """A dollar limit is stale the moment sizing changes — and it changed 2.5x.

    Every historical drawdown was measured at ~$1,400 of notional, so $20 was
    silently equivalent to $8 once PEPE moved to $2,755 — inside ordinary
    variance, which is why it fired on profitable days. The two live slots also
    differ ninefold ($2,755 vs $292), so no single number fits both.
    """

    @staticmethod
    def _ctl():
        from src.execution.live_safety import LiveSafetyController
        return LiveSafetyController()

    def test_limit_is_the_dollar_fallback_until_size_is_known(self):
        c = self._ctl()
        assert c.state.avg_notional_usdt == 0.0
        assert c.drawdown_limit() == c.max_drawdown_usdt

    def test_limit_tracks_the_position_size(self):
        c = self._ctl()
        c.record_close("1000PEPEUSDT", 0.0, notional_usdt=2755.0)
        assert abs(c.drawdown_limit() - 68.9) < 0.5      # 2.5% of 2,755
        d = self._ctl()
        d.record_close("LINKUSDT", 0.0, notional_usdt=292.0)
        assert abs(d.drawdown_limit() - 7.3) < 0.5       # 2.5% of 292

    def test_a_big_slot_survives_what_would_have_killed_it_before(self):
        """$25 of drawdown at $2,755 notional is ordinary; the old $20 killed it."""
        c = self._ctl()
        c.record_close("1000PEPEUSDT", +30.0, notional_usdt=2755.0)
        c.record_close("1000PEPEUSDT", -25.0, notional_usdt=2755.0)
        assert c.state.kill_active is False
        c.record_close("1000PEPEUSDT", -45.0, notional_usdt=2755.0)   # 70 below peak
        assert c.state.kill_active is True

    def test_a_small_slot_is_protected_proportionally(self):
        """The same $25 on a $292 position IS an emergency."""
        c = self._ctl()
        c.record_close("LINKUSDT", +2.0, notional_usdt=292.0)
        c.record_close("LINKUSDT", -25.0, notional_usdt=292.0)
        assert c.state.kill_active is True

    def test_floor_stops_a_bad_notional_making_a_penny_limit(self):
        c = self._ctl()
        c.record_close("X", 0.0, notional_usdt=1.0)
        assert c.drawdown_limit() == c.min_drawdown_usdt

    def test_size_is_smoothed_not_snapped(self):
        """Margin and leverage are randomised ~15% per trade."""
        c = self._ctl()
        c.record_close("X", 0.0, notional_usdt=1000.0)
        c.record_close("X", 0.0, notional_usdt=2000.0)
        assert 1000 < c.state.avg_notional_usdt < 1200


def test_open_limit_alert_text_is_actually_callable():
    """It was called as a bare name inside a try/except, so the alert silently
    never sent — the one thing the throttle feature exists to do."""
    from src.strategy.shadow_engine import ShadowEngine
    eng = ShadowEngine.__new__(ShadowEngine)
    freq = eng._al_text(2, "9082", 65.0, 21600.0, "1000PEPEUSDT")
    assert "SLOT2" in freq and "9082" in freq and "65" in freq
    assert "частоти" in freq
    pair = eng._al_text(1, "2036", 65.0, 21600.0, "1000PEPEUSDT")
    assert "2036" in pair and "1000PEPEUSDT" in pair
    assert "ордер" in pair, "2036 must name the pair, not the pace"


def test_open_limit_alert_is_reachable_from_the_engine_source():
    """Guard against the bare-name form coming back."""
    import inspect
    from src.strategy.shadow_engine import ShadowEngine
    src = inspect.getsource(ShadowEngine._open_position)
    assert "self._al_text(" in src
    assert "\n" + " " * 40 + "_al_text(" not in src
