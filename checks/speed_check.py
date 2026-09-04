#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чи СТАВ бот швидше після легітимних змін (feed / gc.freeze / warmup-removal).

Дві чесні метрики:
  1) ВІК СИГНАЛУ binance_signal_age_ms (signal_features) — прямий доказ, що
     bookTicker-feed будить детектор раніше. До feed колонка NULL; після —
     заповнена (~12мс проти ~100мс на depth20@100ms).
  2) real_entry_latency_ms (live_trades) — детекція→філ. Warmup-removal і
     (на основі) dolos-removal б'ють саме сюди. Ділимо по ЕРАХ деплою.

Аргументи:
  speed_check.py <SYMBOL> <WARMUP_BOUNDARY "YYYY-MM-DD HH:MM" UTC>
Ери (UTC): pre-feed | feed | +gc | +warmup(current).
Читання RO. Скан signal_features обмежений останніми ~48г (bound на ts).
"""
import sqlite3
import statistics
import sys
from datetime import datetime, timezone

BASE = "/root/stakan-bot"
LIVE_DB = f"{BASE}/data/stakan-live.db"
SIG_DB = f"{BASE}/data/stakan.db"

# межі ер (UTC) — з git commit-часів; rebuild невдовзі по коміту
FEED_TS = int(datetime(2026, 8, 11, 21, 0, tzinfo=timezone.utc).timestamp())
GC_TS   = int(datetime(2026, 8, 11, 23, 5, tzinfo=timezone.utc).timestamp())

if len(sys.argv) < 3:
    sys.exit(__doc__)
SYM = sys.argv[1]
WARM_TS = int(datetime.strptime(sys.argv[2], "%Y-%m-%d %H:%M")
              .replace(tzinfo=timezone.utc).timestamp())


def pct(v, p):
    if not v:
        return 0
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))]


def lat_era(lo, hi):
    c = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    q = ("SELECT real_entry_latency_ms FROM live_trades "
         "WHERE symbol=? AND real_entry_latency_ms IS NOT NULL "
         "AND opened_at>=? AND opened_at<?")
    v = [r[0] for r in c.execute(q, (SYM, lo, hi)).fetchall() if r[0]]
    c.close()
    return v


ERAS = [
    ("pre-feed      ", 0,       FEED_TS),
    ("+feed         ", FEED_TS, GC_TS),
    ("+gc/asynclog  ", GC_TS,   WARM_TS),
    ("+warmup (зараз)", WARM_TS, 9999999999),
]

print("=" * 66)
print(f"SPEED CHECK — {SYM}   (warmup-boundary {sys.argv[2]} UTC)")
print("=" * 66)
print("real_entry_latency_ms (детекція→філ, лише ЗАЛИТІ угоди):")
prev50 = None
for lab, lo, hi in ERAS:
    v = lat_era(lo, hi)
    n = len(v)
    if not n:
        print(f"  {lab}: нема угод")
        continue
    p50, p90, p99 = pct(v, .5), pct(v, .9), pct(v, .99)
    delta = ""
    if prev50 is not None and n >= 15:
        d = p50 - prev50
        delta = f"  Δp50 {d:+.0f}мс vs попередня"
    warn = "  ⚠️n<15" if n < 15 else ""
    print(f"  {lab}: n={n:<4} p50={p50:.0f} p90={p90:.0f} p99={p99:.0f}мс{delta}{warn}")
    if n >= 15:
        prev50 = p50

# ── вік сигналу: доказ feed ──
print("\nВІК СИГНАЛУ binance_signal_age_ms (доказ bookTicker-feed):")
try:
    c = sqlite3.connect(f"file:{SIG_DB}?mode=ro", uri=True)
    bound = (FEED_TS - 6 * 3600) * 1000
    # скільки NULL vs не-NULL ПІСЛЯ feed-межі (feed мав почати стемпити)
    after = c.execute(
        "SELECT COUNT(*), COUNT(binance_signal_age_ms) FROM signal_features "
        "WHERE symbol=? AND ts>=?", (SYM, FEED_TS * 1000)).fetchone()
    v = sorted(x[0] for x in c.execute(
        "SELECT binance_signal_age_ms FROM signal_features "
        "WHERE symbol=? AND binance_signal_age_ms IS NOT NULL AND ts>=?",
        (SYM, bound)).fetchall())
    c.close()
    tot, nn = after
    print(f"  після feed-межі: рядків={tot}, з віком={nn} "
          f"({100*nn/tot:.0f}% стемплено)" if tot else "  нема рядків після межі")
    if v:
        n = len(v)
        print(f"  розподіл віку: n={n} p10={pct(v,.1):.0f} p50={pct(v,.5):.0f} "
              f"p90={pct(v,.9):.0f} p99={pct(v,.99):.0f}мс")
        print(f"  ⭐ p50≈{pct(v,.5):.0f}мс = свіжий bookTicker (depth20@100ms дав би ~50-100мс)")
    else:
        print("  колонка ще порожня (feed не стемпив або пара не активна)")
except sqlite3.OperationalError as e:
    print(f"  колонки/таблиці нема: {e}")
print("=" * 66)
