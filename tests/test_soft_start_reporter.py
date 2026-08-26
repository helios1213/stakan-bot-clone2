"""Tests for the live Telegram status message and the closing report.

What these pin:
  1. ONE live message: each action deletes the previous and posts a fresh one
     (delete+repost, not edit — an edit gives no notification, which defeats
     the point of "let me see it is working").
  2. Newest action first, with history under it.
  3. Telegram failures NEVER propagate — warming must not stop because a
     message could not be delivered.
  4. The closing report STAYS (its id is not tracked, so nothing deletes it)
     and says plainly whether everything is clear.
  5. "All clear" is false when anything errored or a position was left open.
"""
from __future__ import annotations

import pytest

from src.execution.soft_start_reporter import SoftStartReporter


class FakeBot:
    def __init__(self, fail_send=False, fail_delete=False):
        self.sent = []
        self.deleted = []
        self.fail_send = fail_send
        self.fail_delete = fail_delete
        self._next_id = 100

    async def send_message(self, chat_id, text, **kw):
        if self.fail_send:
            raise RuntimeError("telegram down")
        self.sent.append(text)
        self._next_id += 1
        return type("M", (), {"message_id": self._next_id})()

    async def delete_message(self, chat_id, message_id):
        if self.fail_delete:
            raise RuntimeError("too old to delete")
        self.deleted.append(message_id)


class FakeAlerts:
    def __init__(self, bot):
        self.bot = bot
        self.owner_id = 42


def rep(bot=None, dry_run=False) -> SoftStartReporter:
    return SoftStartReporter(FakeAlerts(bot or FakeBot()), 1, dry_run)


@pytest.mark.asyncio
async def test_first_action_posts_a_message():
    b = FakeBot()
    r = rep(b)
    await r.spot_buy("MX", 2.5, "1.52")
    assert len(b.sent) == 1
    assert "SPOT BUY MX" in b.sent[0]
    assert b.deleted == [], "nothing to delete on the first post"


@pytest.mark.asyncio
async def test_next_action_deletes_the_previous_and_reposts():
    b = FakeBot()
    r = rep(b)
    await r.spot_buy("MX", 2.5, "1.52")
    first_id = r._message_id
    await r.spot_sell("MX", "1.52")
    assert b.deleted == [first_id], "the old message must be removed"
    assert len(b.sent) == 2
    assert r._message_id != first_id


@pytest.mark.asyncio
async def test_history_shows_newest_first():
    b = FakeBot()
    r = rep(b)
    await r.spot_buy("MX", 2.5, "1.52")
    await r.futures_open("HYPE_USDT", 1, 10, 45)
    body = b.sent[-1]
    assert body.index("FUTURES OPEN") < body.index("SPOT BUY")


@pytest.mark.asyncio
async def test_status_line_carries_day_budget_and_position():
    b = FakeBot()
    r = rep(b)
    await r.spot_buy("MX", 2.5, "1.52", day=2, days=3, spent=0.31,
                     ceiling=5.0, position="HYPE_USDT")
    t = b.sent[-1]
    # Формат змінено 2026-08-26 разом зі зняттям стелі витрат: знаменник
    # лишається лише коли стеля справді задана.
    assert "day 2/3" in t and "витрачено 0.310 USDT" in t and "HYPE_USDT" in t


@pytest.mark.asyncio
async def test_dry_run_is_stated_in_the_message():
    b = FakeBot()
    r = rep(b, dry_run=True)
    await r.spot_buy("MX", 2.5, "1.52")
    assert "DRY-RUN" in b.sent[-1]


@pytest.mark.asyncio
async def test_send_failure_does_not_raise():
    """Reporting is decoration — it must never stop the warm-up."""
    r = rep(FakeBot(fail_send=True))
    await r.spot_buy("MX", 2.5, "1.52")        # must not raise


@pytest.mark.asyncio
async def test_delete_failure_does_not_raise():
    b = FakeBot()
    r = rep(b)
    await r.spot_buy("MX", 2.5, "1.52")
    b.fail_delete = True
    await r.spot_sell("MX", "1.52")            # must not raise
    assert len(b.sent) == 2


@pytest.mark.asyncio
async def test_no_alerts_means_silent_not_broken():
    r = SoftStartReporter(None, 1, False)
    await r.spot_buy("MX", 2.5, "1.52")        # must not raise
    assert len(r.history) == 1


# ---- closing report ------------------------------------------------------

@pytest.mark.asyncio
async def test_final_report_counts_everything():
    b = FakeBot()
    r = rep(b)
    await r.spot_buy("MX", 2.5, "1.52")
    await r.spot_sell("MX", "1.52")
    await r.futures_open("HYPE_USDT", 1, 10, 45)
    await r.futures_close("HYPE_USDT", 45)
    await r.skipped("no 0%-fee pair")
    await r.final_report("campaign finished", spent=0.31, ceiling=5.0)

    t = b.sent[-1]
    assert "Soft-start finished" in t
    assert "1 buys, 1 sells" in t
    assert "1 opened, 1 closed" in t
    assert "skipped : 1" in t
    assert "0.3100 / 5.00 USDT" in t


@pytest.mark.asyncio
async def test_final_report_says_all_clear_when_clean():
    b = FakeBot()
    r = rep(b)
    await r.spot_buy("MX", 2.5, "1.52")
    await r.final_report("campaign finished", spent=0.1, ceiling=5.0)
    t = b.sent[-1]
    assert "All clear" in t
    assert "Every position was closed" in t


@pytest.mark.asyncio
async def test_final_report_flags_a_left_open_position():
    b = FakeBot()
    r = rep(b)
    await r.final_report("campaign finished", spent=0.1, ceiling=5.0,
                         position_left=True)
    t = b.sent[-1]
    assert "All clear" not in t
    assert "could NOT be closed" in t


@pytest.mark.asyncio
async def test_final_report_flags_errors():
    b = FakeBot()
    r = rep(b)
    r.note_error("balance read failed")
    await r.final_report("campaign finished", spent=0.1, ceiling=5.0)
    t = b.sent[-1]
    assert "All clear" not in t
    assert "1 error(s)" in t


@pytest.mark.asyncio
async def test_final_report_stays():
    """Its id is not tracked, so a later repost cannot delete the summary."""
    b = FakeBot()
    r = rep(b)
    await r.spot_buy("MX", 2.5, "1.52")
    live_id = r._message_id
    await r.final_report("done", spent=0.1, ceiling=5.0)
    assert b.deleted == [live_id], "only the live status was removed"
    assert r._message_id is None


@pytest.mark.asyncio
async def test_final_report_survives_telegram_failure():
    r = rep(FakeBot(fail_send=True))
    await r.final_report("done", spent=0.1, ceiling=5.0)   # must not raise


# ---- звіт: фʼючерси не мають тонути у споті (2026-08-26) -------------------

@pytest.mark.asyncio
async def test_futures_events_survive_a_flood_of_spot_actions():
    """ДЕФЕКТ, ЯКИЙ ЦЕ ЛІКУЄ. Спот робить десятки дій на день, фʼючерси —
    одну-три. У спільному списку з 8 рядків фʼючерсне відкриття витіснялось
    спотом за півгодини, і в звіті лишався ТІЛЬКИ спот: оператор не бачив ані
    відкриття, ані закриття позиції, тобто найдорожчих подій прогріву.
    """
    from src.execution.soft_start_reporter import SoftStartReporter
    r = SoftStartReporter(None, 1, dry_run=True)

    await r.futures_open("1000PEPEUSDT", 1, 9, 53, vol=1, notional=37.44)
    for i in range(30):
        await r.spot_buy("MXUSDT", 2.0 + i * 0.01, "0.75")

    out = r.render(day=1, days=3, spent=0.1, position="1000PEPEUSDT")
    assert "FUTURES OPEN 1000PEPEUSDT" in out, (
        "фʼючерсну подію витіснив спот — саме те, що лікували")
    assert "vol=1" in out and "37.44" in out, "розмір позиції не показано"


@pytest.mark.asyncio
async def test_spot_lines_show_the_real_amount_and_quantity():
    """Було `~3.00 USDT (qty ~)` у КОЖНОМУ рядку: раннер передавав
    `order_usdt_max` (стелю розміру) і літерал «~». Звіт, що показує
    константу замість виміру, гірший за відсутній — за ним неможливо
    помітити, що розмір не змінюється."""
    from src.execution.soft_start_reporter import SoftStartReporter
    r = SoftStartReporter(None, 1, dry_run=True)
    await r.spot_buy("MXUSDT", 1.78, "0.6692")
    await r.spot_sell("MXUSDT", "0.5", usdt=1.33)
    out = r.render(day=1, days=3, spent=0.0, position=None)
    assert "1.78" in out and "0.6692" in out
    assert "1.33" in out and "qty 0.5" in out
    assert "qty ~" not in out


def test_report_does_not_advertise_a_ceiling_that_no_longer_exists():
    """Стелю витрат прибрано; «0.117/5.00» читалось би як діючий ліміт."""
    from src.execution.soft_start_reporter import SoftStartReporter
    r = SoftStartReporter(None, 1, dry_run=True)
    out = r.render(day=1, days=3, spent=0.117, ceiling=None, position=None)
    assert "витрачено 0.117 USDT" in out
    assert "/5.00" not in out and "стеля" not in out
