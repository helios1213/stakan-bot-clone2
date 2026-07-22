"""Recovery is ACCOUNT-level: one account on two slots must stop BOTH together
(git — account-wide _recovery_stop). Regression guard for the 2026-07-21 fix."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
from src.main import _recovery_stop


@pytest.mark.asyncio
async def test_recovery_stop_is_account_wide():
    SAME = "webkey_ABC"
    s1 = SimpleNamespace(slot_id=1, webkey=SAME, assigned_pair="1000PEPEUSDT", live_enabled=1)
    s2 = SimpleNamespace(slot_id=2, webkey=SAME, assigned_pair="TAOUSDT", live_enabled=1)
    s3 = SimpleNamespace(slot_id=3, webkey="OTHER", assigned_pair="ZECUSDT", live_enabled=1)
    store = MagicMock()
    store.list_all = AsyncMock(return_value=[s1, s2, s3])
    disabled = []
    store.set_live_enabled = AsyncMock(side_effect=lambda sid, en: disabled.append(sid))
    sm = MagicMock(); demoted = []
    sm.manual_promote = AsyncMock(side_effect=lambda pair, target, reason: demoted.append(pair))
    db = MagicMock(); db.execute = AsyncMock()

    stopped = await _recovery_stop(store, sm, db, 1, "1000PEPEUSDT")

    assert sorted(stopped) == [1, 2], "both slots of the same account must stop"
    assert sorted(disabled) == [1, 2], "set_live_enabled(False) for both siblings"
    assert 3 not in disabled, "a DIFFERENT account must NOT be touched"
    assert sorted(demoted) == ["1000PEPEUSDT", "TAOUSDT"], "both pairs demoted to shadow"


@pytest.mark.asyncio
async def test_recovery_stop_single_account_unaffected():
    """A lone slot (no sibling on its account) stops only itself."""
    s1 = SimpleNamespace(slot_id=1, webkey="A", assigned_pair="PENGUUSDT", live_enabled=1)
    s2 = SimpleNamespace(slot_id=2, webkey="B", assigned_pair="TAOUSDT", live_enabled=1)
    store = MagicMock()
    store.list_all = AsyncMock(return_value=[s1, s2])
    disabled = []
    store.set_live_enabled = AsyncMock(side_effect=lambda sid, en: disabled.append(sid))
    sm = MagicMock(); sm.manual_promote = AsyncMock()
    db = MagicMock(); db.execute = AsyncMock()

    stopped = await _recovery_stop(store, sm, db, 1, "PENGUUSDT")
    assert stopped == [1] and disabled == [1], "only the lone slot stops"
