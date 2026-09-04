# -*- coding: utf-8 -*-
"""Скільки заробили акаунти за вікно — розкладка по ботах, слотах і днях.

⚠️ Це ЖУРНАЛ БОТА (live_trades.net_pnl_usdt), а не сторінка акаунта. Вони
розходяться: журнал не бачить фандингу, а сторінка рахує в 360-денному вікні,
що котиться. Монітор відновлення тому й читає сторінку, а не журнал.
Тут даємо журнал по днях + межі, а звірку зі сторінкою робимо окремо.
"""
import datetime
import sqlite3
import sys
from collections import defaultdict

BOT = sys.argv[1] if len(sys.argv) > 1 else "?"
LO = int(datetime.datetime(2026, 7, 25).timestamp())
HI = int(datetime.datetime(2026, 8, 7).timestamp())

l = sqlite3.connect("file:/root/stakan-bot/data/stakan-live.db?mode=ro", uri=True)
l.row_factory = sqlite3.Row

cols = [r[1] for r in l.execute("PRAGMA table_info(live_trades)")]
idc = [c for c in ("account_label", "slot_id", "mode") if c in cols]
print(f"=== {BOT} ===  колонки-ідентифікатори: {idc}")

rows = [dict(r) for r in l.execute(
    "SELECT * FROM live_trades WHERE closed_at IS NOT NULL AND opened_at>=? AND opened_at<?",
    (LO, HI))]
print(f"  угод у вікні 07-25 … 08-06: {len(rows)}")
if not rows:
    sys.exit()

by_acc = defaultdict(lambda: defaultdict(float))
by_acc_n = defaultdict(lambda: defaultdict(int))
for r in rows:
    acc = r.get("account_label") or "—"
    day = datetime.datetime.utcfromtimestamp(r["opened_at"]).strftime("%m-%d")
    by_acc[acc][day] += r["net_pnl_usdt"] or 0.0
    by_acc_n[acc][day] += 1

days = sorted({d for a in by_acc.values() for d in a})
print()
print(f"  {'день':>6} " + " ".join(f"{a:>14}" for a in sorted(by_acc)))
tot = defaultdict(float)
for d in days:
    line = f"  {d:>6} "
    for a in sorted(by_acc):
        v = by_acc[a].get(d)
        n = by_acc_n[a].get(d, 0)
        tot[a] += v or 0.0
        line += f"{(f'{v:+8.2f}$ /{n:>3}' if v is not None else ' —'):>14} "
    print(line)
print(f"  {'РАЗОМ':>6} " + " ".join(f"{tot[a]:>+13.2f}$" for a in sorted(by_acc)))
print(f"\n  УСЬОГО ПО БОТУ: {sum(tot.values()):+.2f}$  ({len(rows)} угод)")

print("\n  пари у вікні:")
byp = defaultdict(float)
for r in rows:
    byp[(r.get("account_label") or "—", r["symbol"])] += r["net_pnl_usdt"] or 0.0
for (a, s), v in sorted(byp.items(), key=lambda kv: -abs(kv[1])):
    print(f"    {a:<10} {s:<14} {v:>+9.2f}$")
