#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вимір ефекту bookTicker-feed: ДО vs ПІСЛЯ, одна пара, один бот.

Запуск на хості бота:
    python3 /root/checks/feed_ab.py <PAIR> "<YYYY-MM-DD HH:MM>"
приклад:
    python3 /root/checks/feed_ab.py 1000PEPEUSDT "2026-08-11 23:45"   # клон
    python3 /root/checks/feed_ab.py SOXLUSDT     "2026-08-11 23:38"   # основа

Єдиний аргумент часу (Київ, TZ бота) — момент увімкнення feed. З нього:
  • split ФАЙЛОВИХ логів (переживають рестарт, повна дата) → ЗАЛИВ (filled vs
    ioc_expired) — те, чого немає в БД, бо прострочені IOC не пишуть рядок;
  • epoch для live_trades → EDGE (bps, signed-WR), ЛАТЕНТНІСТЬ (real_entry
    p50/p90), обсяг;
  • signal_features → binance_signal_age_ms (лише ПІСЛЯ: колонка нульова доти,
    доки feed не стемпить event-time; це і є прямий доказ, що feed працює).

ПРАВИЛА ЧЕСНОСТІ ВБУДОВАНІ: n<30 у вікні → друкує «РАНО», щоб не читати шум як
результат. bps зважений по обігу. Запускати можна БУДЬ-КОЛИ — вікно «після»
росте само, повторний запуск дає свіжіше порівняння.
"""
import glob
import gzip
import sqlite3
import statistics
import sys
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV = None

BASE = "/root/stakan-bot"
LIVE_DB = f"{BASE}/data/stakan-live.db"
SIG_DB = f"{BASE}/data/stakan-research.db"  # signal_features moved 2026-08-13 (old data in stakan.db)


def die(msg):
    print(msg)
    sys.exit(1)


if len(sys.argv) < 3:
    die(__doc__)
PAIR = sys.argv[1]
CUT_STR = sys.argv[2].strip()           # "2026-08-11 23:45" (Київ)
try:
    _dt = datetime.strptime(CUT_STR, "%Y-%m-%d %H:%M")
except ValueError:
    die(f"невірний формат часу: {CUT_STR!r} — треба \"YYYY-MM-DD HH:MM\"")
if KYIV:
    CUT_EPOCH = int(_dt.replace(tzinfo=KYIV).timestamp())
else:
    CUT_EPOCH = int(_dt.timestamp())
CUT_LOG = CUT_STR + ":00"               # для лексикографічного порівняння line[:19]

WIN = 4 * 3600                          # вікно «до» = 4 год перед cutoff (симетрія з активністю)


def small(n):
    return " ⚠️РАНО(n<30)" if n < 30 else ""


# ─────────────────── 1. ЗАЛИВ з файлових логів ───────────────────
def fill_rate():
    files = sorted(glob.glob(f"{BASE}/logs/stakan*.log*"))
    bo = bf = ao = af = 0
    for fp in files:
        opener = gzip.open if fp.endswith(".gz") else open
        try:
            with opener(fp, "rt", errors="ignore") as fh:
                for line in fh:
                    if PAIR not in line:
                        continue
                    is_exp = "ioc_expired" in line
                    is_fill = (f"OPEN] {PAIR}" in line) and ("mode=live" in line)
                    if not (is_exp or is_fill):
                        continue
                    ts = line[:19]                       # "YYYY-MM-DD HH:MM:SS"
                    if not (ts[:4].isdigit() and ts[4] == "-"):
                        continue                         # рядок без дати — пропуск
                    before = ts < CUT_LOG
                    if is_exp:
                        bf, af = (bf + 1, af) if before else (bf, af + 1)
                    else:
                        bo, ao = (bo + 1, ao) if before else (bo, ao + 1)
        except OSError:
            continue

    def row(o, f):
        t = o + f
        pct = f"{100 * o / t:.0f}%" if t else "—"
        return f"залив {pct:>4}  (залито {o}, прострочено {f}, спроб {t}){small(t)}"

    print("ЗАЛИВ (файлові логи, filled vs ioc_expired):")
    print(f"  ДО feed (уся історія):  {row(bo, bf)}")
    print(f"  ПІСЛЯ feed          :  {row(ao, af)}")


# ─────────────────── 2. EDGE + ЛАТЕНТНІСТЬ з live_trades ───────────────────
def db_edge():
    c = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)

    def seg(where, args):
        rows = c.execute(
            "SELECT net_pnl_usdt, notional_usdt, real_entry_latency_ms "
            f"FROM live_trades WHERE symbol=? AND {where}", args).fetchall()
        n = len(rows)
        if not n:
            return None
        pnl = sum(r[0] or 0 for r in rows)
        notl = sum(r[1] or 0 for r in rows)
        wins = sum(1 for r in rows if (r[0] or 0) > 0)
        bpsv = sorted(1e4 * (r[0] or 0) / r[1] for r in rows if r[1])
        lat = sorted(r[2] for r in rows if r[2])
        return dict(
            n=n, bps=1e4 * pnl / notl if notl else 0, wr=100 * wins / n,
            med_bps=statistics.median(bpsv) if bpsv else 0,
            notl=notl / n if n else 0,
            lat50=lat[len(lat) // 2] if lat else 0,
            lat90=lat[9 * len(lat) // 10] if lat else 0)

    b = seg("opened_at < ? AND opened_at > ?-?", (PAIR, CUT_EPOCH, CUT_EPOCH, WIN))
    a = seg("opened_at >= ?", (PAIR, CUT_EPOCH))
    print("\nEDGE + ЛАТЕНТНІСТЬ (live_trades, лише ЗАЛИТІ угоди):")
    for lab, s in (("ДО feed (4г)", b), ("ПІСЛЯ", a)):
        if s is None:
            print(f"  {lab:<12}: нема угод")
            continue
        print(f"  {lab:<12}: n={s['n']:<4} bps_зваж={s['bps']:+.2f} "
              f"медіана={s['med_bps']:+.2f} WR={s['wr']:.0f}% "
              f"| lat p50={s['lat50']:.0f}/p90={s['lat90']:.0f}мс "
              f"| ноті ${s['notl']:.0f}{small(s['n'])}")
    print("  (real_entry feed НЕ міняє — це «детекція→філ»; виграш feed ВИЩЕ по потоку)")


# ─────────────────── 3. ВІК СИГНАЛУ з signal_features ───────────────────
def signal_age():
    try:
        c = sqlite3.connect(f"file:{SIG_DB}?mode=ro", uri=True)
        v = sorted(x[0] for x in c.execute(
            "SELECT binance_signal_age_ms FROM signal_features "
            "WHERE symbol=? AND binance_signal_age_ms IS NOT NULL AND ts>=?*1000",
            (PAIR, CUT_EPOCH)))
    except sqlite3.OperationalError:
        print("\nВІК СИГНАЛУ: колонки нема (цей бот без SignalRecorder)")
        return
    n = len(v)
    print("\nВІК СИГНАЛУ binance_signal_age_ms (лише ПІСЛЯ feed):")
    if not n:
        print("  ще нема даних")
        return
    print(f"  n={n} p10={v[n // 10]:.0f} p50={v[n // 2]:.0f} p90={v[9 * n // 10]:.0f}мс")
    print(f"  ⭐ p10≈12мс = свіжий bookTicker-сигнал (ДО feed було б ~100мс з depth20@100ms)")


print("=" * 64)
print(f"FEED A/B — {PAIR}  |  cutoff {CUT_STR} Київ (epoch {CUT_EPOCH})")
print("=" * 64)
fill_rate()
db_edge()
signal_age()
print("=" * 64)
print("Нагадування: feed зсуває ТАЙМІНГ сигналу. Головна метрика — EDGE (bps/WR),")
print("не залив і не latency. Судити лише коли ПІСЛЯ набере n≥100 у денному потоці.")
