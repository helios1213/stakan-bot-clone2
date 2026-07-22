"""Min mid-gap entry filter (min_mexc_lag_pct) — re-introduced 2026-06-18.

Blocks entries whose Binance↔MEXC mid-to-mid gap (signal.mexc_lag_pct, in %)
is below the threshold. The tick gate (min_ticks) passes these tiny-mid-gap
signals; data (1093 trades) shows mid-gap < 0.5bps wins ~9% and 77/98 such
trades exit via simple_adverse (the bleed). Config plumbing spans 5 places
(ExecutionConfig field+reader, PairExecConfig field, 2 conversions) — this
pins the full chain + the gate behaviour.
"""
from types import SimpleNamespace

from src.config_loader import ExecutionConfig, _parse_execution
from src.strategy.shadow_engine import PairExecConfig


# ── config plumbing ────────────────────────────────────────────────────
def test_execution_default_disabled():
    # default 0.0 = disabled → behaviour-preserving until set per-pair
    assert ExecutionConfig().min_mexc_lag_pct == 0.0


def test_pairexec_default_disabled():
    assert PairExecConfig().min_mexc_lag_pct == 0.0


def test_parse_execution_reads_value():
    cfg = _parse_execution({"min_mexc_lag_pct": 0.005}, ExecutionConfig())
    assert cfg.min_mexc_lag_pct == 0.005


def test_parse_execution_inherits_default_when_absent():
    cfg = _parse_execution({"ioc_offset_ticks": 0}, ExecutionConfig())
    assert cfg.min_mexc_lag_pct == 0.0


# ── gate behaviour (mirrors the production condition in _handle_signal) ──
def _blocked(cfg_thr, signal_lag):
    cfg = PairExecConfig(min_mexc_lag_pct=cfg_thr)
    sig = SimpleNamespace(mexc_lag_pct=signal_lag)
    return cfg.min_mexc_lag_pct > 0 and abs(sig.mexc_lag_pct) < cfg.min_mexc_lag_pct


def test_gate_blocks_tiny_gap():
    # thr 0.005 (=0.5bps); a 0.003 (=0.3bps) mid-gap → noise → blocked
    assert _blocked(0.005, 0.003) is True


def test_gate_blocks_tiny_negative_gap():
    # sign-agnostic: short signals carry negative lag; magnitude is what matters
    assert _blocked(0.005, -0.003) is True


def test_gate_allows_real_gap():
    # 0.008 (=0.8bps) >= threshold → real lead → allowed
    assert _blocked(0.005, 0.008) is False


def test_gate_disabled_allows_everything():
    # thr 0 = disabled → even a near-zero gap passes
    assert _blocked(0.0, 0.0001) is False


def test_gate_boundary_allows_at_threshold():
    # exactly at threshold is NOT below it → allowed (inclusive keep)
    assert _blocked(0.005, 0.005) is False
