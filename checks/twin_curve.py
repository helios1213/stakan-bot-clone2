#!/usr/bin/env python3
"""Крива відгуку shadow_twin — чи знято тавтологію і чи чесний симулятор.

ЗАПУСК (з каталогу з compose-файлом):
    docker compose exec -T stakan-bot python /app/checks/twin_curve.py

ЩО ЦЕ МІРЯЄ. До 2026-08-21 таблиця shadow_twin була ТАВТОЛОГІЄЮ: знімок книги
брався в мить t0, а ліміт live виводився з ТОГО САМОГО обʼєкта книги, тож умова
філу min(asks) <= best_ask+offset*tick була тотожно істинною і симулятор не міг
протухнути ЖОДНОГО разу (0 із 360 рядків). Тепер вердикт рахується на трьох
затримках від миті ціноутворення:

    d0    0мс                      — стара тавтологія, КОНТРОЛЬ
    draw  uniform(150,205)мс       — те, що робить продакшн-shadow
    rtt   реальний submit RTT      — пряме порівняння з live на тому ж сигналі

ЯК ЧИТАТИ:
  * d0 має лишатись високим (~95-100%). Кадр стрічки може бути до 10мс старішим
    за книгу, яку бачив live, тож рівно 100% не буде — і це нормально.
    Якщо d0 просів НИЖЧЕ ~90% — проблема у СТРІЧЦІ, а не в симуляторі, і решту
    чисел читати не можна.
  * частка протухань при draw має лягти в [0.10; 0.30] — базова лінія
    продакшн-shadow за 7 діб: SOXLUSDT 0.139, 1000PEPEUSDT 0.216.
    ≈0 -> тавтологія десь уціліла. Помітно вище живої частки промахів -> пересолили.
  * при rtt відношення філів shadow/live має йти до 1.0 ± 0.15.

ЗАСТЕРЕЖЕННЯ, яке важливіше за самі числа: n < 200 на СИМВОЛ — це не
калібрування. Пари НЕ пулити: різні ioc_offset_ticks (SOXL 3, PEPE 2) і різна
глибина. Три невдалі ітерації з mexc_feed_lag_ms сталися саме через рішення на
6-14 рядках.
"""
from __future__ import annotations

import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "/app/data/stakan.db"

# Рядки з нової схеми: tape_status заповнюється лише новим кодом, тож це
# і є межа деплою — без неї у вибірку потрапили б тавтологічні рядки.
NEW = "tape_status IS NOT NULL"
# Тільки програна гонка з ринком. Відмови стану акаунта (2005 Balance
# insufficient, 9082 throttle) — це НЕ «симулятор оптимістичніший», і
# виключати їх треба свідомо, окремою метрикою. Див. С4 в аудиті.
MARKET = "(live_filled = 1 OR live_error LIKE '%ioc_expired%')"


def main() -> int:
    c = sqlite3.connect(DB)
    total = c.execute(f"SELECT count(*) FROM shadow_twin WHERE {NEW}").fetchone()[0]
    if not total:
        print("Рядків нової схеми ще НЕМАЄ.")
        print("shadow_twin пише ЛИШЕ на живих спробах — якщо live вимкнено,")
        print("таблиця не росте. Перевір: SELECT slot_id, live_enabled FROM webkey_slots;")
        return 1

    print(f"рядків нової схеми: {total}\n")

    print("ЯКІСТЬ СТРІЧКИ (без цього решту читати не можна)")
    for st, n in c.execute(
            f"SELECT tape_status, count(*) FROM shadow_twin WHERE {NEW} GROUP BY 1 ORDER BY 2 DESC"):
        print(f"   {str(st):20} {n:6}  {n/total*100:5.1f}%")
    row = c.execute(
        f"SELECT avg(tape_age_ms), max(tape_age_ms) FROM shadow_twin "
        f"WHERE {NEW} AND tape_status='ok'").fetchone()
    if row and row[0] is not None:
        print(f"   вік кадру проти замовленого: сер {row[0]:.1f}мс, max {row[1]}мс")

    print("\nКРИВА ВІДГУКУ (тільки tape_status='ok', тільки ринкові протухання)")
    print(f"{'пара':<16}{'n':>6}{'live':>7}{'d0':>7}{'draw':>7}{'rtt':>7}"
          f"{'  протух@draw':>14}{'  shadow/live@rtt':>18}")
    q = f"""SELECT symbol, count(*), sum(live_filled),
                   sum(shadow_filled_d0), sum(shadow_filled_draw), sum(shadow_filled_rtt)
            FROM shadow_twin
            WHERE {NEW} AND tape_status='ok' AND {MARKET}
            GROUP BY symbol ORDER BY 2 DESC"""
    for sym, n, lv, d0, dr, rt in c.execute(q):
        lv = lv or 0
        exp_draw = (n - (dr or 0)) / n if n else 0.0
        ratio = (rt / lv) if lv else float("nan")
        flag = ""
        if n < 200:
            flag = f"   <- n={n} < 200: НЕ калібрування"
        print(f"{sym:<16}{n:>6}{lv:>7}{d0 or 0:>7}{dr or 0:>7}{rt or 0:>7}"
              f"{exp_draw:>13.3f}{ratio:>18.3f}{flag}")

    print("\nБАЗОВА ЛІНІЯ продакшн-shadow (ціль для 'протух@draw'): "
          "SOXLUSDT 0.139 · 1000PEPEUSDT 0.216")

    print("\nОКРЕМО — відмови стану акаунта (НЕ програна гонка, не змішувати):")
    for r in c.execute(
            f"SELECT substr(live_error,1,42), count(*) FROM shadow_twin "
            f"WHERE {NEW} AND live_filled=0 AND live_error NOT LIKE '%ioc_expired%' "
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT 5"):
        print(f"   {r[1]:5}  {r[0]}")

    print("\nСТРОГИЙ vs МʼЯКИЙ поріг філу при draw "
          "(partial = будь-яке ненульове заповнення):")
    r = c.execute(
        f"SELECT sum(shadow_filled_draw), sum(shadow_strict_draw), count(*) "
        f"FROM shadow_twin WHERE {NEW} AND tape_status='ok' AND {MARKET}").fetchone()
    if r and r[2]:
        print(f"   any-fill {r[0] or 0} · strict>=0.99 {r[1] or 0} · з {r[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
