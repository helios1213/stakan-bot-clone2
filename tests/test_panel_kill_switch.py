"""Кіл-світч у вебпанелі: показ стану + кнопка зняття.

ЗАДАЧА. Кіл вмикається сам (peak drawdown) і живе ВИКЛЮЧНО в памʼяті
`SafetyController`. Вебпанель — окремий процес, а для клона ще й інша машина,
тож побачити його вона не може в принципі, і зняти теж. Раніше єдиним способом
був `/unkill` у телеграмі.

РІШЕННЯ. Бот дзеркалить стан у `live_state` (`kill_state:slotN`) і звідти ж
забирає запит на зняття (`kill_release_req:slotN`). Панель ЛИШЕ СТАВИТЬ ЗАПИТ.

ЧОМУ НЕ НАПРЯМУ. Якби панель «зняла» кіл сама (почистила щось у БД), у памʼяті
бота халт лишився б, і кнопка працювала б суто візуально — рівно той клас бага,
що вже був із ручним зняттям, яке не переживало рестарт (`persist_kill_release`).
"""
from __future__ import annotations

import inspect
import sqlite3
import time
from pathlib import Path

import pytest

from src.execution.live_pool import LiveExecutorPool
from src.webpanel import data

POOL = Path("src/execution/live_pool.py").read_text()
APP = Path("src/webpanel/app.py").read_text()
HTML = Path("src/webpanel/templates/index.html").read_text()


# ---- канал БД ------------------------------------------------------------

def test_keys_are_slot_scoped():
    assert LiveExecutorPool._kill_state_key(2) == "kill_state:slot2"
    assert LiveExecutorPool._kill_request_key(2) == "kill_release_req:slot2"
    assert LiveExecutorPool._kill_state_key(1) != LiveExecutorPool._kill_state_key(2)


def test_release_runs_before_state_is_written():
    """У зворотному порядку панель показувала б «кіл активний» ще 30с після
    успішного зняття — оператор тиснув би кнопку ще раз."""
    src = inspect.getsource(LiveExecutorPool.sync_kill_state)
    assert src.index("_kill_request_key") < src.index("_kill_state_key")


def test_request_marker_is_always_consumed():
    """Інакше запит відпрацьовував би на КОЖНОМУ циклі rebuild нескінченно."""
    src = inspect.getsource(LiveExecutorPool.sync_kill_state)
    assert "DELETE FROM live_state WHERE key = ?" in src
    # видалення має бути ПОЗА гілкою is_killed()
    tail = src[src.index("if row and row[0]:"):]
    branch = tail[:tail.index("# 2.")]
    assert branch.count("DELETE FROM live_state") == 1
    assert "else:" in branch, "має бути гілка «кіла немає», і маркер усе одно знімається"


def test_release_goes_through_the_pool_not_the_controller():
    """Пул ще й ЗБЕРІГАЄ факт зняття; напряму через контролер рестарт відновив
    би дорелізний пік і ввімкнув кіл назад."""
    src = inspect.getsource(LiveExecutorPool.sync_kill_state)
    assert "await self.release_kill(sid)" in src
    assert "ctl.release_kill()" not in src


def test_state_is_cleared_when_the_kill_is_gone():
    src = inspect.getsource(LiveExecutorPool.sync_kill_state)
    assert "else:" in src and "DELETE FROM live_state WHERE key = ?" in src


def test_sync_never_raises_into_the_rebuild_loop():
    src = inspect.getsource(LiveExecutorPool.sync_kill_state)
    assert "except Exception:" in src and "logger.exception" in src


def test_rebuild_loop_drives_the_sync():
    """Без цього виклику маркери ніхто не читає і кнопка мертва."""
    assert "await self.sync_kill_state()" in POOL


# ---- панель читає стан ---------------------------------------------------

@pytest.fixture()
def live_db(tmp_path, monkeypatch):
    p = tmp_path / "live.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE live_state (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)")
    c.execute("CREATE TABLE live_trades (account_label TEXT, opened_at INT, real_entry_latency_ms INT)")
    c.commit(); c.close()
    monkeypatch.setattr(data, "LIVE_DB", str(p))
    return p


@pytest.fixture()
def main_db(tmp_path, monkeypatch):
    p = tmp_path / "main.db"
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE webkey_slots (slot_id INT, label TEXT, enabled INT,
                 live_enabled INT, assigned_pair TEXT, last_balance_usdt TEXT,
                 last_latency_ms INT, last_health_check INT, last_error TEXT,
                 webkey_blob BLOB, proxy_blob BLOB)""")
    c.execute("INSERT INTO webkey_slots VALUES (2,'s2',1,1,'X',NULL,NULL,NULL,"
              "'api_error_2005: Balance insufficient',NULL,NULL)")
    c.commit(); c.close()
    monkeypatch.setattr(data, "DB", str(p))
    return p


def _set_kill(live_db, until, reason):
    c = sqlite3.connect(live_db)
    c.execute("INSERT OR REPLACE INTO live_state VALUES (?,?,?)",
              ("kill_state:slot2", f"{until}|{reason}", int(time.time())))
    c.commit(); c.close()


def test_no_kill_leaves_the_api_error_alone(main_db, live_db):
    row = data.accounts()[0]
    assert row["kill_active"] is False
    assert row["last_error"] == "api_error_2005: Balance insufficient"


def test_kill_takes_over_the_error_field(main_db, live_db):
    """Оператор дивиться в поле помилки першим — халт важливіший за будь-яку
    транзиторну помилку health-check."""
    _set_kill(live_db, int(time.time()) + 3600, "peak drawdown -12%")
    row = data.accounts()[0]
    assert row["kill_active"] is True
    assert row["last_error"].startswith("💀 KILL SWITCH: peak drawdown -12%")
    assert "хв" in row["last_error"], "має бути видно, скільки ще триває халт"


def test_the_api_error_is_not_lost(main_db, live_db):
    """Перекриття не має ховати справжню причину — вона поруч."""
    _set_kill(live_db, 0, "peak drawdown")
    row = data.accounts()[0]
    assert row["api_last_error"] == "api_error_2005: Balance insufficient"


def test_indefinite_kill_has_no_countdown(main_db, live_db):
    _set_kill(live_db, 0, "peak drawdown")
    assert data.accounts()[0]["last_error"] == "💀 KILL SWITCH: peak drawdown"


def test_a_broken_marker_does_not_break_the_panel(main_db, live_db):
    c = sqlite3.connect(live_db)
    c.execute("INSERT OR REPLACE INTO live_state VALUES ('kill_state:slotXX','сміття',0)")
    c.execute("INSERT OR REPLACE INTO live_state VALUES ('kill_state:slot2','не-число|чому',0)")
    c.commit(); c.close()
    row = data.accounts()[0]
    assert row["kill_active"] is True and row["kill_until_ts"] == 0


def test_missing_live_db_is_survivable(main_db, monkeypatch):
    """Панель стартує і без live-бази — це має давати «кіла немає», не 500."""
    monkeypatch.setattr(data, "LIVE_DB", "/nonexistent/nope.db")
    assert data.accounts()[0]["kill_active"] is False


# ---- панель ставить запит ------------------------------------------------

def test_request_writes_the_marker(live_db):
    assert data.request_kill_release(2)["ok"] is True
    c = sqlite3.connect(live_db)
    v = c.execute("SELECT value FROM live_state WHERE key='kill_release_req:slot2'").fetchone()
    c.close()
    assert v is not None


def test_request_does_not_touch_the_state_marker(live_db):
    """Панель НЕ знімає кіл сама — інакше кнопка «працювала» б лише візуально,
    поки в памʼяті бота халт стоїть."""
    _set_kill(live_db, 0, "peak drawdown")
    data.request_kill_release(2)
    c = sqlite3.connect(live_db)
    still = c.execute("SELECT value FROM live_state WHERE key='kill_state:slot2'").fetchone()
    c.close()
    assert still is not None, "стан має зняти БОТ, а не панель"


def test_routing_sends_remote_slots_over_rpc(monkeypatch):
    seen = {}
    monkeypatch.setattr(data, "_remote_rpc",
                        lambda srv, payload, **kw: seen.update(srv=srv, **payload) or {"ok": True})
    data.request_kill_release_routed("srv1", 3)
    assert seen["srv"] == "srv1" and seen["op"] == "unkill" and seen["slot_id"] == 3


# ---- слот БЕЗ контролера (live вимкнено) ---------------------------------
# Спіймано наскрізним тестом уже ПІСЛЯ деплою: контролер існує лише поки слот
# live-активний. При live_enabled=0 `_safety_controllers` порожній — і перша
# версія sync_kill_state просто не мала що обходити. Наслідки були два, обидва
# мовчазні: запит із панелі не споживався НІКОЛИ (кнопка «не працює»), а
# протухлий kill_state від минулої сесії висів вічно і показував ФАНТОМНИЙ халт.

def test_sync_covers_slots_that_have_only_a_marker():
    src = inspect.getsource(LiveExecutorPool.sync_kill_state)
    assert "sids = set(self._safety_controllers)" in src
    assert "kill_state:slot%" in src and "kill_release_req:slot%" in src, (
        "слоти без контролера мають потрапляти у вибірку через свої ж маркери")


def test_missing_controller_is_treated_as_no_kill():
    """Немає контролера — немає й халту. Інакше панель показує фантом."""
    src = inspect.getsource(LiveExecutorPool.sync_kill_state)
    assert "if ctl is not None and ctl.is_killed():" in src
    assert src.count("ctl is not None and ctl.is_killed()") == 2, (
        "обидві гілки (запит і дзеркало) мають переживати відсутність контролера")


def test_a_request_on_a_non_live_slot_is_still_consumed():
    """Інакше маркер висить вічно і бот довбить його на кожному циклі."""
    src = inspect.getsource(LiveExecutorPool.sync_kill_state)
    assert "elif ctl is None:" in src
    assert "не live-активний" in src


def test_marker_enumeration_survives_garbage_keys():
    src = inspect.getsource(LiveExecutorPool.sync_kill_state)
    assert "except (IndexError, ValueError):" in src
