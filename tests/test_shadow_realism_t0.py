"""T0 of the shadow-realism plan: make shadow's own numbers honest.

Three separate holes, one test file:

1. `shadow_open_misses` only ever recorded `ioc_expired_no_fill`. Every other way
   a shadow entry dies (latency drift, simulated reject, simulated server error,
   book desync) returned silently, so the denominator of shadow's fill-rate was
   too small and shadow looked better at getting filled than it is.

2. `entry_slippage_pct` on live rows was measured against a stub equal to the
   signal price and never revisited once the real fill landed — it read exactly
   0.00 on all 86,719 live rows ever written.

3. PnL summaries compared shadow to live in DOLLARS, while the two trade
   different notional. On SOXL that made a 3.7x edge gap read as 1.9x.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.telegram_bot.bot import fmt_pnl_row

ENGINE = Path("src/strategy/shadow_engine.py").read_text()


class _Row(dict):
    """Stands in for an sqlite3.Row (supports .keys() like the real thing)."""


# ---- 1. every silent death now leaves a trace ----------------------------

def test_all_shadow_skip_reasons_are_recorded():
    reasons = set(re.findall(r'_record_shadow_miss\(symbol, "(\w+)"\)', ENGINE))
    assert reasons == {
        "ioc_expired_no_fill",   # the only one that used to be written
        "latency_drift",
        "sim_reject",
        "sim_server_error",
        "book_desynced",
    }


def test_no_shadow_only_skip_returns_without_recording():
    """Guard against re-introducing a silent `return`.

    Scoped to the SHADOW-ONLY region of `_try_open_position` — everything after
    `_is_live_pair` is decided. The one skip above that line (orderbook not
    synced at entry) deliberately stays unrecorded: it fires for live pairs too,
    so writing it to `shadow_open_misses` would blame shadow for live's skips.
    Inside the shadow-only region there is no such excuse — an entry that dies
    there and leaves no row silently shrinks the fill-rate denominator.
    """
    shadow_only = ENGINE[ENGINE.index("_is_live_pair = "):]
    for counter in ("signals_skipped_latency_drift", "entries_rejected",
                    "signals_skipped_no_book"):
        for m in re.finditer(rf"self\.{counter} \+= 1", shadow_only):
            tail = shadow_only[m.end():m.end() + 600]
            nxt = tail.find("return")
            if nxt == -1:
                continue
            assert "_record_shadow_miss" in tail[:nxt], (
                f"{counter} increments then returns without recording a miss — "
                f"that entry would vanish from the fill-rate denominator"
            )


def test_recording_is_best_effort_and_cannot_break_trading():
    body = ENGINE[ENGINE.index("async def _record_shadow_miss"):][:900]
    assert "try:" in body and "except Exception:" in body, (
        "a failed stats INSERT must never propagate into the trading loop"
    )


# ---- 2. live entry slippage is measured against the real limit -----------

def test_live_slippage_recomputed_against_submitted_limit():
    seg = ENGINE[ENGINE.index("live_result.fill_price > 0"):][:1400]
    assert "limit_price_scaled" in seg
    assert "pos.entry_limit_price" in seg
    # sign convention must match the shadow path: positive = worse than asked
    assert "(pos.entry_price - _lim) / _lim * 100" in seg      # long
    assert "(_lim - pos.entry_price) / _lim * 100" in seg      # short


def test_limit_price_is_surfaced_by_the_executor():
    ex = Path("src/execution/live_executor.py").read_text()
    assert "limit_price_scaled: float = 0.0" in ex, "field missing on LiveOrderResult"
    assert "limit_price_scaled=limit_scaled" in ex, "success path does not populate it"


def test_entry_target_price_semantics_not_redefined():
    """86k historical rows depend on the old meaning — the fix must ADD a column."""
    for src in ("src/storage/db.py", "src/storage/db_live.py"):
        assert "entry_limit_price" in Path(src).read_text(), f"{src} missing migration"


def test_insert_arity_matches():
    """The shadow/live INSERT is shared and hand-written; a mismatched column
    count fails only at runtime, on a real trade. Count them here instead."""
    i = ENGINE.index("INSERT INTO {target_table}")
    stmt = ENGINE[i:ENGINE.index('"""', i + 10)]
    cols_txt = stmt[stmt.index("(") + 1:stmt.index(")\n")]
    cols = [c.strip() for c in cols_txt.replace("\n", " ").split(",") if c.strip()]
    placeholders = stmt[stmt.index("VALUES"):].count("?")
    assert len(cols) == placeholders, (
        f"{len(cols)} columns vs {placeholders} placeholders"
    )
    assert "entry_limit_price" in cols


# ---- 3. PnL summaries compare in bps, not dollars ------------------------

def test_bps_is_shown_when_notional_is_known():
    row = _Row(n=100, wins=60, pnl=10.0, noc=100_000.0)
    out = fmt_pnl_row(row, "Today")
    assert "+1.000 bps" in out
    assert "$+10.00" in out, "dollars stay — bps is added, not a replacement"


def test_bps_exposes_a_gap_that_dollars_hide():
    """The real 2026-08-21 numbers: live traded 2x the notional of shadow."""
    shadow = fmt_pnl_row(_Row(n=10617, wins=5800, pnl=3374.0, noc=15_607_000.0), "S")
    live = fmt_pnl_row(_Row(n=974, wins=360, pnl=167.0, noc=2_856_000.0), "L")
    s_bps = float(re.search(r"([+-][\d.]+) bps", shadow).group(1))
    l_bps = float(re.search(r"([+-][\d.]+) bps", live).group(1))
    assert s_bps / l_bps > 3.0, "in bps the gap is ~3.7x"
    # while the dollar-per-trade ratio understates it badly
    assert (3374.0 / 10617) / (167.0 / 974) < 2.0


def test_missing_notional_degrades_to_dollars_only():
    out = fmt_pnl_row(_Row(n=5, wins=2, pnl=1.0), "Today")
    assert "bps" not in out and "$+1.00" in out


def test_zero_notional_does_not_divide_by_zero():
    out = fmt_pnl_row(_Row(n=5, wins=2, pnl=1.0, noc=0.0), "Today")
    assert "bps" not in out


def test_no_trades_row():
    assert fmt_pnl_row(_Row(n=0), "Today") == "<b>Today:</b> no trades"
    assert fmt_pnl_row(None, "Today") == "<b>Today:</b> no trades"


def test_both_summaries_share_one_renderer():
    """If they drift apart, one screen shows bps and the other doesn't — and the
    two stop being comparable, which is the whole point of the change."""
    bot = Path("src/telegram_bot/bot.py").read_text()
    assert bot.count("fmt = fmt_pnl_row") == 2


@pytest.mark.parametrize("table", ["shadow_trades", "live_trades"])
def test_both_pnl_queries_select_notional(table):
    bot = Path("src/telegram_bot/bot.py").read_text()
    for m in re.finditer(rf"FROM {table}\b", bot):
        head = bot[max(0, m.start() - 700):m.start()]
        if "SELECT COUNT(*) AS n" not in head:
            continue          # not a summary query
        assert "SUM(notional_usdt) AS noc" in head, (
            f"a {table} summary query without notional cannot render bps"
        )


# ---- 4. the dead knob stays dead ----------------------------------------

def test_dead_shadow_position_cap_is_gone():
    cfg = Path("config/config.yaml").read_text()
    assert "max_concurrent_shadow_positions: 5" not in cfg
    assert "max_positions_per_symbol: 1" in cfg, "the REAL cap must remain"
