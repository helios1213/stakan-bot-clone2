"""The burst alert says "increase the size" — so it must know what the size IS.

Before this, the alert reported frequency, movement and 5-min PnL, then advised
raising the size without a single number about the current one. The operator had
to go and look up whether there was any room, which is exactly the lookup you do
not want to be doing while a burst is running.

Now it carries: notional and margin of the trades IN THIS BURST, the pair's
configured margin cap, the slot balance, and which of the two knobs is actually
binding — the size within the cap, or the cap itself.
"""
from __future__ import annotations

import asyncio

import pytest

from src.strategy.shadow_engine import ShadowEngine


class _DB:
    """Minimal stand-in: returns rows by looking at the SQL."""

    def __init__(self, cap=50.0, balance=258.0, fail=False):
        self.cap, self.balance, self.fail = cap, balance, fail

    async def fetchone(self, sql, params=()):
        if self.fail:
            raise RuntimeError("db down")
        if "slot_pair_sizing" in sql:
            return (self.cap,) if self.cap is not None else None
        if "webkey_slots" in sql:
            return (self.balance,) if self.balance is not None else None
        return None


def ctx(cur_margin, cur_notional, **kw):
    eng = object.__new__(ShadowEngine)
    eng.db = _DB(**kw)
    return asyncio.run(eng._size_context(2, "PENGUUSDT", cur_margin, cur_notional))


def test_reports_the_size_actually_being_traded():
    out = ctx(48.5, 2185.0)
    assert "$2185" in out
    assert "48.5" in out


def test_shows_the_cap_so_the_advice_is_actionable():
    out = ctx(20.0, 900.0, cap=50.0)
    assert "$50" in out
    assert "можна ще" in out, "there is room — say so"


def test_says_the_cap_itself_is_binding_when_size_is_maxed():
    """The distinction that matters: raise the SIZE vs raise the LIMIT."""
    out = ctx(49.0, 2200.0, cap=50.0)
    assert "вибрано ліміт" in out
    assert "можна ще" not in out


@pytest.mark.parametrize("margin,expect_capped", [(47.4, False), (47.6, True)])
def test_cap_threshold_is_95_percent(margin, expect_capped):
    out = ctx(margin, 2000.0, cap=50.0)
    assert ("вибрано ліміт" in out) is expect_capped


def test_margin_is_expressed_against_the_balance():
    """$48 means nothing on its own — 19% of the account means something."""
    out = ctx(48.0, 2185.0, balance=258.0)
    assert "19%" in out
    assert "$258" in out


def test_survives_a_missing_sizing_row():
    out = ctx(48.0, 2185.0, cap=None)
    assert "$2185" in out, "still reports what it does know"
    assert "ліміт" not in out, "must not invent a cap it never read"


def test_survives_a_missing_balance():
    out = ctx(48.0, 2185.0, balance=None)
    assert "$2185" in out
    assert "баланс" not in out


def test_a_broken_db_never_breaks_the_alert():
    """Decoration must not take down the burst notification itself."""
    assert ctx(48.0, 2185.0, fail=True) == ""


def test_alert_pulls_size_from_the_burst_window_not_from_config():
    """Averaging the trades IN the window is the whole point — a config number
    would not tell you what is being traded right now."""
    from pathlib import Path
    src = Path("src/strategy/shadow_engine.py").read_text()
    assert 'rnoc = sum((r["notional_usdt"] or 0.0) for r in w) / n_win' in src
    assert 'rmar = sum((r["margin_usdt"] or 0.0) for r in w) / n_win' in src
    # and the query must actually fetch them
    i = src.index("peak_ticks_at_1000ms, notional_usdt, margin_usdt")
    assert i > 0


def test_push_carries_the_size_too():
    """The push is what reaches the phone — the number has to be in there."""
    from pathlib import Path
    src = Path("src/strategy/shadow_engine.py").read_text()
    i = src.index("_send_pushover(\n")
    seg = src[i:i + 700]
    assert "маржа $%.1f" in seg and "ноціонал $%.0f" in seg


# ---- the bps trigger: anomalously good trading, whatever the size ---------

def _burst_src():
    from pathlib import Path
    src = Path("src/strategy/shadow_engine.py").read_text()
    i = src.index("async def _burst_alert_loop")
    return src[i:src.index("\n    async def ", i + 50)]


def test_bps_is_normalised_by_notional_so_size_cannot_matter():
    """The whole point: $500 and $5000 must be measured with one ruler."""
    b = _burst_src()
    assert 'rbps = (rpnl / _wn * 1e4) if _wn > 0 else 0.0' in b
    assert '_wn = sum((r["notional_usdt"] or 0.0) for r in w)' in b


def test_bps_is_compared_against_the_pairs_own_norm():
    """Absolute bps would fire constantly on a naturally rich pair."""
    b = _burst_src()
    assert "rbps >= bps_mult * max(base_bps, 0.2)" in b, (
        "must compare to the pair's own baseline, with a floor so a barely "
        "profitable norm does not turn noise into a burst"
    )


def test_either_trigger_can_fire_but_movement_gate_is_mandatory():
    """pk1000 is what separates a real move from chop — dropping it took the
    replay from 100% profitable to 88-90% with losing alerts."""
    b = _burst_src()
    assert "is_burst = _move_ok and (_by_rate or _by_bps or _by_abs)" in b


def test_bps_trigger_uses_a_lower_frequency_bar():
    """Its evidence is quality, not volume — so it must not need the full 2.25x."""
    b = _burst_src()
    assert "rate >= bps_rate_mult * base_rate" in b
    assert 'BURST_BPS_RATE_MULT", "1.5"' in b
    assert 'BURST_RATE_MULT", "3.0"' in b or "rate_mult" in b


def test_thresholds_are_env_tunable():
    b = _burst_src()
    for k in ("BURST_BPS_MULT", "BURST_BPS_MIN", "BURST_BPS_RATE_MULT"):
        assert k in b, f"{k} must be tunable without a rebuild"


def test_alert_says_which_trigger_fired():
    """6 of 8 replayed alerts came from bps — without this you cannot tell
    which half of the detector is earning its keep."""
    b = _burst_src()
    assert '("частота", _by_rate)' in b and '("bps", _by_bps)' in b
    assert "тригер: %s" in b


def test_bps_and_norm_are_both_shown():
    b = _burst_src()
    assert "rbps, base_bps" in b


# ---- the blind spot: a uniformly good period is its own baseline ---------

def test_absolute_bps_trigger_exists():
    """Replaying 2026-08-18 — the day the docstring claims was caught — the
    relative-only detector fired ZERO times: window bps +1.21 against a norm of
    +0.96 is only x1.26, far under the x2.5 bar. The norm is the trailing hour,
    so a day that is good ALL DAY becomes its own baseline and nothing looks
    anomalous. An absolute floor is the only thing that sees it."""
    b = _burst_src()
    assert 'BURST_BPS_ABS' in b
    assert "_by_abs = (bps_abs > 0 and rbps >= bps_abs" in b


def test_absolute_trigger_can_be_switched_off():
    b = _burst_src()
    assert "bps_abs > 0" in b, "0 must disable it, like every other knob here"


def test_absolute_trigger_still_requires_movement_and_some_activity():
    """Left alone it would fire on a single lucky trade in a dead market."""
    b = _burst_src()
    assert "is_burst = _move_ok and (_by_rate or _by_bps or _by_abs)" in b
    i = b.index("_by_abs = ")
    assert "rate >= bps_rate_mult * base_rate" in b[i:i + 220]


def test_base_trades_gate_lowered_with_the_reason_recorded():
    """20/hour blocked 1950 scans on 08-18 — the detector never even got to
    evaluate the day it was supposed to catch."""
    b = _burst_src()
    assert 'BURST_MIN_BASE_TRADES", "10"' in b


def test_trigger_label_lists_every_firing_condition():
    b = _burst_src()
    assert '("абс.bps", _by_abs)' in b
