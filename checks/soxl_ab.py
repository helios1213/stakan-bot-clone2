#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SOXL тюнінг before/after: чекає n>=NEED угод ПІСЛЯ зміни (epoch), тоді зрівнює
з offset3-ерою ДО зміни (та сама конфіга, різниця лише min_ticks 4->5 + trail).
Читання RO. Локально на основі."""
import sqlite3, time
from collections import Counter

DB = "file:/root/stakan-bot/data/stakan-live.db?mode=ro"
EP = 1786552728              # зміна тюнінгу
OFF3 = 1786488540           # offset3 почався 2026-08-11 22:49 UTC
NEED = 100
MAXMIN = 75


def fetch(lo, hi):
    db = sqlite3.connect(DB, uri=True)
    r = db.execute("SELECT exit_reason,net_pnl_usdt,notional_usdt FROM live_trades "
                   "WHERE symbol='SOXLUSDT' AND opened_at>=? AND opened_at<?",
                   (lo, hi)).fetchall()
    db.close()
    return r


def rep(name, rows):
    n = len(rows)
    if not n:
        print(f"  {name}: нема"); return
    pnl = sum(x[1] or 0 for x in rows); notl = sum(x[2] or 0 for x in rows)
    wins = sum(1 for x in rows if (x[1] or 0) > 0)
    tr = [x for x in rows if x[0] == "simple_trail"]
    trnet = sum(x[1] or 0 for x in tr)
    print(f"  {name}: n={n:<4} net=${pnl:+.2f} bps_зваж={1e4*pnl/notl if notl else 0:+.2f} "
          f"WR={100*wins/n:.0f}% | trail: n={len(tr)} net=${trnet:+.2f}")
    print(f"       exit_mix: {dict(Counter(x[0] for x in rows))}")


waited = 0
while waited < MAXMIN * 60:
    if len(fetch(EP, 9e12)) >= NEED:
        break
    time.sleep(90); waited += 90

pre = fetch(OFF3, EP)
post = fetch(EP, 9e12)
print(f"SOXL before/after тюнінгу (min_ticks4->5 + trail be5->1.5/dist3->2), чекав {waited//60} хв:")
rep("ДО (offset3, старі ворота+trail)", pre)
rep("ПІСЛЯ (нові ворота+trail)       ", post)
if len(post) < NEED:
    print(f"  ⚠️ن<{NEED} — рано, читати обережно")
else:
    pb = 1e4*sum(x[1] or 0 for x in pre)/sum(x[2] or 0 for x in pre) if pre else 0
    qb = 1e4*sum(x[1] or 0 for x in post)/sum(x[2] or 0 for x in post)
    print(f"  Δ bps_зваж = {qb-pb:+.2f} (ПІСЛЯ − ДО). Едж тонкий — суди по знаку+WR+trail, не по 1 угоді.")
