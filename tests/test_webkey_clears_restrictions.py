# -*- coding: utf-8 -*-
"""Видалення або переклеювання вебкея = чистий слот (рішення оператора 09.09).

ВИМІРЯНО НА КЛОНІ 2026-09-09, слот 1:
    18:40:19  🚨 FEE GUARD (ПРЕВЕНТИВНО) — тариф ненульовий, walletBalance=0
    ~18:48    оператор вставив НОВИЙ вебкей
    18:51+    66 ордерів поспіль: [LIVE OPEN FAIL] ... fee_guard_halted

Тобто новий ключ полагодив сесію (тариф одразу став нульовим), але слот усе
одно не торгував: халт живе в ПАМʼЯТІ екзекутора, `cmd_webkey` інвалідував
лише кеш клієнта, а `rebuild_from_store` екзекутор не перестворює. Жоден із
тих 66 ордерів навіть не полетів на біржу.

ЩО ПІНИТЬСЯ:
  * чистяться всі НАШІ блокування слота, і функція чесно каже, що саме зняла;
  * НЕ чіпається `live_enabled` — вмикати живі гроші у відповідь на вставлений
    ключ бот не має права;
  * НЕ чіпається кіл просадки — він про збитки, а не про ключ;
  * ПРОВОДКА: обидва шляхи (нова вставка і видалення) справді це кличуть.
"""
import types

import pytest

from src.execution.live_executor import LiveExecutor
from src.execution.live_pool import LiveExecutorPool
from src.telegram_bot import cmd_webkey as cw

RK = "Position opening is unavailable until risk control verification is completed"


def _executor(slot_id=1):
    e = LiveExecutor.__new__(LiveExecutor)
    e.slot_id = slot_id
    e._halted = False
    e._halt_was_preventive = False
    e._fee_probe_until = 0.0
    e.slot_level_error = None
    e.slot_level_error_at_ts = 0
    e.account_block = None
    e.account_block_msg = None
    e.account_block_at_ts = 0.0
    e.last_error = None
    e.alerts = None
    e.webkey_store = None
    return e


class _Store:
    def __init__(self):
        self.cleared = []
        self.live_calls = []

    async def clear_slot_error(self, slot_id):
        self.cleared.append(slot_id)

    async def set_live_enabled(self, slot_id, on):      # має лишитись невикликаним
        self.live_calls.append((slot_id, on))


def _pool(ex=None, store=None):
    p = LiveExecutorPool.__new__(LiveExecutorPool)
    p._executors = {1: ex} if ex is not None else {}
    p._safety_controllers = {}
    p.webkey_store = store
    return p


# ------------------------------------------------------------ що знімається

@pytest.mark.asyncio
async def test_it_clears_the_fee_guard_halt():
    """Рівно той стан, через який 66 ордерів не полетіли на біржу."""
    ex = _executor()
    ex._halted = True
    ex._halt_was_preventive = True
    ex._fee_probe_until = 1e18
    store = _Store()

    cleared = await _pool(ex, store).clear_slot_restrictions(1, reason="новий вебкей")

    assert ex._halted is False
    assert ex._halt_was_preventive is False
    assert ex._fee_probe_until == 0.0
    assert any("fee-guard" in c for c in cleared)
    assert store.cleared == [1], "рядок помилки в панелі лишився б висіти"


@pytest.mark.asyncio
async def test_it_clears_the_account_level_error_and_block():
    ex = _executor()
    ex.slot_level_error = RK
    ex.slot_level_error_at_ts = 123
    ex._note_account_block("6026", RK)

    cleared = await _pool(ex, _Store()).clear_slot_restrictions(1, reason="новий вебкей")

    assert ex.slot_level_error is None and ex.slot_level_error_at_ts == 0
    assert ex.account_block_fresh() is None
    assert any("блок акаунта" in c for c in cleared)


@pytest.mark.asyncio
async def test_it_reports_HONESTLY_when_nothing_was_set():
    """«Знято обмеження» над чистим слотом — це напівправда, яку оператор
    прочитає як «щось лагодилось»."""
    cleared = await _pool(_executor(), _Store()).clear_slot_restrictions(1, reason="x")
    assert cleared == []


@pytest.mark.asyncio
async def test_a_slot_without_an_executor_still_clears_the_panel_line():
    store = _Store()
    cleared = await _pool(None, store).clear_slot_restrictions(2, reason="x")
    assert store.cleared == [2]
    assert cleared == []


# --------------------------------------------------- чого воно НЕ чіпає

@pytest.mark.asyncio
async def test_it_does_NOT_enable_live_trading():
    """ГОЛОВНА МЕЖА. Вставлений ключ не є дозволом торгувати живими грошима —
    `live_enabled` лишається вимикачем оператора."""
    ex = _executor()
    ex._halted = True
    store = _Store()

    await _pool(ex, store).clear_slot_restrictions(1, reason="новий вебкей")

    assert store.live_calls == [], "бот сам увімкнув живу торгівлю"


@pytest.mark.asyncio
async def test_it_does_NOT_release_the_drawdown_kill():
    """Кіл — про ЗБИТКИ, а не про ключ. У нього власна кнопка."""
    released = []

    class _Ctl:
        def release_kill(self):
            released.append(True)
            return (True, "x")

    p = _pool(_executor(), _Store())
    p._safety_controllers = {1: _Ctl()}

    await p.clear_slot_restrictions(1, reason="новий вебкей")

    assert released == [], "знято запобіжник просадки"


# ------------------------------------------------------------- ПРОВОДКА

def _ctx(pool, store):
    app = types.SimpleNamespace(bot_data={"live_pool": pool, "webkey_store": store,
                                          "webkey_client_pool": None})
    sent = []

    async def send_message(**kw):
        sent.append(kw.get("text", ""))

    return types.SimpleNamespace(application=app, bot=types.SimpleNamespace(
        send_message=send_message)), sent


class _Msg:
    async def delete(self):
        pass

    async def reply_text(self, *a, **k):
        pass


def _upd():
    return types.SimpleNamespace(message=_Msg(),
                                 effective_user=types.SimpleNamespace(id=1),
                                 effective_chat=types.SimpleNamespace(id=1))


class _WkStore(_Store):
    def __init__(self):
        super().__init__()
        self.saved = []
        self.deleted = []

    async def set_webkey(self, slot_id, text):
        self.saved.append(slot_id)

    async def delete(self, slot_id):
        self.deleted.append(slot_id)
        return True


@pytest.mark.asyncio
async def test_PASTING_a_new_webkey_clears_the_halt():
    """ГОЛОВНИЙ ТЕСТ ПРОВОДКИ — саме сценарій 09.09.

    Виконує реальний `_step_webkey`, а не пінить рядок: без цього виклику
    функція вище була б ідеальною і невживаною.
    """
    ex = _executor()
    ex._halted = True
    store = _WkStore()
    pool = _pool(ex, store)
    ctx, sent = _ctx(pool, store)
    fsm = cw._WizardState(step=cw.STEP_WEBKEY, slot_id=1)

    await cw._step_webkey(_upd(), ctx, "WEB" + "a" * 64, fsm)

    assert store.saved == [1], "ключ не збережено — тест міряє не те"
    assert ex._halted is False, "новий вебкей не зняв халт"
    assert any("Знято обмеження" in t for t in sent), "оператору не сказали"


@pytest.mark.asyncio
async def test_DELETING_a_webkey_clears_the_halt():
    ex = _executor()
    ex._halted = True
    ex.slot_level_error = RK
    store = _WkStore()
    pool = _pool(ex, store)
    ctx, _ = _ctx(pool, store)
    fsm = cw._WizardState(step=cw.STEP_REMOVE_CONFIRM, target_slot_id=1)

    await cw._step_remove_confirm(_upd(), ctx, "yes", fsm)

    assert store.deleted == [1]
    assert ex._halted is False and ex.slot_level_error is None


@pytest.mark.asyncio
async def test_a_CANCELLED_removal_clears_nothing():
    """Контроль: без нього тест вище проходив би і на коді, що чистить завжди."""
    ex = _executor()
    ex._halted = True
    store = _WkStore()
    ctx, _ = _ctx(_pool(ex, store), store)
    fsm = cw._WizardState(step=cw.STEP_REMOVE_CONFIRM, target_slot_id=1)

    await cw._step_remove_confirm(_upd(), ctx, "no", fsm)

    assert store.deleted == []
    assert ex._halted is True, "скасоване видалення зняло халт"


def test_the_menu_delete_button_clears_too():
    """Третій шлях — інлайн-кнопка в меню, інший файл.

    Джерело інспектується свідомо: гілка сидить усередині великого
    роутера callback'ів, підняти який у тесті дорожче, ніж він вартий.
    Але без перевірки цей шлях лишився б єдиним, що чистить не все.
    """
    import inspect

    from src.telegram_bot import bot as botmod
    src = inspect.getsource(botmod)
    # Якір саме на гілку ВИДАЛЕННЯ: перше входження `m:webkey:remove:` — це
    # екран підтвердження, і тест на ньому мовчки міряв би не те.
    i = src.find('data.startswith("m:webkey:remove:") and data.endswith(":yes")')
    assert i > 0, "гілку видалення не знайдено — перейменували callback?"
    branch = src[i:i + 1200]
    assert "_clear_restrictions(context, sid" in branch, "кнопка меню не чистить слот"
    assert "invalidate(sid)" in branch, "кешований клієнт лишається зі старим ключем"
