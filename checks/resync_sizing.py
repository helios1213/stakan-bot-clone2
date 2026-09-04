# -*- coding: utf-8 -*-
"""Пересинхронізувати ямловий фолбек розміру з таблицею slot_pair_sizing.

Інваріант, який має триматись: ямлові margin_*/leverage_* дорівнюють МІНІМУМУ
по слотах, щоб кнопка «↺ reset» (видалити рядок з slot_pair_sizing) ніколи не
могла ПІДНЯТИ експозицію.

Чому знадобилось повторно: інваріант перевіряється в момент запису, а
slot_pair_sizing живе своїм життям. TAO 2026-08-03: скрипт відпрацював о 22:06,
коли слот 2 мав 45-50, і записав мінімум 40. О 22:16 оператор перекинув слот 2 на
TAO, сайзинг став 30-35 — і ямл почав перевищувати мінімум на 33%. Знімок у
коментарі теж застарів («слот 2: 45-50» — уже неправда).

Тому цей прогін оновлює І числа, І рядок-знімок. Запускати після кожної зміни
сайзингу, або принаймні звіряти.

Поведінку не міняє: поки рядок у slot_pair_sizing існує, ямлові числа не читає
ніхто (див. memory/config-truth-audit-2026-08-04.md).
"""
import glob
import os
import re
import sqlite3
import sys

ROOT = "/root/stakan-bot"
MARK = "# ⚠️ ФОЛБЕК, не істина."
SNAP = "  # Станом на"

conn = sqlite3.connect(f"file:{ROOT}/data/stakan.db?mode=ro", uri=True)
rows = list(conn.execute(
    "SELECT symbol, slot_id, margin_min_usdt, margin_max_usdt,"
    "       leverage_min, leverage_max FROM slot_pair_sizing"))
if not rows:
    sys.exit("ABORT: slot_pair_sizing порожня")

by_sym = {}
for sym, sid, m0, m1, l0, l1 in rows:
    by_sym.setdefault(sym, []).append((sid, m0, m1, l0, l1))

KEYS = ("margin_min_usdt", "margin_max_usdt", "leverage_min", "leverage_max")
changed = 0

for sym, slots in sorted(by_sym.items()):
    path = f"{ROOT}/config/pairs/{sym}.yaml"
    if not os.path.exists(path):
        continue
    floor = (min(s[1] for s in slots), min(s[2] for s in slots),
             min(s[3] for s in slots), min(s[4] for s in slots))
    txt = open(path, encoding="utf-8").read()

    cur = []
    for k in KEYS:
        m = re.search(rf"^\s*{k}:\s*([0-9.]+)\s*$", txt, re.M)
        if not m:
            cur = None
            break
        cur.append(float(m.group(1)))
    if cur is None:
        print(f"  {sym}: ключів розміру нема — пропуск")
        continue

    want = [float(x) for x in floor]
    desc = "; ".join(f"слот {s[0]}: {s[1]:g}-{s[2]:g} × {s[3]:g}-{s[4]:g}"
                     for s in sorted(slots))
    new_snap = f"  # Знімок slot_pair_sizing (може змінитись без правки цього файлу): {desc}"
    old_snap = re.search(rf"^{SNAP}.*$", txt, re.M)
    snap_stale = (old_snap is None) or (old_snap.group(0).strip() != new_snap.strip())

    if cur == want and not snap_stale:
        print(f"  {sym}: вже відповідає")
        continue

    for k, v in zip(KEYS, want):
        val = f"{int(v)}" if k.startswith("leverage") else f"{v:g}"
        txt = re.sub(rf"^(\s*){k}:\s*[0-9.]+\s*$",
                     lambda m, k=k, val=val: f"{m.group(1)}{k}: {val}",
                     txt, count=1, flags=re.M)

    if old_snap:
        txt = re.sub(rf"^{SNAP}.*$", lambda m: new_snap, txt, count=1, flags=re.M)
    elif MARK in txt:
        txt = re.sub(rf"^(\s*)margin_min_usdt:", lambda m: new_snap + "\n" + m.group(0),
                     txt, count=1, flags=re.M)

    open(path, "w", encoding="utf-8").write(txt)
    changed += 1
    note = "" if cur == want else f"  {[f'{x:g}' for x in cur]} → {[f'{x:g}' for x in want]}"
    print(f"  {sym}:{note}{'  (знімок освіжено)' if snap_stale else ''}")
    if cur != want and any(w < c for w, c in zip(want, cur)):
        print(f"     ⚠️ ямл перевищував мінімум — reset підняв би експозицію")

print(f"OK — оновлено {changed}")
