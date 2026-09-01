"""Дефекти прогріву, знайдені аудитом 2026-09-01, і їхні фікси.

КОЖЕН тест тут виконує ПРОВОДКУ — справжній `soft_start_loop` або справжній
`SlotWarmer.tick()`, — а не лише функцію, у якій живе формула. Аудит показав,
що саме на цьому сюїта і брехала: мутант «прибрати `- spot_pnl` із фінального
звіту» лишав ВСІ 1447 тестів зеленими, бо всі виклики подавали `spot_pnl=0.0`.

Що тут пінається:
  §1.1  warmer, що дренажиться через ЗАВЕРШЕННЯ кампанії, мусить ретраїти
        закриття щополла (а не рівно один раз за все життя);
  §1.3  гард C4 (`futures_allowed`) перечитується щополла, а не лише в
        конструкторі;
  §1.4  позиція на слоті з ВИМКНЕНОЮ кнопкою все одно дозакривається;
  §2.6  фінальний звіт не друкується, доки розпродаж не добіг;
  §2.7  набір токенів кампанії переживає `start_if_new()`.
"""
from __future__ import annotations

import asyncio

import pytest

from src.execution import soft_start_runner as ssr
from src.execution.soft_start_runner import SlotWarmer


# --------------------------------------------------------------------------
# §2.6 — розпродаж і фінальний звіт
# --------------------------------------------------------------------------

def _warmer_with(spot, camp, wound_down=False):
    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w.draining = False
    w._wound_down = wound_down
    w._wind_passes = 0
    w._spot_viable = True
    w._held_market = None
    w._held_market_at = 0.0
    w.campaign = camp
    w.spot = spot
    w.futures = object()
    return w


class _Camp:
    def __init__(self):
        self.finished = 0
        self.state = type("S", (), {"tokens": ["MX"]})()

    def expired(self):
        return True

    def finish(self):
        self.finished += 1


class _Spot:
    def __init__(self, sells_per_pass):
        self.plan = type("P", (), {"tokens": ["MX"]})()
        self.sells = list(sells_per_pass)
        self.passes = 0

    async def wind_down(self, keep, tokens=None):
        self.passes += 1
        return self.sells.pop(0) if self.sells else 0


@pytest.mark.asyncio
async def test_the_slot_is_not_finished_while_the_wind_down_still_sells():
    """ГОЛОВНИЙ ДЕФЕКТ §2.6.

    `tick()` виходить після кожної пачки ордерів із коментарем «ще один тік на
    решту» — але того тіку не було НІКОЛИ: цикл питав `finished()` у ТІЙ САМІЙ
    ітерації, отримував True і одразу друкував фінальний звіт. Виміряно: у
    звіті 30.0 USDT у монетах проти 6.0 на біржі, і `campaign.finish()` не
    виконувався взагалі.
    """
    camp, spot = _Camp(), _Spot([2, 0])
    w = _warmer_with(spot, camp)

    await SlotWarmer.tick(w)
    assert spot.passes == 1
    assert w.finished() is False, "слот завершився посеред розпродажу"
    assert camp.finished == 0

    await SlotWarmer.tick(w)                      # той самий «ще один тік»
    assert spot.passes == 2
    assert w._wound_down is True
    assert w.finished() is True
    assert camp.finished == 1


@pytest.mark.asyncio
async def test_a_wind_down_that_never_converges_still_ends_the_campaign():
    """Стеля проходів. Без неї `finished()`, який чекає на `_wound_down`,
    тримав би кампанію відкритою вічно, якби біржа відхиляла кожен ордер."""
    camp = _Camp()
    spot = _Spot([3] * 50)                        # «продає» без кінця
    w = _warmer_with(spot, camp)

    for _ in range(ssr.MAX_WIND_DOWN_PASSES + 2):
        await SlotWarmer.tick(w)
        if w.finished():
            break
    assert w.finished() is True
    assert spot.passes <= ssr.MAX_WIND_DOWN_PASSES, "стеля проходів не діє"
    assert camp.finished == 1


@pytest.mark.asyncio
async def test_a_slot_with_no_spot_half_is_finished_immediately():
    """Порожня спотова половина не має тримати кампанію: продавати нічого."""
    camp, spot = _Camp(), _Spot([])
    w = _warmer_with(spot, camp)
    w._spot_viable = False
    assert w.finished() is True


# --------------------------------------------------------------------------
# §1.1 — ретрай закриття для АВТОЗАВЕРШЕНОЇ кампанії
# --------------------------------------------------------------------------

class _StuckWarmer:
    """Кампанія протухла, закриття падає — рівно сцена §1.1."""

    made: list = []
    held_is_measured = False

    def __init__(self, slot_id, webkey, client, universe, *, dry_run,
                 futures_allowed=True, **kw):
        self.slot_id = slot_id
        self.stop_calls = 0
        self.ticks = 0
        self.draining = False
        self.clean = False
        self.futures_allowed = futures_allowed
        self.reporter = None
        self.futures = object()
        self._stop_attempts = 0
        self._final_sent = False
        self.budget = type("B", (), {"spent": 0.0, "futures_pnl": 0.0,
                                     "spot_pnl": 0.0, "pnl": 0.0,
                                     "exhausted": lambda self: False,
                                     "state": type("S", (), {"max_usdt": 5.0,
                                                             "entries": []})()})()
        self.campaign = type("C", (), {
            "expired": lambda self: True,
            "elapsed_hours": lambda self: 72.0,
            "state": type("S", (), {"stats": {}, "tokens": []})(),
        })()
        type(self).made.append(self)

    def set_futures_allowed(self, allowed):
        self.futures_allowed = allowed

    def _held_spot_value(self):
        return 0.0

    def stuck(self):
        return self.draining and not self.clean

    def finished(self):
        return True

    async def start(self):
        pass

    async def tick(self):
        self.ticks += 1

    async def stop(self):
        self.stop_calls += 1
        self.draining = True
        if not self.clean:
            self._stop_attempts += 1
        return self.clean


class _Slot:
    def __init__(self, sid, soft_start_enabled=True, live_enabled=False):
        self.slot_id = sid
        self.webkey = "WEB" + "0" * 64
        self.soft_start_enabled = soft_start_enabled
        self.live_enabled = live_enabled


class _Store:
    def __init__(self, slots):
        self.slots = slots
        self.switched_off = []

    async def list_all(self):
        return self.slots

    async def set_soft_start(self, sid, enabled):
        if not enabled:
            self.switched_off.append(sid)
        for s in self.slots:
            if s.slot_id == sid:
                s.soft_start_enabled = enabled


class _Pool:
    async def get(self, sid):
        return object()


async def _drive(store, passes, monkeypatch, warmer_cls, on_pass=None,
                 tmp_dir="/nonexistent-soft-start-dir"):
    """Прокрутити СПРАВЖНІЙ `soft_start_loop` рівно `passes` полів."""
    monkeypatch.setattr(ssr, "SlotWarmer", warmer_cls)
    n = {"i": 0}

    async def fake_sleep(_):
        n["i"] += 1
        if on_pass is not None:
            on_pass(n["i"])
        if n["i"] >= passes:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(ssr.asyncio, "sleep", fake_sleep)
    # Підмітання сиріт має свій тест; тут воно не має нічого знаходити.
    # ВАЖЛИВО: стаб не має кликати asyncio.sleep — той підмінений і слугує
    # ЛІЧИЛЬНИКОМ полів, тож зайвий виклик обрізав би прогін удвічі.
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(ssr, "_sweep_orphan_futures", _noop)
    with pytest.raises(asyncio.CancelledError):
        await ssr.soft_start_loop(store, _Pool(), lambda: ["HYPEUSDT"])


@pytest.mark.asyncio
async def test_a_finished_campaign_keeps_retrying_a_failed_close(monkeypatch):
    """ДЕФЕКТ, ЩО ЛАМАЄ ГРОШІ (§1.1).

    Кампанія протухла, `close_all_positions` повернув не-0. Warmer іде в
    дренаж, але кнопку свідомо не гасять (позиція ж відкрита) — і через це
    слот лишається в `wanted`, тобто гілка «кнопку зняли» його не бере, гілка
    старту пропускає (`sid in warmers`), а гілка тіків до фікса робила голий
    `continue`. Виміряно: 8 полів -> РІВНО ОДНА спроба закриття. Позиція з
    плечем висіла на біржі без дедлайну і без повторних алертів.
    """
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    _StuckWarmer.made = []
    store = _Store([_Slot(1)])

    await _drive(store, 6, monkeypatch, _StuckWarmer)

    w = _StuckWarmer.made[0]
    assert len(_StuckWarmer.made) == 1, "цикл перестворив warmer — позиція б осиротіла"
    assert w.stop_calls >= 5, (
        f"закриття ретраїлось лише {w.stop_calls} раз(и) за 6 полів — "
        "warmer знову заморожено")
    assert store.switched_off == [], "кнопку не можна гасити з відкритою позицією"


@pytest.mark.asyncio
async def test_a_successful_retry_finally_switches_the_slot_off(monkeypatch):
    """Ретрай мусить не лише повторюватись, а й ЗАВЕРШУВАТИСЬ."""
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    _StuckWarmer.made = []
    store = _Store([_Slot(1)])

    def _heal(i):
        if i == 3 and _StuckWarmer.made:
            _StuckWarmer.made[0].clean = True     # біржа відпустила

    await _drive(store, 6, monkeypatch, _StuckWarmer, on_pass=_heal)

    assert store.switched_off == [1], "слот так і не вимкнувся після вдалого закриття"


@pytest.mark.asyncio
async def test_a_manually_stopped_slot_is_not_retried_twice_per_poll(monkeypatch):
    """Слот, знятий КНОПКОЮ, ретраїть гілка вище. Якби гілка тіків брала і
    його, на кожен полл ішло б по дві спроби закриття."""
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    _StuckWarmer.made = []
    slot = _Slot(1)
    store = _Store([slot])

    def _off(i):
        slot.soft_start_enabled = False

    await _drive(store, 4, monkeypatch, _StuckWarmer, on_pass=_off)

    w = _StuckWarmer.made[0]
    assert w.stop_calls <= 4, f"{w.stop_calls} спроб на 4 полли — подвійний ретрай"


# --------------------------------------------------------------------------
# §1.3 — гард C4 перечитується щополла
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turning_live_arb_on_stops_the_futures_half(monkeypatch):
    """ДЕФЕКТ, ЩО ЛАМАЄ ГРОШІ (§1.3).

    `futures_allowed` читався РІВНО ОДИН РАЗ, у конструкторі. Цикл warmer не
    перестворює, тож вмикання живого арбітражу на слоті, який уже гріється,
    гард не помічало: виміряно 9 тіків фʼючерсного прогріву при
    `live_enabled=1`. Далі реконсайлер бачить прогрівну позицію як сироту і
    закриває її по ринку, записуючи PnL у кіл просадки арбітражного слота.
    """
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    _StuckWarmer.made = []

    class _Alive(_StuckWarmer):
        made: list = []

        def finished(self):
            return False                          # кампанія триває

    slot = _Slot(1, live_enabled=False)
    store = _Store([slot])

    def _go_live(i):
        if i == 2:
            slot.live_enabled = True

    _Alive.made = []
    await _drive(store, 5, monkeypatch, _Alive, on_pass=_go_live)

    w = _Alive.made[0]
    assert w.futures_allowed is False, (
        "живий арбітраж увімкнули, а фʼючерсна половина прогріву й далі "
        "вважає, що їй можна відкривати позиції")


@pytest.mark.asyncio
async def test_the_gate_recomputes_viability_not_just_the_flag():
    """Прапорця мало: тікає `_fut_viable`, і саме його читає `tick()`."""
    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w.futures_allowed = True
    w._fut_bal_ok = True
    w._fut_viable = True

    w.set_futures_allowed(False)
    assert w.futures_allowed is False and w._fut_viable is False

    w.set_futures_allowed(True)
    assert w._fut_viable is True

    w._fut_bal_ok = False                          # балансу бракує
    w.set_futures_allowed(False)
    w.set_futures_allowed(True)
    assert w._fut_viable is False, "гард не має воскрешати половину без балансу"


# --------------------------------------------------------------------------
# §1.4 — позиція на слоті з ВИМКНЕНОЮ кнопкою
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_position_is_closed_even_after_the_button_went_off(
        monkeypatch, tmp_path):
    """ДЕФЕКТ, ЩО ЛАМАЄ ГРОШІ (§1.4).

    `recover()` має рівно одного викликача — `SlotWarmer.start()`, а той
    виконується лише для слотів із увімкненою кнопкою. Тобто позиція ставала
    невидимою одразу, щойно оператор гасив кнопку — а лог при цьому обіцяв
    «restart the bot to let recover() do it». Реконсайлер арбітражу її теж не
    бачить: він ходить лише по live-екзекуторах.
    """
    (tmp_path / "futures_soft_start_slot1.json").write_text(
        '{"position": {"symbol": "HYPE_USDT", "vol": 1}}')

    recovered = []

    class _FSS:
        def __init__(self, *a, **kw):
            pass

        async def recover(self):
            recovered.append(1)

        def has_exposure(self):
            return False

    monkeypatch.setattr(ssr, "FuturesSoftStart", _FSS)
    monkeypatch.setattr(ssr, "FeeGate", lambda *a, **kw: object())

    await ssr._sweep_orphan_futures(
        _Store([]), _Pool(), [_Slot(1, soft_start_enabled=False)], {},
        lambda: ["HYPEUSDT"], True, None, data_dir=str(tmp_path))

    assert recovered == [1], "позицію на вимкненому слоті ніхто не закрив"


@pytest.mark.asyncio
async def test_the_sweep_does_not_touch_a_slot_that_trades_live(
        monkeypatch, tmp_path):
    """`close_all_positions` символо-широкий: на живому слоті він зніс би
    арбітражну позицію. Тому там — гучний алерт, а не автозакриття."""
    (tmp_path / "futures_soft_start_slot1.json").write_text(
        '{"position": {"symbol": "HYPE_USDT", "vol": 1}}')

    recovered, alerted = [], []

    class _FSS:
        def __init__(self, *a, **kw):
            pass

        async def recover(self):
            recovered.append(1)

        def has_exposure(self):
            return False

    class _Alerts:
        async def send(self, msg):
            alerted.append(msg)

    monkeypatch.setattr(ssr, "FuturesSoftStart", _FSS)
    monkeypatch.setattr(ssr, "FeeGate", lambda *a, **kw: object())

    await ssr._sweep_orphan_futures(
        _Store([]), _Pool(),
        [_Slot(1, soft_start_enabled=False, live_enabled=True)], {},
        lambda: ["HYPEUSDT"], True, _Alerts(), data_dir=str(tmp_path))

    assert recovered == [], "на живому слоті закривати самим не можна"
    assert alerted, "оператора не попередили про осиротілу позицію"


@pytest.mark.asyncio
async def test_the_sweep_stays_quiet_when_nothing_is_open(monkeypatch, tmp_path):
    """Порожній стан не має піднімати клієнта — інакше це запит на кожен полл."""
    (tmp_path / "futures_soft_start_slot1.json").write_text('{"position": null}')
    built = []
    monkeypatch.setattr(ssr, "FuturesSoftStart",
                        lambda *a, **kw: built.append(1))
    await ssr._sweep_orphan_futures(
        _Store([]), _Pool(), [_Slot(1, soft_start_enabled=False)], {},
        lambda: ["X"], True, None, data_dir=str(tmp_path))
    assert built == []


@pytest.mark.asyncio
async def test_an_unreadable_state_file_is_not_read_as_no_position(
        monkeypatch, tmp_path):
    """Нечитабельний файл — це НЕ «позиції немає»: рівно так вона й губиться."""
    (tmp_path / "futures_soft_start_slot1.json").write_text("{broken")
    recovered = []

    class _FSS:
        def __init__(self, *a, **kw):
            pass

        async def recover(self):
            recovered.append(1)

        def has_exposure(self):
            return False

    monkeypatch.setattr(ssr, "FuturesSoftStart", _FSS)
    monkeypatch.setattr(ssr, "FeeGate", lambda *a, **kw: object())
    await ssr._sweep_orphan_futures(
        _Store([]), _Pool(), [_Slot(1, soft_start_enabled=False)], {},
        lambda: ["X"], True, None, data_dir=str(tmp_path))
    assert recovered == [1], "битий файл прочитано як «нічого немає»"


@pytest.mark.asyncio
async def test_the_loop_actually_calls_the_orphan_sweep(monkeypatch):
    """ТЕСТ ПРОВОДКИ, а не формули.

    Дванадцятий за проєкт випадок того самого класу: підмітання сиріт можна
    було написати ідеально і НЕ ПІДКЛЮЧИТИ — усі тести самої функції лишались
    би зеленими. Мутант, що прибирає виклик із циклу, мусить валити тест.
    """
    monkeypatch.delenv("SOFT_START_LIVE", raising=False)
    _StuckWarmer.made = []
    called = []

    async def _spy(*a, **kw):
        called.append(kw.get("data_dir") or a)

    monkeypatch.setattr(ssr, "_sweep_orphan_futures", _spy)
    monkeypatch.setattr(ssr, "SlotWarmer", _StuckWarmer)
    n = {"i": 0}

    async def fake_sleep(_):
        n["i"] += 1
        if n["i"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(ssr.asyncio, "sleep", fake_sleep)
    store = _Store([_Slot(1, soft_start_enabled=False)])
    with pytest.raises(asyncio.CancelledError):
        await ssr.soft_start_loop(store, _Pool(), lambda: ["X"])

    assert len(called) == 2, "цикл не кличе підмітання сиріт щополла"


# --------------------------------------------------------------------------
# §2.7 / §2.3 — порядок у start()
# --------------------------------------------------------------------------

async def _run_start(monkeypatch, *, free_usdt, coins_usdt, drawn):
    """Виконати СПРАВЖНІЙ `SlotWarmer.start()` із застабленими краями."""
    w = SlotWarmer.__new__(SlotWarmer)
    w.slot_id = 1
    w.data_dir = "/tmp"
    w.dry_run = True
    w.universe = ["HYPEUSDT"]
    w.client = object()
    w.spot_client = object()
    w.fee_gate = object()
    w.reporter = None
    w.budget = object()
    w.futures_allowed = True
    w._account_key = "k"
    w._wound_down = False
    w._wind_passes = 0
    w._held_market = None
    w._held_market_at = 0.0

    order = []

    class _Camp:
        state = type("S", (), {"tokens": [], "days": 3,
                               "day_index": lambda self: 0,
                               "remaining_days": lambda self: 3.0})()

        def token_pool(self, cands):
            order.append("draw")
            return list(drawn)

        def start_if_new(self, key):
            order.append("start_if_new")
            self.state.tokens = []          # нова кампанія стирає набір
            return True

        def bump(self, *a, **kw):
            pass

    w.campaign = _Camp()
    w._reset_accounting = lambda: order.append("reset")

    async def _bal():
        return free_usdt, 999.0
    w._read_balances = _bal

    async def _coins(tokens):
        return coins_usdt
    w.spot_client_coins_value = _coins

    async def _resolve(t):
        return None
    w.spot_client = type("SC", (), {
        "_resolver": type("R", (), {"resolve": staticmethod(_resolve)})()})()

    seen = {}
    monkeypatch.setattr(ssr, "SpotSoftStart",
                        lambda client, cfg, **kw: seen.setdefault("cfg", cfg))

    class _F:
        def __init__(self, *a, **kw):
            pass

        async def recover(self):
            pass
    monkeypatch.setattr(ssr, "FuturesSoftStart", _F)

    await SlotWarmer.start(w)
    return w, order, seen["cfg"]


@pytest.mark.asyncio
async def test_the_campaign_token_set_survives_its_own_start(monkeypatch):
    """ДЕФЕКТ §2.7.

    `_spot_universe()` кликав `campaign.token_pool()`, той запамʼятовував
    набір у стані кампанії, а `start_if_new()` через чверть секунди створював
    НОВИЙ стан і набір стирав. Живий лог слота 2 01.09:
    `12:47:17.797 спотовий набір ['ADA','MX','TRX']` ->
    `12:47:18.041 старт (новий акаунт)` -> у файлі кампанії tokens=[].
    Наслідок не косметичний: `maybe_sell` бере токен лише з денного плану,
    тож монети старого набору неможливо продати аж до розпродажу.
    """
    _, order, cfg = await _run_start(monkeypatch, free_usdt=50.0,
                                     coins_usdt=0.0, drawn=["ADA", "MX"])
    assert order.index("start_if_new") < order.index("draw"), (
        "набір розігрується ДО start_if_new, який його ж і стирає")
    assert set(cfg.universe) == {"ADA", "MX"}


@pytest.mark.asyncio
async def test_sizing_comes_from_the_whole_spot_wallet_not_free_usdt(monkeypatch):
    """ДЕФЕКТ §2.3.

    Сайзинг і денна стеля рахувались від ВІЛЬНОГО USDT і заморожувались у
    `start()` назавжди. Живий лог слота 2 31.08: `вільних 3.24 + монет 22.51
    = 25.76`, а далі 8 поспіль `skip MXUSDT — daily ceiling 3 (spent 2.54)`.
    Гроші на місці, план мертвий — USDT просто перетворився на монети.
    """
    _, _, poor = await _run_start(monkeypatch, free_usdt=3.24,
                                  coins_usdt=0.0, drawn=["MX"])
    _, _, rich = await _run_start(monkeypatch, free_usdt=3.24,
                                  coins_usdt=22.51, drawn=["MX"])
    assert rich.daily_buy_usdt_ceiling > poor.daily_buy_usdt_ceiling, (
        "монети не враховані — стеля й далі виводиться з вільного USDT")
    assert rich.baseline_usdt_per_token > poor.baseline_usdt_per_token
