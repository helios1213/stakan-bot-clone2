# -*- coding: utf-8 -*-
"""Тег (мітка) слота в Telegram — як «Мітка (ім'я)» у вебпанелі (запит оператора 2026-09-14).

Поведінкові тести: виконується реальний майстер (`_step_webkey`, `webkey_text_handler`) і реальний
`callback_router` з фейковими update/context. Пиниться:
  * після вставки ключа майстер ПРОПОНУЄ тег, але ключ уже збережено — тег необовʼязковий;
  * наступне повідомлення стає тегом (те саме поле `label`, що в панелі), довгий — не зберігається;
  * кнопка постійної клавіатури на кроці тегу НЕ стає тегом, а спрацьовує як завжди;
  * «🏷 Тег» у меню слота, «Лишити/Без тегу» і «Прибрати тег» роблять рівно своє.
"""
import types

import pytest

from src.telegram_bot import bot as B
from src.telegram_bot import cmd_webkey as cw


class _Slot:
    def __init__(self, slot_id, label=None, empty=False):
        self.slot_id, self.label, self.is_empty = slot_id, label, empty
        self.is_complete = not empty
        self.live_enabled = False
        self.assigned_pair = None

    def masked_webkey(self):
        return "WEB…abcd"


class _Store:
    def __init__(self, label=None, empty=False):
        self.slot = _Slot(1, label, empty)
        self.labels = []
        self.saved = []

    async def set_webkey(self, slot_id, text):
        self.saved.append(slot_id)

    async def set_label(self, slot_id, label):
        self.labels.append((slot_id, label))
        self.slot.label = label.strip()[:50] if label else None
        return True

    async def get(self, slot_id):
        return self.slot


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.chat_id = 7

    async def delete(self):
        pass

    async def reply_text(self, text, **k):
        self.replies.append((text, k))


def _ctx(store):
    sent = []

    async def send_message(**kw):
        sent.append(kw)

    data = {"webkey_store": store, "live_pool": None, "webkey_client_pool": None,
            "state_manager": None, "shadow_engine": None}
    app = types.SimpleNamespace(bot_data=data)
    return types.SimpleNamespace(application=app, bot_data=data,
                                 bot=types.SimpleNamespace(send_message=send_message)), sent


def _upd(text=""):
    return types.SimpleNamespace(message=_Msg(text), effective_user=types.SimpleNamespace(id=1),
                                 effective_chat=types.SimpleNamespace(id=7))


def _query(data):
    async def answer(*a, **k):
        pass
    return types.SimpleNamespace(data=data, answer=answer, from_user=types.SimpleNamespace(id=1), message=_Msg())


@pytest.fixture(autouse=True)
def _clean_fsm():
    cw._reset(1)
    yield
    cw._reset(1)


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_pasting_a_key_offers_an_optional_tag_and_the_next_message_becomes_it():
    store = _Store()
    ctx, sent = _ctx(store)
    await cw._step_webkey(_upd(), ctx, "WEB" + "a" * 64, cw._WizardState(step=cw.STEP_WEBKEY, slot_id=1))
    assert store.saved == [1], "ключ не збережено ДО кроку тегу"
    assert cw._fsm(1).step == cw.STEP_LABEL and cw._fsm(1).slot_id == 1
    prompt = sent[-1]
    assert "Тег слота 1" in prompt["text"] and ("Без тегу", "m:webkey:label_skip:1") in _buttons(prompt["reply_markup"])
    upd = _upd("acc-main")
    assert await cw.webkey_text_handler(upd, ctx, passthrough=B.KEYBOARD_BUTTONS) is True
    assert store.labels == [(1, "acc-main")] and cw._fsm(1).is_idle()
    assert "acc-main" in upd.message.replies[-1][0]


@pytest.mark.asyncio
async def test_repaste_shows_the_current_tag_and_offers_to_keep_or_clear_it():
    text, kb = cw.label_prompt(1, "acc_1")
    btns = _buttons(kb)
    assert "acc_1" in text and ("Лишити «acc_1»", "m:webkey:label_skip:1") in btns
    assert ("🧹 Прибрати тег", "m:webkey:label_clear:1") in btns
    text2, kb2 = cw.label_prompt(1, "<b>x</b>")
    assert "<b>x</b>" not in text2, "тег не екранується в HTML"


@pytest.mark.asyncio
async def test_keyboard_button_on_the_tag_step_is_not_saved_as_a_tag():
    store = _Store()
    ctx, _ = _ctx(store)
    cw._enter_step(1, cw.STEP_LABEL, 1)
    assert await cw.webkey_text_handler(_upd("📊 Menu"), ctx, passthrough=B.KEYBOARD_BUTTONS) is False
    assert store.labels == [] and cw._fsm(1).is_idle(), "кнопка меню стала тегом"


@pytest.mark.asyncio
async def test_too_long_tag_is_rejected_and_the_step_stays():
    store = _Store()
    ctx, _ = _ctx(store)
    cw._enter_step(1, cw.STEP_LABEL, 1)
    upd = _upd("x" * 51)
    assert await cw.webkey_text_handler(upd, ctx, passthrough=B.KEYBOARD_BUTTONS) is True
    assert store.labels == [] and cw._fsm(1).step == cw.STEP_LABEL
    assert "задовгий" in upd.message.replies[-1][0]


def test_slot_menu_has_tag_button_only_for_a_configured_slot():
    assert ("🏷 Тег", "m:webkey:label:1") in _buttons(B._kb_webkey_slot(_Slot(1)))
    assert not any("label" in (d or "") for _, d in _buttons(B._kb_webkey_slot(_Slot(1, empty=True))))


def test_keyboard_buttons_constant_matches_the_real_keyboard():
    real = {b.text for row in B._kb_persistent().keyboard for b in row}
    assert real == set(B.KEYBOARD_BUTTONS), f"клавіатура розійшлась із KEYBOARD_BUTTONS: {real ^ set(B.KEYBOARD_BUTTONS)}"


@pytest.mark.asyncio
async def test_tag_button_skip_and_clear_callbacks():
    store = _Store(label="old")
    ctx, sent = _ctx(store)
    await B.callback_router(types.SimpleNamespace(callback_query=_query("m:webkey:label:1")), ctx)
    assert cw._fsm(1).step == cw.STEP_LABEL and "old" in sent[-1]["text"], "кнопка «🏷 Тег» не запустила крок"
    q = _query("m:webkey:label_skip:1")
    await B.callback_router(types.SimpleNamespace(callback_query=q), ctx)
    assert cw._fsm(1).is_idle() and store.labels == [] and "без змін" in q.message.replies[-1][0]
    cw._enter_step(1, cw.STEP_LABEL, 1)
    q = _query("m:webkey:label_clear:1")
    await B.callback_router(types.SimpleNamespace(callback_query=q), ctx)
    assert store.labels == [(1, None)] and cw._fsm(1).is_idle() and "прибрано" in q.message.replies[-1][0]
    empty = _Store(empty=True)
    ctx2, sent2 = _ctx(empty)
    q = _query("m:webkey:label:1")
    await B.callback_router(types.SimpleNamespace(callback_query=q), ctx2)
    assert cw._fsm(1).is_idle() and "спершу встав webkey" in q.message.replies[-1][0]
