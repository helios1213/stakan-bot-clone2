import sqlite3, time
t0 = time.time()
c = sqlite3.connect('file:/app/data/stakan.db?mode=ro', uri=True)

# ts range
r = c.execute("SELECT MIN(ts), MAX(ts) FROM signal_features WHERE symbol='HYPEUSDT' AND fillable=1").fetchone()
tmin, tmax = r
tmid = (tmin+tmax)/2
print("ts range:", tmin, tmax, "mid:", tmid, "  (", time.time()-t0, "s)")

bi_signed = "(CASE WHEN direction='long' THEN binance_impulse_bps WHEN direction='short' THEN -binance_impulse_bps ELSE NULL END)"

sel = []
meta = []
def add(glabel, blabel, cond, retcol='ret_1s'):
    s = f"(CASE WHEN direction='long' THEN {retcol} WHEN direction='short' THEN -{retcol} ELSE NULL END)"
    full = f"(fillable=1 AND ({cond}) AND {retcol} IS NOT NULL)"
    sel.append(f"SUM(CASE WHEN {full} THEN 1 ELSE 0 END)"); meta.append((glabel,blabel,'n'))
    sel.append(f"SUM(CASE WHEN {full} THEN {s} ELSE 0 END)"); meta.append((glabel,blabel,'sum'))
    sel.append(f"SUM(CASE WHEN {full} THEN {s}*{s} ELSE 0 END)"); meta.append((glabel,blabel,'sumsq'))

# A. time-split: early half vs late half, for confirm/neutral/fade buckets, ret_1s only
for half_lbl, half_cond in [("EARLY", f"ts<{tmid}"), ("LATE", f"ts>={tmid}")]:
    for lbl,cond in [("fade(<-0.5)", f"{bi_signed}<-0.5"), ("neutral", f"{bi_signed}>=-0.5 AND {bi_signed}<=0.5"), ("confirm(>0.5)", f"{bi_signed}>0.5")]:
        add(f"TIMESPLIT_{half_lbl}", lbl, f"({half_cond}) AND ({cond})")

# B. within-direction check: does binance_impulse_SIGNED effect hold separately for LONG-only and SHORT-only?
for dlbl, dcond in [("LONG", "direction='long'"), ("SHORT", "direction='short'")]:
    for lbl,cond in [("fade(<-0.5)", f"{bi_signed}<-0.5"), ("neutral", f"{bi_signed}>=-0.5 AND {bi_signed}<=0.5"), ("confirm(>0.5)", f"{bi_signed}>0.5")]:
        add(f"BYDIR_{dlbl}", lbl, f"({dcond}) AND ({cond})")

# C. BEST_combo time-split verification
for half_lbl, half_cond in [("EARLY", f"ts<{tmid}"), ("LATE", f"ts>={tmid}")]:
    add("BEST_TIMESPLIT", half_lbl, f"({half_cond}) AND (gap_age_ms<10) AND ({bi_signed}>0.5) AND (mexc_impulse_bps IS NOT NULL)")

sql = "SELECT " + ", ".join(sel) + " FROM signal_features WHERE symbol='HYPEUSDT'"
t1 = time.time()
row = c.execute(sql).fetchone()
print("query B executed in", time.time()-t1, "s")

from collections import defaultdict
data = defaultdict(dict)
for (g,b,k), v in zip(meta, row):
    data[(g,b)][k] = v
seen = []
for (g,b) in data:
    if g not in seen: seen.append(g)
for g in seen:
    print(f"\n--- {g} ---")
    for (g2,b),vals in data.items():
        if g2!=g: continue
        n,sm,sq = vals.get('n',0), vals.get('sum',0), vals.get('sumsq',0)
        if not n:
            print(f"  {b:20s} n=0"); continue
        mean=sm/n; var=sq/n-mean*mean; sd=var**0.5 if var>0 else 0
        se=sd/(n**0.5) if n>0 else 0; t=mean/se if se>0 else 0
        print(f"  {b:20s} n={n:8d} mean={mean:+8.4f}bps sd={sd:7.3f}bps t={t:+6.2f}")

# D. entry slip (separate small query)
r = c.execute("""
SELECT
  SUM(CASE WHEN fillable=1 AND entry_mid IS NOT NULL AND entry_exec IS NOT NULL AND entry_mid!=0 THEN 1 ELSE 0 END),
  SUM(CASE WHEN fillable=1 AND entry_mid IS NOT NULL AND entry_exec IS NOT NULL AND entry_mid!=0
       THEN (CASE WHEN direction='long' THEN (entry_exec-entry_mid)/entry_mid*1e4 ELSE -(entry_exec-entry_mid)/entry_mid*1e4 END) ELSE 0 END),
  SUM(CASE WHEN fillable=1 AND entry_mid IS NOT NULL AND entry_exec IS NOT NULL AND entry_mid!=0
       THEN (CASE WHEN direction='long' THEN (entry_exec-entry_mid)/entry_mid*1e4 ELSE -(entry_exec-entry_mid)/entry_mid*1e4 END) *
            (CASE WHEN direction='long' THEN (entry_exec-entry_mid)/entry_mid*1e4 ELSE -(entry_exec-entry_mid)/entry_mid*1e4 END)
       ELSE 0 END)
FROM signal_features WHERE symbol='HYPEUSDT'
""").fetchone()
n,sm,sq = r
mean = sm/n; var = sq/n-mean*mean; sd=var**0.5 if var>0 else 0; se=sd/(n**0.5); t=mean/se if se>0 else 0
print(f"\n--- ENTRY SLIP (signed bps, positive=cost) --- n={n} mean={mean:+.4f}bps sd={sd:.3f}bps t={t:+.2f}")

print("\nDONE total:", time.time()-t0)
