# -*- coding: utf-8 -*-
"""Халт із «комісією $0.000000» мусить називати СПРАВЖНЮ причину.

ВИМІРЯНО (primary, слот 2, 2026-09-04):
    15:01:47  [IOC OPEN] slot-level error code=6026
              msg=Position opening is unavailable until risk control
                  verification is completed
    ... ще 7 таких відмов ...
    15:02:06  🚨 FEE GUARD: slot 2 PEPE_USDT fill charged fee=$0.000000

Тобто зупинила нас перевірка обличчя на MEXC, а повідомлення говорило про
комісію — і оператор ішов шукати промо замість того, щоб пройти перевірку.

ЩО ПІНИТЬСЯ:
  * класифікація акаунт-рівневих відмов (включно з 6002/6028/Help Center,
    яких `is_slot_level_error` не ловить);
  * коди звіряються окремим полем, а не пошуком числа в тексті;
  * ПРОВОДКА: обидва шляхи відкриття записують блок, обидва шляхи успіху
    його гасять;
  * свіжий блок перебиває текст про тариф, СТАРИЙ — ні (інакше алерт
    пояснював би сьогоднішній халт учорашньою подією);
  * реактивний халт (є справжня комісія) не зачеплено.
"""
import pytest

from src.execution.live_executor import (ACCOUNT_BLOCK_FRESH_SEC, LiveExecutor,
                                         classify_account_block,
                                         is_slot_level_error)

RK = "Position opening is unavailable until risk control verification is completed"


def _ex(slot_id=1):
    """Виконавець без __init__ — рівно ті поля, що читає цей механізм."""
    e = LiveExecutor.__new__(LiveExecutor)
    e.slot_id = slot_id
    e._halted = False
    e._halt_was_preventive = False
    e._fee_probe_until = 0.0
    e.account_block = None
    e.account_block_msg = None
    e.account_block_at_ts = 0.0
    e.alerts = None
    e.webkey_store = None
    return e


class _Alerts:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kw):
        self.sent.append(text)


# --------------------------------------------------------- класифікація

@pytest.mark.parametrize("code,msg,needle", [
    ("6026", RK, "risk control"),
    (None, RK, "risk control"),
    ("6002", "Position opening is forbidden. Please contact Customer Service",
     "заборонено"),
    ("6028", "After the platform's risk review, ...", "ризик-перевірка"),
    (None, "Please go to Help Center to submit information", "Help Center"),
    (None, "identity verification required", "identity verification"),
])
def test_account_blocks_are_named(code, msg, needle):
    label = classify_account_block(code, msg)
    assert label is not None, f"не розпізнано: {code} {msg}"
    assert needle in label


def test_the_open_rate_throttle_is_NOT_an_account_block():
    """9082/10014/2036 минають самі за хвилини — акаунт при цьому справний.

    Назвати їх «блоком» означало б відправити оператора проходити
    верифікацію там, де треба просто зачекати.
    """
    assert classify_account_block("9082", "Frequent operation") is None
    assert classify_account_block("10014", "Too many requests") is None


def test_a_bare_number_in_the_text_does_NOT_trigger():
    """Голе число збіглося б із ціною або id ордера.

    Та сама пастка вже була з «401» — тому коди звіряються ОКРЕМИМ полем.
    """
    assert classify_account_block(None, "filled at 6002.5") is None
    assert classify_account_block(None, "orderId=6028771823") is None


def test_it_covers_MORE_than_the_retry_gate():
    """`is_slot_level_error` навмисно НЕ розширено — він гейтить ретраї.

    Тому класифікатор мусить бути ширшим за нього, інакше 6002/6028 і далі
    лишались би без імені.
    """
    forbidden = "Position opening is forbidden. Please contact Customer Service"
    assert is_slot_level_error(forbidden) is False, (
        "гейт ретраїв змінився — це вже зміна торгівлі, а не тексту")
    assert classify_account_block("6002", forbidden) is not None


# --------------------------------------------------------- памʼять про блок

def test_a_block_is_remembered_and_expires():
    e = _ex()
    e._note_account_block("6026", RK)
    assert e.account_block_fresh() is not None
    t = e.account_block_at_ts
    assert e.account_block_fresh(now=t + ACCOUNT_BLOCK_FRESH_SEC - 1) is not None
    assert e.account_block_fresh(now=t + ACCOUNT_BLOCK_FRESH_SEC + 1) is None


def test_an_unrelated_error_is_not_remembered():
    e = _ex()
    e._note_account_block("2005", "Balance insufficient")
    assert e.account_block_fresh() is None


def test_a_successful_open_clears_it():
    e = _ex()
    e._note_account_block("6026", RK)
    e._clear_account_block()
    assert e.account_block_fresh() is None


# --------------------------------------------------------- ПРОВОДКА алерта

@pytest.mark.asyncio
async def test_a_preventive_halt_DURING_a_block_names_the_block():
    """ГОЛОВНИЙ ТЕСТ. Саме цього бракувало 04.09."""
    e = _ex()
    e.alerts = _Alerts()
    e._note_account_block("6026", RK)
    await e._trip_fee_guard("PEPE_USDT", 0.0, preventive=True)

    text = e.alerts.sent[0]
    assert "risk control" in text, "не названо перевірку особи"
    assert RK in text, "не показано текст біржі"
    assert "не падіння комісії" in text
    assert "тариф брехав" not in text, "стара версія тексту перемогла"


@pytest.mark.asyncio
async def test_a_preventive_halt_WITHOUT_a_block_keeps_the_tariff_text():
    """Контроль. Без нього тест вище проходив би і на тексті, що завжди
    кричить про верифікацію — тобто брехав би в інший бік."""
    e = _ex()
    e.alerts = _Alerts()
    await e._trip_fee_guard("PEPE_USDT", 0.0, preventive=True)

    text = e.alerts.sent[0]
    assert "ТАРИФ ПОКАЗАВ КОМІСІЮ" in text
    assert "risk control" not in text


@pytest.mark.asyncio
async def test_a_STALE_block_does_not_explain_todays_halt():
    e = _ex()
    e.alerts = _Alerts()
    e._note_account_block("6026", RK)
    e.account_block_at_ts -= ACCOUNT_BLOCK_FRESH_SEC + 60      # учорашня подія
    await e._trip_fee_guard("PEPE_USDT", 0.0, preventive=True)

    assert "ТАРИФ ПОКАЗАВ КОМІСІЮ" in e.alerts.sent[0]


@pytest.mark.asyncio
async def test_a_REACTIVE_halt_still_reports_the_real_fee():
    """Тут комісію справді зняли — блок акаунта цього не скасовує."""
    e = _ex()
    e.alerts = _Alerts()
    e._note_account_block("6026", RK)
    await e._trip_fee_guard("PEPE_USDT", 0.074010)

    text = e.alerts.sent[0]
    assert "FEE DETECTED" in text
    assert "0.074010" in text


@pytest.mark.asyncio
async def test_the_panel_line_says_it_too():
    """Оператор частіше бачить рядок у панелі, ніж алерт."""
    class _Store:
        def __init__(self):
            self.err = None

        async def set_live_enabled(self, *a):
            pass

        async def set_slot_error(self, slot_id, text):
            self.err = text

        async def _pair_has_another_slot(self, *a):
            return True

    e = _ex()
    e.alerts = _Alerts()
    e.webkey_store = _Store()
    e._note_account_block("6026", RK)
    await e._trip_fee_guard("PEPE_USDT", 0.0, preventive=True)

    assert "НЕ через комісію" in e.webkey_store.err
    assert "risk control" in e.webkey_store.err


# --------------------------------------------- ПРОВОДКА шляхів відкриття

def test_both_open_paths_record_and_both_success_paths_clear():
    """Сторож проводки: формула може бути ідеальною і невживаною.

    Джерело інспектується свідомо — довести це виконанням означало б
    протягнути ордер крізь мережу, WS-пул і півсотні гілок. Але без цієї
    перевірки блок ніколи не записався б, і алерт мовчав би про причину.
    """
    import inspect
    src = inspect.getsource(LiveExecutor)
    assert src.count("self._note_account_block(code, msg)") == 2, \
        "обидва шляхи відкриття мають записувати блок"
    assert src.count("self._clear_account_block()") == 2, \
        "обидва шляхи успіху мають гасити блок"
