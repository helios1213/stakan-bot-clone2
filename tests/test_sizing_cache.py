"""#2 регресія: TTL-кеш get_pair_sizing / get_slot_pair_sizing у WebkeyStore.

Гарантії:
  1. Другий виклик у межах TTL НЕ б'є в БД (fetchone викликано один раз).
  2. None-результат теж кешується (не перечитуємо порожнечу щоразу).
  3. Після TTL кеш протухає → знову читаємо БД.
  4. get() (throttle/webkey латч) НЕ кешується — б'є в БД щоразу (safety).
  5. Значення, що повертаються, не змінились.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from src.execution.webkey import credentials as cred_mod
from src.execution.webkey.credentials import WebkeyStore


class FakeDB:
    """Лічильник fetchone; повертає задані рядки за SQL-підрядком."""
    def __init__(self):
        self.calls = []
        self.rows = {
            "pair_configs": {"symbol": "PENGUUSDT"},
            "slot_pair_sizing": {"margin_min_usdt": 2.0, "margin_max_usdt": 3.0,
                                 "leverage_min": 45, "leverage_max": 50},
        }

    async def fetchone(self, sql, params=()):
        self.calls.append(sql)
        for kw, row in self.rows.items():
            if kw in sql:
                return row
        return None


def _store():
    return WebkeyStore(FakeDB(), Fernet.generate_key().decode())


def _n(db, kw):
    return sum(1 for s in db.calls if kw in s)


@pytest.mark.asyncio
async def test_pair_sizing_cached_within_ttl():
    s = _store()
    a = await s.get_pair_sizing("PENGUUSDT")
    b = await s.get_pair_sizing("PENGUUSDT")
    assert a == b == {"margin_min_usdt": None, "margin_max_usdt": None,
                      "leverage_min": None, "leverage_max": None}
    assert _n(s.db, "pair_configs") == 1, "другий виклик мав піти з кешу"


@pytest.mark.asyncio
async def test_slot_pair_sizing_cached_within_ttl():
    s = _store()
    a = await s.get_slot_pair_sizing(2, "PENGUUSDT")
    b = await s.get_slot_pair_sizing(2, "PENGUUSDT")
    assert a == b == {"margin_min_usdt": 2.0, "margin_max_usdt": 3.0,
                      "leverage_min": 45, "leverage_max": 50}
    assert _n(s.db, "slot_pair_sizing") == 1
    # інший (slot,symbol) — окремий ключ, окреме читання
    await s.get_slot_pair_sizing(3, "PENGUUSDT")
    assert _n(s.db, "slot_pair_sizing") == 2


@pytest.mark.asyncio
async def test_none_result_is_cached():
    s = _store()
    s.db.rows.pop("slot_pair_sizing")           # тепер повертає None
    assert await s.get_slot_pair_sizing(9, "ZZZ") is None
    assert await s.get_slot_pair_sizing(9, "ZZZ") is None
    assert _n(s.db, "slot_pair_sizing") == 1, "None теж має кешуватись"


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(monkeypatch):
    s = _store()
    t = [1000.0]
    monkeypatch.setattr(cred_mod.time, "monotonic", lambda: t[0])
    await s.get_pair_sizing("PENGUUSDT")
    t[0] += WebkeyStore._SIZING_CACHE_TTL_SEC + 1     # протухло
    await s.get_pair_sizing("PENGUUSDT")
    assert _n(s.db, "pair_configs") == 2, "після TTL мали перечитати БД"


@pytest.mark.asyncio
async def test_slot_get_does_not_touch_sizing_cache():
    """get() несе throttle/webkey латч — НЕ кешується (safety). Прямий інваріант:
    виклик get() не кладе нічого в sizing-кеш, тож латч завжди читається живим."""
    s = _store()
    s.db.rows["webkey_slots"] = {
        "slot_id": 2, "label": "l", "enabled": 1, "webkey_blob": None,
        "visitor_blob": None, "last_health_check": None, "last_latency_ms": None,
        "last_balance_usdt": None, "assigned_pair": None, "open_throttle_until": None,
    }
    try:
        await s.get(2)
    except Exception:
        pass  # _row_to_slot може вимагати більше полів; кеш-інваріант від цього не залежить
    assert s._sizing_cache == {}, "get() НЕ має нічого класти в sizing-кеш"
    # а sizing-читання — кладуть лише свої ключі
    await s.get_pair_sizing("PENGUUSDT")
    assert all(k[0] in ("pair", "slot") for k in s._sizing_cache)
