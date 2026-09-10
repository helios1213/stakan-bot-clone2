# -*- coding: utf-8 -*-
"""Вебпанель теж чистить слот при видаленні/додаванні ключа (запит оператора).

ЧОМУ ЗАПИТОМ, А НЕ НАПРЯМУ. `_halted`, `slot_level_error` і блок акаунта
живуть у ПАМʼЯТІ `LiveExecutor`. Панель — окремий процес, а для клона ще й
інша машина; вона до тієї памʼяті не дістає в принципі. Почистити щось у БД
мало б вигляд успіху, а слот однаково не торгував би — виміряно на клоні
09.09: після нового ключа 66 ордерів поспіль відбились `fee_guard_halted`.

Канал той самий, що вже носить зняття кіла: маркер у `live_state`, який
виконує бот у циклі rebuild (~30с) і сам же прибирає.

ЩО ПІНИТЬСЯ:
  * свіжий маркер чистить слот і зникає;
  * ПРОТЕРМІНОВАНИЙ не чистить (він так само знімає запобіжник, як і в кіла);
  * маркер зникає В БУДЬ-ЯКОМУ разі, інакше запит крутився б щоцикла;
  * панель ставить запит ЛИШЕ на успіху операції;
  * віддалений бот має відповідний RPC-оп, інакше клон мовчки лишався б брудним.
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
_RPC_PATH = Path("scripts/stakan-account-rpc.py")
# Скрипт тримається в репо ОСНОВИ; на клоні його немає — тест нижче скіпається
# ВИДИМО, а не мовчки зеленіє.
RPC = _RPC_PATH.read_text() if _RPC_PATH.exists() else None


class _FakeDB:
    """Мінімальний live_db: рівно ті методи, що кличе споживач.

    `fetchone` зʼявився 10.09 разом із парним маркером `campaign_wipe_req`
    (видалення ключа з панелі забуває кампанію прогріву). Без нього фейк
    відставав би від реального інтерфейсу — той самий дрейф, що вже ламав
    фейки warmer'а і сторожа.
    """

    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted = []

    async def fetchall(self, sql, params=()):
        return list(self.rows)

    async def fetchone(self, sql, params=()):
        for k, v in self.rows:
            if k == params[0]:
                return (v,)
        return None

    async def execute(self, sql, params=()):
        if "DELETE" in sql.upper():
            self.deleted.append(params[0])
        return None


def _pool(rows):
    p = LiveExecutorPool.__new__(LiveExecutorPool)
    p._executors = {}
    p._safety_controllers = {}
    p.webkey_store = None
    p.live_db = _FakeDB(rows)
    p.cleared = []

    # Дзеркалить РЕАЛЬНИЙ інтерфейс: `wipe_campaign` додано 10.09 (видалення
    # ключа з панелі забуває кампанію прогріву, вставка — ні). Фейк без нього
    # мовчки ламав би проводку, а не падав.
    async def _clear(slot_id, *, reason, wipe_campaign=False):
        p.cleared.append((slot_id, reason, wipe_campaign))
        return []

    p.clear_slot_restrictions = _clear
    return p


# ------------------------------------------------------------- бік бота

@pytest.mark.asyncio
async def test_a_fresh_request_clears_the_slot():
    p = _pool([("clear_restrictions_req:slot2", str(int(time.time())))])

    await p.sync_clear_requests()

    assert [s for s, *_ in p.cleared] == [2]
    assert [w for *_, w in p.cleared] == [False], (
        "без парного маркера кампанія не має забуватись")
    assert p.live_db.deleted == ["clear_restrictions_req:slot2",
                                 "campaign_wipe_req:slot2"], "маркер не спожито"


@pytest.mark.asyncio
async def test_a_STALE_request_does_not_clear():
    """Маркер віком у тижні зняв би щойно поставлений халт — тобто повернув би
    слот до живих грошей без жодної дії оператора. `live_state` не входить у
    RET_LIVE, тож саме воно не зникає."""
    old = str(int(time.time()) - 999_999)
    p = _pool([("clear_restrictions_req:slot1", old)])

    await p.sync_clear_requests()

    assert p.cleared == [], "протермінований запит почистив слот"
    # Парний маркер кампанії прибирається разом з основним: лишившись, він
    # причепився б до НАСТУПНОГО, вже свіжого запиту і забув би кампанію,
    # якої ніхто не просив забувати.
    assert p.live_db.deleted == ["clear_restrictions_req:slot1",
                                 "campaign_wipe_req:slot1"], "і їх треба прибрати"


@pytest.mark.asyncio
async def test_an_unreadable_value_counts_as_stale():
    """Невідомий вік не знімає запобіжник — та сама рамка, що в кіла."""
    p = _pool([("clear_restrictions_req:slot1", "не-число")])
    await p.sync_clear_requests()
    assert p.cleared == []


@pytest.mark.asyncio
async def test_a_broken_key_is_dropped_not_crashed():
    p = _pool([("clear_restrictions_req:slotX", str(int(time.time())))])
    await p.sync_clear_requests()
    assert p.cleared == []
    assert p.live_db.deleted == ["clear_restrictions_req:slotX"]


@pytest.mark.asyncio
async def test_one_bad_slot_does_not_skip_the_rest():
    """Ізоляція: інакше один зламаний запит глушив би чистку решти слотів."""
    now = str(int(time.time()))
    p = _pool([("clear_restrictions_req:slot1", now),
               ("clear_restrictions_req:slot2", now)])
    calls = []

    async def _boom(slot_id, *, reason, wipe_campaign=False):
        calls.append(slot_id)
        if slot_id == 1:
            raise RuntimeError("бум")
        return []

    p.clear_slot_restrictions = _boom
    await p.sync_clear_requests()

    assert calls == [1, 2]


def test_the_rebuild_loop_drives_it():
    """Без цього виклику маркери ніхто не читає і панель мертва."""
    assert "await self.sync_clear_requests()" in POOL


def test_the_key_is_slot_scoped():
    assert LiveExecutorPool._clear_request_key(2) == "clear_restrictions_req:slot2"
    assert (LiveExecutorPool._clear_request_key(1)
            != LiveExecutorPool._clear_request_key(2))


# ------------------------------------------------------------ бік панелі

@pytest.fixture()
def live_db(tmp_path, monkeypatch):
    p = tmp_path / "live.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE live_state (key TEXT PRIMARY KEY, value TEXT, "
              "updated_at INTEGER)")
    c.commit(); c.close()
    monkeypatch.setattr(data, "LIVE_DB", str(p))
    return p


def test_the_panel_writes_the_marker(live_db):
    res = data.request_clear_restrictions(2)
    assert res["ok"] is True
    c = sqlite3.connect(live_db)
    row = c.execute("SELECT key, value FROM live_state").fetchone()
    c.close()
    assert row[0] == "clear_restrictions_req:slot2"
    assert abs(int(row[1]) - int(time.time())) < 5, "без свіжого таймстемпа запит протухлий"


def test_removing_an_account_queues_the_clear(monkeypatch):
    queued = []
    monkeypatch.setattr(data, "remove_account", lambda sid: True)
    monkeypatch.setattr(data, "request_clear_restrictions_routed",
                        lambda srv, sid, wipe=False:
                            queued.append((srv, sid, wipe)) or {"ok": True})

    res = data.remove_account_routed("primary", 2)

    assert res["ok"] is True
    assert queued == [("primary", 2, True)], (
        "видалення має і чистити слот, і забувати кампанію прогріву")


def test_a_FAILED_removal_queues_nothing(monkeypatch):
    """«Слот уже порожній» — не привід знімати халт зі слота, якого не чіпали."""
    queued = []
    monkeypatch.setattr(data, "remove_account", lambda sid: False)
    monkeypatch.setattr(data, "request_clear_restrictions_routed",
                        lambda srv, sid, wipe=False:
                            queued.append((srv, sid, wipe)) or {"ok": True})

    res = data.remove_account_routed("primary", 2)

    assert res["ok"] is False
    assert queued == []


def test_adding_an_account_queues_the_clear_for_the_REAL_slot(monkeypatch):
    """Слот міг бути вибраний автоматично — чистити треба той, куди лягло."""
    queued = []
    monkeypatch.setattr(data, "add_account",
                        lambda webkey, label, slot_id: {"slot_id": 2})
    monkeypatch.setattr(data, "request_clear_restrictions_routed",
                        lambda srv, sid, wipe=False:
                            queued.append((srv, sid, wipe)) or {"ok": True})

    res = data.add_account_routed("primary", webkey="WEB" + "a" * 64)

    assert res["ok"] is True
    assert queued == [("primary", 2, False)], (
        "вставка ключа не має забувати кампанію — переклеювання її продовжує")


def test_a_queue_failure_does_not_break_the_removal(monkeypatch):
    """Чистка — зручність; вона не має перетворювати успішне видалення на 400."""
    monkeypatch.setattr(data, "remove_account", lambda sid: True)

    def _boom(srv, sid, wipe=False):
        raise RuntimeError("ssh впав")

    monkeypatch.setattr(data, "request_clear_restrictions_routed", _boom)

    res = data.remove_account_routed("primary", 2)

    assert res["ok"] is True
    assert res["restrictions_queued"] is False


def test_the_panel_asks_a_remote_bot_over_rpc():
    """Для клона запит їде опом RPC — інакше він писав би маркер у БД ОСНОВИ,
    тобто чистив би не той слот і не на тій машині."""
    src = inspect.getsource(data.request_clear_restrictions_routed)
    assert '"op": "clear_restrictions"' in src
    assert 'server == "primary"' in src


@pytest.mark.skipif(RPC is None, reason="scripts/stakan-account-rpc.py — лише в репо основи")
def test_the_rpc_script_handles_the_op():
    """Без опа клон мовчки лишався б брудним: панель відправила б запит, а
    скрипт відповів би «unknown op».

    Скрипт живе в репо ОСНОВИ (звідти його копіюють на кожного віддаленого
    бота в /usr/local/bin), тож на клоні цей тест видимо скіпається — але
    сам файл на коробці клона мусить бути оновлений разом із цією зміною.
    """
    assert '"clear_restrictions"' in RPC
    assert "data.request_clear_restrictions(" in RPC
