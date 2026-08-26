"""Спільні фікстури тестів. Головне тут — прибрати РЕАЛЬНІ очікування.

ЗВІДКИ ВЗЯЛОСЬ. Профіль сюїти 2026-08-26: **25 тестів зі 1243 з'їдали 114с зі
144**, тобто 79% часу. Причина не в складності — у справжніх таймаутах:

  * `_poll_close_fill` крутить `while monotonic() < deadline` на 5-8 СЕКУНД.
    У тесті стаб `get_history_positions` завжди порожній, тож філ не знайдеться
    НІКОЛИ — цикл просто вигоряє до дедлайну (13 тестів ≈ 75с);
  * `_poll_fill_price` — те саме на 2с (кілька тестів);
  * фантом-перевірка йде вікнами `[1.2, 2.5, 6, 12]` — тест чесно спить 12с
    (2 тести ≈ 24с).

Жодне з цих очікувань нічого не перевіряє: воно чекає на подію, якої стаб не
дасть за визначенням.

ЧОМУ ФІКС ТУТ, А НЕ В ПРОДАКШН-КОДІ. `rest_timeout_sec` прибиті літералами
(0.5/5.0/8.0) у трьох місцях `live_executor.py`. Винести їх у env було б
зміною ОРДЕРНОГО шляху заради швидкості тестів — такий обмін на грошовому коді
неприйнятний. Тут ми стискаємо очікування ззовні: виконується ТОЙ САМИЙ код,
цикл робить ті самі виклики, коротшає лише дедлайн.

ЯКЩО ТЕСТУ ПОТРІБНІ СПРАВЖНІ ТАЙМАУТИ — познач `@pytest.mark.real_waits`.

ГРАБЛІ, НА ЯКІ Я ВЖЕ НАСТУПИВ (не повторювати): перша версія патчила
`LiveExecutor._poll_close_fill`, а це **функція МОДУЛЯ, не метод**. З
`raising=False` monkeypatch мовчки не зробив нічого, сюїта лишилась 131с, і
все виглядало як «фікс застосовано». Тому нижче — `raising=True` і явна
перевірка `_REQUIRED_NAMES`: якщо щось перейменують, тести впадуть ГУЧНО, а не
тихо сповільняться назад до двох хвилин.
"""
from __future__ import annotations

import pytest

# Стиснуті значення. НЕ нуль: нуль перетворив би цикл на «жодної ітерації», і
# тести перестали б перевіряти те, заради чого написані (фантом — що вікон
# саме чотири; полли — що вони взагалі опитують).
_FAST_POLL_TIMEOUT_SEC = 0.05
_FAST_PHANTOM_DELAYS = [0.001, 0.002, 0.003, 0.004]

# Ім'я -> що це. Якщо будь-чого з цього не стане в модулі, фікстура впаде.
_REQUIRED_NAMES = ("_poll_close_fill", "_poll_fill_price",
                   "PHANTOM_FILL_CHECK_DELAYS_SEC")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_waits: не стискати таймаути — тест перевіряє саме поведінку "
        "на реальному очікуванні",
    )


def _cap(fn, default_timeout):
    """Обгортка, що ріже лише дедлайн. Усередині — справжня функція."""
    async def _wrapped(*a, timeout_sec: float = default_timeout, **kw):
        return await fn(*a,
                        timeout_sec=min(timeout_sec, _FAST_POLL_TIMEOUT_SEC),
                        **kw)
    return _wrapped


@pytest.fixture(autouse=True)
def _fast_waits(request, monkeypatch):
    """Стиснути реальні очікування у КОЖНОМУ тесті, крім помічених.

    Autouse свідомо: інакше кожен новий тест на close-шляху мовчки додавав би
    5 секунд до сюїти, і через рік ми б знову мали двохвилинний прогін.
    """
    if request.node.get_closest_marker("real_waits"):
        return

    try:
        from src.execution import live_executor as _le
    except Exception:  # модуль не імпортується — не наша справа, хай падає тест
        return

    missing = [n for n in _REQUIRED_NAMES if not hasattr(_le, n)]
    assert not missing, (
        f"tests/conftest.py розсинхронізувався з live_executor.py: немає "
        f"{missing}. Полагодь імена, інакше стиснення таймаутів мовчки "
        f"перестане діяти і сюїта поповзе назад до ~144с."
    )

    # 1) Фантом-перевірка: зберігаємо КІЛЬКІСТЬ вікон (це те, що перевіряють
    #    тести), прибираємо лише їхню тривалість.
    monkeypatch.setattr(_le, "PHANTOM_FILL_CHECK_DELAYS_SEC",
                        list(_FAST_PHANTOM_DELAYS))

    # 2) Полли філу: обмежуємо ДЕДЛАЙН, не чіпаючи логіку. Обидва — функції
    #    рівня модуля, тож підміна атрибута модуля ловить і внутрішні виклики.
    monkeypatch.setattr(_le, "_poll_close_fill",
                        _cap(_le._poll_close_fill, 8.0))
    monkeypatch.setattr(_le, "_poll_fill_price",
                        _cap(_le._poll_fill_price, 2.0))
