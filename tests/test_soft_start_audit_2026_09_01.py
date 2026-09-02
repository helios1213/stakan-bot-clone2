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

async def _run_start(monkeypatch, *, free_usdt, coins_usdt, drawn, fut_usdt=999.0):
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
        return free_usdt, fut_usdt
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


# --------------------------------------------------------------------------
# §2.2 — спред рахувався ДВІЧІ
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_flat_market_round_trip_shows_zero_spot_pnl(tmp_path,
                                                            monkeypatch):
    """ДЕФЕКТ §2.2 — фантомний спотовий збиток.

    Купівля йде за `px*(1+buffer)`, тож біржа віддає `qty = usdt/(px*(1+b))`.
    Собівартість писалась як `usdt` на цю кількість, тобто по ціні
    `px*(1+b)`; продаж же записує виручку по МІДУ (`qty*px`). При геть
    нерухомому ринку це давало `spot_pnl = -usdt*b` — а той самий буфер уже
    сидить у `spent` через `_order_cost`. Спред рахувався двічі.

    Живі дані слота 1: всі 8 продажів відʼємні і центровані рівно на ставці
    буфера (зважено -0.001926 проти передбаченого -0.001996), тобто ~96%
    рядка «спот» у звіті — це подвійний облік, а не рух ринку.
    """
    from src.execution import spot_soft_start as sss
    from src.execution.soft_start_budget import SoftStartBudget

    PX = 2.0
    BUF = 0.002

    class _Cur:
        currency_id = "cid"
        market_currency_id = "mid"
        price_decimals = 6
        quantity_decimals = 6

    class _Res:
        def __init__(self, qty, price):
            self.ok = True
            self.dry_run = False
            self.quantity = qty
            self.price = price
            self.code = 200
            self.raw = {}

    class _Client:
        async def currency(self, t):
            return _Cur()

        async def balances(self, ids):
            return {"USDT": {"available": 1000.0}, "MX": {"available": 100.0}}

        async def buy(self, token, *, usdt, price):
            return _Res(usdt / price, price)      # біржа наливає ЗА ЦІНОЮ ЛІМІТА

        async def sell(self, token, *, quantity, price):
            return _Res(quantity, price)

    monkeypatch.setattr(sss, "_price_async",
                        lambda symbol: asyncio.sleep(0, result=PX))

    budget = SoftStartBudget(str(tmp_path / "b.json"), 1e9)
    cfg = sss.SoftStartConfig(universe=("MX",),
                              state_path=str(tmp_path / "s.json"),
                              order_usdt_min=10.0, order_usdt_max=10.0,
                              baseline_usdt_per_token=0.0,
                              marketable_buffer=BUF)
    e = sss.SpotSoftStart(_Client(), cfg, budget=budget)
    e.plan.tokens = ["MX"]
    e.plan.buys_done = 0
    e.plan.buys_target = 5
    e.plan.sells_done = 0
    e.plan.sells_target = 5

    assert await e.maybe_buy() is True
    qty = budget.state.spot_positions["MX"]["qty"]
    assert qty == pytest.approx(10.0 / (PX * (1 + BUF)), rel=1e-9)

    sold = await e.maybe_sell()

    assert sold is True, "продаж не пройшов — тест нічого не міряє"
    assert budget.spot_pnl == pytest.approx(0.0, abs=1e-6), (
        f"нерухомий ринок дав spot_pnl={budget.spot_pnl:.6f} — спред "
        "рахується двічі")
    assert budget.spent > 0, "спред мусить лишитись у витратах, і рівно раз"


# --------------------------------------------------------------------------
# §2.1 — синхронний urllib у event loop
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prices_are_fetched_off_the_event_loop():
    """Виміряно в контейнері: 17 цін поспіль тримали loop 1387 мс (фон 5.9).
    17 підряд — це рівно те, що робить `wind_down` по своєму пулу."""
    import threading
    from src.execution import spot_soft_start as sss

    where = {}

    def _slow(symbol):
        where["thread"] = threading.current_thread().name
        return 1.0

    orig = sss.public_last_price
    sss.public_last_price = _slow
    try:
        beats = []

        async def heartbeat():
            for _ in range(60):
                beats.append(1)
                await asyncio.sleep(0.001)

        hb = asyncio.create_task(heartbeat())
        await sss._price_async("MXUSDT")
        hb.cancel()
    finally:
        sss.public_last_price = orig

    assert where["thread"] != threading.main_thread().name, (
        "ціну тягнуть у головному потоці — це морозить event loop арбітражу")


# --------------------------------------------------------------------------
# §1.5 / §2.5 — атомарність і fail-closed
# --------------------------------------------------------------------------

def test_an_unreadable_campaign_does_not_start_three_more_live_days(tmp_path):
    """ДЕФЕКТ §1.5, найдорожчий за наслідком.

    Було: битий файл -> `started_at=0` -> `start_if_new()` True ->
    `_reset_accounting()` -> ЩЕ 3 ДОБИ ЖИВОЇ ТОРГІВЛІ на акаунті, який
    оператор вважав завершеним, плюс обнулений облік. Тригер реальний: диск
    на primary 96%, а `open(...,"w")` обрізає файл ДО запису.
    """
    from src.execution.soft_start_campaign import SoftStartCampaign

    p = tmp_path / "c.json"
    p.write_text('{"started_at": 1, "days": 3')      # обрізаний JSON
    c = SoftStartCampaign(str(p))

    assert c._unreadable is True
    assert c.start_if_new("acct") is False, "битий файл почав нову кампанію"
    assert c.expired() is True, "слот мусить чисто вимкнутись, а не гріти далі"
    assert list(tmp_path.glob("c.json.corrupt.*")), "битий файл не збережено"


def test_campaign_and_budget_are_written_atomically(tmp_path):
    """`open(...,"w")` обрізає файл ДО запису — саме так стан і зникає."""
    from src.execution.soft_start_budget import SoftStartBudget
    from src.execution.soft_start_campaign import SoftStartCampaign

    c = SoftStartCampaign(str(tmp_path / "c.json"))
    c.start_if_new("k")
    b = SoftStartBudget(str(tmp_path / "b.json"), 5.0)
    b.charge(0.1, "x")

    for mod, path in ((SoftStartCampaign, tmp_path / "c.json"),
                      (SoftStartBudget, tmp_path / "b.json")):
        src = __import__("inspect").getsource(mod._save)
        assert "_atomic_write_json" in src, f"{mod.__name__}._save не атомарний"
    assert (tmp_path / "c.json").read_text().strip().endswith("}")
    assert (tmp_path / "b.json").read_text().strip().endswith("}")


def test_a_torn_budget_does_not_double_count_the_coins(tmp_path):
    """ДЕФЕКТ §2.5, другого порядку.

    Свіжий стан мав `accounting_version=0`, який зберігався на диск; наступне
    завантаження ловило `< 2` і сіяло `legacy_spot_cost = max(0, -spot_flow)`,
    тоді як ті самі купівлі вже лежать у `spot_positions`. Виміряно: після
    битого читання одна купівля ADA на 5.0 давала legacy 5.0 І позицію 5.0 —
    `held_spot_value` 10.0 замість 5.0. Не самолікується.
    """
    from src.execution.soft_start_budget import SoftStartBudget

    p = tmp_path / "b.json"
    p.write_text("{ обрізано")
    b = SoftStartBudget(str(p), 5.0)
    assert b.state.accounting_version == 2

    b.record_spot_buy("ADA", 5.0, 10.0)
    again = SoftStartBudget(str(p), 5.0)
    assert again.state.legacy_spot_cost == 0.0, "монети пораховано двічі"
    assert again.held_spot_value == pytest.approx(5.0)


# --------------------------------------------------------------------------
# §2.4 — порожні баланси != «монет немає»
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_http_error_on_balances_is_not_read_as_zero_funds():
    """ДЕФЕКТ §2.4.

    `balances()` віддавав `{}` на будь-який не-200, і споживач мовчки читав
    це як `free = 0.00`, друкуючи в лог СТВЕРДЖЕННЯ «вільного USDT лише
    0.00». Гілка «не прочитали — не гейтимо» ловила лише мережеві винятки.
    """
    from src.execution.webkey.spot_client import (SpotBalancesUnavailable,
                                                  SpotWebClient)

    c = SpotWebClient.__new__(SpotWebClient)
    c._timeout = 1
    c.quote = "USDT"

    class _R:
        status_code = 429

        def json(self):
            return {}

    class _S:
        async def get(self, *a, **kw):
            return _R()

    c._ensure_session = lambda: _S()
    c._headers = lambda: {}

    with pytest.raises(SpotBalancesUnavailable):
        await c.balances(["cid"])


# --------------------------------------------------------------------------
# §1.2 — рішення про реальну позицію з одного семпла
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_pending_open_is_rechecked_before_being_written_off(tmp_path):
    """ДЕФЕКТ, ЩО ЛАМАЄ ГРОШІ (§1.2).

    `reconcile_pending` робив ОДНЕ читання з нульовою затримкою, і порожня
    відповідь НЕЗВОРОТНО стирала pending. Але цей самий проєкт уже виміряв
    затримку видимості філу («MEXC's fill-visibility lag is ~50-200ms after
    submit», `live_executor.py`), і схему «одна перевірка» там уже відкидали
    після реальної ліквідації (-$25.82, 49 хв голого шорта). Ризикова саме
    ШВИДКА мережева відмова при RTT ~150мс — рівно в ту смугу.

    Наслідок: наступний тік відкриває ДРУГУ позицію, а перша не закриється
    ніколи (`close_position` символо-широкий і бачить лише відстежувану).
    """
    from src.execution import futures_soft_start as fss

    reads = {"n": 0}

    class _Client:
        async def get_open_positions(self):
            reads["n"] += 1
            if reads["n"] == 1:
                return {"code": "0", "data": []}      # біржа ще не показує
            return {"code": "0", "data": [
                {"symbol": "HYPE_USDT", "holdVol": "1", "positionType": 1,
                 "leverage": 9, "openAvgPrice": "10"}]}

    e = fss.FuturesSoftStart.__new__(fss.FuturesSoftStart)
    e.client = _Client()
    e.cfg = fss.FuturesSoftStartConfig(state_path=str(tmp_path / "f.json"))
    e.state = fss.FuturesState(pending={"symbol": "HYPEUSDT", "vol": 1,
                                        "leverage": 9, "hold_min": 10,
                                        "sent_at": 0.0})
    e.budget = None
    e.dry_run = True
    e.on_action = None
    e._last_fee_estimate = 0.0
    e.rng = __import__("random").Random(0)
    e.last_closed = None

    adopted = await e.reconcile_pending()

    assert reads["n"] >= 2, (
        f"біржу спитали лише {reads['n']} раз(и) — позицію списали з одного "
        "порожнього семпла")
    assert adopted is True, "позицію не адоптовано, хоча біржа її показала"
    assert e.state.pending is None
    assert e.state.position is not None


@pytest.mark.asyncio
async def test_a_genuinely_unopened_order_is_still_cleared(tmp_path):
    """Ретрай не має перетворювати «не відкрилось» на вічний pending —
    інакше слот більше ніколи не відкриє позицію."""
    from src.execution import futures_soft_start as fss

    class _Client:
        async def get_open_positions(self):
            return {"code": "0", "data": []}

    e = fss.FuturesSoftStart.__new__(fss.FuturesSoftStart)
    e.client = _Client()
    e.cfg = fss.FuturesSoftStartConfig(state_path=str(tmp_path / "f.json"))
    e.state = fss.FuturesState(pending={"symbol": "HYPEUSDT", "vol": 1,
                                        "leverage": 9, "hold_min": 10,
                                        "sent_at": 0.0})
    e.budget = None
    e.dry_run = True
    e.on_action = None
    e._last_fee_estimate = 0.0
    e.rng = __import__("random").Random(0)
    e.last_closed = None

    assert await e.reconcile_pending() is False
    assert e.state.pending is None, "pending завис назавжди"


@pytest.mark.asyncio
async def test_an_unreadable_exchange_keeps_the_question_open(tmp_path):
    """Нечитабельна біржа — це НЕ «нічого не відкрилось»."""
    from src.execution import futures_soft_start as fss

    class _Client:
        async def get_open_positions(self):
            return {"code": "500", "data": None}

    e = fss.FuturesSoftStart.__new__(fss.FuturesSoftStart)
    e.client = _Client()
    e.cfg = fss.FuturesSoftStartConfig(state_path=str(tmp_path / "f.json"))
    e.state = fss.FuturesState(pending={"symbol": "HYPEUSDT", "vol": 1,
                                        "leverage": 9, "hold_min": 10,
                                        "sent_at": 0.0})
    e.budget = None
    e.dry_run = True
    e.on_action = None
    e._last_fee_estimate = 0.0
    e.rng = __import__("random").Random(0)
    e.last_closed = None

    assert await e.reconcile_pending() is False
    assert e.state.pending is not None, "питання закрили без відповіді"


# --------------------------------------------------------------------------
# §3 — діри, через які мутанти виживали в УСІЙ сюїті
# --------------------------------------------------------------------------

def test_the_final_report_actually_subtracts_the_spot_pnl():
    """ДІРА §3.1, виміряна мутантом.

    Прибирання `- spot_pnl` із `render_final` лишало ВСІ 1447 тестів
    зеленими: з 11 викликів `render_final` десять подавали `spot_pnl=0.0`, а
    єдиний із `-0.3` перевіряв лише відсутність фрази «не виміряно».
    Контроль: та сама правка в живому статусі валила 2 тести — тобто діра
    була саме у ФІНАЛЬНОМУ звіті, який оператор і читає.
    """
    import re

    from src.execution.soft_start_reporter import SoftStartReporter

    r = SoftStartReporter(None, 1, dry_run=False)
    txt = re.sub(r"</?[a-z]+>", "", r.render_final(
        "x", spent=0.30, futures_pnl=0.0, spot_pnl=-2.0,
        held_value=0.0, held_measured=True))
    assert "2.30" in txt, (
        f"спотовий PnL не увійшов у підсумок: {txt!r}")


def test_the_futures_window_really_gates_the_open():
    """ДІРА §3.2, виміряна мутантом.

    `active_now → True` лишало всю сюїту зеленою: чотири тести вікна роблять
    `monkeypatch.setattr(ss, "active_now", ...)`, тобто підміняють саме те,
    що мали б перевіряти. Спотовий аналог покритий по-справжньому — цей ні.
    Вікно існує не для краси: без нього фʼючерси відкривали позицію о 01:35,
    поки спот законно спав.
    """
    from datetime import datetime, timezone

    from src.execution.futures_soft_start import (FuturesSoftStart,
                                                  FuturesSoftStartConfig)

    e = FuturesSoftStart.__new__(FuturesSoftStart)
    e.cfg = FuturesSoftStartConfig()
    at = lambda h: datetime(2026, 9, 1, h, 0, tzinfo=timezone.utc)

    assert e.active_now(at(12)) is True
    assert e.active_now(at(1)) is False, "нічне вікно не гейтиться"
    assert e.active_now(at(23)) is False


# --------------------------------------------------------------------------
# §2.3 — прогрів мусить бути СКАСОВНИМ, а пул закриватись ПІСЛЯ
# --------------------------------------------------------------------------

def test_the_soft_start_task_is_cancellable_and_the_pool_closes_last():
    """ДЕФЕКТ §2.3 — тест ПРОВОДКИ по джерелу `main()`.

    Задача прогріву створювалась голим `create_task` і НЕ потрапляла у
    `tasks`, тож на зупинці бота її обробник `CancelledError` — єдине місце,
    що закриває відкриту позицію при завершенні — не виконувався
    детерміновано. Гірше: `webkey_client_pool.close_all()` стояв ПЕРЕД
    скасуванням задач, тобто відбирав у прогріву рівно того клієнта, яким той
    мав рятувати гроші.

    Виконати `main()` у тесті неможливо, тож пінимо два факти в джерелі: що
    задача кладеться у `tasks`, і що пул закривається ПІСЛЯ `gather`.
    """
    src = __import__("pathlib").Path("src/main.py").read_text()

    assert "tasks.append(soft_start_task)" in src, (
        "задача прогріву не потрапляє у `tasks` — на зупинці її ніхто не "
        "скасує, і позиція лишиться відкритою")

    i_gather = src.index("await asyncio.gather(*tasks")
    i_close = src.index("await webkey_client_pool.close_all()")
    assert i_close > i_gather, (
        "пул вебкей-клієнтів закривається ДО скасування задач — прогріву не "
        "буде чим закрити позицію")


def test_an_old_budget_file_does_not_count_the_same_coins_twice(tmp_path):
    """§1.5, друга половина: файл версії <2, у якому ВЖЕ є `spot_positions`.

    Свіжий стан я закрив версією 2, але старий файл із обома джерелами
    порахував би ті самі монети двічі — раз у позиціях, раз у legacy-відрі.
    """
    import json

    from src.execution.soft_start_budget import SoftStartBudget

    p = tmp_path / "b.json"
    p.write_text(json.dumps({
        "max_usdt": 5.0,
        "accounting_version": 0,
        "spot_flow_usdt": -30.0,          # витрачено 30 на монети
        "spot_positions": {"ADA": {"qty": 10.0, "cost": 12.0}},
        "entries": [],
    }))
    b = SoftStartBudget(str(p), 5.0)

    assert b.state.legacy_spot_cost == pytest.approx(18.0), (
        f"legacy={b.state.legacy_spot_cost} — відстежувані 12.0 не відняті")
    assert b.held_spot_value == pytest.approx(30.0), "монети пораховано двічі"


@pytest.mark.asyncio
async def test_http_200_with_a_null_body_is_not_read_as_zero_funds():
    """`data: null` при HTTP 200 — так виглядає протухла сесія. Читати це як
    «монет немає» означає мовчки спинити купівлі і написати хибну причину."""
    from src.execution.webkey.spot_client import (SpotBalancesUnavailable,
                                                  SpotWebClient)

    c = SpotWebClient.__new__(SpotWebClient)
    c._timeout = 1
    c.quote = "USDT"

    class _R:
        status_code = 200

        def json(self):
            return {"code": 401, "data": None}

    c._ensure_session = lambda: type("S", (), {
        "get": staticmethod(lambda *a, **kw: asyncio.sleep(0, result=_R()))})()
    c._headers = lambda: {}

    with pytest.raises(SpotBalancesUnavailable):
        await c.balances(["cid"])


def test_the_final_report_is_fed_the_campaign_counters(monkeypatch):
    """ДІРА §3.3.

    `_final_report` бере підсумки з КАМПАНІЇ (`stats`, `elapsed_hours`), бо
    лічильники репортера обнуляються на кожному рестарті — саме це давало
    «spot 0 buys, Ran for 14.0h» після 3 діб (баг 29.08). Прибирання цих двох
    аргументів лишало сюїту зеленою: єдиний тест, що їх торкався, пінив лише
    ФАКТ виклику звіту, а не його вміст.
    """
    import inspect

    from src.execution import soft_start_runner as ssr

    src = inspect.getsource(ssr._final_report)
    assert "stats=" in src and "elapsed_h=" in src, (
        "фінальний звіт більше не отримує підсумків кампанії — він знову "
        "друкуватиме лічильники репортера, обнулені рестартом")

    captured = {}

    class _Rep:
        async def final_report(self, reason, **kw):
            captured.update(kw)

    class _W:
        slot_id = 1
        reporter = _Rep()
        _final_sent = False
        futures = None
        held_is_measured = True
        campaign = type("C", (), {
            "elapsed_hours": lambda self: 72.0,
            "expired": lambda self: True,
            "state": type("S", (), {"stats": {"spot_buys": 13,
                                              "futures_opens": 7}})(),
        })()
        budget = type("B", (), {"spent": 0.53, "futures_pnl": 0.43,
                                "spot_pnl": -0.01, "pnl": 0.42,
                                "state": type("S", (), {"max_usdt": 5.0,
                                                        "entries": []})()})()

        def _held_spot_value(self):
            return 6.19

    asyncio.run(ssr._final_report(_W(), 1, "campaign finished",
                                  position_left=False))

    assert captured.get("stats", {}).get("spot_buys") == 13, (
        f"лічильники кампанії не дійшли до звіту: {captured.get('stats')}")
    assert captured.get("elapsed_h") == pytest.approx(72.0)


# --------------------------------------------------------------------------
# Поріг 20+20 (рішення оператора 2026-09-02)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_floor_is_applied_per_venue_not_to_the_total(monkeypatch):
    """«20 на споті і 20 на фʼючерсах = 40 разом» тримається РІВНО тому, що
    поріг судить майданчики ОКРЕМО.

    Якби він міряв суму, акаунт із 39 на споті і 1 на фʼючерсах пройшов би
    цілком — і фʼючерсна половина відкривала б позиції з маржею 0.06-0.14,
    які біржа відхиляє. Тест ПРОВОДКИ: ганяє справжній `start()`.
    """
    from src.execution.soft_start_budget import MIN_VIABLE_BALANCE_USDT as FLOOR

    async def run(spot, fut):
        w, _, _ = await _run_start(monkeypatch, free_usdt=spot, coins_usdt=0.0,
                                   drawn=["MX"], fut_usdt=fut)
        return w._spot_viable, w._fut_bal_ok

    # обидва вище порога
    s, f = await run(FLOOR + 5, FLOOR + 5)
    assert (s, f) == (True, True)

    # спот нижче — фʼючерси НЕ мають від цього постраждати, і навпаки
    s, f = await run(FLOOR - 1, FLOOR + 5)
    assert s is False and f is True, "поріг перетік між майданчиками"

    s, f = await run(FLOOR + 5, FLOOR - 1)
    assert s is True and f is False, "поріг перетік між майданчиками"

    # сума 40 при перекосі 39/1 НЕ має проходити — інакше «20+20» це не поріг
    s, f = await run(2 * FLOOR - 1, 1.0)
    assert f is False, "поріг рахує СУМУ — тоді 39/1 пройшло б цілком"


# --------------------------------------------------------------------------
# Денна стеля: продажі її відпускають (рішення оператора 2026-09-02)
# --------------------------------------------------------------------------

def _spot_engine(tmp_path, monkeypatch, *, px=2.0, ceiling=20.0, order=2.0):
    """Справжній SpotSoftStart із застабленими краями (без мережі й біржі)."""
    from src.execution import spot_soft_start as sss

    class _Cur:
        currency_id = "cid"; market_currency_id = "mid"
        price_decimals = 6; quantity_decimals = 6

    class _Res:
        def __init__(self, qty, price):
            self.ok = True; self.dry_run = False
            self.quantity = qty; self.price = price
            self.code = 200; self.raw = {}

    class _Client:
        async def currency(self, t): return _Cur()
        async def balances(self, ids):
            return {"USDT": {"available": 10_000.0}, "MX": {"available": 10_000.0}}
        async def buy(self, token, *, usdt, price): return _Res(usdt / price, price)
        async def sell(self, token, *, quantity, price): return _Res(quantity, price)

    monkeypatch.setattr(sss, "_price_async",
                        lambda symbol: asyncio.sleep(0, result=px))
    cfg = sss.SoftStartConfig(universe=("MX",),
                              state_path=str(tmp_path / "s.json"),
                              order_usdt_min=order, order_usdt_max=order,
                              baseline_usdt_per_token=0.0,
                              daily_buy_usdt_ceiling=ceiling)
    e = sss.SpotSoftStart(_Client(), cfg)
    e.plan.tokens = ["MX"]
    e.plan.buys_done = 0; e.plan.buys_target = 999
    e.plan.sells_done = 0; e.plan.sells_target = 999
    return e


@pytest.mark.asyncio
async def test_a_sell_releases_the_daily_ceiling(tmp_path, monkeypatch):
    """ДЕФЕКТ, ЩО РІЗАВ ДЕННИЙ ПЛАН НА БУДЬ-ЯКОМУ БАЛАНСІ.

    `plan.spent_usdt` тільки РІС, тож стеля міряла ВАЛОВІ купівлі за добу, хоча
    коментар у конфігу описував її так, ніби продажі її звільняють. Наслідок
    арифметичний і не залежав від розміру рахунку: стеля = баланс, середній
    ордер = 8% балансу -> впиралось на ~12 купівлях і на 20 USDT, і на 500,
    тоді як план цілив у 25.
    """
    e = _spot_engine(tmp_path, monkeypatch, ceiling=20.0, order=2.0)

    for _ in range(10):
        assert await e.maybe_buy() is True
    assert e.plan.spent_usdt == pytest.approx(20.0)
    assert await e.maybe_buy() is False, "стеля не спрацювала — тест нічого не міряє"

    assert await e.maybe_sell() is True
    assert e.plan.spent_usdt < 20.0, "продаж не відпустив стелю"
    assert await e.maybe_buy() is True, "після продажу купівля все ще заблокована"


@pytest.mark.asyncio
async def test_selling_old_coins_cannot_lift_the_ceiling_above_the_balance(
        tmp_path, monkeypatch):
    """ПІДЛОГА НА НУЛІ — не косметика.

    Монети накопичуються за ВСЮ історію слота, тож доба могла б початися з
    продажу старих монет і отримати відʼємний `spent`, тобто стелю
    `баланс + продажі` замість `баланс`. Стеля має лишатись стелею.
    """
    e = _spot_engine(tmp_path, monkeypatch, ceiling=20.0, order=2.0)

    for _ in range(5):                       # продаємо «старі» монети першими
        assert await e.maybe_sell() is True
    assert e.plan.spent_usdt == pytest.approx(0.0), (
        f"spent={e.plan.spent_usdt} пішов нижче нуля — стеля роздулась")

    bought = 0
    while await e.maybe_buy():
        bought += 1
        if bought > 30:
            break
    assert bought == 10, (
        f"куплено {bought} ордерів по 2.0 при стелі 20 — стеля перестала бути стелею")
