# -*- coding: utf-8 -*-
"""Розклад заробітку по слотах з розрізом на межі заміни ключа.

`account_label` у live_trades — це СЛОТ, а не акаунт. Ключі за вікно мінялись,
тож «заробіток акаунта» ≠ «заробіток слота». Ріжемо кожен слот по відомій
даті останньої заміни ключа: до неї — попередній акаунт, після — поточний.

⚠️ У БД зберігається лише ОСТАННЯ заміна (`webkey_refreshed_at`). Ранішні
свопи всередині вікна відновити з даних неможливо — якщо вони були, розріз
нижче неповний. Це обмеження, а не оцінка.
"""
import datetime
import sqlite3
import sys

BOT = sys.argv[1]
LO = int(datetime.datetime(2026, 7, 25).timestamp())
HI = int(datetime.datetime(2026, 8, 7).timestamp())

s = sqlite3.connect("file:/root/stakan-bot/data/stakan.db?mode=ro", uri=True)
l = sqlite3.connect("file:/root/stakan-bot/data/stakan-live.db?mode=ro", uri=True)

refresh = {}
for sid, wk, has in s.execute(
        "SELECT slot_id, webkey_refreshed_at, webkey_blob IS NOT NULL FROM webkey_slots"):
    refresh[sid] = (wk, bool(has))

print(f"=== {BOT} ===")
grand = 0.0
for sid in sorted(refresh):
    wk, has = refresh[sid]
    lab = f"slot{sid}"
    tot, n = l.execute(
        "SELECT COALESCE(SUM(net_pnl_usdt),0), COUNT(*) FROM live_trades"
        " WHERE closed_at IS NOT NULL AND account_label=? AND opened_at>=? AND opened_at<?",
        (lab, LO, HI)).fetchone()
    grand += tot
    key_state = "ключ Є" if has else "ключ ВИДАЛЕНО"
    print(f"\n  {lab}  ({key_state})   усього за вікно: {tot:+.2f}$  ({n} угод)")
    if wk and LO < wk < HI:
        before, nb = l.execute(
            "SELECT COALESCE(SUM(net_pnl_usdt),0), COUNT(*) FROM live_trades"
            " WHERE closed_at IS NOT NULL AND account_label=? AND opened_at>=? AND opened_at<?",
            (lab, LO, wk)).fetchone()
        after, na = l.execute(
            "SELECT COALESCE(SUM(net_pnl_usdt),0), COUNT(*) FROM live_trades"
            " WHERE closed_at IS NOT NULL AND account_label=? AND opened_at>=? AND opened_at<?",
            (lab, wk, HI)).fetchone()
        t = datetime.datetime.utcfromtimestamp(wk).strftime("%m-%d %H:%M")
        print(f"     ключ замінено {t} UTC:")
        print(f"       ПОПЕРЕДНІЙ акаунт (до заміни):  {before:+9.2f}$  ({nb} угод)")
        print(f"       ПОТОЧНИЙ акаунт  (після):       {after:+9.2f}$  ({na} угод)")
    elif wk:
        t = datetime.datetime.utcfromtimestamp(wk).strftime("%m-%d %H:%M")
        print(f"     ключ від {t} UTC — поза вікном, увесь період = один акаунт")
    else:
        print("     ключа немає — акаунт відʼєднано, весь період = попередній акаунт")
print(f"\n  РАЗОМ ПО БОТУ: {grand:+.2f}$")
