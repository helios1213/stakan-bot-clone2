"""Tests for simple_trail v3 exit logic (May 2026).

Covers all 5 rules of simple_trail:
  1. simple_adverse
  2. simple_breakeven
  3. simple_trail
  4. simple_stalled
  5. simple_dead_on_arrival

Plus parser tests for the 3 new YAML fields.
"""
import time
from dataclasses import dataclass
from unittest.mock import patch


# ── Minimal stubs ──────────────────────────────────────────────────

@dataclass
class FakeExitStrategy:
    mode: str = "simple_trail"
    stop_adverse_ticks: int = 3
    trail_distance_ticks: int = 1
    min_hold_ms: int = 0
    breakeven_trigger_ticks: float = 2.0
    stall_timeout_ms: int = 1500
    dead_on_arrival_timeout_ms: int = 1000
    # bps overrides (0 = use ticks). Mirrors the real ExitStrategyConfig so
    # the fake stays compatible with shadow_engine's effective_* calls.
    stop_adverse_bps: float = 0.0
    trail_distance_bps: float = 0.0
    breakeven_trigger_bps: float = 0.0

    # effective_* — identical semantics to src/config_loader.py
    # ExitStrategyConfig: bps wins when > 0, otherwise fall back to ticks.
    def effective_stop_adverse_ticks(self, entry_price, tick_scaled):
        if self.stop_adverse_bps > 0 and entry_price > 0 and tick_scaled > 0:
            return (entry_price * self.stop_adverse_bps / 10000.0) / tick_scaled
        return float(self.stop_adverse_ticks)

    def effective_trail_distance_ticks(self, entry_price, tick_scaled):
        if self.trail_distance_bps > 0 and entry_price > 0 and tick_scaled > 0:
            return (entry_price * self.trail_distance_bps / 10000.0) / tick_scaled
        return float(self.trail_distance_ticks)

    def effective_breakeven_trigger_ticks(self, entry_price, tick_scaled):
        if self.breakeven_trigger_bps > 0 and entry_price > 0 and tick_scaled > 0:
            return (entry_price * self.breakeven_trigger_bps / 10000.0) / tick_scaled
        return float(self.breakeven_trigger_ticks)


@dataclass
class FakePairConfig:
    symbol: str = "SUIUSDT"
    exit_strategy: FakeExitStrategy = None
    def __post_init__(self):
        if self.exit_strategy is None:
            self.exit_strategy = FakeExitStrategy()


class FakeConfigLoader:
    def __init__(self, pc=None):
        self._pc = pc or FakePairConfig()
    def get(self, symbol):
        return self._pc


def _make_engine(exit_strategy=None):
    from src.strategy.shadow_engine import ShadowEngine
    eng = object.__new__(ShadowEngine)
    pc = FakePairConfig(exit_strategy=exit_strategy or FakeExitStrategy())
    eng._config_loader = FakeConfigLoader(pc)
    return eng


def _make_pos(direction="long", entry=1.0000, current=1.0000,
              peak=0.0, elapsed_sec=1.5, last_progress_ago_ms=0):
    from src.strategy.shadow_position import ShadowPosition
    pos = ShadowPosition(
        symbol="SUIUSDT",
        direction=direction,
        detector_source="static_gap",
        confidence=0.7,
    )
    pos.entry_price = entry
    pos.current_price = current
    pos.peak_price_favorable = peak if peak > 0 else 0.0
    pos.opened_at_ms = int((time.time() - elapsed_sec) * 1000)
    now = int(time.time() * 1000)
    pos.last_progress_ms = now - last_progress_ago_ms
    pos.update_price(current)
    # update_price may overwrite our peak — restore.
    pos.peak_price_favorable = peak if peak > 0 else 0.0
    return pos


# SUI: TICK=1e-4, SCALE=4 → tick_scaled = 4e-4
TICK_SCALED = 4e-4
PATCH = "src.strategy.shadow_engine"


# ── Rule 1: adverse ────────────────────────────────────────────────

@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_adverse_fires(mock_to, mock_scale, mock_tick):
    eng = _make_engine()
    pos = _make_pos(entry=1.0, current=1.0 - 3 * TICK_SCALED)
    assert eng._check_simple_trail_exit(pos) == "simple_adverse"


@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_adverse_below_threshold(mock_to, mock_scale, mock_tick):
    """Adverse 2.5t < threshold 3t and no other rule fires → None.

    Disable DOA + stall to isolate adverse rule behavior.
    """
    es = FakeExitStrategy(
        stop_adverse_ticks=3,
        stall_timeout_ms=0,
        dead_on_arrival_timeout_ms=0,
    )
    eng = _make_engine(es)
    pos = _make_pos(entry=1.0, current=1.0 - 2.5 * TICK_SCALED)
    assert eng._check_simple_trail_exit(pos) is None


# ── Rule 2: breakeven lock ─────────────────────────────────────────

@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_breakeven_fires(mock_to, mock_scale, mock_tick):
    """Peak hit 2.5t (above trigger=2.0), current at 0.3t → lock."""
    eng = _make_engine()
    pos = _make_pos(
        entry=1.0,
        current=1.0 + 0.3 * TICK_SCALED,
        peak=1.0 + 2.5 * TICK_SCALED,
    )
    assert eng._check_simple_trail_exit(pos) == "simple_breakeven"


@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_breakeven_disabled_when_zero(mock_to, mock_scale, mock_tick):
    es = FakeExitStrategy(breakeven_trigger_ticks=0.0)
    eng = _make_engine(es)
    pos = _make_pos(
        entry=1.0,
        current=1.0 + 0.3 * TICK_SCALED,
        peak=1.0 + 2.5 * TICK_SCALED,
    )
    # Without breakeven, this state doesn't trigger trail (pullback=2.2,
    # which IS >= trail=1) — so we get simple_trail instead. The point
    # is we DON'T get simple_breakeven.
    result = eng._check_simple_trail_exit(pos)
    assert result != "simple_breakeven"


@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_breakeven_not_fired_when_current_above_half(mock_to, mock_scale, mock_tick):
    """Peak at 2.5t but current at 1.0t (>0.5) → no breakeven."""
    eng = _make_engine()
    pos = _make_pos(
        entry=1.0,
        current=1.0 + 1.0 * TICK_SCALED,
        peak=1.0 + 2.5 * TICK_SCALED,
    )
    # pullback = 1.5t, trail threshold = 1t → simple_trail fires
    assert eng._check_simple_trail_exit(pos) == "simple_trail"


# ── Rule 3: trail from peak ────────────────────────────────────────

@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_trail_fires(mock_to, mock_scale, mock_tick):
    """Peak=3t, current=1.8t → pullback 1.2t >= trail 1t."""
    eng = _make_engine()
    pos = _make_pos(
        entry=1.0,
        current=1.0 + 1.8 * TICK_SCALED,
        peak=1.0 + 3.0 * TICK_SCALED,
    )
    assert eng._check_simple_trail_exit(pos) == "simple_trail"


# ── Rule 4: stall detection ────────────────────────────────────────

@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_stall_fires(mock_to, mock_scale, mock_tick):
    """Peak=0.7t (small profit), no progress 1600ms > threshold 1500."""
    eng = _make_engine()
    pos = _make_pos(
        entry=1.0,
        current=1.0 + 0.5 * TICK_SCALED,
        peak=1.0 + 0.7 * TICK_SCALED,
        last_progress_ago_ms=1600,
    )
    assert eng._check_simple_trail_exit(pos) == "simple_stalled"


@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_stall_disabled_when_zero(mock_to, mock_scale, mock_tick):
    es = FakeExitStrategy(stall_timeout_ms=0, dead_on_arrival_timeout_ms=0)
    eng = _make_engine(es)
    pos = _make_pos(
        entry=1.0,
        current=1.0 + 0.5 * TICK_SCALED,
        peak=1.0 + 0.7 * TICK_SCALED,
        last_progress_ago_ms=5000,
    )
    # Pullback=0.2t < trail=1t, no stall, no DOA → None
    assert eng._check_simple_trail_exit(pos) is None


@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_stall_not_fired_when_no_profit(mock_to, mock_scale, mock_tick):
    """peak=0 → stall doesn't apply (dead_on_arrival handles this case)."""
    es = FakeExitStrategy(stall_timeout_ms=1500, dead_on_arrival_timeout_ms=0)
    eng = _make_engine(es)
    pos = _make_pos(
        entry=1.0,
        current=1.0 - 0.2 * TICK_SCALED,
        peak=0.0,
        last_progress_ago_ms=5000,
        elapsed_sec=5.0,
    )
    assert eng._check_simple_trail_exit(pos) is None


# ── Rule 5: dead-on-arrival ────────────────────────────────────────

@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_dead_on_arrival_fires(mock_to, mock_scale, mock_tick):
    """No peak after 1100ms (> threshold 1000) → DOA exit."""
    eng = _make_engine()
    pos = _make_pos(
        entry=1.0,
        current=1.0 - 0.5 * TICK_SCALED,  # slight drawdown but not adverse
        peak=0.0,
        elapsed_sec=1.1,
    )
    assert eng._check_simple_trail_exit(pos) == "simple_dead_on_arrival"


@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_dead_on_arrival_too_early(mock_to, mock_scale, mock_tick):
    """No peak but elapsed=500ms < threshold 1000 → no DOA yet."""
    eng = _make_engine()
    pos = _make_pos(
        entry=1.0,
        current=1.0 - 0.5 * TICK_SCALED,
        peak=0.0,
        elapsed_sec=0.5,
    )
    assert eng._check_simple_trail_exit(pos) is None


@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_dead_on_arrival_skipped_when_peak_exists(mock_to, mock_scale, mock_tick):
    """Peak > 0 means trade showed life — stall handles, not DOA."""
    es = FakeExitStrategy(
        stall_timeout_ms=0,  # disable stall
        dead_on_arrival_timeout_ms=1000,
    )
    eng = _make_engine(es)
    pos = _make_pos(
        entry=1.0,
        current=1.0 + 0.3 * TICK_SCALED,
        peak=1.0 + 0.6 * TICK_SCALED,  # peak exists
        elapsed_sec=2.0,
    )
    # peak > 0 → DOA doesn't fire. No other rule fires either → None.
    assert eng._check_simple_trail_exit(pos) is None


@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_dead_on_arrival_disabled_when_zero(mock_to, mock_scale, mock_tick):
    es = FakeExitStrategy(dead_on_arrival_timeout_ms=0, stall_timeout_ms=0)
    eng = _make_engine(es)
    pos = _make_pos(
        entry=1.0,
        current=1.0 - 0.5 * TICK_SCALED,
        peak=0.0,
        elapsed_sec=10.0,
    )
    assert eng._check_simple_trail_exit(pos) is None


# ── min_hold_ms filter ─────────────────────────────────────────────

@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_min_hold_blocks_all_exits(mock_to, mock_scale, mock_tick):
    """Within min_hold, no rule fires even if adverse deep."""
    es = FakeExitStrategy(min_hold_ms=1000)
    eng = _make_engine(es)
    pos = _make_pos(
        entry=1.0,
        current=1.0 - 5 * TICK_SCALED,  # deep adverse
        elapsed_sec=0.5,  # 500ms < 1000ms min_hold
    )
    assert eng._check_simple_trail_exit(pos) is None


# ── Short direction symmetry ───────────────────────────────────────

@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_short_dead_on_arrival(mock_to, mock_scale, mock_tick):
    """SHORT: price went UP (against us), peak never hit. DOA after 1.1s."""
    eng = _make_engine()
    pos = _make_pos(
        direction="short",
        entry=1.0,
        current=1.0 + 0.5 * TICK_SCALED,  # mild adverse for short
        peak=0.0,
        elapsed_sec=1.1,
    )
    assert eng._check_simple_trail_exit(pos) == "simple_dead_on_arrival"


@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_short_breakeven(mock_to, mock_scale, mock_tick):
    """SHORT: peak went DOWN to entry - 2.5t, current at entry - 0.3t."""
    eng = _make_engine()
    pos = _make_pos(
        direction="short",
        entry=1.0,
        current=1.0 - 0.3 * TICK_SCALED,  # favorable_ticks = 0.3
        peak=1.0 - 2.5 * TICK_SCALED,  # peak_favorable_ticks = 2.5
    )
    assert eng._check_simple_trail_exit(pos) == "simple_breakeven"


# ── Config parser tests ────────────────────────────────────────────

def test_parser_reads_new_fields():
    from src.config_loader import _parse_exit_strategy, ExitStrategyConfig
    raw = {
        "mode": "simple_trail",
        "stop_adverse_ticks": 4,
        "trail_distance_ticks": 2,
        "min_hold_ms": 500,
        "breakeven_trigger_ticks": 1.5,
        "stall_timeout_ms": 1200,
        "dead_on_arrival_timeout_ms": 800,
    }
    es = _parse_exit_strategy(raw, ExitStrategyConfig())
    assert es.breakeven_trigger_ticks == 1.5
    assert es.stall_timeout_ms == 1200
    assert es.dead_on_arrival_timeout_ms == 800


def test_parser_defaults_new_fields_to_zero():
    """Backward compat: existing YAMLs without new fields default to disabled."""
    from src.config_loader import _parse_exit_strategy, ExitStrategyConfig
    raw = {"mode": "simple_trail"}
    es = _parse_exit_strategy(raw, ExitStrategyConfig())
    assert es.breakeven_trigger_ticks == 0.0
    assert es.stall_timeout_ms == 0
    assert es.dead_on_arrival_timeout_ms == 0


# ── Priority order tests ───────────────────────────────────────────

@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_adverse_wins_over_breakeven(mock_to, mock_scale, mock_tick):
    """If both adverse and breakeven would fire, adverse fires first."""
    eng = _make_engine()
    # peak=2.5t (trigger), current=-3t (adverse)
    pos = _make_pos(
        entry=1.0,
        current=1.0 - 3 * TICK_SCALED,
        peak=1.0 + 2.5 * TICK_SCALED,
    )
    assert eng._check_simple_trail_exit(pos) == "simple_adverse"


@patch(f"{PATCH}.get_tick_size", return_value=1e-4)
@patch(f"{PATCH}.get_binance_scale", return_value=4)
@patch(f"{PATCH}.to_mexc", return_value="SUI_USDT")
def test_breakeven_wins_over_trail(mock_to, mock_scale, mock_tick):
    """If both breakeven and trail would fire, breakeven fires first."""
    eng = _make_engine()
    # peak=2.5t, current=0.3t. Pullback=2.2t (trail would fire), but
    # current <= 0.5 so breakeven also fires. Breakeven priority.
    pos = _make_pos(
        entry=1.0,
        current=1.0 + 0.3 * TICK_SCALED,
        peak=1.0 + 2.5 * TICK_SCALED,
    )
    assert eng._check_simple_trail_exit(pos) == "simple_breakeven"
