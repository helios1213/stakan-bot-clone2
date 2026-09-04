import sqlite3, time, math

t0 = time.time()
c = sqlite3.connect('file:/app/data/stakan.db?mode=ro', uri=True)

SIGNED = "(CASE WHEN direction='long' THEN {r} WHEN direction='short' THEN -{r} ELSE NULL END)"

def agg_expr(cond, retcol, clip=None):
    s = SIGNED.format(r=retcol)
    if clip:
        s = f"(CASE WHEN ABS({s})<={clip} THEN {s} ELSE NULL END)"
    full_cond = f"fillable=1 AND ({cond})"
    n = f"SUM(CASE WHEN {full_cond} AND {retcol} IS NOT NULL THEN 1 ELSE 0 END)"
    sm = f"SUM(CASE WHEN {full_cond} THEN {s} ELSE 0 END)"
    sq = f"SUM(CASE WHEN {full_cond} THEN {s}*{s} ELSE 0 END)"
    return n, sm, sq

def run(label, buckets, retcol='ret_1s', clip=0.05):
    # clip: max abs return (fraction) to include in "clean" stats, e.g. 0.05 = 500bps
    print(f"\n--- {label} (retcol={retcol}, clip=±{clip*1e4:.0f}bps) ---")
    parts = []
    for blabel, cond in buckets:
        n, sm, sq = agg_expr(cond, retcol, clip)
        parts.append((blabel, n, sm, sq))
    sql = "SELECT " + ", ".join(f"{n}, {sm}, {sq}" for _,n,sm,sq in parts) + " FROM signal_features WHERE symbol='HYPEUSDT'"
    row = c.execute(sql).fetchone()
    for i, (blabel, *_ ) in enumerate(parts):
        n, sm, sq = row[i*3], row[i*3+1], row[i*3+2]
        if not n:
            print(f"  {blabel:32s} n=0")
            continue
        mean = sm/n
        var = sq/n - mean*mean
        sd = var**0.5 if var>0 else 0
        se = sd/(n**0.5) if n>0 else 0
        t = mean/se if se>0 else 0
        print(f"  {blabel:32s} n={n:8d} mean={mean*1e4:+8.3f}bps sd={sd*1e4:8.2f}bps t={t:+6.2f}")

print("connected, starting queries", time.time()-t0)

# 0. raw outlier check: how many rows have |ret_1s| > 5% (500bps)?
r = c.execute("""
SELECT COUNT(*), SUM(CASE WHEN ABS(ret_1s)>0.05 THEN 1 ELSE 0 END), MIN(ret_1s), MAX(ret_1s)
FROM signal_features WHERE symbol='HYPEUSDT' AND fillable=1 AND ret_1s IS NOT NULL
""").fetchone()
print("\nret_1s outlier check (fillable=1): n=%s extreme(>5%%%%)=%s min=%s max=%s" % r)
print("time so far:", time.time()-t0)
