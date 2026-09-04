"""SignalRecorder: forward MFE/MAE/horizons, fillability, dedup, direction sign."""
import asyncio
from src.strategy.signal_recorder import SignalRecorder

FEAT = dict(gap_ticks=5.0, long_gap=5.0, short_gap=-1.0, mid_gap_bps=2.0, passes_gate=1,
            bin_impulse=1.5, mexc_impulse=0.3, spread_bps=0.35, imbalance=0.6,
            gap_age_ms=120.0, hour=14, entry_exec=100.0)

# row indices after fillability insertion:
I_TOUCH, I_FILL, I_MFE, I_MAE = 16, 17, 18, 19
I_RET500, I_RET1S, I_RET10S = 21, 22, 26


class FakeDB:
    def __init__(self): self.rows = []; self.created = False
    async def execute(self, sql, params=()): self.created = True
    async def executemany(self, sql, rows): self.rows.extend(rows)


def _run(coro): return asyncio.new_event_loop().run_until_complete(coro)


def test_disabled_is_noop():
    r = SignalRecorder(FakeDB(), enabled=False)
    r.record("X", "long", 1000, 100.0, FEAT); r.on_tick("X", 1500, 101.0, 101.0, 101.0)
    assert r.recorded == 0 and not r._pending


def test_forward_mfe_mae_horizons():
    db = FakeDB(); r = SignalRecorder(db, enabled=True)
    r.record("X", "long", 1000, 100.0, FEAT)
    r.on_tick("X", 1500, 101.0, 101.0, 101.0)    # +100bps, age500
    r.on_tick("X", 2000, 99.0, 99.0, 99.0)       # -100bps, age1000
    r.on_tick("X", 11001, 100.5, 100.5, 100.5)   # +50bps, age10001 -> finalize
    _run(r.flush())
    assert len(db.rows) == 1
    row = db.rows[0]
    assert row[I_MFE] == 100.0
    assert row[I_MAE] == -100.0
    assert row[I_RET500] == 100.0
    assert row[I_RET1S] == -100.0
    assert row[I_RET10S] == 50.0


def test_short_direction_sign():
    db = FakeDB(); r = SignalRecorder(db, enabled=True)
    r.record("X", "short", 1000, 100.0, FEAT)
    r.on_tick("X", 1500, 99.0, 99.0, 99.0)       # price DOWN = favorable for SHORT = +100bps
    r.on_tick("X", 11001, 99.0, 99.0, 99.0)
    _run(r.flush())
    assert db.rows[0][I_MFE] == 100.0            # short wins on down-move


def test_fillable_when_touch_survives():
    # long: ask stays <= entry_exec the whole window -> our IOC would fill
    db = FakeDB(); r = SignalRecorder(db, enabled=True)
    r.record("X", "long", 1000, 100.0, FEAT)      # entry_exec=100.0
    r.on_tick("X", 1100, 99.9, 99.9, 99.8)        # ask 99.9 <= 100.0 -> no breach
    r.on_tick("X", 11001, 99.9, 99.9, 99.8)
    _run(r.flush())
    assert db.rows[0][I_TOUCH] == 10000.0          # never breached
    assert db.rows[0][I_FILL] == 1                 # fillable


def test_unfillable_when_touch_breaches_fast():
    # long: ask jumps above entry_exec at age 50ms (< 150ms) -> we MISS the fill
    db = FakeDB(); r = SignalRecorder(db, enabled=True)
    r.record("X", "long", 1000, 100.0, FEAT)
    r.on_tick("X", 1050, 100.5, 100.5, 100.4)     # ask 100.5 > 100.0 -> breach@50ms
    r.on_tick("X", 11001, 100.5, 100.5, 100.4)
    _run(r.flush())
    assert db.rows[0][I_TOUCH] == 50.0
    assert db.rows[0][I_FILL] == 0                 # not fillable (50ms < 150ms floor)


def test_dedup_window():
    r = SignalRecorder(FakeDB(), enabled=True, dedup_ms=800)
    r.record("X", "long", 1000, 100.0, FEAT)
    r.record("X", "long", 1500, 100.0, FEAT)       # within 800ms -> skipped
    r.record("X", "long", 2000, 100.0, FEAT)       # past 800ms -> recorded
    assert r.recorded == 2


def test_entry_mid_guard():
    r = SignalRecorder(FakeDB(), enabled=True)
    r.record("X", "long", 1000, 0.0, FEAT)         # bad entry_mid -> skipped
    assert r.recorded == 0


def test_not_finalized_before_horizon():
    db = FakeDB(); r = SignalRecorder(db, enabled=True)
    r.record("X", "long", 1000, 100.0, FEAT)
    r.on_tick("X", 5000, 101.0, 101.0, 100.9)      # age 4000 < 10000 -> still pending
    _run(r.flush())
    assert len(db.rows) == 0 and r.finalized == 0
