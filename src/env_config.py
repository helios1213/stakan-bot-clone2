"""Читання числових налаштувань з env так, щоб одруківка не клала бота.

ЧОМУ ЦЕ ОКРЕМИЙ МОДУЛЬ. У `src/` було 13 місць виду

    POLL_SEC = float(os.environ.get("FEE_WATCHDOG_SEC", "60"))

на РІВНІ МОДУЛЯ. Кожне з них означає: одруківка в `docker-compose.yml`
(`FEE_WATCHDOG_SEC=6O` з літерою O, зайвий пробіл, `10s`) підіймає
`ValueError` під час ІМПОРТУ — тобто бот не стартує взагалі, а в логах лежить
трейсбек із рядка, який до торгівлі стосунку не має. Для налаштування, яке в
найгіршому разі мало б відкотитись до дефолту, ціна невідповідна.

`ShadowEngine._env_float` уже робив саме це — але лише для себе. Тут та сама
ідея, доступна всім, плюс попередження в лог: тихо підставити дефолт означало
б, що оператор думає, ніби його значення діє.

МЕЖІ (`lo`/`hi`) — не косметика. `TWIN_TAPE_INTERVAL_MS=0` давало
`await asyncio.sleep(0)` у нескінченному циклі, тобто busy-loop на єдиному
ядрі; `TWIN_TAPE_LEN=0` — `deque(maxlen=0)`, який мовчки нічого не памʼятає і
при цьому виглядає робочим. Число, що технічно парситься, теж буває отруйним.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _clamped(name: str, value: float, lo, hi) -> float:
    if lo is not None and value < lo:
        logger.warning("[ENV] %s=%s нижче межі %s — беру %s", name, value, lo, lo)
        return lo
    if hi is not None and value > hi:
        logger.warning("[ENV] %s=%s вище межі %s — беру %s", name, value, hi, hi)
        return hi
    return value


def env_float(name: str, default: float, *, lo=None, hi=None) -> float:
    """float з env; непарсабельне -> дефолт + WARNING, ніколи не виняток."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return _clamped(name, float(default), lo, hi)
    try:
        return _clamped(name, float(str(raw).strip()), lo, hi)
    except (TypeError, ValueError):
        logger.warning("[ENV] %s=%r не число — беру дефолт %s", name, raw, default)
        return _clamped(name, float(default), lo, hi)


def env_int(name: str, default: int, *, lo=None, hi=None) -> int:
    """int з env. Дробове значення НЕ помилка — обрізається до int.

    `SESSION_TTL=3600.0` цілком очікуване від людини; падати через це нема за що.
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return int(_clamped(name, float(default), lo, hi))
    try:
        return int(_clamped(name, float(str(raw).strip()), lo, hi))
    except (TypeError, ValueError):
        logger.warning("[ENV] %s=%r не число — беру дефолт %s", name, raw, default)
        return int(_clamped(name, float(default), lo, hi))
