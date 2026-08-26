"""
MexcWebClient — async HTTP client for MEXC futures via webkey (v5: webkey-only).

Multi-slot edition: each MexcWebClient instance is bound to ONE WebkeySlot.
For parallel trading across slots, instantiate one client per slot and
fan out requests with asyncio.gather.

Auth model:
    - webkey from slot       → Authorization header + u_id cookie
    - visitor_id from slot   → mtoken header + mtoken body field
    - mhash = MD5(visitor)   → mhash URL param + mhash body field
    - chash = bootstrap      → chash body field (constant for all users)
    - trochilus-uid = "0"    → header (placeholder; server doesn't validate)

Akamai cookies are fetched on demand via cold GET /futures/{symbol}, then
cached in the AsyncSession's cookie jar (per-instance — no cross-slot
contamination).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from curl_cffi import requests as curl_requests

from . import device_profile, dolos_config
from .credentials import BOOTSTRAP_CHASH, WebkeySlot
from .signing import sign_dolos, sign_web

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MexcClientError(Exception):
    """Base for client errors (network, invalid creds, server rejection)."""


class MexcAuthError(MexcClientError):
    """Server returned auth/cookie failure (e.g., expired webkey, Akamai block)."""


class MexcServerError(MexcClientError):
    """Server returned non-zero code with a recognisable error msg."""

    def __init__(self, code: Any, msg: str, payload: dict | None = None) -> None:
        super().__init__(f"code={code} msg={msg}")
        self.code = code
        self.msg = msg
        self.payload = payload


# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

# ТРИ МІСЦЯ, ЯКІ МУСЯТЬ РУХАТИСЬ РАЗОМ: TLS-ціль curl_cffi (_CHROME_IMPERSONATE),
# User-Agent і sec-ch-ua. Зсунути одне — означає СТВОРИТИ розсинхрон, якого
# зараз немає: саме неузгодженість «TLS каже 136, заголовок каже 151» і є тим,
# що фінгерпринт-системи бачать найлегше. Тому версія тут одна змінна.
# MEXC_CHROME_VER дозволяє перевірити іншу версію без зміни коду; ціль має
# існувати в curl_cffi (є chrome124/131/133a/136/142/145/146).
# 146, бо це найновіша ціль, яку знає curl_cffi (реальний браузер оператора —
# 147, знімок 2026-08-26). Рівність не потрібна, потрібна УЗГОДЖЕНІСТЬ трьох
# джерел і близькість до реального; 136 відставав на 11 версій.
_CHROME_VER = os.environ.get("MEXC_CHROME_VER", "146").strip() or "146"
_CHROME_IMPERSONATE = f"chrome{_CHROME_VER}"
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{_CHROME_VER}.0.0.0 Safari/537.36"
)
# Формат ДОСЛІВНО з живого знімка браузера (2026-08-26):
#   "Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"
# У нас було `Google Chrome, Chromium, Not.A/Brand` з v="99" — і інший порядок,
# і застаріла GREASE-версія. Обидва відхилення видно простим порівнянням рядка.
_DEFAULT_SEC_CH_UA = (
    f'"Google Chrome";v="{_CHROME_VER}", "Not.A/Brand";v="8", '
    f'"Chromium";v="{_CHROME_VER}"'
)
_DEFAULT_SEC_CH_UA_PLATFORM = '"macOS"'

_DEFAULT_DOLOS_PARAMETERS = [
    "mtoken", "ts", "symbol", "side",
    "openType", "type", "vol", "leverage",
]

_WARMUP_SYMBOL = "ZEC_USDT"
_DEFAULT_COOKIE_MAX_AGE_SEC = 600
# BASE_URL is the WEB origin — used ONLY for the origin/referer headers, which
# must keep looking like the browser front-end (futures.mexc.com).
_BASE_URL = "https://futures.mexc.com"

# API_URL is where requests are actually SENT. Measured A/B 2026-08-12 from the
# Tokyo box, alternating hosts, signed IOCs on /order/create:
#     futures.mexc.com   min 189.1  med 193.5  p90 210.1
#     contract.mexc.com  min 139.7  med 143.3  p90 154.5
# Non-overlapping distributions — ~50ms systematically, and futures is also the
# unstable one (its authenticated read median swung 28ms -> 72ms between runs
# while contract held ~28ms). Both hosts return code=0 with origin/referer left
# pointing at futures, which is exactly the configuration measured above.
# contract.mexc.com is already the host the private WS uses (wss://.../edge).
# Override with MEXC_API_HOST to revert without a code change.
_API_URL = os.environ.get("MEXC_API_HOST", "https://contract.mexc.com").rstrip("/")

# Чи підписувати /order/create блоком dolos. ДЕФОЛТ — УВІМКНЕНО (2026-08-25).
#
# Історія. Абляція 2026-08-12 показала, що MEXC ПРИЙМАЄ ордер і без dolos, і
# його прибрали заради швидкості. 2026-08-25 оператор вирішив повернути: акаунт
# втратив 0% промо, і його підозра — що «голий» шлях відрізняється від
# браузерного і це помітно збоку.
#
# ВАРТІСТЬ ВИМІРЯНА, а не оцінена: sign_dolos p50=1.708мс, p90=2.945, p99=4.624
# (300 прогонів у контейнері). Проти реального HTTP RTT до MEXC p50=152мс це
# ~1% шляху. Тобто «пару мілісекунд», заради яких його колись прибрали, — це
# 1.7мс, і компромісу тут фактично немає.
#
# ЧЕСНО ПРО ПРИЧИННІСТЬ: доказів, що dolos-drop спричинив втрату промо, НЕМАЄ,
# і є три виміри проти (клон мав dolos-drop із 13.08 і 24 303 угоди без жодної
# комісії; перше спрацювання fee-guard на primary — 17.08, тобто ДО того, як
# dolos-drop туди приїхав; а тариф читається з /account/tiered_fee_rate як
# властивість АКАУНТА, не запиту). Повертаємо як рішення оператора при
# вимірювано мізерній ціні, а не як доведений фікс.
#
# ── РЕЖИМ ШЛЯХУ: один перемикач замість пʼяти окремих змінних ────────────
#
#   MEXC_PATH_MODE=full   (дефолт) — «браузерний»: dolos на /order/create,
#       живий конфіг (chash + поля пристрою), профіль пристрою на слот,
#       заголовки під живий знімок браузера. Ціна: ~3.9мс на ордер
#       (1.7мс наш підпис + 2.2мс на боці біржі).
#
#   MEXC_PATH_MODE=bare — «голий»: НАЙКОРОТШИЙ доведений шлях. Без dolos на
#       /order/create (абляція 12.08: біржа приймає плоске тіло + web-sign),
#       тобто -3.9мс. Усе інше — TLS-профіль, заголовки, referer за парою,
#       новий chash — ЛИШАЄТЬСЯ: воно нічого не коштує за часом, а
#       `close_all_positions` у будь-якому разі шле dolos, і саме там новий
#       chash і застосовується.
#
# Окремі змінні (MEXC_DOLOS_ON_ORDER тощо) перебивають режим — щоб можна було
# зібрати проміжну комбінацію, не чіпаючи код.
_PATH_MODE = os.environ.get("MEXC_PATH_MODE", "full").strip().lower() or "full"
if _PATH_MODE not in ("full", "bare"):
    logger.warning("[PATH MODE] невідомий режим %r — беру 'full'", _PATH_MODE)
    _PATH_MODE = "full"

# MEXC_DOLOS_ON_ORDER перебиває режим; без нього рішення ухвалює режим.
_DOLOS_ON_ORDER = (
    os.environ.get("MEXC_DOLOS_ON_ORDER", "").strip().lower() not in (
        "0", "false", "no", "off")
    if os.environ.get("MEXC_DOLOS_ON_ORDER", "").strip()
    else _PATH_MODE == "full")

logger.info("[PATH MODE] %s — dolos на /order/create: %s",
            _PATH_MODE, "ТАК" if _DOLOS_ON_ORDER else "ні")

# trochilus-uid — у браузері це РЕАЛЬНИЙ uid акаунта (знімок 2026-08-26 показав
# 8-значне число). Ми роками слали буквальний "0": сервер значення не валідує,
# але константа "0" — це найдешевший можливий маркер «це не браузер».
# Тепер: якщо MEXC_TROCHILUS_UID не заданий, заголовок НЕ надсилається зовсім.
# Відсутній заголовок — слабший сигнал, ніж явно синтетичне значення.
# Значення НЕ секрет (це публічний id акаунта), але воно РІЗНЕ на двох ботах,
# тому живе в env кожного, а не в коді.
_TROCHILUS_UID = os.environ.get("MEXC_TROCHILUS_UID", "").strip()


# Повна емуляція dolos: класти в p0 САМЕ ті поля, яких вимагає серверна сцена
# під нашим chash, а не поля ордера.
#
# ЩО З'ЯСУВАЛОСЬ ІЗ ЖИВОГО КОНФІГУ (2026-08-26, `dolos_config.py`):
#   * наш прибитий chash 973e5a66… у поточному конфізі ВІДСУТНІЙ — старий реліз;
#   * ЖОДНА сцена не приймає поля ордера. Усі 5 сцен беруть характеристики
#     ПРИСТРОЮ. Наш список (symbol/side/vol/…) не відповідав жодній сцені ніколи.
#   * і попри це пройшло 16 932 ордери -> MEXC не перевіряє ВМІСТ p0 на
#     /order/create (узгоджується з абляцією 12.08: ордер приймають і без dolos).
#
# ЧЕСНО ПРО МЕЖУ ТОЧНОСТІ. Які поля потрібні — ми знаємо ТОЧНО (з конфігу).
# Які там мають бути ЗНАЧЕННЯ — ні: щоб побачити browser p0, потрібен приватний
# RSA-ключ MEXC. Тому `sys`/`sys_ver` виведені з нашого ж User-Agent (щоб не
# суперечити заголовкам), а `tencent_device_token` порожній — його видає SDK
# Tencent, якого в нас немає. Вигадувати правдоподібне сміття тут гірше, ніж
# лишити порожнім: порожнє поле пояснюється, а неправильний токен — ні.
# MEXC_DOLOS_LEGACY=1 повертає стару схему (chash 973e5a66… + поля ордера).
_DOLOS_LEGACY = os.environ.get("MEXC_DOLOS_LEGACY", "").strip().lower() in (
    "1", "true", "yes", "on")


def _new_request_id() -> str:
    """Формат узято з кукі браузера `x_fingerprint_requestId=1787685214220.at2lp1`:
    мілісекунди + крапка + 6 символів [a-z0-9]."""
    import random
    import string
    tail = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{int(time.time() * 1000)}.{tail}"


@dataclass
class _DolosRuntime:
    """Runtime dolos config — auto-derived from a WebkeySlot, no user input."""
    visitor_id: str
    mhash: str
    chash: str = BOOTSTRAP_CHASH
    parameters: list[str] = field(default_factory=lambda: list(_DEFAULT_DOLOS_PARAMETERS))

    @classmethod
    def from_visitor(cls, visitor_id: str) -> _DolosRuntime:
        mhash = hashlib.md5(visitor_id.encode("utf-8")).hexdigest()
        return cls(visitor_id=visitor_id, mhash=mhash)

    @property
    def as_signing_dict(self) -> dict[str, Any]:
        if not _DOLOS_LEGACY:
            # Живий конфіг. `get()` синхронний і без мережі — оновлює його
            # фонова задача, шлях ордера ніколи не чекає на HTTP.
            cfg = dolos_config.CACHE.get()
            return {
                "chash": cfg["chash"],
                "mtoken": self.visitor_id,
                "mhash": self.mhash,
                "parameters": cfg["parameters"],
                "data_upload": cfg["data_upload"],
            }
        # Legacy: доведено робоча пара «старий chash + поля ордера».
        # Саме її пройшли 16 932 ордери, тож це і є відкат.
        return {
            "chash": dolos_config.LEGACY_CHASH,
            "mtoken": self.visitor_id,
            "mhash": self.mhash,
            "parameters": list(dolos_config.LEGACY_PARAMETERS),
            "data_upload": 1,
        }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class MexcWebClient:
    """Async MEXC futures Web API client with cookie auto-warmup.

    Bound to a single WebkeySlot. Use `MexcWebClient.from_slot(slot)` as
    the standard factory.
    """

    BASE_URL = _BASE_URL      # web origin — origin/referer headers only
    API_URL = _API_URL        # where requests are actually sent
    API_BASE = "/api/v1/private"

    def __init__(
        self,
        webkey: str,
        visitor_id: str,
        slot_id: int | None = None,
        impersonate: str = _CHROME_IMPERSONATE,
        timeout: int = 10,
        cookie_max_age_sec: int = _DEFAULT_COOKIE_MAX_AGE_SEC,
        user_agent: str = _DEFAULT_UA,
        sec_ch_ua: str = _DEFAULT_SEC_CH_UA,
        sec_ch_ua_platform: str = _DEFAULT_SEC_CH_UA_PLATFORM,
    ) -> None:
        self.webkey = webkey
        self.dolos = _DolosRuntime.from_visitor(visitor_id)
        self.slot_id = slot_id  # for logging only
        # Свій СТАБІЛЬНИЙ профіль пристрою на слот. До 2026-08-26 усі слоти
        # обох ботів (до чотирьох акаунтів) ходили одним і тим самим UA/ОС/TLS —
        # чотири акаунти виглядали одним пристроєм. Профіль детермінований від
        # visitor_id: НЕ рандомний і НЕ змінюється при рестарті, бо у реального
        # браузера відбиток стабільний, і саме стрибки були б аномалією.
        # MEXC_DEVICE_PROFILE=0 повертає єдиний спільний профіль (відкат).
        self._profile = None
        if os.environ.get("MEXC_DEVICE_PROFILE", "1").strip().lower() not in (
                "0", "false", "no", "off"):
            try:
                # За НОМЕРОМ СЛОТА, не за visitor_id: ротація вебкея не має
                # міняти пристрій (людина перезаходить на тому ж компʼютері),
                # а зсув за слотом гарантує різні профілі різним слотам.
                self._profile = (device_profile.for_slot(slot_id)
                                 if slot_id is not None
                                 else device_profile.for_visitor(visitor_id))
            except Exception:
                logger.warning("[DEVICE] профіль не побудовано", exc_info=True)
        if self._profile is not None:
            # Явно передані аргументи мають пріоритет: тести й ручні проби
            # задають їх свідомо.
            if impersonate == _CHROME_IMPERSONATE:
                impersonate = self._profile.impersonate
            if user_agent == _DEFAULT_UA:
                user_agent = self._profile.user_agent
            if sec_ch_ua == _DEFAULT_SEC_CH_UA:
                sec_ch_ua = self._profile.sec_ch_ua
            if sec_ch_ua_platform == _DEFAULT_SEC_CH_UA_PLATFORM:
                sec_ch_ua_platform = self._profile.sec_ch_ua_platform
        self.impersonate = impersonate
        self.timeout = timeout
        self.cookie_max_age_sec = cookie_max_age_sec
        self.user_agent = user_agent
        self.sec_ch_ua = sec_ch_ua
        self.sec_ch_ua_platform = sec_ch_ua_platform

        self._session: curl_requests.AsyncSession | None = None
        self._lock = asyncio.Lock()
        self._warmed_at: float = 0.0

    @classmethod
    def from_slot(cls, slot: WebkeySlot, **kwargs) -> MexcWebClient:
        """Build a client from a complete (webkey + visitor_id) slot."""
        if slot.webkey is None:
            raise MexcClientError(f"Slot {slot.slot_id} has no webkey")
        if slot.visitor_id is None:
            raise MexcClientError(
                f"Slot {slot.slot_id} has no visitor_id — re-run /webkey_setup"
            )

        return cls(
            webkey=slot.webkey,
            visitor_id=slot.visitor_id,
            slot_id=slot.slot_id,
            **kwargs,
        )

    # ---- async context ----
    async def __aenter__(self) -> MexcWebClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception as e:
                logger.debug("Session close raised %s — ignored", e)
            self._session = None
            self._warmed_at = 0.0

    async def _ensure_session(self) -> curl_requests.AsyncSession:
        if self._session is not None:
            return self._session
        async with self._lock:
            if self._session is None:
                session = curl_requests.AsyncSession(
                    impersonate=self.impersonate,
                    timeout=self.timeout,
                )
                # App-auth cookies (webkey -> u_id/uc_token). No network.
                # Akamai cookies (_abck/bm_sz) are NOT fetched: the private
                # host enforces them on neither reads nor /order/create
                # (probe 2026-08-12). The cold warmup GET is gone and can no
                # longer leak onto the order hot path.
                session.cookies.set("u_id", self.webkey, domain=".mexc.com")
                session.cookies.set("uc_token", self.webkey, domain=".mexc.com")
                self._session = session
                self._warmed_at = time.monotonic()
        return self._session

    # ---- cookie warmup (neutered) ----
    async def warmup(self, symbol: str = _WARMUP_SYMBOL, force: bool = False) -> None:
        """No-op retained for API compatibility.

        Previously did a cold GET /futures/{symbol} to collect Akamai cookies
        (_abck/bm_sz). Empirically the private host enforces them on neither
        reads nor /order/create (probe 2026-08-12), so the network warmup is
        gone — cookie acquisition can no longer land on the order hot path.
        Ensuring the session (with its app-auth cookies, seeded once in
        _ensure_session) is all that remains. `symbol` and `force` are kept in
        the signature so existing callers (pool.start, health_check,
        get_account_pnl_usdt) need no change.
        """
        await self._ensure_session()

    # ---- headers ----
    def _device_payload(self) -> dict[str, Any]:
        """Поля ПРИСТРОЮ, яких вимагає серверна сцена dolos.

        Точний перелік беремо з живого конфігу; тут — лише значення. Кожне
        виведене так, щоб НЕ суперечити нашим власним заголовкам: інакше ми
        створили б рівно ту неузгодженість, заради усунення якої все це й
        робиться (UA каже macOS, а p0 каже Windows — це гірше, ніж нічого).

        `tencent_device_token` свідомо ПОРОЖНІЙ: його видає SDK Tencent, якого
        в нас немає. Вигадати правдоподібний токен неможливо, а неправильний
        гірший за відсутній.
        `member_id` — публічний uid акаунта, різний на двох ботах, тому з env
        (та сама змінна, що й для заголовка trochilus-uid).
        """
        host = self.BASE_URL.split("//", 1)[-1]
        return {
            "hostname": host,
            "member_id": _TROCHILUS_UID,
            "platform_type": 3,
            "product_type": 0,
            "request_id": _new_request_id(),
            "sys": self._sys_name(),
            "sys_ver": self._sys_version(),
            "tencent_device_token": "",
        }

    def _sys_name(self) -> str:
        """ОС із НАШОГО ж User-Agent — щоб p0 не суперечив заголовку."""
        if self._profile is not None:
            return self._profile.sys
        ua = self.user_agent
        if "Macintosh" in ua or "Mac OS" in ua:
            return "Mac OS"
        if "Windows" in ua:
            return "Windows"
        if "Linux" in ua:
            return "Linux"
        return ""

    def _sys_version(self) -> str:
        """Версія ОС звідти ж: `Intel Mac OS X 10_15_7` -> `10.15.7`."""
        if self._profile is not None:
            return self._profile.sys_ver
        import re as _re
        m = _re.search(r"Mac OS X (\d+[_.]\d+(?:[_.]\d+)?)", self.user_agent)
        if m:
            return m.group(1).replace("_", ".")
        m = _re.search(r"Windows NT ([\d.]+)", self.user_agent)
        return m.group(1) if m else ""

    def _common_headers(self, with_layer2_sign: dict[str, str] | None = None,
                        symbol: str | None = None) -> dict[str, str]:
        # referer МУСИТЬ вказувати на сторінку тієї пари, яку ми торгуємо.
        # Досі тут завжди стояв _WARMUP_SYMBOL (ZEC_USDT) — тобто ордер на SOXL
        # ішов із заголовком «я на сторінці ZEC». Знімок браузера 2026-08-26
        # підтвердив: там referer = сторінка ТІЄЇ САМОЇ пари. Це була найдешевша
        # для виявлення розбіжність з усіх, які ми мали.
        _ref_sym = symbol or _WARMUP_SYMBOL
        h = {
            "accept": "*/*",
            "accept-language": (self._profile.accept_language
                                if self._profile is not None
                                else "en-US,en;q=0.9"),
            "authorization": self.webkey,
            "content-type": "application/json",
            "language": "English",
            "mtoken": self.dolos.visitor_id,
            "origin": self.BASE_URL,
            "platform": "H5-web",
            "pragma": "akamai-x-cache-on",
            "priority": "u=1, i",
            "referer": f"{self.BASE_URL}/futures/{_ref_sym}?type=linear_swap",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self.sec_ch_ua_platform,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self.user_agent,
            "x-language": "en-US",
        }
        if _TROCHILUS_UID:
            h["trochilus-uid"] = _TROCHILUS_UID
        if with_layer2_sign:
            h.update(with_layer2_sign)
        return h

    # ---- request core ----
    async def _request(
        self,
        method: str,
        endpoint: str,
        body: dict[str, Any] | None = None,
        query_params: str = "",
        needs_dolos: bool = False,
        needs_web_sign: bool = False,
    ) -> dict[str, Any]:
        # latency-probe-v1: granular timing instrumentation.
        # Logs per-stage latency at DEBUG level always, and at INFO for the
        # hot path /order/create (so it shows up in normal logs).
        # Counters: t0=enter, t1=after warmup, t2=after dolos sign,
        # t3=after web sign + header build, t4=after HTTP roundtrip,
        # t5=after JSON parse.
        t0 = time.perf_counter_ns()

        # No warmup() on the hot path — the private host enforces no Akamai
        # cookies (probe 2026-08-12). _ensure_session seeds app cookies once.
        session = await self._ensure_session()
        t1 = time.perf_counter_ns()

        url = f"{self.API_URL}{self.API_BASE}{endpoint}"
        if query_params:
            url += f"?{query_params}" if "?" not in url else f"&{query_params}"

        full_body = body
        if needs_dolos and body is not None:
            # Запобіжник: dolos — це ПІДПИС, а не торгове рішення. Якщо його не
            # вдалось зібрати (порожній visitor_id, зіпсований конфіг), ордер
            # має піти БЕЗ нього, а не впасти: MEXC приймає і плоске тіло
            # (абляція 2026-08-12), тож деградація безпечна. Раніше цей шлях на
            # /order/create не виконувався взагалі (needs_dolos=False), тож
            # виняток тут ніхто не ловив — а тепер це гарячий шлях із грошима.
            # Мовчати НЕ можна: logger.error, інакше деградація стане невидимою.
            try:
                if not getattr(self.dolos, "visitor_id", None):
                    raise ValueError("visitor_id порожній")
                # Кандидати на вміст p0. `sign_dolos` сам відбере з них ті, що
                # перелічені в `parameters` серверної сцени, — тож зайве тут
                # нешкідливе, а поля ордера лишаються для legacy-режиму.
                input_payload = {
                    **body,
                    "mtoken": self.dolos.visitor_id,
                    "mhash": self.dolos.mhash,
                    **self._device_payload(),
                }
                dolos_sig = sign_dolos(input_payload, self.dolos.as_signing_dict)
                full_body = {**body, **dolos_sig}

                sep = "&" if "?" in url else "?"
                url += f"{sep}mhash={dolos_sig['mhash']}"
            except Exception:
                logger.error(
                    "[DOLOS] %s: підпис не зібрано — ордер іде БЕЗ dolos", endpoint,
                    exc_info=True)
                full_body = body
        t2 = time.perf_counter_ns()

        web_headers: dict[str, str] = {}
        if needs_web_sign and full_body is not None:
            web_headers = sign_web(full_body, self.webkey)

        # Символ беремо з тіла запиту — так referer сам іде за парою, яку
        # торгуємо, і не треба прокидати його через кожен виклик.
        _sym = None
        try:
            if isinstance(body, dict):
                _sym = body.get("symbol") or None
        except Exception:
            _sym = None
        headers = self._common_headers(web_headers, symbol=_sym)

        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": self.timeout,
        }
        t3 = time.perf_counter_ns()

        try:
            if method == "POST":
                kwargs["json"] = full_body
                response = await session.post(url, **kwargs)
            else:
                response = await session.get(url, **kwargs)
        except Exception as e:
            raise MexcClientError(f"Network error on {method} {endpoint}: {e}") from e
        t4 = time.perf_counter_ns()

        try:
            payload = response.json()
        except Exception as e:
            raise MexcClientError(
                f"Non-JSON response on {method} {endpoint}: status={response.status_code}"
            ) from e
        t5 = time.perf_counter_ns()

        # Emit timings. /order/create is the latency-critical hot path —
        # log it at INFO so it surfaces in `docker compose logs` without
        # bumping global log level. Everything else stays DEBUG.
        warmup_ms = (t1 - t0) / 1e6
        dolos_ms = (t2 - t1) / 1e6
        sign_ms = (t3 - t2) / 1e6
        http_ms = (t4 - t3) / 1e6
        parse_ms = (t5 - t4) / 1e6
        total_ms = (t5 - t0) / 1e6
        log_msg = (
            "[LATPROBE] %s %s slot=%s warmup=%.1f dolos=%.1f sign=%.1f "
            "http=%.1f parse=%.1f TOTAL=%.1f"
        )
        log_args = (
            method, endpoint, self.slot_id,
            warmup_ms, dolos_ms, sign_ms, http_ms, parse_ms, total_ms,
        )
        if endpoint == "/order/create":
            logger.info(log_msg, *log_args)
        else:
            logger.debug(log_msg, *log_args)

        return payload

    # ---- account ----
    async def get_account_assets(self) -> dict[str, Any]:
        return await self._request("GET", "/account/assets")

    # ---- positions ----
    async def get_open_positions(self) -> dict[str, Any]:
        return await self._request("GET", "/position/open_positions")

    async def close_all_positions(self, symbol: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/position/close_all",
            body={"symbol": symbol},
            needs_dolos=True,
            needs_web_sign=True,
        )

    async def get_history_positions(
        self,
        symbol: str | None = None,
        page_num: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """
        Get user's closed positions history.

        Returns list of closed positions with:
          - closeAvgPrice  (real average exit price)
          - realised       (realized PnL gross)
          - closeProfitLoss
          - fee
          - state == 3 (closed)
          - updateTime   (close timestamp ms)

        We use this to get accurate exit price after close_all_positions
        (which does not return an orderId we could query directly).
        """
        params = f"page_num={page_num}&page_size={page_size}"
        if symbol:
            params += f"&symbol={symbol}"
        return await self._request(
            "GET", "/position/list/history_positions",
            query_params=params,
        )

    async def get_account_pnl_usdt(self, window_days: int = 359) -> float | None:
        """Account's accumulated realized PnL over `window_days` — the EXACT
        number shown on the web `/futures/analysis` page ("Recent N Days PNL").

        Calls the web analysis endpoint (on www.mexc.com, NOT the contract host)
        with the same webkey + web-sign auth the bot already uses, and reads
        data.historyDailyData.accumPnlList[-1] (the last cumulative-PnL point).
        curl_cffi chrome impersonation passes Akamai without browser cookies.
        Range must be < 360 days (the endpoint rejects >= 360 with code 600).
        Returns the float PnL, or None if the call fails (caller retries) — never
        guesses, so a transient failure can't false-trigger the recovery cap.
        """
        import json as _json, time as _t
        from src.execution.webkey.signing import sign_web
        window_days = min(window_days, 359)
        now_ms = int(_t.time() * 1000)
        body = {
            "startTime": now_ms - window_days * 86400 * 1000,
            "endTime": now_ms,
            "includeUnrealisedPnl": 0,
            "currencyType": "",
        }
        body_json = _json.dumps(body, separators=(",", ":"))
        try:
            await self.warmup()
            session = await self._ensure_session()
            headers = self._common_headers(
                sign_web(body, self.webkey),
                symbol=(body or {}).get("symbol") if isinstance(body, dict) else None)
            headers["content-type"] = "application/json"
            headers["authorization"] = self.webkey
            headers["origin"] = "https://www.mexc.com"
            headers["referer"] = "https://www.mexc.com/futures/analysis"
            resp = await session.post(
                "https://www.mexc.com/api/platform/futures/api/v1/private/account/asset/analysis/v3",
                data=body_json, headers=headers, timeout=self.timeout,
            )
            payload = resp.json()
            data = (payload or {}).get("data") or {}
            lst = (data.get("historyDailyData") or {}).get("accumPnlList") or []
            if not lst:
                logger.warning("get_account_pnl_usdt: empty accumPnlList (code=%s)", (payload or {}).get("code"))
                return None
            return float(lst[-1])
        except Exception as e:
            logger.warning("get_account_pnl_usdt failed: %s", e)
            return None

    # ---- orders ----
    async def submit_order(
        self,
        symbol: str,
        side: int,
        vol: int,
        leverage: int,
        open_type: int = 1,
        order_type: str = "5",
        price: str | None = None,
        market_ceiling: bool = False,
        price_protect: str = "0",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "openType": open_type,
            "type": order_type,
            "vol": vol,
            "leverage": leverage,
            "marketCeiling": market_ceiling,
            "priceProtect": price_protect,
        }
        if price is not None:
            body["price"] = price
        # Debug log to verify what we actually send to MEXC.
        # Type meanings: 1=LIMIT(GTC), 2=POST_ONLY, 3=IOC, 4=FOK, 5=MARKET, 6=CONVERT.
        logger.info(
            "[ORDER SUBMIT] symbol=%s side=%s type=%s vol=%s lev=%s price=%s",
            symbol, side, order_type, vol, leverage, price,
        )
        return await self._request(
            "POST", "/order/create",
            body=body,
            # dolos на /order/create: НЕ ВИМАГАЄТЬСЯ біржею (абляція 2026-08-12
            # — ордер приймається і з плоским тілом), але з 2026-08-25 ми його
            # ШЛЕМО за замовчуванням. Причини, вартість і чесне застереження про
            # причинність — біля _DOLOS_ON_ORDER угорі файлу.
            # web-sign обовʼязковий у будь-якому разі: без нього code=602.
            # close_all_positions завжди слав dolos і не змінювався.
            needs_dolos=_DOLOS_ON_ORDER,
            needs_web_sign=True,
        )

    async def get_order_deals(self, order_id: str) -> dict[str, Any]:
        """Fetch deal (fill) details for a specific order.

        Returns list of partial fills with {price, vol, ...} in `data`.
        Used by _poll_fill_price for fast, targeted fill detection after
        IOC submit — smaller payload and faster MEXC visibility than
        /position/open_positions.

        Endpoint: GET /order/deal_details/{order_id}
        Auth: webkey header only (no dolos/web sign needed for read).
        """
        return await self._request(
            "GET", f"/order/deal_details/{order_id}",
        )

    # ---- health ----
    async def health_check(self) -> dict[str, Any]:
        """
        Verify slot health by fetching balance + open positions.

        Performance:
            balance and positions are fetched IN PARALLEL via asyncio.gather.
            On a typical proxy round-trip of ~700ms, this saves ~700ms vs
            sequential. Total wall-clock is roughly max(balance, positions).

            latency_ms reports the wall-clock of the parallel block (the
            slower of the two requests, since gather waits for both).
        """
        report: dict[str, Any] = {
            "valid": False,
            "balance": None,
            "open_positions": None,
            "latency_ms": None,
            "warmed": False,
            "error": None,
        }

        try:
            await self.warmup()
            report["warmed"] = True
        except MexcClientError as e:
            report["error"] = f"warmup failed: {e}"
            return report

        t0 = time.monotonic()
        # Fan out: balance and positions don't depend on each other
        balance_task = asyncio.create_task(self.get_account_assets())
        positions_task = asyncio.create_task(self.get_open_positions())

        try:
            balance, positions = await asyncio.gather(
                balance_task, positions_task,
                return_exceptions=True,
            )
        finally:
            report["latency_ms"] = int((time.monotonic() - t0) * 1000)

        # Process balance result
        if isinstance(balance, Exception):
            report["error"] = f"balance: {balance}"
            return report

        if balance.get("code") != 0:
            report["error"] = f"code={balance.get('code')} msg={balance.get('msg')!r}"
            return report

        report["valid"] = True
        data = balance.get("data")
        usdt = None
        if isinstance(data, list):
            for entry in data:
                if entry.get("currency") == "USDT":
                    usdt = entry
                    break
        elif isinstance(data, dict):
            usdt = data.get("USDT")
        report["balance"] = usdt

        # Process positions result (best-effort — failure here doesn't invalidate
        # the slot, balance is the primary auth check)
        if isinstance(positions, Exception):
            logger.warning(
                "[slot %s] get_open_positions failed during health_check: %s",
                self.slot_id, positions,
            )
        elif positions.get("code") == 0:
            pos_data = positions.get("data") or []
            report["open_positions"] = (
                len(pos_data) if isinstance(pos_data, list) else None
            )

        return report
