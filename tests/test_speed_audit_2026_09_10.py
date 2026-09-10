# -*- coding: utf-8 -*-
"""Аудит швидкості 10.09: self-heal книг Binance + три мікрооптимізації.

SELF-HEAL — головне тут, і це НЕ про швидкість, а про цілісність даних.
`_fetch_snapshot` на невдачі HTTP і на протухлому знімку ставив
`_snapshot_ready=False` і виходив, а більше ніхто його не кликав:
`_handle_depth_msg` при `not ready` лише БУФЕРИЗУЄ диф, тож
`_apply_depth_diff` (де живе наявний resnap по розриву послідовності) не
виконується НІКОЛИ. Книга мертва до рестарту процесу.

ВИМІРЯНО 2026-09-10 16:10 UTC, через 2 год після рестарту primary:
    primary  BIN synced=7/23   MEX 23/23
    клон     BIN synced=22/23  MEX 23/23
MEXC цілий на обох саме тому, що в `mexc_ws` цей цикл є з початку.

ЧОМУ БУЛО ТИХО: детектор гейтить на `OrderBook.is_synced`, який ставиться в
`apply_snapshot` і НІКОЛИ не скидається, а топ книги тримає свіжим
bookTicker. Сигнали йшли як ні в чому не бувало (виміряно: розподіл по
символах на обох боксах збігається), псувалась лише драбина нижче топу.

МІКРООПТИМІЗАЦІЇ: три `logger.info` коштують ~0.29мс кожен (виміряно в
бойовому контейнері, best of 3 по 2000 викликів) і стояли МІЖ рішенням і
дротом. Тепер друкуються після відправки, але у `finally` — інакше на
таймауті ми втратили б діагностику саме тоді, коли вона потрібна.
"""
import asyncio

import pytest

from src.exchanges import binance_ws as bws
from src.exchanges.binance_ws import BinanceWSClient
from src.execution.webkey.client import MexcWebClient


def _ws(symbols=("AUSDT", "BUSDT"), ready=None):
    w = BinanceWSClient.__new__(BinanceWSClient)
    w._symbols = set(symbols)
    w._snapshot_ready = dict(ready or {})
    w._resnap_history = {}
    w._stop_event = asyncio.Event()
    w._ws_depth = object()          # зʼєднання підняте
    w.fetched = []

    async def _fetch(sym):
        w.fetched.append(sym)
        w._snapshot_ready[sym] = True

    w._fetch_snapshot = _fetch
    return w


async def _run_one_cycle(w, monkeypatch):
    """Прокрутити РІВНО одну ітерацію циклу лікаря."""
    calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(d):
        # перший sleep — інтервал циклу; далі 0.5с між символами
        if d == bws.SELF_HEAL_INTERVAL_SEC:
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(bws.asyncio, "sleep", fake_sleep)
    # Цикл САМ ловить CancelledError і виходить — тож він просто повертається.
    await w._self_heal_loop()


# ----------------------------------------------------------- self-heal

@pytest.mark.asyncio
async def test_it_refetches_a_dead_book(monkeypatch):
    """ГОЛОВНИЙ ТЕСТ. Саме цього бракувало — 16 із 23 книг на primary."""
    w = _ws(ready={"AUSDT": True, "BUSDT": False})
    await _run_one_cycle(w, monkeypatch)
    assert w.fetched == ["BUSDT"], f"перезабрано не те: {w.fetched}"


@pytest.mark.asyncio
async def test_it_leaves_healthy_books_alone(monkeypatch):
    """Зворотний бік: без цього тест вище проходив би і на коді, що смикає
    біржу по ВСІХ символах щопівхвилини."""
    w = _ws(ready={"AUSDT": True, "BUSDT": True})
    await _run_one_cycle(w, monkeypatch)
    assert w.fetched == []


@pytest.mark.asyncio
async def test_it_waits_for_the_socket(monkeypatch):
    """Поки depth-сокет не піднято, «немає знімка» означає «ще не стартували».
    Знімок без потоку дифів однаково протухне — не смикаємо біржу дарма."""
    w = _ws(ready={"AUSDT": False, "BUSDT": False})
    w._ws_depth = None
    await _run_one_cycle(w, monkeypatch)
    assert w.fetched == []


@pytest.mark.asyncio
async def test_it_respects_the_resnap_throttle(monkeypatch):
    """3/хв/символ — той самий тротл, що й на решті шляхів resnap.
    16 мертвих книг без нього дали б чергу запитів до /fapi."""
    w = _ws(ready={"AUSDT": False, "BUSDT": False})
    w._can_resnap = lambda sym: sym == "AUSDT"
    await _run_one_cycle(w, monkeypatch)
    assert w.fetched == ["AUSDT"]


@pytest.mark.asyncio
async def test_a_crash_does_not_kill_the_binance_feed(monkeypatch):
    """`run()` збирає задачі через gather(return_exceptions=False), тож
    виняток тут поклав би ВЕСЬ фід Binance — рівно те, що ми лікуємо."""
    w = _ws(ready={"BUSDT": False})

    async def _boom(sym):
        raise RuntimeError("біржа впала")

    w._fetch_snapshot = _boom
    await _run_one_cycle(w, monkeypatch)   # не має підняти RuntimeError


def test_the_loop_is_actually_started():
    """Сторож проводки: цикл, який ніхто не запускає, — це no-op,
    що виглядає робочим. Дванадцять разів у цьому проєкті."""
    import inspect
    src = inspect.getsource(BinanceWSClient.run)
    assert "self._self_heal_loop()" in src


def test_mexc_still_has_its_own(monkeypatch):
    """Не зламали дзеркало: у MEXC цей цикл був і лишається."""
    from src.exchanges import mexc_ws
    assert hasattr(mexc_ws.MexcWSClient, "_self_heal_loop")


# -------------------------------------------------- мікрооптимізації

def test_the_book_snapshot_is_logged_AFTER_the_submit():
    """`logger.info` коштує ~0.29мс; до фікса вони стояли між зняттям BBO
    і дротом. Значення знімаються до відправки, друкуються після."""
    import inspect
    from src.execution.live_executor import LiveExecutor
    src = inspect.getsource(LiveExecutor)
    i_capture = src.index("_ob_bid_px, _ob_ask_px = best_bid.price")
    i_log = src.index('"[IOC_OB] %s %s bid=')
    i_submit = src.index("async with asyncio.timeout(self.order_timeout_sec)")
    assert i_capture < i_submit < i_log, "лог знову стоїть перед відправкою"


def test_the_submit_uses_asyncio_timeout_not_wait_for():
    """`wait_for` обгортав корутину в Task — сабміт стартував лише з
    наступного проходу черги готових колбеків (~1.2мс)."""
    import inspect
    from src.execution.live_executor import LiveExecutor
    src = inspect.getsource(LiveExecutor)
    assert "async with asyncio.timeout(self.order_timeout_sec)" in src
    assert "asyncio.wait_for(\n                    client.submit_order" not in src


@pytest.mark.asyncio
async def test_order_submit_is_logged_even_when_the_post_fails(caplog):
    """У `finally`, а не після return: інакше на таймауті зник би ЄДИНИЙ
    запис про те, що саме ми слали — рівно тоді, коли він потрібен."""
    c = MexcWebClient.__new__(MexcWebClient)

    async def _boom(*a, **kw):
        raise TimeoutError("біржа мовчить")

    c._request = _boom
    with caplog.at_level("INFO", logger="src.execution.webkey.client"):
        with pytest.raises(TimeoutError):
            await c.submit_order(symbol="PEPE_USDT", side=1, vol=1,
                                 leverage=10, open_type=1, order_type=3,
                                 price="0.001")
    assert "[ORDER SUBMIT]" in caplog.text, "діагностику втрачено на відмові"
