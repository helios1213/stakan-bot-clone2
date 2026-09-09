"""Попереджувальний сторож комісії: спинити торгівлю ДО платного філу.

НАВІЩО, якщо вже є fee-guard. Наявний `LiveExecutor._trip_fee_guard` реактивний:
він спрацьовує, КОЛИ філ уже повернувся з комісією. Тобто ціна виявлення —
одна платна угода (2026-08-25 це коштувало $0.407004 на PEPE). Цей модуль
питає біржу НАПРЯМУ і халтить слот, поки жодного платного філу ще не було.

ЧОМУ ЦЕ ВЗАГАЛІ МОЖЛИВО (перевірено 2026-08-26 на двох акаунтах підряд):
    акаунт із промо:   PEPE 0/0        SOXL 0/0        BTC 0/0.0002
    акаунт без промо:  усі пари 0.0001/0.0004,  feeRateType=BASE
Тобто `/account/tiered_fee_rate/v2` віддає ПРАВДУ. Раніше я тричі вирішив, що
він бреше, — насправді слот тримав інший акаунт. Тому нижче в лог завжди йде
`walletBalance`: це єдиний спосіб побачити, що питаєш ТОЙ САМИЙ акаунт.

ТРИ ПРАВИЛА БЕЗПЕКИ, без яких сторож шкідливіший за свою користь:

1. **Помилка мережі — НЕ комісія.** Таймаут, 5xx, рейт-ліміт, порожня відповідь
   не халтять нічого. Інакше блимання мережі зупиняло б торгівлю. У CLAUDE.md
   це вже записано кров'ю: «a rate-limited None is NOT a non-zero fee».

2. **Потрібні ДВА поспіль ненульові читання.** Одне може бути збоєм на боці
   біржі; халт на одному читанні означав би зупинку торгівлі через глюк.
   Два поспіль на інтервалі 60с — це 2 хвилини затримки, і все одно на
   порядки раніше за платний філ.

3. **Питаємо ПАРУ, ЯКОЮ ТОРГУЄМО, а не BTC.** `BTC_USDT` навіть із діючим
   промо має `taker 0.0002` — сторож на ньому давав би ПОСТІЙНУ хибну тривогу.
   Це саме та помилка, що була в браузерному скрипті оператора.

ЩО САМЕ ПЕРЕВІРЯЄМО: `realMakerFee` І `realTakerFee` (не `original*`). Наші IOC
свідомо перетинають спред, тобто платять ТЕЙКЕРА — нульового мейкера замало.
`real*` враховують знижки; `original*` — лише базову сітку.
"""
from __future__ import annotations

import logging
import os
from src.env_config import env_float, env_int

logger = logging.getLogger(__name__)

# ДВА ІНТЕРВАЛИ, бо ризик несиметричний.
#
# Коли слот ОЗБРОЄНИЙ (live_enabled + призначена пара), ордер може піти будь-якої
# миті, і кожна секунда затримки — це шанс на платний філ. Коли лайв вимкнено,
# поспішати нікуди: ми все одно нічого не відправляємо.
#
# ЧОМУ НЕ 1с, як у браузерному скрипті: добу по секунді — 86 400 запитів, зайвий
# привід для рейт-ліміту. 10с при одному живому слоті — це 8 640/добу.
#
# ЧЕСНО ПРО МЕЖУ ЦЬОГО ВАЖЕЛЯ. Під час активної торгівлі нас і так рятує
# `push.personal.order`: комісія приходить у мить філу, і реактивний guard
# халтить на ПЕРШОМУ платному ордері. Тобто опитування виграє лише у вузькому
# вікні «тариф уже змінився, а наступний ордер ще не пішов». При кулдауні SOXL
# 30/30 ордери йдуть раз на ~33с, тож 10с там справді встигають; при PEPE 2/3
# (ордер раз на ~8с) — здебільшого ні. Довести до нуля платних ордерів можна
# лише перевіркою ПЕРЕД кожним ордером, а це +19мс на критичний шлях, і такий
# обмін відкинуто свідомо: дві комісійні події на 16 932 ордери, $0.43 сумарно.
POLL_SEC = env_float("FEE_WATCHDOG_SEC", 60.0, lo=1.0)               # у простої
ACTIVE_SEC = env_float("FEE_WATCHDOG_ACTIVE_SEC", 10.0, lo=1.0)      # слот озброєний
# Скільки ненульових читань поспіль потрібно, щоб халтити (див. правило 2).
CONFIRMATIONS = env_int("FEE_WATCHDOG_CONFIRM", 2, lo=1)
ENABLED = os.environ.get("FEE_WATCHDOG", "1").strip().lower() not in (
    "0", "false", "no", "off")

_EPS = 1e-12


class FeeWatchdog:
    """Стан сторожа. Лічильник підтверджень живе тут, а не в циклі, щоб
    перезапуск циклу не скидав його непомітно."""

    def __init__(self) -> None:
        self._strikes: dict[int, int] = {}

    async def check_slot(self, slot_id: int, client, symbol_mexc: str,
                         executor=None) -> dict:
        """Одна перевірка. Повертає словник для логу; НІЧОГО не кидає.

        `executor` — той, у кого є `_trip_fee_guard`. None означає «лише
        подивитись», без халту (використовується в тестах і для діагностики).
        """
        out = {"slot": slot_id, "symbol": symbol_mexc, "ok": False,
               "maker": None, "taker": None, "balance": None,
               "strikes": self._strikes.get(slot_id, 0), "halted": False}
        # ПРОБА. Оператор скинув превентивний халт, щоб перевірити тариф
        # РЕАЛЬНИМ ордером. Якби ми далі опитували тариф, то халтнули б знову
        # за ~20с (10с × 2 підтвердження) — і жоден живий ордер не встиг би
        # статись, бо він чекає на сигнал детектора, а не йде негайно.
        # Мовчимо, доки проба діє; вирок винесе філ, а не тариф.
        try:
            if executor is not None and executor.fee_probe_active():
                out["probe"] = True
                return out
        except Exception:
            pass
        try:
            resp = await client._request(
                "GET", f"/account/tiered_fee_rate/v2?symbol={symbol_mexc}",
                needs_web_sign=True)
            data = (resp or {}).get("data") or {}
            maker = data.get("realMakerFee")
            taker = data.get("realTakerFee")
            if maker is None or taker is None:
                # Правило 1: немає даних — це НЕ комісія. Лічильник НЕ рухаємо:
                # інакше серія таймаутів накопичила б халт без жодного читання.
                logger.debug("[FEE WATCH] slot %d %s: відповідь без ставок",
                             slot_id, symbol_mexc)
                return out
            out.update(ok=True, maker=float(maker), taker=float(taker),
                       balance=data.get("walletBalance"))
        except Exception:
            logger.debug("[FEE WATCH] slot %d %s: запит не вдався",
                         slot_id, symbol_mexc, exc_info=True)
            return out

        nonzero = abs(out["maker"]) > _EPS or abs(out["taker"]) > _EPS
        if not nonzero:
            if self._strikes.get(slot_id):
                logger.info("[FEE WATCH] slot %d %s: ставки знову нульові — "
                            "лічильник скинуто", slot_id, symbol_mexc)
            self._strikes[slot_id] = 0
            out["strikes"] = 0
            return out

        n = self._strikes.get(slot_id, 0) + 1
        self._strikes[slot_id] = n
        out["strikes"] = n
        logger.warning(
            "[FEE WATCH] slot %d %s: НЕНУЛЬОВА ставка maker=%s taker=%s "
            "(баланс %s) — підтвердження %d/%d",
            slot_id, symbol_mexc, out["maker"], out["taker"], out["balance"],
            n, CONFIRMATIONS)
        if n < CONFIRMATIONS:
            return out

        if executor is None:
            return out
        try:
            # Той самий шлях, що й у реактивного guard: халт у памʼяті,
            # вимкнення слота в БД, пара в shadow, алерт у Telegram.
            # fee_usdt=0.0 — платного філу НЕ БУЛО, і саме в цьому суть:
            # ми зупиняємось до нього.
            await executor._trip_fee_guard(symbol_mexc, 0.0, preventive=True)
            out["halted"] = True
            logger.critical(
                "🚨 [FEE WATCH] slot %d ЗУПИНЕНО ПРЕВЕНТИВНО: %s maker=%s "
                "taker=%s (баланс %s). Платного філу не було.",
                slot_id, symbol_mexc, out["maker"], out["taker"], out["balance"])
        except Exception:
            logger.exception("[FEE WATCH] slot %d: халт не вдався", slot_id)
        return out


WATCHDOG = FeeWatchdog()
