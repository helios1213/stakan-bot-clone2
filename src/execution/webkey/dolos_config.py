"""Конфіг dolos: тягнути з MEXC замість того, щоб прибивати константами.

ЯК ЦЕ ПРАЦЮЄ У БРАУЗЕРІ (розібрано зі `static.mocortech.com/mx-fingerprintjs/fp.umd.js`,
2026-08-26 — того самого бандла, що будує dolos на сайті):

    POST https://www.mexc.com/ucgateway/device_api/dolos/all_biz_config?mhash=<md5(visitor_id)>
    тіло: {ts, type:0, platform_type:3, product_type:0, scene:0,
           app_v:"", sdk_v:"1.0.0", mtoken:<visitor_id>}
    відповідь: data — base64, AES-GCM: 12 байт nonce + шифротекст + 16 байт тег
    ключ:      _CONFIG_KEY нижче (константа з бандла, однакова для всіх)

Розшифроване — список сцен, кожна з `{scene, chash, parameters, data_upload}`.

ЩО ЦЕ ДАЛО (і чому воно важливіше за сам chash):
  * наш прибитий `chash` 973e5a66… у поточному конфізі **ВІДСУТНІЙ ВЗАГАЛІ** —
    він із давнішого релізу;
  * **жодна** сцена не приймає поля ордера. Усі беруть характеристики пристрою.
    Наш список `mtoken, ts, symbol, side, openType, type, vol, leverage` не
    відповідав жодній серверній сцені НІКОЛИ.

І попри це 16 932 ордери пройшли. Отже MEXC **не перевіряє вміст `p0`** на
`/order/create` — що узгоджується з абляцією 2026-08-12, де ордер прийняли
взагалі без dolos-блоку. Тобто dolos тут — «косметика протоколу», а не
криптографічна умова. Це слід памʼятати, перш ніж лякатись розбіжностей.

ЧОМУ МОДУЛЬ ВСЕ ОДНО ПОТРІБЕН: значення застарівають при кожному релізі
їхнього фронтенду (у знімку видно `sentry-release=v5.40.164`). Раніше це
означало ручний знімок із браузера; тепер оновлюється саме.

БЕЗПЕКА ДЛЯ ТОРГІВЛІ — головне тут:
  * мережа НІКОЛИ не чіпає шлях ордера: конфіг тягнеться фоновою задачею,
    а `get()` віддає те, що вже в памʼяті, синхронно й без await;
  * не вдалось завантажити — беруться `FALLBACK_*` (знімок 2026-08-26),
    тобто поведінка рівно така, як була б без цього модуля;
  * жодного винятку назовні: збій — це `logger.warning`, не зупинка торгівлі.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# Константа з fp.umd.js — ключ, яким MEXC шифрує ВІДПОВІДЬ конфігу.
# Це не секрет акаунта: він однаковий для всіх і лежить у публічному бандлі.
_CONFIG_KEY = b"1b8c71b668084dda9dc0285171ccf753"
_CONFIG_URL = "https://www.mexc.com/ucgateway/device_api/dolos/all_biz_config"
# sdk_v="" дає code=33333 (помилка параметрів); "1.0.0" дає code=0. Перевірено.
_SDK_V = "1.0.0"

# Знімок 2026-08-26 — на випадок, якщо ендпоінт недоступний. Сцени 6/23/24/
# 28/29/30/32/33 віддають ОДИН І ТОЙ САМИЙ chash, тож конкретний номер не
# принциповий; беремо його як «ордерну» сцену.
FALLBACK_CHASH = "d6c64d28e362f314071b3f9d78ff7494d9cd7177ae0465e772d1840e9f7905d8"
FALLBACK_PARAMETERS = [
    "hostname", "member_id", "mhash", "mtoken", "platform_type",
    "product_type", "request_id", "sys", "sys_ver", "tencent_device_token",
]
# Попередній прибитий chash. Лишається для відкату без археології в git:
# на ньому пройшло 16 932 прийнятих ордери.
LEGACY_CHASH = "973e5a66902be9ff97f3e916b71d4535c47b8a30c5f4122a7683d6ef701f30dd"
LEGACY_PARAMETERS = [
    "mtoken", "ts", "symbol", "side", "openType", "type", "vol", "leverage",
]

_ORDER_SCENE = int(os.environ.get("MEXC_DOLOS_SCENE", "28"))


def _decrypt(blob_b64: str) -> str:
    """AES-GCM, як `ze()` у бандлі: nonce(12) + шифротекст + тег(16)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(blob_b64)
    return AESGCM(_CONFIG_KEY).decrypt(raw[:12], raw[12:], None).decode()


def fetch_sync(visitor_id: str, timeout: float = 8.0) -> dict | None:
    """Забрати і розшифрувати конфіг. Блокуюча — викликати ТІЛЬКИ у потоці.

    Повертає {'chash': ..., 'parameters': [...], 'data_upload': 1} або None.
    """
    try:
        from curl_cffi import requests as curl_requests
        mhash = hashlib.md5(visitor_id.encode("utf-8")).hexdigest()
        body = {
            "ts": int(time.time() * 1000), "type": 0, "platform_type": 3,
            "product_type": 0, "scene": 0, "app_v": "", "sdk_v": _SDK_V,
            "mtoken": visitor_id,
        }
        s = curl_requests.Session(impersonate="chrome146")
        try:
            r = s.post(f"{_CONFIG_URL}?mhash={mhash}", json=body, timeout=timeout,
                       headers={"origin": "https://www.mexc.com",
                                "content-type": "application/json",
                                "referer": "https://www.mexc.com/futures/BTC_USDT"})
            payload = r.json()
        finally:
            s.close()
        if payload.get("code") != 0 or not isinstance(payload.get("data"), str):
            logger.warning("[DOLOS CFG] відмова: code=%s msg=%s",
                           payload.get("code"), payload.get("msg"))
            return None
        scenes = json.loads(_decrypt(payload["data"]))
        by_scene = {c.get("scene"): c for c in scenes if isinstance(c, dict)}
        cfg = by_scene.get(_ORDER_SCENE)
        if cfg is None:
            # Сцена зникла — беремо будь-яку з тим самим chash, що й у знімку;
            # так оновлення нумерації сцен не ламає забір.
            cfg = next((c for c in scenes if c.get("chash") == FALLBACK_CHASH), None)
        if not cfg or not cfg.get("chash") or not cfg.get("parameters"):
            logger.warning("[DOLOS CFG] сцену %s не знайдено серед %d",
                           _ORDER_SCENE, len(scenes))
            return None
        return {"chash": cfg["chash"],
                "parameters": list(cfg["parameters"]),
                "data_upload": int(cfg.get("data_upload", 1))}
    except Exception:
        logger.warning("[DOLOS CFG] не вдалось завантажити", exc_info=True)
        return None


class DolosConfigCache:
    """Те, що читає шлях ордера. `get()` СИНХРОННИЙ і без мережі — завжди.

    Оновлюється тільки `refresh()`, який має викликатись фоновою задачею.
    Поки оновлення не сталось (або провалилось) — віддаються FALLBACK-значення,
    тобто поведінка рівно така сама, як була до появи цього модуля.
    """

    def __init__(self) -> None:
        self._chash = FALLBACK_CHASH
        self._parameters = list(FALLBACK_PARAMETERS)
        self._data_upload = 1
        self._fetched_at = 0.0
        self._from_server = False

    def get(self) -> dict:
        return {"chash": self._chash, "parameters": list(self._parameters),
                "data_upload": self._data_upload}

    @property
    def is_from_server(self) -> bool:
        return self._from_server

    @property
    def age_sec(self) -> float:
        return (time.time() - self._fetched_at) if self._fetched_at else -1.0

    def apply(self, cfg: dict | None) -> bool:
        """Прийняти новий конфіг. Порожній/битий — мовчки ігнорується."""
        if not cfg or not cfg.get("chash") or not cfg.get("parameters"):
            return False
        changed = (cfg["chash"] != self._chash
                   or list(cfg["parameters"]) != self._parameters)
        was_from_server = self._from_server
        self._chash = cfg["chash"]
        self._parameters = list(cfg["parameters"])
        self._data_upload = int(cfg.get("data_upload", 1))
        self._fetched_at = time.time()
        self._from_server = True
        if changed:
            # Гучно: зміна chash означає новий реліз їхнього фронтенду, і це
            # рівно той момент, коли прибиті константи почали б застарівати.
            logger.warning("[DOLOS CFG] конфіг ОНОВЛЕНО: chash=%s… полів=%d",
                           self._chash[:16], len(self._parameters))
        elif not was_from_server:
            # ПЕРШИЙ успішний забір, що збігся зі знімком. Без цього рядка
            # «усе працює» і «задача мертва» виглядають у логах ОДНАКОВО —
            # рівно та тиха успішність, на якій цей проєкт уже горів.
            logger.info("[DOLOS CFG] підтверджено з сервера: chash=%s… полів=%d "
                        "(збігається зі знімком)",
                        self._chash[:16], len(self._parameters))
        return True


CACHE = DolosConfigCache()
