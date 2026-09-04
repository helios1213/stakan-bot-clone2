# -*- coding: utf-8 -*-
"""Що саме важить у stakan.db — перш ніж щось чистити.

⚠️ stakan.db це НЕ «база шадов». Там же лежить уся конфігурація й стан:
webkey_slots (ключі!), pair_configs, pair_states, slot_pair_sizing,
state_transitions. Чистити наосліп = стерти бота.
"""
import sqlite3

c = sqlite3.connect("file:/root/stakan-bot/data/stakan.db?mode=ro", uri=True)
page = c.execute("PRAGMA page_size").fetchone()[0]
total_pages = c.execute("PRAGMA page_count").fetchone()[0]
print(f"файл: {page * total_pages / 1e9:.2f} ГБ   (page={page}, pages={total_pages:,})\n")

try:
    c.execute("SELECT name FROM dbstat LIMIT 1")
    has_dbstat = True
except Exception:
    has_dbstat = False

rows = []
for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    try:
        n = c.execute(f"SELECT COUNT(*) FROM `{name}`").fetchone()[0]
    except Exception:
        n = -1
    size = None
    if has_dbstat:
        try:
            size = c.execute(
                "SELECT SUM(pgsize) FROM dbstat WHERE name=?", (name,)).fetchone()[0]
        except Exception:
            pass
    rows.append((name, n, size or 0))

rows.sort(key=lambda r: -(r[2] or 0) if has_dbstat else -r[1])
print(f"{'таблиця':<26} {'рядків':>12} {'розмір':>10}")
for name, n, size in rows:
    if n == 0:
        continue
    s = f"{size/1e6:.0f} МБ" if size else "—"
    print(f"  {name:<24} {n:>12,} {s:>10}")

print("\n=== часові межі великих таблиць ===")
for tbl, col in (("signal_features", "ts"), ("signals", "created_at"),
                 ("shadow_trades", "opened_at")):
    try:
        lo, hi, n = c.execute(f"SELECT MIN({col}), MAX({col}), COUNT(*) FROM {tbl}").fetchone()
        import datetime
        f = lambda v: datetime.datetime.utcfromtimestamp(
            v / 1000 if v and v > 1e11 else v).strftime("%Y-%m-%d") if v else "—"
        print(f"  {tbl:<18} {n:>12,}  {f(lo)} … {f(hi)}")
    except Exception as e:
        print(f"  {tbl}: {e}")
