"""Крок 1+2+3: зняття ТАВТОЛОГІЇ у shadow_twin.

ЩО БУЛО ЗЛАМАНО. `_record_twin` брав знімок книги в мить t0 і судив філ проти
ліміту, який live вивів із ТОГО САМОГО обʼєкта книги мікросекундами пізніше.
Умова філу — `min(asks) <= best_ask(t0) + offset*tick` — тотожно істинна, тож
симулятор не міг протухнути ЖОДНОГО разу: `shadow_filled=0` у 0 рядках із 360.
«Головне число» shadow/live алгебраїчно дорівнювало `1/(живий fill-rate)`.

ЩО ЗМІНИЛОСЬ. Фонова стрічка (`_twin_tape_loop`) памʼятає кадри книги, а
`_record_twin` бере кадр віком «мить ціноутворення + затримка». Книгу за минулу
мить можна лише ПАМʼЯТАТИ — доспати не можна, бо twin-задача стартує вже після
відповіді біржі.

Головний тест тут — `test_a_move_past_the_limit_now_expires`: до цієї правки
його НЕМОЖЛИВО було написати так, щоб він падав. Це і є доказ, що тавтологія
знята.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from src.exchanges.orderbook import OrderBook
from src.strategy.shadow_engine import ShadowEngine

SRC = Path("src/strategy/shadow_engine.py").read_text()


def _fn(name):
    return inspect.getsource(getattr(ShadowEngine, name))


def _eng():
    """Двигун без __init__ — той самий прийом, що і в решті сюїти.
    УВАГА: нові атрибути з __init__ треба дзеркалити тут, інакше тест
    зламається на рівному місці (це вже ламало сюїту двічі)."""
    e = ShadowEngine.__new__(ShadowEngine)
    e._twin_tape = {}
    e._twin_tape_symbols = set()
    e._twin_tape_len = 250
    e._twin_tape_interval_s = 0.01
    e._max_book_age_ms = 0
    e._queue_frac = 1.0
    e._max_acceptable_drift_pct = 0.05
    e._latency_min_ms = 150
    e._latency_max_ms = 205
    return e


def _frame(ts, bids, asks, uid=1, synced=True, orig_ts_ms=1_000_000):
    return (ts, uid, synced, orig_ts_ms, dict(bids), dict(asks))


# ---- вибір кадру ---------------------------------------------------------

def test_it_takes_the_last_frame_at_or_before_the_deadline():
    """Не найсвіжіший узагалі — інакше судили б проти книги, якої на той
    момент ще не існувало, тобто замінили б одну тавтологію на іншу."""
    e = _eng()
    e._twin_tape["X"] = [
        _frame(10.0, {9: 1}, {10: 1}, uid=1),
        _frame(10.1, {9: 1}, {11: 1}, uid=2),
        _frame(10.3, {9: 1}, {12: 1}, uid=3),   # уже ПІСЛЯ дедлайну 10.2
    ]
    fr, status, age = e._tape_frame_at("X", 10.2)
    assert status == "ok"
    assert fr[1] == 2, "взято не той кадр"
    assert age == pytest.approx(100, abs=2)


def test_no_tape_is_reported_not_swallowed():
    """Тиха гілка = брехливий знаменник (урок T0.1). Рядок має писатись."""
    e = _eng()
    fr, status, age = e._tape_frame_at("НЕМАЄ", 10.0)
    assert fr is None and status == "no_tape" and age is None


def test_frames_newer_than_the_deadline_are_reported_separately():
    e = _eng()
    e._twin_tape["X"] = [_frame(50.0, {9: 1}, {10: 1})]
    fr, status, _ = e._tape_frame_at("X", 10.0)
    assert fr is None and status == "tape_starts_later"


# ---- відновлення книги ---------------------------------------------------

def test_restored_book_keeps_its_ORIGINAL_age():
    """apply_snapshot штампує last_update_ts_ms=now (orderbook.py:163). Без
    відновлення оригіналу вік книги у twin завжди ~0, і гейт max_book_age_ms
    структурно інертний — відкалібрувати його стало б неможливо."""
    e = _eng()
    ob = e._ob_from_frame("X_USDT", _frame(1.0, {9.0: 5}, {10.0: 5},
                                           orig_ts_ms=1_234_567))
    assert ob.last_update_ts_ms == 1_234_567


def test_restored_book_carries_the_levels():
    e = _eng()
    ob = e._ob_from_frame("X_USDT", _frame(1.0, {9.0: 5}, {10.0: 7}))
    assert ob.best_bid().price == 9.0
    assert ob.best_ask().price == 10.0


def test_an_unsynced_frame_stays_unsynced():
    e = _eng()
    ob = e._ob_from_frame("X_USDT", _frame(1.0, {9.0: 5}, {10.0: 5}, synced=False))
    assert ob.is_synced is False


# ---- ГОЛОВНЕ: тавтологія знята ------------------------------------------

def test_a_move_past_the_limit_now_expires():
    """ЦЕЙ ТЕСТ НЕМОЖЛИВО БУЛО НАПИСАТИ ДО ПРАВКИ.

    Раніше книга й ліміт походили з одного кадру, тож філ був гарантований.
    Тепер ліміт фіксується на t0, а книга береться на t0+затримка — і якщо
    ask пішов ЗА ліміт, симулятор мусить протухнути.
    """
    from src.execution.ioc_executor import IOCExecutor
    e = _eng()
    e.ioc_executor = IOCExecutor()
    # t0: ask=100.00 → ліміт (at-touch) 100.00
    # t0+180мс: ask поїхав на 100.05 — за ліміт
    later = e._ob_from_frame("X_USDT", _frame(1.18, {99.9: 100}, {100.05: 100}))
    r = e.ioc_executor.simulate_ioc_entry(
        mexc_ob=later, direction="long", notional_usdt=1000.0, limit_price=100.00)
    assert r.status == "expired", "книга пішла за ліміт, а симулятор налився"

    # контроль: на книзі миті t0 той самий ліміт наливається
    at_t0 = e._ob_from_frame("X_USDT", _frame(1.0, {99.9: 100}, {100.00: 100}))
    r0 = e.ioc_executor.simulate_ioc_entry(
        mexc_ob=at_t0, direction="long", notional_usdt=1000.0, limit_price=100.00)
    assert r0.status in ("filled", "partial")


# ---- крива відгуку: три затримки -----------------------------------------

def test_three_delays_are_evaluated():
    body = _fn("_record_twin")
    assert "verdict(0.0)" in body, "контроль d0 зник — не буде з чим порівняти"
    assert "verdict(draw_ms)" in body
    assert "verdict(rtt_ms)" in body


def test_draw_uses_the_production_latency_window():
    """Інакше 'продакшн-shadow' у таблиці означав би не те, що в проді."""
    body = _fn("_record_twin")
    assert "random.uniform(self._latency_min_ms, self._latency_max_ms)" in body


def test_rtt_comes_from_the_real_order_not_a_constant():
    body = _fn("_record_twin")
    assert 'getattr(live_result, "submit_latency_ms", 0)' in body


def test_the_row_carries_all_three_verdicts_and_the_delays():
    body = _fn("_record_twin")
    for col in ("shadow_filled_d0", "shadow_filled_draw", "shadow_filled_rtt",
                "delay_draw_ms", "delay_rtt_ms", "tape_status", "tape_age_ms"):
        assert col in body, f"{col} не пишеться — крива буде неінтерпретована"


# ---- паритет із продакшн-shadow ------------------------------------------

def test_twin_and_production_pass_the_same_simulator_knobs():
    """Якби twin ходив драбиною з іншими аргументами, калібрування міряло б
    нашу власну неузгодженість, а не біржу. max_book_age_ms уже раз був
    пропущений саме тут."""
    body = _fn("_record_twin")
    for kw in ("max_book_age_ms=self._max_book_age_ms",
               "queue_frac=self._queue_frac"):
        assert kw in body, f"twin не передає {kw}"
    assert SRC.count("max_book_age_ms=self._max_book_age_ms") == 2
    assert SRC.count("queue_frac=self._queue_frac") == 2


def test_deterministic_post_latency_gates_are_replicated():
    """Продакшн-shadow після сну перевіряє is_synced і дрейф ціни. Без них
    twin протухав би рідше за shadow з причин, що не стосуються книги."""
    body = _fn("_record_twin")
    assert "book_desynced" in body
    assert "latency_drift" in body
    assert "self._max_acceptable_drift_pct" in body, "поріг має бути з атрибута"


def test_random_penalties_are_NOT_replicated():
    """should_reject_order / simulate_server_error разом ~2.5% — вони додали б
    лише дисперсію в порівняння, яке й так на малому n."""
    body = _fn("_record_twin")
    # докстрінг ПОЯСНЮЄ, чому їх немає — перевіряємо самі інструкції
    code = body[body.index('"""', body.index('"""') + 3) + 3:]
    assert "should_reject_order" not in code
    assert "simulate_server_error" not in code


def test_both_fill_thresholds_are_recorded():
    """status='partial' означає БУДЬ-ЯКЕ ненульове заповнення (у даних є рядок
    із часткою 0.0341, і він рахувався філом). Пишемо обидва пороги."""
    body = _fn("_record_twin")
    assert ">= 0.99" in body
    assert "shadow_strict_draw" in body


# ---- життєвий цикл стрічки ----------------------------------------------

def test_tape_loop_is_started_and_stopped():
    start, stop = _fn("start"), _fn("stop")
    assert "self._twin_tape_task = asyncio.create_task(self._twin_tape_loop())" in start
    assert "_twin_tape_task" in stop and "cancel()" in stop


def test_tape_loop_never_dies_silently():
    """Якщо семплер помре тихо, twin просто перестане писати, і це виглядатиме
    як 'даних мало', а не як поломка."""
    body = _fn("_twin_tape_loop")
    assert "logger.error" in body, "падіння ітерації має бути гучним"
    assert "except asyncio.CancelledError:" in body, "скасування має проходити наскрізь"


def test_tape_drops_symbols_that_left_live():
    """Інакше буфери мертвих пар живуть до рестарту."""
    body = _fn("_twin_tape_loop")
    assert "self._twin_tape.pop(sym, None)" in body
    assert "self._twin_tape_symbols.discard(sym)" in body


def test_tape_dedupes_by_update_id():
    body = _fn("_twin_tape_loop")
    assert "last_update_id" in body


def test_tape_is_bounded():
    body = _fn("_twin_tape_loop")
    assert "maxlen=self._twin_tape_len" in body


def test_migration_adds_the_new_columns():
    db = Path("src/storage/db.py").read_text()
    for col in ("shadow_filled_d0", "shadow_filled_draw", "shadow_filled_rtt",
                "shadow_strict_draw", "tape_status", "tape_age_ms"):
        assert f'("{col}"' in db, f"{col} немає в ідемпотентній міграції"


def test_live_executor_surfaces_the_pricing_timestamp():
    """Без спільної точки відліку стрічку неможливо адресувати."""
    ex = Path("src/execution/live_executor.py").read_text()
    assert "priced_at_perf: float = 0.0" in ex
    assert ex.count("priced_at_perf=priced_at_perf,") == 2, (
        "мітка має йти в ОБИДВА return — інакше протухлі ордери знову без twin")


def test_pricing_timestamp_is_bound_before_the_attempt_loop():
    """Той самий клас бага, що вже був із limit_scaled: присвоєння в циклі,
    читання після нього, а в циклі є continue → NameError на живому ордері."""
    ex = Path("src/execution/live_executor.py").read_text()
    init = ex.index("priced_at_perf = 0.0")
    loop = ex.index("for attempt in range(1, max_attempts + 1):")
    assert init < loop


def test_pricing_timestamp_is_taken_with_the_bbo():
    """Якщо зняти мітку пізніше, зсув поїде на час обчислень і стрічка
    віддаватиме кадр не того віку."""
    ex = Path("src/execution/live_executor.py").read_text()
    i = ex.index("priced_at_perf = time.perf_counter()")
    j = ex.index("limit_scaled = best_ask.price + offset_ticks * tick_scaled")
    assert 0 < j - i < 400, "мітка відірвалась від місця ціноутворення"


def test_the_three_walks_yield_between_each_other():
    """Прохід по драбині синхронний (5-15мс). Три поспіль без поступки
    блокували б цикл подій на 15-45мс — а за кривою Gate 0 кожні ~50мс
    приблизно подвоюють втрати філів."""
    body = _fn("_record_twin")
    seg = body[body.index("verdict(0.0)"):body.index("verdict(rtt_ms)")]
    assert seg.count("await asyncio.sleep(0)") == 2, (
        "між трьома проходами драбини має бути поступка циклу подій")
