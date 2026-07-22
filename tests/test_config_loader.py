"""Tests for Stage 18 config_loader.

Validates:
  1. Parsing — YAML → typed PairConfig with proper defaults inheritance
  2. Fallback — missing file → returns built-in defaults
  3. Override semantics — pair file beats global; partial overrides work
  4. Hot reload — file change is picked up on maybe_reload()
  5. Deletion — removing a YAML removes the pair from cache
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from src.config_loader import (
    ConfigLoader,
    DetectorConfig,
    _parse_detector,
)


# ──────────────────────────────────────────────────────────────────────
# Test fixtures
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmpcfg(tmp_path) -> Path:
    """Empty config dir with pairs subdir."""
    (tmp_path / "pairs").mkdir()
    return tmp_path


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(data, f)


# ──────────────────────────────────────────────────────────────────────
# 1. Parsing — _parse_detector
# ──────────────────────────────────────────────────────────────────────

def test_parse_detector_empty_returns_defaults():
    d = _parse_detector({}, DetectorConfig())
    assert d == DetectorConfig()


def test_parse_detector_overrides_only_specified_fields():
    d = _parse_detector({"min_ticks": 5}, DetectorConfig())
    assert d.min_ticks == 5
    # Untouched fields keep defaults
    assert d.cooldown_sec == 5.0


def test_parse_detector_inherits_passed_defaults():
    """Defaults arg should be used when raw dict misses a key."""
    custom_defaults = DetectorConfig(min_ticks=99, cooldown_sec=42.0)
    d = _parse_detector({}, custom_defaults)
    assert d.min_ticks == 99
    assert d.cooldown_sec == 42.0


def test_parse_detector_string_bool_coercion():
    """YAML bools come in as Python bools, but ints (0/1) might also appear
    via env-style migration; verify both work (long_only — both_sides removed)."""
    d = _parse_detector({"long_only": True}, DetectorConfig())
    assert d.long_only is True
    d = _parse_detector({"long_only": 1}, DetectorConfig())
    assert d.long_only is True
    d = _parse_detector({"long_only": 0}, DetectorConfig())
    assert d.long_only is False


# ──────────────────────────────────────────────────────────────────────
# 2. ConfigLoader.load — initial state
# ──────────────────────────────────────────────────────────────────────

def test_load_with_no_files_uses_builtin_defaults(tmpcfg):
    loader = ConfigLoader(tmpcfg)
    loader.load()
    pc = loader.get("PENGUUSDT")
    assert pc.symbol == "PENGUUSDT"
    assert pc.detector == DetectorConfig()


def test_load_global_only(tmpcfg):
    write_yaml(tmpcfg / "global.yaml", {
        "detector": {"min_ticks": 3, "cooldown_sec": 2.0},
    })
    loader = ConfigLoader(tmpcfg)
    loader.load()
    # Any pair returns global defaults
    pc = loader.get("PENGUUSDT")
    assert pc.detector.min_ticks == 3
    assert pc.detector.cooldown_sec == 2.0


def test_load_pair_overrides_global(tmpcfg):
    write_yaml(tmpcfg / "global.yaml", {"detector": {"min_ticks": 3}})
    write_yaml(tmpcfg / "pairs" / "TAOUSDT.yaml", {
        "detector": {"min_ticks": 9},
    })
    loader = ConfigLoader(tmpcfg)
    loader.load()

    pengu = loader.get("PENGUUSDT")
    assert pengu.detector.min_ticks == 3  # from global

    tao = loader.get("TAOUSDT")
    assert tao.detector.min_ticks == 9  # from pair file


def test_pair_inherits_global_for_unspecified_fields(tmpcfg):
    write_yaml(tmpcfg / "global.yaml", {
        "detector": {"min_ticks": 3, "cooldown_sec": 5.0},
    })
    write_yaml(tmpcfg / "pairs" / "TAOUSDT.yaml", {
        "detector": {"min_ticks": 9},  # only override one field
    })
    loader = ConfigLoader(tmpcfg)
    loader.load()
    tao = loader.get("TAOUSDT")
    assert tao.detector.min_ticks == 9
    # Global values inherited for unspecified
    assert tao.detector.cooldown_sec == 5.0


def test_list_pairs(tmpcfg):
    write_yaml(tmpcfg / "pairs" / "PENGUUSDT.yaml", {"detector": {"min_ticks": 3}})
    write_yaml(tmpcfg / "pairs" / "TAOUSDT.yaml", {"detector": {"min_ticks": 9}})
    loader = ConfigLoader(tmpcfg)
    loader.load()
    assert loader.list_pairs() == ["PENGUUSDT", "TAOUSDT"]


# ──────────────────────────────────────────────────────────────────────
# 3. Hot reload via maybe_reload
# ──────────────────────────────────────────────────────────────────────

def test_maybe_reload_returns_false_within_ttl(tmpcfg):
    write_yaml(tmpcfg / "pairs" / "TAOUSDT.yaml", {"detector": {"min_ticks": 9}})
    loader = ConfigLoader(tmpcfg, reload_ttl_sec=60.0)
    loader.load()
    # Immediately after load, TTL hasn't elapsed
    assert loader.maybe_reload() is False


def test_maybe_reload_picks_up_changed_file(tmpcfg):
    pair_path = tmpcfg / "pairs" / "TAOUSDT.yaml"
    write_yaml(pair_path, {"detector": {"min_ticks": 9}})
    loader = ConfigLoader(tmpcfg, reload_ttl_sec=0.0)  # always check on maybe_reload
    loader.load()
    assert loader.get("TAOUSDT").detector.min_ticks == 9

    # Bump mtime far enough that filesystem definitely sees the change.
    time.sleep(0.01)
    write_yaml(pair_path, {"detector": {"min_ticks": 12}})
    # Force mtime change to be visible (some FS have second-granular mtime)
    import os
    os.utime(pair_path, (time.time() + 1, time.time() + 1))

    assert loader.maybe_reload() is True
    assert loader.get("TAOUSDT").detector.min_ticks == 12


def test_maybe_reload_picks_up_new_pair_file(tmpcfg):
    loader = ConfigLoader(tmpcfg, reload_ttl_sec=0.0)
    loader.load()
    assert loader.list_pairs() == []

    # Add new file
    write_yaml(tmpcfg / "pairs" / "BCHUSDT.yaml", {"detector": {"min_ticks": 7}})
    assert loader.maybe_reload() is True
    assert "BCHUSDT" in loader.list_pairs()
    assert loader.get("BCHUSDT").detector.min_ticks == 7


def test_maybe_reload_removes_deleted_pair_file(tmpcfg):
    pair_path = tmpcfg / "pairs" / "TAOUSDT.yaml"
    write_yaml(pair_path, {"detector": {"min_ticks": 9}})
    loader = ConfigLoader(tmpcfg, reload_ttl_sec=0.0)
    loader.load()
    assert "TAOUSDT" in loader.list_pairs()

    pair_path.unlink()
    assert loader.maybe_reload() is True
    assert "TAOUSDT" not in loader.list_pairs()
    # get() falls back to global defaults
    pc = loader.get("TAOUSDT")
    assert pc.detector == DetectorConfig()


def test_global_change_rebuilds_pair_inheritance(tmpcfg):
    """If global.yaml changes, pairs that inherited from old globals
    must be recomputed with new globals."""
    global_path = tmpcfg / "global.yaml"
    write_yaml(global_path, {"detector": {"cooldown_sec": 3.0}})
    write_yaml(tmpcfg / "pairs" / "TAOUSDT.yaml", {"detector": {"min_ticks": 9}})
    loader = ConfigLoader(tmpcfg, reload_ttl_sec=0.0)
    loader.load()
    assert loader.get("TAOUSDT").detector.cooldown_sec == 3.0

    # Change global
    time.sleep(0.01)
    write_yaml(global_path, {"detector": {"cooldown_sec": 7.0}})
    import os
    os.utime(global_path, (time.time() + 1, time.time() + 1))

    assert loader.maybe_reload() is True
    assert loader.get("TAOUSDT").detector.cooldown_sec == 7.0
    # Pair's own override survives the global rebuild
    assert loader.get("TAOUSDT").detector.min_ticks == 9


# ──────────────────────────────────────────────────────────────────────
# 4. Error handling
# ──────────────────────────────────────────────────────────────────────

def test_malformed_pair_yaml_does_not_crash(tmpcfg):
    bad = tmpcfg / "pairs" / "BAD.yaml"
    bad.write_text("this is: : : not [valid yaml")
    good = tmpcfg / "pairs" / "GOOD.yaml"
    write_yaml(good, {"detector": {"min_ticks": 5}})

    loader = ConfigLoader(tmpcfg)
    loader.load()  # must not raise

    # Good pair still loaded
    assert loader.get("GOOD").detector.min_ticks == 5
    # Bad pair falls back to defaults
    assert loader.get("BAD").detector == DetectorConfig()


def test_empty_pair_yaml_is_valid(tmpcfg):
    """An empty pair file means 'inherit everything from global'."""
    (tmpcfg / "pairs" / "EMPTY.yaml").write_text("")
    write_yaml(tmpcfg / "global.yaml", {"detector": {"min_ticks": 4}})

    loader = ConfigLoader(tmpcfg)
    loader.load()
    assert loader.get("EMPTY").detector.min_ticks == 4


# ──────────────────────────────────────────────────────────────────────
# 6. bps=0 guard — an explicit 0/negative exit bps must fail loudly
#    (it silently reverts to the deprecated tick fallback otherwise).
# ──────────────────────────────────────────────────────────────────────

def test_validate_exit_strategy_explicit_zero_raises():
    from src.config_loader import _validate_exit_strategy
    with pytest.raises(ValueError, match="stop_adverse_bps must be >0"):
        _validate_exit_strategy({"stop_adverse_bps": 0}, "ZECUSDT")
    with pytest.raises(ValueError, match="trail_distance_bps must be >0"):
        _validate_exit_strategy({"trail_distance_bps": -1}, "PENGUUSDT")


def test_loader_skips_pair_with_bad_exit_bps(tmpcfg, caplog):
    """A pair with explicit bps<=0 is excluded from the cache + logs an error
    (the loader swallows per-pair load errors so one bad file can't crash the
    bot; the reload path keeps the previous good config)."""
    import logging
    write_yaml(tmpcfg / "global.yaml",
               {"exit_strategy": {"stop_adverse_bps": 10, "trail_distance_bps": 3}})
    write_yaml(tmpcfg / "pairs" / "BADADV.yaml",
               {"exit_strategy": {"stop_adverse_bps": 0}})
    write_yaml(tmpcfg / "pairs" / "GOOD.yaml", {"detector": {"min_ticks": 5}})
    loader = ConfigLoader(tmpcfg)
    with caplog.at_level(logging.ERROR):
        loader.load()
    assert "BADADV" not in loader.list_pairs()
    assert "GOOD" in loader.list_pairs()
    assert any("stop_adverse_bps must be >0" in r.message for r in caplog.records)


def test_exit_breakeven_bps_zero_is_ok(tmpcfg):
    """breakeven_bps=0 = valid 'breakeven off' — must NOT raise."""
    write_yaml(tmpcfg / "pairs" / "BEZERO.yaml",
               {"exit_strategy": {"breakeven_trigger_bps": 0,
                                  "stop_adverse_bps": 10, "trail_distance_bps": 3}})
    loader = ConfigLoader(tmpcfg)
    loader.load()
    assert loader.get("BEZERO").exit_strategy.breakeven_trigger_bps == 0.0


def test_exit_bps_omitted_inherits_ok(tmpcfg):
    """Omitting bps (inherit positive global) must NOT trip the guard."""
    write_yaml(tmpcfg / "global.yaml",
               {"exit_strategy": {"stop_adverse_bps": 10, "trail_distance_bps": 3}})
    write_yaml(tmpcfg / "pairs" / "INHERIT.yaml", {"detector": {"min_ticks": 5}})
    loader = ConfigLoader(tmpcfg)
    loader.load()
    assert loader.get("INHERIT").exit_strategy.stop_adverse_bps == 10.0
