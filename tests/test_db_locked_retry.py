"""«database is locked» під час прибирання бази — повтор запису (2026-09-15).

Виміряно на клоні 1: відмова приходить МИТТЄВО (не після busy_timeout), рівно під час db_prune_loop.
Механізм: на спільному зʼєднанні відкрите читання зі старим знімком, а інше зʼєднання вже закомітило —
SQLite віддає SQLITE_BUSY без очікування. Тести відтворюють саме цей стан на справжньому SQLite
і перевіряють, що запис переживає його, а контроль доводить, що без повтору запис справді падає.
"""
import asyncio
import sqlite3

import pytest

from src.storage import db as dbmod
from src.storage.db import Database, init_db, retry_if_locked
from src.storage.db_live import LiveDatabase
from src.strategy.signal import Signal, SignalWriter


@pytest.fixture(autouse=True)
def _fast_delays(monkeypatch):
    monkeypatch.setattr(dbmod, "_LOCK_RETRY_DELAYS", (0.03, 0.06, 0.12))


class _NoTxn:
    in_transaction = False


def _locked():
    return sqlite3.OperationalError("database is locked")


@pytest.mark.asyncio
async def test_retry_recovers_from_transient_lock():
    calls = []

    async def op():
        calls.append(1)
        if len(calls) < 3:
            raise _locked()
        return "ok"

    assert await retry_if_locked(_NoTxn(), op) == "ok" and len(calls) == 3


@pytest.mark.asyncio
async def test_other_errors_are_not_retried():
    calls = []

    async def op():
        calls.append(1)
        raise sqlite3.OperationalError("no such table: x")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        await retry_if_locked(_NoTxn(), op)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_persistent_lock_still_raises_after_bounded_retries():
    calls = []

    async def op():
        calls.append(1)
        raise _locked()

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await retry_if_locked(_NoTxn(), op)
    assert len(calls) == 1 + len(dbmod._LOCK_RETRY_DELAYS)


async def _stale_reader_scenario(conn, path, table):
    """Відкрити читання зі старим знімком на `conn`, закомітити з ІНШОГО зʼєднання, закрити читання за 50 мс."""
    held = await conn.execute(f"SELECT rowid FROM {table}")
    await held.fetchone()
    other = sqlite3.connect(path)
    other.execute(f"DELETE FROM {table} WHERE rowid = (SELECT MIN(rowid) FROM {table})")
    other.commit()
    other.close()

    async def release():
        await asyncio.sleep(0.05)
        await held.close()
    return asyncio.create_task(release())


async def _db_with_rows(tmp_path):
    path = str(tmp_path / "stakan.db")
    await init_db(path)
    d = Database(path)
    await d.connect()
    await d.conn.execute("CREATE TABLE t (v INTEGER)")
    await d.conn.executemany("INSERT INTO t (v) VALUES (?)", [(i,) for i in range(20)])
    await d.conn.commit()
    return d, path


@pytest.mark.asyncio
async def test_control_the_scenario_really_raises_without_retry(tmp_path):
    """Без цього контролю інші тести були б вакуумні: доводить, що стан справді дає миттєве «locked»."""
    d, path = await _db_with_rows(tmp_path)
    try:
        rel = await _stale_reader_scenario(d.conn, path, "t")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await d.conn.execute("INSERT INTO t (v) VALUES (999)")
        await d.conn.rollback()
        await rel
    finally:
        await d.close()


@pytest.mark.asyncio
async def test_database_execute_survives_the_prune_race(tmp_path):
    d, path = await _db_with_rows(tmp_path)
    try:
        rel = await _stale_reader_scenario(d.conn, path, "t")
        await d.execute("INSERT INTO t (v) VALUES (999)")
        await rel
        assert (await d.fetchone("SELECT COUNT(*) FROM t WHERE v = 999"))[0] == 1
    finally:
        await d.close()


@pytest.mark.asyncio
async def test_signal_batch_is_not_lost_in_the_prune_race(tmp_path):
    d, path = await _db_with_rows(tmp_path)
    try:
        w = SignalWriter(d)
        sig = Signal(symbol="SOXLUSDT", direction="long", source="static_gap", binance_price=1.0,
                     mexc_price=1.0, binance_impulse_pct=0.0, mexc_lag_pct=0.1, confidence=1.0)
        rel = await _stale_reader_scenario(d.conn, path, "t")
        await w._write_batch([sig])
        await rel
        assert w.total_written == 1
        assert (await d.fetchone("SELECT COUNT(*) FROM signals WHERE symbol = 'SOXLUSDT'"))[0] == 1
    finally:
        await d.close()


@pytest.mark.asyncio
async def test_live_database_execute_survives_the_prune_race(tmp_path):
    path = str(tmp_path / "stakan-live.db")
    ld = LiveDatabase(path)
    await ld.connect()
    try:
        await ld.conn.execute("CREATE TABLE t (v INTEGER)")
        await ld.conn.executemany("INSERT INTO t (v) VALUES (?)", [(i,) for i in range(20)])
        await ld.conn.commit()
        rel = await _stale_reader_scenario(ld.conn, path, "t")
        await ld.execute("INSERT INTO t (v) VALUES (999)")
        await rel
        assert (await ld.fetchone("SELECT COUNT(*) FROM t WHERE v = 999"))[0] == 1
    finally:
        await ld.close()
