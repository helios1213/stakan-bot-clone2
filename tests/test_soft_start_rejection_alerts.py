# -*- coding: utf-8 -*-
"""Soft-start повідомляє в Telegram про відмови біржі (запит оператора 2026-09-15).

До цього відмови soft-start лишались у лозі сервера: «перевірка особи» / risk control / KYC оператор
бачив лише за тим, що прогрів тихо перестав купувати. Пиниться:
  * блок акаунта (код або фраза) — одразу, тим самим класифікатором, що в арбітражному виконавці;
  * звичайна разова відмова — НЕ пуш; та сама відмова 3 рази за 30 хв — пуш;
  * ПРОВОДКА: спот (купівля/продаж/розпродаж) і фʼючерси (відкриття/закриття/позиції) справді пишуть
    відмову, а `tick()` раннера справді її відправляє — навіть якщо сам тік упав.
"""
import random
import time

import pytest

from src.execution import soft_start_errors as SE
from src.execution.soft_start_runner import SlotWarmer

RK = "Position opening is unavailable until risk control verification is completed"


class _Alerts:
    def __init__(self):
        self.sent = []

    async def send(self, text, category="general", throttle_sec=0, suppress_during_quiet=True):
        self.sent.append((text, category, throttle_sec, suppress_during_quiet))
        return True


def _rej(venue="спот", op="купівля", sym="PENGUUSDT", code=None, msg="", ts=None):
    return {"venue": venue, "op": op, "symbol": sym, "code": code, "msg": msg, "ts": ts or time.time()}


@pytest.mark.asyncio
async def test_account_block_code_alerts_immediately_even_in_quiet_hours():
    al = _Alerts()
    await SE.RejectionAlerter().process([_rej(venue="фʼючерси", op="відкриття", code="6026", msg=RK)], al, 2)
    assert len(al.sent) == 1, al.sent
    text, cat, thr, quiet = al.sent[0]
    assert "слот 2" in text and "risk control" in text and "code=6026" in text, text
    assert quiet is False and thr == SE.BLOCK_THROTTLE_SEC and cat.startswith("softstart_block_s2_")


@pytest.mark.asyncio
async def test_face_verification_phrase_without_a_code_is_a_block():
    al = _Alerts()
    await SE.RejectionAlerter().process([_rej(code="30001", msg="Please complete identity verification")], al, 1)
    assert al.sent and "перевірка особи" in al.sent[0][0], al.sent


@pytest.mark.asyncio
async def test_single_ordinary_rejection_is_not_pushed_but_three_in_a_row_are():
    t = [1000.0]
    a = SE.RejectionAlerter(clock=lambda: t[0])
    al = _Alerts()
    r = dict(code="30004", msg="order amount below minimum")
    await a.process([_rej(ts=1000.0, **r)], al, 1)
    await a.process([_rej(ts=1001.0, **r)], al, 1)
    assert al.sent == [], "разова/подвійна звичайна відмова не має будити оператора"
    t[0] = 1002.0
    await a.process([_rej(ts=1002.0, **r)], al, 1)
    assert len(al.sent) == 1 and "3 рази" in al.sent[0][0] and "code=30004" in al.sent[0][0], al.sent


@pytest.mark.asyncio
async def test_old_rejections_outside_the_window_do_not_count():
    t = [1.0]
    a = SE.RejectionAlerter(clock=lambda: t[0])
    al = _Alerts()
    r = dict(code="30004", msg="x")
    for ts in (1.0, 10.0):
        t[0] = ts
        await a.process([_rej(ts=ts, **r)], al, 1)
    t[0] = SE.REPEAT_WINDOW_SEC + 100
    await a.process([_rej(ts=t[0], **r)], al, 1)
    assert al.sent == [], al.sent


def test_rejection_log_drains_once():
    log = SE.RejectionLog()
    log.note_resp("фʼючерси", "відкриття", "HYPEUSDT", {"code": 6026, "msg": RK})
    items = log.drain()
    assert items[0]["code"] == "6026" and items[0]["msg"] == RK and log.drain() == []


# ------------------------------------------------------------ проводка рушіїв

@pytest.mark.asyncio
async def test_spot_rejected_buy_is_recorded(tmp_path):
    from tests.test_spot_soft_start import FakeClient, engine
    e = engine(tmp_path, FakeClient(ok=False))
    assert await e.maybe_buy() is False
    items = e.rejections.drain()
    assert items and items[0]["venue"] == "спот" and items[0]["op"] == "купівля" and items[0]["code"] == "400", items


@pytest.mark.asyncio
async def test_futures_rejected_open_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFT_START_LIVE", "1")
    from tests.test_futures_soft_start import FakeClient, mk
    ss, _, _ = mk(tmp_path, dry_run=False, client=FakeClient(open_code="6026"))
    assert await ss.open_position() is False
    items = ss.rejections.drain()
    assert items and items[0]["op"] == "відкриття" and items[0]["code"] == "6026", items


# ------------------------------------------------------------ проводка раннера

class _Eng:
    def __init__(self, items=()):
        self.rejections = SE.RejectionLog()
        for it in items:
            self.rejections.note(**it)

    async def tick(self):
        pass


def _warmer(spot, futures, alerts, boom=False):
    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w.alerts = alerts
    w._rej_alerter = SE.RejectionAlerter()
    w.spot, w.futures = spot, futures

    async def _tick():
        if boom:
            raise RuntimeError("тік упав")
    w._tick = _tick
    return w


@pytest.mark.asyncio
async def test_runner_tick_sends_rejections_from_both_engines_even_if_the_tick_failed():
    al = _Alerts()
    spot = _Eng([dict(venue="спот", op="купівля", symbol="X", code="6001", msg="")])
    fut = _Eng([dict(venue="фʼючерси", op="відкриття", symbol="Y", code=None, msg=RK)])
    w = _warmer(spot, fut, al, boom=True)
    with pytest.raises(RuntimeError):
        await w.tick()
    assert len(al.sent) == 2 and {"слот 1" in t for t, *_ in al.sent} == {True}, al.sent


@pytest.mark.asyncio
async def test_warmer_built_without_telegram_does_not_crash():
    w = SlotWarmer.__new__(SlotWarmer)
    w.spot = w.futures = None
    await w.tick()                               # класові дефолти: alerts=None, _rej_alerter=None
