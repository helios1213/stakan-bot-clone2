# -*- coding: utf-8 -*-
"""Злив буфера після знімка MEXC вимагав правила BINANCE — і морозив книгу.

ЩО БУЛО. `_fetch_snapshot` вимагав, щоб перший диф після знімка мав
`version == snapshot + 1`. Це правило BINANCE, де дифи інкрементні. У MEXC
`version` — ГЛОБАЛЬНИЙ лічильник змін: Δv між сусідніми пушами штатно 9..300
(це написано в коментарі до `_handle_depth` і підтверджено живим виміром), а
кожен пуш САМОДОСТАТНІЙ — абсолютні рівні, `vol=0` = видалення. Тобто розрив
там не втрата даних.

Наслідок: будь-який АКТИВНИЙ символ оголошувався `snapshot stale`, буфер
чистився, `_snapshot_ready` лишався False — і `_handle_depth` далі лише
буферизував, тобто книга MEXC НЕ ОНОВЛЮВАЛАСЬ ЗОВСІМ.

ВИМІРЯНО на primary 2026-09-10 (`docker logs`):
    ZEC_USDT  13 невдалих спроб поспіль, розриви 2-86
              книга не оновлювалась 16:27:40 -> 16:34:26 = 6 хв 51 с
              за цей час детектор випустив 403 сигнали ZECUSDT
    MUSTOCK_USDT 14 спроб, SOXL_USDT 4 — тобто рівно найактивніші символи.
Детектор при цьому НЕ мовчав: він гейтить на `is_synced`, який ставиться в
`apply_snapshot` і ніколи не скидається, тож сигнали рахувались зі свіжого
Binance проти замороженої MEXC.

ЩО ПІНИТЬСЯ ТУТ:
  * розрив у межах коалесценції ПРИЙМАЄТЬСЯ і книга синкається;
  * рівно `+1` теж працює (не зламали звичайний випадок);
  * справді величезний розрив і далі веде до пересинку;
  * ОБИДВА шляхи (усталений і злив) міряють одним порогом.
"""
import asyncio

import pytest

from src.exchanges import mexc_ws as mws
from src.exchanges.mexc_ws import MexcWSClient


class _Snap:
    """Мінімальний REST-клієнт: віддає знімок із заданою версією."""

    def __init__(self, version):
        self.version = version

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_depth(self, sym, limit=200):
        return {"version": self.version,
                "bids": [[100.0, 1.0]], "asks": [[101.0, 1.0]]}


def _client(monkeypatch, snap_version, buffered_versions):
    c = MexcWSClient.__new__(MexcWSClient)
    c._http = object()
    c._symbols_mexc = {"ZEC_USDT"}
    c._snapshot_ready = {"ZEC_USDT": False}
    c._last_version = {}
    c._scale_map = {}
    c._resnap_history = {}
    c.resync_count = 0
    from collections import deque
    c._buffered_diffs = {"ZEC_USDT": deque(
        [{"version": v, "bids": [], "asks": []} for v in buffered_versions],
        maxlen=1000)}

    from src.exchanges.orderbook import OrderBookManager
    c.ob_manager = OrderBookManager()
    monkeypatch.setattr(mws, "MexcRestClient", lambda http: _Snap(snap_version))
    return c


# ------------------------------------------------------- головний випадок

@pytest.mark.asyncio
async def test_a_coalesced_gap_still_syncs(monkeypatch):
    """ГОЛОВНИЙ ТЕСТ. Виміряний випадок ZEC: знімок 8327270350, перший диф
    8327270417 — розрив 67, тобто всередині штатних 9..300."""
    c = _client(monkeypatch, snap_version=8327270350,
                buffered_versions=[8327270417])

    await c._fetch_snapshot("ZEC_USDT")

    assert c._snapshot_ready["ZEC_USDT"] is True, "книга лишилась замороженою"
    assert c._last_version["ZEC_USDT"] == 8327270417


@pytest.mark.asyncio
async def test_the_exact_plus_one_case_still_works(monkeypatch):
    """Не зламали звичайний випадок, заради якого правило й писалось."""
    c = _client(monkeypatch, snap_version=1000, buffered_versions=[1001])
    await c._fetch_snapshot("ZEC_USDT")
    assert c._snapshot_ready["ZEC_USDT"] is True


@pytest.mark.asyncio
async def test_a_truly_huge_gap_still_resyncs(monkeypatch):
    """Зворотний бік: без цього тест вище проходив би і на коді, що ковтає
    БУДЬ-ЯКИЙ розрив, тобто мовчки торгував би по книзі з дірою."""
    c = _client(monkeypatch, snap_version=1000,
                buffered_versions=[1000 + mws._MAX_VERSION_GAP + 5])

    await c._fetch_snapshot("ZEC_USDT")

    assert c._snapshot_ready["ZEC_USDT"] is False
    assert c.resync_count == 1
    assert not c._buffered_diffs["ZEC_USDT"], "буфер мав бути очищений"


@pytest.mark.asyncio
async def test_diffs_older_than_the_snapshot_are_skipped(monkeypatch):
    """Дифи до версії знімка — дублікати, їх пропускаємо, а не рахуємо розривом."""
    c = _client(monkeypatch, snap_version=2000,
                buffered_versions=[1900, 1950, 2000, 2040])
    await c._fetch_snapshot("ZEC_USDT")
    assert c._snapshot_ready["ZEC_USDT"] is True
    assert c._last_version["ZEC_USDT"] == 2040


@pytest.mark.asyncio
async def test_an_empty_buffer_syncs(monkeypatch):
    """Тихий символ: дифів ще не було — знімок сам по собі вже валідний."""
    c = _client(monkeypatch, snap_version=500, buffered_versions=[])
    await c._fetch_snapshot("ZEC_USDT")
    assert c._snapshot_ready["ZEC_USDT"] is True


# ------------------------------------------- одне джерело істини для порогу

def test_both_paths_use_the_same_threshold():
    """Вони вже розійшлись одного разу — злив жив за правилом Binance, поки
    усталений режим знав правду. Константа тепер одна на обидва вживання."""
    import inspect
    src = inspect.getsource(mws)
    assert src.count("_MAX_VERSION_GAP") >= 4, "поріг знову продубльовано числом"
    # Якір саме на КОД, а не на прозу: коментарі в цьому ж файлі цитують
    # старе правило текстом, і наївний пошук підрядка ловив їх (мій перший
    # варіант цього тесту саме так і впав).
    assert "if v == version + 1:" not in src, "повернулось правило Binance"
    drain = inspect.getsource(MexcWSClient._fetch_snapshot)
    steady = inspect.getsource(MexcWSClient._handle_depth)
    assert "_MAX_VERSION_GAP" in drain and "_MAX_VERSION_GAP" in steady
    # Голого числа не має лишитись у ЖОДНОМУ з двох — інакше поріг знову
    # роз'їдеться. Перший варіант цього тесту рахував ВХОДЖЕННЯ і пропускав
    # мутанта, що повертав літерал лише в одну з двох перевірок.
    for _name, _src in (("_fetch_snapshot", drain), ("_handle_depth", steady)):
        _code = "\n".join(ln.split("#")[0] for ln in _src.splitlines())
        assert "10000" not in _code, f"{_name}: поріг знову літералом"
