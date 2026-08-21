"""T2.2 — pairwise shadow-vs-live on the SAME signal, without touching live.

Until now the intersection of `signal_uid` between `shadow_trades` and
`live_trades` was EXACTLY ZERO: a pair is either live or shadow, never both, so
every "did shadow get more honest?" measurement compared different calendar
windows. That is how three rounds of feed-lag calibration ended up flying blind.

Now every live entry attempt also records what the simulator WOULD have decided
against the SAME book and the SAME submitted limit — both answers on one row.

The hard constraint is that live must not pay for it:
  * on the path to submit there is only a snapshot of two dicts (<=40 entries)
  * the ladder walk — 5-15ms, the very cost that got it removed from the live
    fast-path — runs afterwards, in a detached task
  * nothing it does can raise into the order path
  * it writes to its OWN table, so no existing aggregate, alert or pair-promotion
    can be moved by it
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from src.strategy.shadow_engine import ShadowEngine

SRC = Path("src/strategy/shadow_engine.py").read_text()


def _fn(name):
    return inspect.getsource(getattr(ShadowEngine, name))


# ---- the live path must stay untouched ----------------------------------

def test_only_a_dict_snapshot_happens_before_submit():
    """Everything between the snapshot and place_ioc_open is what live pays."""
    i = SRC.index("_twin_snap = None")
    j = SRC.index("live_result = await executor.place_ioc_open(")
    between = SRC[i:j]
    assert "simulate_ioc_entry" not in between, "the 5-15ms walk crept back in"
    assert "await" not in between, "no awaits may be added before submit"
    assert "dict(_ob._bids)" in between and "dict(_ob._asks)" in between


def test_snapshot_failure_cannot_break_the_order():
    i = SRC.index("_twin_snap = None")
    seg = SRC[i:i + 400]
    assert "try:" in seg and "except Exception:" in seg


def test_the_walk_runs_only_after_the_live_result_exists():
    """Scheduling must sit AFTER place_ioc_open returns, never before."""
    j = SRC.index("live_result = await executor.place_ioc_open(")
    k = SRC.index("self._schedule_twin(")
    assert k > j, "twin scheduled before the live order was even sent"


def test_scheduling_is_fire_and_forget():
    body = _fn("_schedule_twin")
    assert "asyncio.create_task" in body
    assert "await" not in body.split("def _schedule_twin")[1].split("except")[0], (
        "the caller must not wait for the twin"
    )


def test_scheduling_never_raises():
    assert "except Exception:" in _fn("_schedule_twin")


def test_the_recorder_never_raises():
    assert "except Exception:" in _fn("_record_twin")


def test_task_refs_are_held_so_gc_cannot_kill_them_mid_flight():
    assert "self._twin_tasks.add(task)" in _fn("_schedule_twin")
    assert "self._twin_tasks: set = set()" in SRC


# ---- what it records has to be comparable -------------------------------

def test_it_compares_against_the_limit_that_really_went_to_the_exchange():
    """Recomputing the limit would compare shadow against something that never
    existed. entry_limit_price was added for exactly this."""
    body = _fn("_record_twin")
    assert 'getattr(live_result, "limit_price_scaled", 0.0)' in body
    assert "if limit <= 0:" in body, "no limit means nothing to compare"


def test_it_replays_the_snapshot_not_the_current_book():
    """By the time the task runs the real book has moved on — using it would
    measure the delay, not the simulator."""
    body = _fn("_record_twin")
    assert "ob.apply_snapshot(list(bids.items()), list(asks.items())" in body
    assert "self.ob_manager.get" not in body


def test_both_verdicts_land_on_one_row():
    body = _fn("_record_twin")
    for col in ("live_filled", "shadow_filled", "live_price", "shadow_price",
                "live_filled_pct", "shadow_filled_pct", "signal_uid"):
        assert col in body, f"{col} missing — the row would not be comparable"


def test_it_writes_to_its_own_table():
    """Writing into shadow_trades would poison fill-rate, PnL and promotion."""
    body = _fn("_record_twin")
    assert "INSERT INTO shadow_twin" in body
    # docstring mentions shadow_trades to explain WHY — check statements only
    code = body[body.index('"""', body.index('"""') + 3) + 3:]
    for verb in ("INSERT INTO shadow_trades", "UPDATE shadow_trades",
                 "INSERT INTO live_trades", "UPDATE live_trades"):
        assert verb not in code, f"twin must never write to {verb.split()[-1]}"


def test_table_exists_in_the_migration():
    db = Path("src/storage/db.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS shadow_twin" in db
    assert "idx_shadow_twin_sym_ts" in db


def test_orderbook_is_imported():
    """py_compile passes on a missing name — it fails at runtime, on a fill."""
    assert "from src.exchanges.orderbook import OrderBook, OrderBookManager" in SRC


@pytest.mark.parametrize("name", ["_schedule_twin", "_record_twin"])
def test_methods_are_reachable(name):
    assert callable(getattr(ShadowEngine, name))


# ---- the expired case is the one that matters ---------------------------

def test_expiry_path_also_carries_the_limit():
    """255 twin rows showed 100% agreement — because only FILLED live orders
    ever produced a row: `limit_price_scaled` was populated on the success
    return only, and the recorder skips rows without a limit. The interesting
    case (live expired, would the simulator have filled?) was invisible."""
    ex = Path("src/execution/live_executor.py").read_text()
    i = ex.index('"[IOC OPEN] %s SKIPPED after %d attempts')
    seg = ex[i:i + 3000]      # the comment block before the return is long
    assert "limit_price_scaled=limit_scaled" in seg, (
        "the expiry return drops the limit, so expired orders never get a twin"
    )


def test_limit_is_bound_before_the_attempt_loop():
    """It is assigned INSIDE the loop but read AFTER it. If every iteration
    exits early via continue, the name is unbound — NameError on the live
    order path."""
    ex = Path("src/execution/live_executor.py").read_text()
    init = ex.index("limit_scaled = 0.0")
    loop = ex.index("for attempt in range(1, max_attempts + 1):")
    assert init < loop, "limit_scaled must be initialised before the loop"


def test_twin_skips_rows_without_a_limit():
    body = _fn("_record_twin")
    assert "if limit <= 0:" in body
