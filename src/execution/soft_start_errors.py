"""Відмови біржі в soft-start -> повідомлення оператору в Telegram (запит 2026-09-15).

ДО ЦЬОГО soft-start писав відмову лише в лог сервера і пробував далі: «перевірка особи», risk control чи
KYC оператор бачив хіба що за тим, що прогрів тихо перестав купувати. Розпізнавання блоку акаунта було
лише в арбітражному виконавці (`live_executor.classify_account_block`) — ТОЙ САМИЙ класифікатор тут і
вживається, щоб два тексти для однієї відмови не розійшлись.

Рушії (спот/фʼючерси) про Telegram не знають: вони лише записують відмову в `RejectionLog`, а раннер
після кожного тіку забирає записи й вирішує, про що сказати:
  * блок акаунта (код 6001/6002/6026/6028 або фраза risk control / verification / KYC / frozen ...) —
    одразу, не частіше разу на BLOCK_THROTTLE_SEC для того самого ярлика слота;
  * будь-яка інша відмова — лише коли ТА САМА (майданчик, дія, код) повторилась REPEAT_N разів за
    REPEAT_WINDOW_SEC: разова відмова (мінімальний ноціонал, рух ціни) — звичайна річ і не варта пуша.
Поведінку прогріву це НЕ змінює: він пробує далі, як і раніше.
"""
from __future__ import annotations

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

REJECTION_LOG_MAX = 50
REPEAT_N = 3
REPEAT_WINDOW_SEC = 1800.0
BLOCK_THROTTLE_SEC = 1800
REPEAT_THROTTLE_SEC = 3600


class RejectionLog:
    """Буфер відмов біржі одного рушія. Раннер забирає його після кожного тіку."""

    def __init__(self) -> None:
        self._q: deque = deque(maxlen=REJECTION_LOG_MAX)

    def note(self, venue: str, op: str, symbol: str | None, code, msg) -> None:
        self._q.append({"venue": venue, "op": op, "symbol": symbol or "?",
                        "code": None if code is None else str(code), "msg": str(msg or ""),
                        "ts": time.time()})

    def note_resp(self, venue: str, op: str, symbol: str | None, resp) -> None:
        r = resp if isinstance(resp, dict) else {}
        self.note(venue, op, symbol, r.get("code"), r.get("msg") or r.get("message") or "")

    def drain(self) -> list[dict]:
        out = list(self._q)
        self._q.clear()
        return out


def classify(code, msg) -> str | None:
    from .live_executor import classify_account_block      # лінивий імпорт: важкий модуль арбітражу
    return classify_account_block(code, msg)


class RejectionAlerter:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._recent: dict[tuple, deque] = {}

    async def process(self, items: list[dict], alerts, slot_id: int) -> list[str]:
        """Повертає тексти, які спробували надіслати (для логів і тестів)."""
        sent: list[str] = []
        now = self._clock()
        for r in items:
            head = f"{r['venue']} {r['op']} {r['symbol']}"
            raw = f"code={r['code']} {r['msg'][:160]}".strip()
            label = classify(r["code"], r["msg"])
            if label:
                text = (f"🚨 SOFT-START слот {slot_id}: {label}\n"
                        f"Біржа відхилила: {head} — {raw}\n"
                        f"Прогрів пробує далі. Якщо MEXC просить перевірку — пройди її в застосунку.")
                logger.error("soft-start slot %d: блок акаунта (%s) — %s: %s", slot_id, label, head, raw)
                if await _send(alerts, text, f"softstart_block_s{slot_id}_{label}", BLOCK_THROTTLE_SEC):
                    sent.append(text)
                continue
            key = (r["venue"], r["op"], r["code"])
            q = self._recent.setdefault(key, deque())
            q.append(r["ts"] or now)
            while q and now - q[0] > REPEAT_WINDOW_SEC:
                q.popleft()
            if len(q) >= REPEAT_N:
                text = (f"⚠️ SOFT-START слот {slot_id}: біржа {len(q)} рази за {int(REPEAT_WINDOW_SEC // 60)} хв "
                        f"відхилила {r['venue']} {r['op']}\nОстання: {r['symbol']} — {raw}")
                logger.warning("soft-start slot %d: повторні відмови %s %s: %s", slot_id, r["venue"], r["op"], raw)
                if await _send(alerts, text, f"softstart_rej_s{slot_id}_{r['venue']}_{r['op']}_{r['code']}",
                               REPEAT_THROTTLE_SEC):
                    sent.append(text)
                q.clear()
        return sent


async def _send(alerts, text: str, category: str, throttle: int) -> bool:
    if alerts is None:
        return False
    try:
        return bool(await alerts.send(text, category=category, throttle_sec=throttle,
                                      suppress_during_quiet=False))
    except Exception:
        logger.debug("soft-start rejection alert failed", exc_info=True)
        return False
