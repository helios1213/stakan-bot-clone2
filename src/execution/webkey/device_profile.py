"""Свій СТАБІЛЬНИЙ профіль пристрою на кожен слот.

ПРОБЛЕМА, ЯКУ ЦЕ ЛІКУЄ. До 2026-08-26 усі слоти обох ботів (до чотирьох
акаунтів) ходили з ОДНАКОВИМ профілем: той самий User-Agent, та сама ОС, той
самий TLS-відбиток, той самий `sys`/`sys_ver` у p0. Відрізнявся лише
`visitor_id`. Тобто чотири різні акаунти виглядали як один пристрій — це
тривіально кластеризується.

ЧОМУ НЕ «ПОСТІЙНО МІНЯТИ» — і це головне тут. У реального браузера відбиток
СТАБІЛЬНИЙ: людина не міняє ОС і версію браузера щодня. Профіль, що стрибає
при кожному старті, — сам по собі найяскравіший маркер автоматизації, помітніший
за однаковість. Тому:

    профіль = f(посів машини, номер слота)   — детермінований, той самий
                                               після рестарту І після ротації вебкея

Тобто «один акаунт = один пристрій», а не «один акаунт = новий пристрій щоразу».

ПРИВʼЯЗКА САМЕ ДО СЛОТА, А НЕ ДО `visitor_id` — це друга правка, і вона важлива.
`visitor_id` міняється при РОТАЦІЇ ВЕБКЕЯ, тож перелогін на біржі давав би слоту
новий пристрій; у реальності людина перезаходить на тому самому компʼютері.
І другий бік тієї ж помилки: при виборі `hash(visitor) % N` два незалежні слоти
легко брали ОДИН профіль — на чотирьох акаунтах це сталось одразу (два з
чотирьох дістали однаковий chrome142/Windows). Номер слота як ЗСУВ прибирає
зіткнення в межах машини за побудовою.

ПОСІВ МАШИНИ обовʼязково задавати в env КОЖНОГО бота (`MEXC_DEVICE_SEED`):
обидві коробки звуться `vultr`, тож автоматично їх не розрізнити, і без посіву
слот 1 на primary збігся б зі слотом 1 на клоні.

ЩО ОБОВʼЯЗКОВО МУСИТЬ ЗБІГАТИСЬ УСЕРЕДИНІ ПРОФІЛЮ (інакше ми створюємо ту
саму неузгодженість, заради усунення якої все робиться):
    TLS-ціль curl_cffi  ↔  User-Agent  ↔  sec-ch-ua  ↔  sec-ch-ua-platform
                        ↔  sys / sys_ver у dolos-p0
Тест `test_device_profiles.py` пінить кожну з цих відповідностей.

Набір цілей обмежений тим, що вміє curl_cffi (перевірено 2026-08-26:
chrome…146, safari…184, firefox…147, edge99/101). Беремо ЛИШЕ свіжі й
поширені: старий браузер — теж аномалія.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceProfile:
    impersonate: str          # ціль curl_cffi (TLS/JA3)
    user_agent: str
    sec_ch_ua: str            # порожньо для не-Chromium (Safari/Firefox не шлють)
    sec_ch_ua_platform: str
    sys: str                  # для dolos-p0
    sys_ver: str
    accept_language: str

    @property
    def is_chromium(self) -> bool:
        return bool(self.sec_ch_ua)


def _chrome(ver: int, os_kind: str) -> DeviceProfile:
    if os_kind == "mac":
        ua_os, plat, sysn, sysv = ("Macintosh; Intel Mac OS X 10_15_7",
                                   '"macOS"', "Mac OS", "10.15.7")
    else:
        ua_os, plat, sysn, sysv = ("Windows NT 10.0; Win64; x64",
                                   '"Windows"', "Windows", "10.0")
    return DeviceProfile(
        impersonate=f"chrome{ver}",
        user_agent=(f"Mozilla/5.0 ({ua_os}) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"),
        # Порядок брендів і GREASE-версія — дослівно як у живому знімку
        # браузера 2026-08-26: "Google Chrome";v="147", "Not.A/Brand";v="8", …
        sec_ch_ua=(f'"Google Chrome";v="{ver}", "Not.A/Brand";v="8", '
                   f'"Chromium";v="{ver}"'),
        sec_ch_ua_platform=plat,
        sys=sysn, sys_ver=sysv,
        accept_language="en-US,en;q=0.9",
    )


# Свідомо ЛИШЕ Chrome: MEXC-фронтенд шле sec-ch-ua і `platform: H5-web`, а
# Safari/Firefox цих заголовків не надсилають — профіль «Firefox, що шле
# sec-ch-ua» був би гіршим за будь-який Chrome. Різноманіття даємо версією
# і операційною системою, не рушієм.
_PROFILES: tuple[DeviceProfile, ...] = (
    _chrome(146, "mac"),
    _chrome(146, "win"),
    _chrome(145, "mac"),
    _chrome(145, "win"),
    _chrome(142, "win"),
    _chrome(136, "mac"),
)


def profile_count() -> int:
    return len(_PROFILES)


# Посів машини. ОБИДВІ наші коробки звуться `vultr`, тож hostname розрізнити їх
# не може — потрібне явне значення в env КОЖНОГО бота (docker-compose).
# Якщо не задано, обидва боти візьмуть однакову базу, і слот 1 на primary
# збіжиться зі слотом 1 на клоні. Це не тихо: див. `seed_is_explicit()`.
_DEFAULT_SEED = "stakan-default"


def machine_seed() -> str:
    import os
    return os.environ.get("MEXC_DEVICE_SEED", "").strip() or _DEFAULT_SEED


def seed_is_explicit() -> bool:
    return machine_seed() != _DEFAULT_SEED


def machine_offset() -> int:
    """Зсув профілів для ЦІЄЇ машини.

    ЯВНЕ число, а не хеш посіву — і це важливо. Перша версія брала
    `sha256(посів)[0] % N`, і два РІЗНІ посіви дали ОДНАКОВИЙ зсув (перевірено
    на справжніх значеннях primary/clone: обидва дали ті самі профілі). При
    шести профілях це один шанс із шести, і він випав одразу.
    Зсув знімає везіння: оператор задає 0 на одній машині і, скажімо, 2 на
    іншій — і профілі не можуть збігтись за побудовою.
    """
    import os
    raw = os.environ.get("MEXC_DEVICE_OFFSET", "").strip()
    if raw:
        try:
            return int(raw) % len(_PROFILES)
        except ValueError:
            pass
    # Запасний шлях: із посіву. Не гарантує розрізнення двох машин — саме тому
    # MEXC_DEVICE_OFFSET має бути заданий явно.
    return hashlib.sha256(machine_seed().encode("utf-8")).digest()[0] % len(_PROFILES)


def for_slot(slot_id: int | None, seed: str | None = None,
             offset: int | None = None) -> DeviceProfile:
    """Профіль слота: детермінований від (машина, номер слота).

    ЧОМУ НЕ ВІД `visitor_id` (перша спроба була саме такою, і вона хибна).
    `visitor_id` міняється при РОТАЦІЇ ВЕБКЕЯ. Тобто перелогін на біржі давав
    би слоту НОВИЙ пристрій — а в реальності людина перезаходить на тому
    самому компʼютері, і відбиток браузера не змінюється. Прив'язка до слота
    робить це правильно: ротація ключа пристрій не чіпає.

    ЧОМУ ЦЕ ЩЕ Й ПРИБИРАЄ ЗІТКНЕННЯ. При виборі `hash(visitor) % N` два
    незалежні слоти легко брали один профіль (перевірено: два з чотирьох
    отримали однаковий chrome142/Windows). Тут номер слота йде ЗСУВОМ, тож
    різні слоти на одній машині гарантовано різні, поки слотів <= профілів.
    """
    n = len(_PROFILES)
    if offset is not None:
        base = int(offset) % n
    elif seed is not None:
        base = hashlib.sha256(seed.encode("utf-8")).digest()[0] % n
    else:
        base = machine_offset()
    if slot_id is None:
        return _PROFILES[base]
    return _PROFILES[(base + int(slot_id)) % n]


def for_visitor(visitor_id: str | None) -> DeviceProfile:
    """Запасний шлях, коли номер слота невідомий (проби, ручні виклики).

    НЕ використовувати для торгових слотів: див. `for_slot` — прив'язка до
    visitor_id міняє пристрій при кожній ротації вебкея.
    """
    if not visitor_id:
        return _PROFILES[0]
    h = hashlib.sha256(visitor_id.encode("utf-8")).digest()
    return _PROFILES[h[0] % len(_PROFILES)]
