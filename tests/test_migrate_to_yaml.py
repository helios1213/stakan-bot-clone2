"""Tests for Stage 18 migrate_to_yaml.py.

Validates:
  1. .env parsing handles common forms (quotes, comments, blanks)
  2. Detector env keys → global.yaml correctly typed
  3. Per-pair DB gap_* columns → pairs/<SYMBOL>.yaml
  4. NIZAR_<SYMBOL>_ADAPTIVE_* env vars → pair adaptive_exit section
  5. Pairs with no overrides produce NO yaml file
  6. New .env strips all migrated keys, keeps the rest
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Import the script as a module
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import migrate_to_yaml as mig


# ──────────────────────────────────────────────────────────────────────
# .env parsing
# ──────────────────────────────────────────────────────────────────────

def test_parse_env_basic(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "KEY1=value1\n"
        "KEY2=value2\n"
        "\n"
        "# another comment\n"
        "KEY3 = value3\n"
    )
    env = mig.parse_env(env_file)
    assert env == {"KEY1": "value1", "KEY2": "value2", "KEY3": "value3"}


def test_parse_env_strips_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('KEY="quoted"\nOTHER=\'single\'\n')
    env = mig.parse_env(env_file)
    assert env == {"KEY": "quoted", "OTHER": "single"}


def test_parse_env_handles_empty_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("EMPTY=\nFILLED=x\n")
    env = mig.parse_env(env_file)
    assert env == {"EMPTY": "", "FILLED": "x"}


def test_parse_env_missing_file(tmp_path):
    env = mig.parse_env(tmp_path / "nonexistent")
    assert env == {}


# ──────────────────────────────────────────────────────────────────────
# Build global.yaml from env
# ──────────────────────────────────────────────────────────────────────

def test_build_global_extracts_detector_keys():
    env = {
        "STATIC_GAP_NIZAR_MIN_TICKS": "3",
        "STATIC_GAP_COOLDOWN_SEC": "3",
        "STATIC_GAP_BOTH_SIDES": "1",
        # Non-detector keys are ignored
        "MASTER_KEY": "abc",
        "IOC_MAX_ATTEMPTS": "3",
    }
    g = mig.build_global_yaml(env)
    assert g["detector"]["min_ticks"] == 3
    assert g["detector"]["cooldown_sec"] == 3.0
    assert g["detector"]["both_sides"] is True
    assert "MASTER_KEY" not in str(g)
    assert "ioc" not in g


def test_build_global_handles_missing_keys():
    """If env has no STATIC_GAP_* keys, global.yaml is empty dict."""
    env = {"MASTER_KEY": "abc"}
    g = mig.build_global_yaml(env)
    assert g == {}


def test_build_global_bool_coercion():
    """0/1 strings must become Python bool in YAML output."""
    env = {
        "STATIC_GAP_NIZAR_MODE": "1",
        "STATIC_GAP_BOTH_SIDES": "0",
    }
    g = mig.build_global_yaml(env)
    assert g["detector"]["nizar_mode"] is True
    assert g["detector"]["both_sides"] is False


# ──────────────────────────────────────────────────────────────────────
# Per-pair from DB
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def db_with_pair(tmp_path):
    """A minimal DB with pair_configs schema and one PENGU row."""
    db = tmp_path / "stakan.db"
    con = sqlite3.connect(db)
    con.execute("""
        CREATE TABLE pair_configs (
            symbol TEXT PRIMARY KEY,
            gap_min_ticks INTEGER,
            gap_cooldown_sec REAL
        )
    """)
    yield db, con
    con.close()


def test_build_pair_yaml_with_overrides(db_with_pair, tmp_path):
    db, con = db_with_pair
    con.execute(
        "INSERT INTO pair_configs VALUES ('PENGUUSDT', 3, 2.5)"
    )
    con.commit()

    py = mig.build_pair_yaml("PENGUUSDT", db, env={})
    assert py is not None
    assert py["detector"]["min_ticks"] == 3
    assert py["detector"]["cooldown_sec"] == 2.5


def test_build_pair_yaml_no_overrides_returns_none(db_with_pair):
    """A pair with all-NULL gap_* columns should produce no YAML file."""
    db, con = db_with_pair
    con.execute(
        "INSERT INTO pair_configs VALUES "
        "('ADAUSDT', NULL, NULL)"
    )
    con.commit()
    py = mig.build_pair_yaml("ADAUSDT", db, env={})
    assert py is None


def test_build_pair_yaml_pulls_adaptive_from_env(db_with_pair):
    """NIZAR_<SYMBOL>_ADAPTIVE_* env vars go into pair YAML."""
    db, con = db_with_pair
    con.execute(
        "INSERT INTO pair_configs VALUES "
        "('PENGUUSDT', 3, NULL)"
    )
    con.commit()

    env = {
        "NIZAR_PENGUUSDT_ADAPTIVE_MIN_HOLD_MS": "0",
        "NIZAR_PENGUUSDT_ADAPTIVE_STALL_MS": "2500",
        "NIZAR_PENGUUSDT_ADAPTIVE_REVERSAL_TICKS": "1.0",
        # Different pair's adaptive vars should NOT leak in
        "NIZAR_TAOUSDT_ADAPTIVE_STALL_MS": "1234",
    }
    py = mig.build_pair_yaml("PENGUUSDT", db, env=env)
    assert py["adaptive_exit"]["min_hold_ms"] == 0
    assert py["adaptive_exit"]["stall_ms"] == 2500
    assert py["adaptive_exit"]["reversal_ticks"] == 1.0
    # TAOUSDT's value should not be here
    assert "1234" not in str(py)


def test_build_pair_yaml_adaptive_only_no_db_overrides(db_with_pair):
    """A pair with only adaptive env vars but no DB overrides still
    produces a YAML file."""
    db, con = db_with_pair
    con.execute(
        "INSERT INTO pair_configs VALUES "
        "('PENGUUSDT', NULL, NULL)"
    )
    con.commit()
    env = {"NIZAR_PENGUUSDT_ADAPTIVE_STALL_MS": "2500"}
    py = mig.build_pair_yaml("PENGUUSDT", db, env=env)
    assert py is not None
    assert py["adaptive_exit"]["stall_ms"] == 2500
    assert "detector" not in py


# ──────────────────────────────────────────────────────────────────────
# New .env construction
# ──────────────────────────────────────────────────────────────────────

def test_build_new_env_keeps_secrets_drops_static_gap():
    env = {
        "MASTER_KEY": "abc",
        "TELEGRAM_BOT_TOKEN": "tok",
        "STATIC_GAP_MIN_TICKS": "4",
        "STATIC_GAP_IMBALANCE_ENABLED": "1",
        "IOC_MAX_ATTEMPTS": "3",
        "LIVE_TRADING_ENABLED": "1",
        "NIZAR_PENGUUSDT_ADAPTIVE_STALL_MS": "2500",
    }
    new = mig.build_new_env(env)
    assert "MASTER_KEY=abc" in new
    assert "TELEGRAM_BOT_TOKEN=tok" in new
    assert "IOC_MAX_ATTEMPTS=3" in new
    assert "LIVE_TRADING_ENABLED=1" in new
    # Migrated keys must be gone
    assert "STATIC_GAP_MIN_TICKS" not in new
    assert "STATIC_GAP_IMBALANCE" not in new
    assert "NIZAR_PENGUUSDT" not in new


def test_build_new_env_empty():
    new = mig.build_new_env({})
    # Header still present even when empty
    assert "stakan-bot environment" in new


# ──────────────────────────────────────────────────────────────────────
# set_nested helper
# ──────────────────────────────────────────────────────────────────────

def test_set_nested_creates_path():
    d = {}
    mig.set_nested(d, "a.b.c", 5)
    assert d == {"a": {"b": {"c": 5}}}


def test_set_nested_merges_with_existing():
    d = {"a": {"x": 1}}
    mig.set_nested(d, "a.y", 2)
    assert d == {"a": {"x": 1, "y": 2}}


def test_set_nested_flat_key():
    d = {}
    mig.set_nested(d, "flat", 7)
    assert d == {"flat": 7}
