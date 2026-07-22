"""Tests for bps-based exit strategy thresholds."""


def test_effective_stop_adverse_bps_overrides_ticks():
    """When stop_adverse_bps > 0, it should override stop_adverse_ticks."""
    from src.config_loader import ExitStrategyConfig

    es = ExitStrategyConfig(
        stop_adverse_ticks=5,
        stop_adverse_bps=10,  # 10bps = 0.10%
    )
    # ZEC: entry=$550, tick_scaled=$0.01
    # 10bps of $550 = $0.55 → $0.55 / $0.01 = 55 ticks
    result = es.effective_stop_adverse_ticks(entry_price=550.0, tick_scaled=0.01)
    assert abs(result - 55.0) < 0.01, f"Expected 55 ticks, got {result}"


def test_effective_stop_adverse_falls_back_to_ticks():
    """When stop_adverse_bps = 0, use stop_adverse_ticks."""
    from src.config_loader import ExitStrategyConfig

    es = ExitStrategyConfig(
        stop_adverse_ticks=5,
        stop_adverse_bps=0,
    )
    result = es.effective_stop_adverse_ticks(entry_price=550.0, tick_scaled=0.01)
    assert result == 5.0


def test_effective_trail_distance_bps():
    """trail_distance_bps should convert correctly."""
    from src.config_loader import ExitStrategyConfig

    es = ExitStrategyConfig(
        trail_distance_ticks=2,
        trail_distance_bps=5,  # 5bps = 0.05%
    )
    # SUI: entry=$1.032, tick_scaled=0.0001
    # 5bps of $1.032 = $0.000516 → $0.000516 / $0.0001 = 5.16 ticks
    result = es.effective_trail_distance_ticks(entry_price=1.032, tick_scaled=0.0001)
    assert abs(result - 5.16) < 0.1, f"Expected ~5.16 ticks, got {result}"


def test_effective_breakeven_trigger_bps():
    """breakeven_trigger_bps should convert correctly."""
    from src.config_loader import ExitStrategyConfig

    es = ExitStrategyConfig(
        breakeven_trigger_ticks=2.0,
        breakeven_trigger_bps=8,  # 8bps
    )
    # BTC: entry=$76000, tick_scaled=0.1
    # 8bps of $76000 = $60.8 → $60.8 / $0.1 = 608 ticks
    result = es.effective_breakeven_trigger_ticks(entry_price=76000.0, tick_scaled=0.1)
    assert abs(result - 608.0) < 0.1, f"Expected ~608 ticks, got {result}"


def test_bps_same_percentage_across_pairs():
    """Same bps value should produce same % risk across different pairs."""
    from src.config_loader import ExitStrategyConfig

    es = ExitStrategyConfig(stop_adverse_bps=10)  # 10bps = 0.10%

    # ZEC $550 tick $0.01
    zec_ticks = es.effective_stop_adverse_ticks(550.0, 0.01)
    zec_pct = zec_ticks * 0.01 / 550.0 * 100

    # PEPE $0.003624 tick $0.000001
    pepe_ticks = es.effective_stop_adverse_ticks(0.003624, 0.000001)
    pepe_pct = pepe_ticks * 0.000001 / 0.003624 * 100

    # BTC $76000 tick $0.1
    btc_ticks = es.effective_stop_adverse_ticks(76000.0, 0.1)
    btc_pct = btc_ticks * 0.1 / 76000.0 * 100

    # All should be 0.10% (10bps)
    assert abs(zec_pct - 0.10) < 0.001, f"ZEC: {zec_pct}%"
    assert abs(pepe_pct - 0.10) < 0.001, f"PEPE: {pepe_pct}%"
    assert abs(btc_pct - 0.10) < 0.001, f"BTC: {btc_pct}%"


def test_bps_zero_entry_price_falls_back():
    """If entry_price is 0 or invalid, fall back to ticks."""
    from src.config_loader import ExitStrategyConfig

    es = ExitStrategyConfig(stop_adverse_ticks=5, stop_adverse_bps=10)
    assert es.effective_stop_adverse_ticks(0, 0.01) == 5.0
    assert es.effective_stop_adverse_ticks(550.0, 0) == 5.0


def test_parse_exit_strategy_includes_bps():
    """YAML parser should pick up bps fields."""
    from src.config_loader import _parse_exit_strategy, ExitStrategyConfig

    raw = {
        "mode": "simple_trail",
        "stop_adverse_bps": 10,
        "trail_distance_bps": 5,
        "breakeven_trigger_bps": 8,
        "stall_timeout_ms": 2000,
    }
    es = _parse_exit_strategy(raw, ExitStrategyConfig())
    assert es.stop_adverse_bps == 10.0
    assert es.trail_distance_bps == 5.0
    assert es.breakeven_trigger_bps == 8.0
    assert es.stall_timeout_ms == 2000


def test_parse_exit_strategy_bps_defaults_zero():
    """When bps not specified in YAML, defaults to 0 (use ticks)."""
    from src.config_loader import _parse_exit_strategy, ExitStrategyConfig

    raw = {"mode": "simple_trail", "stop_adverse_ticks": 3}
    es = _parse_exit_strategy(raw, ExitStrategyConfig())
    assert es.stop_adverse_bps == 0.0
    assert es.trail_distance_bps == 0.0
    assert es.breakeven_trigger_bps == 0.0
    # ticks should still work
    assert es.stop_adverse_ticks == 3
