"""Soft-start is a 3-DAY CAMPAIGN, not a permanent mode.

Warming an account is a finite job: it runs for a few days, looks like a human
poking at the account, and then stops. Leaving it on forever would keep bleeding
the spend ceiling and keep placing orders nobody is watching.

So the runner owns a campaign with:
  * a start timestamp and a length in days (default 3),
  * automatic shutdown when it expires — the per-slot button is flipped OFF in
    the DB, so the UI reflects reality instead of claiming it is still warming,
  * per-day randomisation drawn ONCE per day and persisted, so a restart does
    not reroll the plan and accidentally double a day's activity.

RANDOMISATION
-------------
"Random" here means every observable dimension varies, not just the amounts:

  * how many actions happen on a given day (and some days are quiet),
  * WHICH actions and in what ORDER — buys and sells are shuffled together, so
    the sequence is not a predictable buy-then-sell rhythm,
  * when in the day they happen (jittered gaps, active-hours window),
  * sizes, tokens, sides, hold times, pauses (owned by the engines).

A warm-up that fires the same shape every day is a pattern, which is exactly
what it is meant not to be.
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CAMPAIGN_DAYS = 3
DAY_SEC = 86400

# ПЕРЕДПРОДАЖ ПЕРЕД НОВОЮ КАМПАНІЄЮ (рішення оператора 2026-09-05).
#
# Прогрів має починатись із чистого спотового балансу. Монети лишаються на
# слоті завжди: `maybe_sell` НІКОЛИ не продає нижче базового залишку, а
# розпродаж наприкінці кампанії свідомо лишає 20% спотового балансу. Тож
# наступна кампанія стартувала б на балансі, де USDT майже немає, а вся
# вартість замкнена в монетах — рівно та ситуація, що дала 31.08 дедлок і
# що зараз тримає слот 1 клона з 0.93 вільних USDT при 27.37 у монетах.
#
# Вікно РОЗМАЗАНЕ, а не миттєве: `wind_down` ріже всі монети однією часткою
# за ОДИН виклик, тож `keep_frac=0` злив би баланс однією пачкою ордерів.
# Поступове зменшення виглядає як звичайне скорочення позицій — те саме
# міркування, що вже записане в самому `wind_down`.
PRECLEAR_MIN_SEC = 600      # 10 хв
PRECLEAR_MAX_SEC = 1800     # 30 хв


def _atomic_write_json(path: str, payload: dict) -> None:
    """tmp + os.replace: читач бачить або старий файл, або новий, ніколи обрізаний.

    `open(..., "w")` обрізає файл ДО запису (виміряно: розмір 0 одразу після
    open). Тобто ENOSPC або краш у цьому вікні лишає порожній файл, який
    читається як «стану немає». Правильний зразок уже лежав у сусідньому
    модулі — `futures_soft_start.save_state`.
    """
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, p)


@dataclass
class CampaignState:
    started_at: float = 0.0
    days: int = DEFAULT_CAMPAIGN_DAYS
    finished: bool = False
    # day index -> the activity weight rolled for that day (0.0-1.0). Persisted
    # so a restart resumes the same plan instead of rerolling it.
    day_weights: dict = field(default_factory=dict)
    # Набір токенів на ВСЮ кампанію, розіграний один раз і збережений.
    # Не «нові монети щодня»: кожен куплений токен лишає базовий залишок,
    # який не можна продати, тож 12 різних монет за 3 дні заблокували б увесь
    # спотовий баланс і прогрів став би нікуди. Плюс людина зазвичай крутить
    # кілька своїх монет, а не нову щодня.
    tokens: list = field(default_factory=list)
    # ПІДСУМКИ КАМПАНІЇ, а не процесу. Лічильники жили в памʼяті репортера,
    # який створюється наново на КОЖНОМУ рестарті бота — тож фінальний звіт
    # 29.08 показав «spot 0 buys, futures 2 opened, Ran for 14.0h» замість
    # реальних 17/9 і 8/7 за три доби. Він міряв час від останнього рестарту,
    # а не кампанію. Тут вони переживають рестарт разом зі станом кампанії.
    stats: dict = field(default_factory=dict)
    # Відбиток АКАУНТА, для якого йшла ця кампанія. НЕ сам вебкей — лише
    # sha256-префікс: у стані на диску секретам не місце.
    #
    # НАВІЩО. Стан прогріву лежить per-SLOT, а не per-account. Оператор
    # замінив вебкеї на клоні — і новий акаунт успадкував би чужу кампанію
    # разом із її бюджетом, монетами в legacy-відрі та лічильниками. Тепер
    # зміна ключа = нова кампанія з чистого аркуша.
    account_key: str = ""
    # ПЕРЕДПРОДАЖ: до якої миті монети мають бути злиті, і чи вже злиті.
    #
    # ДЕФОЛТ `preclear_done=True` НАВМИСНО, і це найважливіше в цих двох
    # полях. Стан читається як `CampaignState(**json)`, тож у файлі, який
    # писала попередня версія, цих ключів немає і вони візьмуть дефолт.
    # Якби дефолт був False, перший же тік після деплою почав би ліквідувати
    # баланс ЧИННОЇ кампанії. Передпродаж вмикає ЛИШЕ `start_if_new()` —
    # тобто момент, коли кампанія справді починається.
    #
    # Початок вікна не зберігаємо окремо: він дорівнює `started_at`, бо
    # кампанія і передпродаж озброюються одним викликом.
    preclear_until: float = 0.0
    preclear_done: bool = True

    def elapsed_days(self, now: float | None = None) -> float:
        if not self.started_at:
            return 0.0
        return ((now or time.time()) - self.started_at) / DAY_SEC

    def day_index(self, now: float | None = None) -> int:
        return int(self.elapsed_days(now))

    def expired(self, now: float | None = None) -> bool:
        return self.finished or self.elapsed_days(now) >= self.days

    def remaining_days(self, now: float | None = None) -> float:
        return max(0.0, self.days - self.elapsed_days(now))


class SoftStartCampaign:
    """A finite, randomised warming run for one slot."""

    def __init__(self, path: str, days: int = DEFAULT_CAMPAIGN_DAYS,
                 rng: random.Random | None = None) -> None:
        self.path = path
        self.rng = rng or random.Random()
        # Битий стан НЕ означає «нова кампанія» — див. `_load`.
        self._unreadable = False
        self.state = self._load(path, days)

    def _load(self, path: str, days: int) -> CampaignState:
        """Прочитати стан. Нечитабельний файл — це FAIL-CLOSED.

        Раніше тут стояло «starting fresh», і ланцюг був такий: битий файл ->
        `started_at=0` -> `start_if_new()` True -> `_reset_accounting()` ->
        **ще 3 доби ЖИВОЇ торгівлі** на акаунті, який оператор вважав
        завершеним, плюс обнулений облік. Тригер реальний: диск на primary
        96%, а `open(..., "w")` обрізає файл ДО запису.

        Ми не знаємо, чи кампанія тривала і скільки лишалось, тож єдина
        безпечна відповідь — НЕ починати нову. Стан позначається як
        нечитабельний: `expired()` віддає True (слот чисто вимкнеться), а
        `start_if_new()` відмовляє. Оператор може натиснути 🌱 ще раз —
        свідомо, а не через збій файлової системи.
        """
        p = Path(path)
        if p.exists():
            try:
                return CampaignState(**json.loads(p.read_text()))
            except Exception as e:
                logger.critical("СТАН КАМПАНІЇ НЕЧИТАБЕЛЬНИЙ (%s) — НЕ починаю "
                                "нову кампанію: це були б ще 3 доби живої "
                                "торгівлі через збій файлу. Натисни 🌱 ще раз, "
                                "якщо прогрів справді потрібен", e)
                try:
                    p.rename(p.with_suffix(p.suffix + f".corrupt.{int(time.time())}"))
                except Exception as e2:              # pragma: no cover - fs edge
                    logger.warning("битий файл кампанії не збережено: %s", e2)
                self._unreadable = True
        return CampaignState(days=days)

    def _save(self) -> None:
        try:
            _atomic_write_json(self.path, asdict(self.state))
        except Exception as e:
            logger.error("campaign state SAVE FAILED (%s) — a restart may "
                         "restart the campaign", e)

    # ---- lifecycle ------------------------------------------------------

    def start_if_new(self, account_key: str = "") -> bool:
        """Почати кампанію, якщо її ще нема, вона ВЖЕ ЗАВЕРШЕНА або змінився
        акаунт. True, якщо цей виклик її почав.

        ТРИ ПРИЧИНИ ПОЧАТИ, і раніше працювала лише перша:
        1. кампанії ще не було (`started_at == 0`);
        2. **попередня завершилась** — до 30.08 повторне натискання 🌱 нічого
           не давало: `started_at` уже стояв, тож `start_if_new` мовчки
           повертав False, а `expired()` одразу гасив слот назад. Прогрів
           можна було запустити рівно один раз на слот, назавжди;
        3. **змінився акаунт** — стан лежить per-SLOT, тож новий вебкей
           успадкував би чужу кампанію: її бюджет, лічильники і монети в
           legacy-відрі. Останнє особливо погано: ми б рахували «у монетах»
           те, чого на цьому акаунті немає.
        """
        if self._unreadable:
            # FAIL-CLOSED: див. `_load`. Не починаємо кампанію через збій файлу.
            return False
        prev_key = self.state.account_key or ""
        key_changed = bool(account_key) and bool(prev_key) and account_key != prev_key
        fresh = (not self.state.started_at) or self.state.expired() or key_changed

        if not fresh:
            # Кампанія триває — лише дописуємо ключ, якщо його ще не було
            # (файл із часів до появи поля).
            if account_key and not prev_key:
                self.state.account_key = account_key
                self._save()
            return False

        why = ("новий акаунт у слоті" if key_changed
               else ("попередня завершилась" if self.state.started_at
                     else "перший запуск"))
        days = self.state.days
        # ПОВНИЙ СКИД, а не часткове оновлення: лічильники, ваги днів, набір
        # токенів і прапорець finished належали ТІЙ кампанії. Часткове
        # оновлення лишило б, наприклад, чужі підсумки у фінальному звіті.
        _now = time.time()
        _window = self.rng.uniform(PRECLEAR_MIN_SEC, PRECLEAR_MAX_SEC)
        self.state = CampaignState(days=days, started_at=_now,
                                   account_key=account_key or prev_key,
                                   preclear_until=_now + _window,
                                   preclear_done=False)
        self._save()
        logger.info("soft-start campaign: старт (%s), %d доби; передпродаж "
                    "монет розмазано на %.0f хв", why, days, _window / 60)
        return True

    # ---- передпродаж перед стартом --------------------------------------

    def preclear_pending(self) -> bool:
        """Чи треба ще злити монети, перш ніж гріти."""
        if self._unreadable:
            return False          # fail-closed: кампанії однаково не буде
        return not self.state.preclear_done

    def preclear_keep_frac(self, now: float | None = None) -> float:
        """Яку частку вартості монет ЩЕ можна лишити на цьому тіку.

        Лінійно 1.0 -> 0.0 від `started_at` до `preclear_until`. Саме це
        число йде в `wind_down(keep_frac=...)`, тож кожен тік зрізає
        черговий шматок, а на дедлайні лишається нуль.

        Повертає 0.0, якщо вікно вже минуло або зіпсоване (until <= start) —
        тобто «продай усе», а не «не продавай нічого». Помилка в цей бік
        самовиправляється наступним тіком; у протилежний — лишила б монети
        назавжди.
        """
        n = now if now is not None else time.time()
        span = self.state.preclear_until - self.state.started_at
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (self.state.preclear_until - n) / span))

    def mark_precleared(self) -> None:
        if self.state.preclear_done:
            return
        self.state.preclear_done = True
        self._save()

    def expired(self) -> bool:
        if self._unreadable:
            return True           # fail-closed: див. `_load`
        return self.state.expired()

    def finish(self) -> None:
        if self.state.finished:
            return
        self.state.finished = True
        self._save()
        logger.info("soft-start campaign: finished after %.2f day(s)",
                    self.state.elapsed_days())

    def reset(self) -> None:
        self.state = CampaignState(days=self.state.days)
        self._save()

    # ---- per-day randomisation ------------------------------------------

    def day_weight(self) -> float:
        """Activity weight for today, in [0.15, 1.0], rolled once and kept.

        A low weight makes a quiet day: fewer actions, sometimes none. Rolled
        per campaign-day rather than per tick so the day has a shape instead of
        flickering, and persisted so a restart cannot reroll it into a busier
        day and double the activity.
        """
        key = str(self.state.day_index())
        if key not in self.state.day_weights:
            self.state.day_weights[key] = round(self.rng.uniform(0.15, 1.0), 3)
            self._save()
            logger.info("soft-start campaign: day %s weight %.2f",
                        key, self.state.day_weights[key])
        return float(self.state.day_weights[key])

    def token_pool(self, candidates, n_min: int = 3, n_max: int = 5) -> list:
        """Набір токенів кампанії: розіграти один раз і памʼятати.

        Персиститься з тієї ж причини, що й вага дня: рестарт не має міняти
        те, чим акаунт торгує, — це виглядало б як інша людина за тим самим
        ключем. Якщо збережений набір більше не входить у кандидатів (токен
        зник із біржі), відсіюємо його, а порожній набір розігруємо наново.
        """
        cands = [c for c in dict.fromkeys(candidates) if c]
        if not cands:
            return []
        kept = [t for t in (self.state.tokens or []) if t in cands]
        if kept:
            if len(kept) != len(self.state.tokens or []):
                self.state.tokens = kept
                self._save()
            return list(kept)
        n = max(1, min(len(cands), self.rng.randint(n_min, n_max)))
        picked = sorted(self.rng.sample(cands, n))
        self.state.tokens = picked
        self._save()
        logger.info("soft-start campaign: набір токенів %s", picked)
        return list(picked)

    def bump(self, key: str, n: int = 1) -> None:
        """Порахувати дію кампанії. Best-effort: збій запису не має зупиняти
        прогрів — гірший наслідок тут це неточний підсумковий звіт."""
        try:
            self.state.stats[key] = int(self.state.stats.get(key, 0)) + int(n)
            self._save()
        except Exception:
            logger.debug("soft-start campaign: лічильник %s не збережено", key,
                         exc_info=True)

    def elapsed_hours(self) -> float:
        """Скільки триває САМА КАМПАНІЯ, а не поточний процес."""
        if not self.state.started_at:
            return 0.0
        return max(0.0, (time.time() - self.state.started_at) / 3600.0)

    def scale_target(self, base_max: int) -> int:
        """Стеля денної цілі (купівлі, продажі, фʼючерсні ордери) за вагою дня.

        ЄДИНЕ ДЖЕРЕЛО ЦІЄЇ ФОРМУЛИ. До 2026-08-26 метод існував, але його не
        викликав НІХТО: ту саму арифметику переписали вбудовано в
        `SlotWarmer._apply_day_weight()`. Дубль нічого не ламав, але читаючи
        цей метод можна було зробити хибний висновок про поведінку — власне
        те, заради чого код і читають.
        """
        return max(0, int(round(base_max * self.day_weight())))


def shuffled_actions(rng: random.Random, **available: bool) -> list[str]:
    """Actions that are possible right now, in random order.

    Returning a shuffled list — rather than checking buy then sell in a fixed
    sequence — is what stops the warm-up having a recognisable rhythm.
    """
    acts = [name for name, ok in available.items() if ok]
    rng.shuffle(acts)
    return acts
