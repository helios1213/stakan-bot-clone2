"""Recovery monitor: confirming 2nd PnL read guards against a bad spike.

Bug 2026-06-18: a single account-PnL page read returned +$92.82 while the
account was really ~-$199, false-firing a "recovered" live→shadow stop. The
fix: _pnl_confirms() re-reads and only confirms a trigger if the 2nd read
agrees with the first within _RECOVERY_CONFIRM_TOL ($25). These pin that a
disagreeing/missing 2nd read yields None (caller takes NO action).
"""
import asyncio

import pytest

import src.main as m


class _FakeClient:
    """get_account_pnl_usdt returns the next value in `seq` per call."""
    def __init__(self, *seq):
        self.seq = list(seq)
        self.calls = 0

    async def get_account_pnl_usdt(self, window_days=359):
        v = self.seq[self.calls]
        self.calls += 1
        return v


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(*a, **k):
        return None
    monkeypatch.setattr(m.asyncio, "sleep", _instant)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_bug_case_disagreeing_read_rejected():
    # first=+92.82 (the spurious spike), real 2nd read ~-199 → disagree → None
    assert _run(m._pnl_confirms(_FakeClient(-199.17), 92.82)) is None


def test_agreeing_read_confirms():
    # two reads of the true value agree → returns the confirming value
    assert _run(m._pnl_confirms(_FakeClient(-199.5), -199.0)) == -199.5


def test_second_read_none_rejected():
    # transient None on the confirm read → no action
    assert _run(m._pnl_confirms(_FakeClient(None), -1.2)) is None


def test_boundary_at_tolerance_confirms():
    # exactly _RECOVERY_CONFIRM_TOL apart is NOT > tol → confirmed
    assert _run(m._pnl_confirms(_FakeClient(-100.0 - m._RECOVERY_CONFIRM_TOL), -100.0)) == -125.0


def test_just_over_tolerance_rejected():
    # one cent past tolerance → rejected
    assert _run(m._pnl_confirms(_FakeClient(-100.0 - m._RECOVERY_CONFIRM_TOL - 0.01), -100.0)) is None


def test_recovered_trigger_confirmed_path():
    # a GENUINE recovery: both reads near breakeven → confirmed, caller will stop
    v = _run(m._pnl_confirms(_FakeClient(-0.8), -1.0))
    assert v == -0.8 and v >= -1.5  # passes the caller's `pnl >= -buf` check
