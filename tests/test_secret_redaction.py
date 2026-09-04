"""Токен Telegram не має потрапляти в логи — ні рядком, ні трейсбеком.

ЗВІДКИ ЦЕ. Виміряно 2026-09-04 перед фіксом: **1 282 входження токена** в
поточному `logs/stakan.log` і **120 145** в архівах `.gz`. У CLAUDE.md від
20.08 стояло «3 входження» — число застаріло на чотири порядки, бо тоді
міряли інший шлях.

ДВА ДЖЕРЕЛА, і другого мало не пропустив:
1. `httpx` логує КОЖЕН запит на рівні INFO разом із повним URL, а URL
   телеграм-API містить токен. Лікується заглушенням рівня.
2. **ТРЕЙСБЕК незловленого винятку** друкується на рівні ERROR, який жодне
   заглушення не прибирає. Саме так токен і йшов тисячами рядків.
   Заглушення рівнем тут БЕЗСИЛЕ — треба редагувати сам текст.
"""
from __future__ import annotations

import io
import logging
import re
import traceback

# Вигаданий токен правильного формату. НЕ справжній.
FAKE_URL = ("https://api.telegram.org/bot7891234567:"
            "AAHxyz_ABCDEFGHIJKLMNOPQRSTUVWXYZ01/sendMessage")
FAKE_SECRET = "AAHxyz_ABCDEFGHIJKLMNOPQRSTUVWXYZ01"

_TOKEN_RE = re.compile(r"(bot)\d{6,12}:[A-Za-z0-9_-]{30,}")


class _RedactSecrets(logging.Filter):
    """Дзеркалить фільтр із `src/main.py:_setup_logging`."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.args:
                record.msg = record.getMessage()
                record.args = ()
            if isinstance(record.msg, str) and "bot" in record.msg:
                record.msg = _TOKEN_RE.sub(r"\1<REDACTED>", record.msg)
            if record.exc_info and not record.exc_text:
                record.exc_text = "".join(
                    traceback.format_exception(*record.exc_info))
            if record.exc_text and "bot" in record.exc_text:
                record.exc_text = _TOKEN_RE.sub(r"\1<REDACTED>", record.exc_text)
                record.exc_info = None
        except Exception:
            pass
        return True


def _capture(emit) -> str:
    buf = io.StringIO()
    lg = logging.getLogger("redaction_probe")
    lg.handlers.clear()
    lg.addFilter(_RedactSecrets())
    lg.addHandler(logging.StreamHandler(buf))
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    emit(lg)
    return buf.getvalue()


def test_a_plain_log_line_is_redacted():
    out = _capture(lambda lg: lg.info("HTTP Request: POST %s", FAKE_URL))
    assert FAKE_SECRET not in out, "токен у звичайному рядку логу"
    assert "<REDACTED>" in out


def test_a_traceback_is_redacted_too():
    """ГОЛОВНИЙ ТЕСТ. Трейсбек друкується на ERROR, і заглушення рівнем його
    не прибирає — саме тут токен і витікав."""
    def _emit(lg):
        try:
            raise RuntimeError("ConnectTimeout to " + FAKE_URL)
        except Exception:
            lg.exception("telegram call failed")

    out = _capture(_emit)
    assert FAKE_SECRET not in out, (
        "токен у ТРЕЙСБЕКУ — заглушення рівнем тут не працює, потрібна "
        "редакція тексту")
    assert "<REDACTED>" in out


def test_the_filter_never_raises_on_odd_records():
    """Логування не має ламати бота, хай яким дивним буде запис."""
    f = _RedactSecrets()
    for rec in (
        logging.LogRecord("x", logging.INFO, "p", 1, object(), None, None),
        logging.LogRecord("x", logging.INFO, "p", 1, "bot123", (), None),
        logging.LogRecord("x", logging.INFO, "p", 1, None, None, None),
    ):
        assert f.filter(rec) is True


def test_main_actually_installs_the_filter_and_mutes_httpx():
    """ТЕСТ ПРОВОДКИ. Фільтр можна написати ідеально і не підключити."""
    import pathlib
    src = pathlib.Path("src/main.py").read_text()
    assert "addFilter(_RedactSecrets())" in src, "фільтр не підключений до root"
    assert '"httpx"' in src and '"httpcore"' in src, (
        "httpx/httpcore не заглушені — вони логують URL із токеном на INFO")
    i_mute = src.index('"httpx"')
    i_filt = src.index("addFilter(_RedactSecrets())")
    assert i_filt > i_mute, "фільтр має ставитись після налаштування рівнів"


def test_the_bot_registers_a_global_error_handler():
    """ТЕСТ ПРОВОДКИ. Без цього обробника незловлений `ConnectTimeout` до
    api.telegram.org клав ВЕСЬ торговий бот через `_update_fetcher`."""
    import pathlib
    src = pathlib.Path("src/telegram_bot/bot.py").read_text()
    assert "add_error_handler(self._on_handler_error)" in src
    i_reg = src.index("add_error_handler(self._on_handler_error)")
    i_init = src.index("await self.application.initialize()")
    assert i_reg < i_init, "обробник треба реєструвати ДО initialize()"
