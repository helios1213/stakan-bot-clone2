import sqlite3
c = sqlite3.connect('file:/app/data/stakan.db?mode=ro', uri=True)
print("--- 20 raw rows ---")
for r in c.execute("""
SELECT entry_mid, entry_exec, ret_500ms, ret_1s, ret_2s, ret_3s, gap_ticks, mid_gap_bps, direction, fillable
FROM signal_features WHERE symbol='HYPEUSDT' AND fillable=1 AND ret_1s IS NOT NULL
LIMIT 20
"""):
    print(r)

print("\n--- price range check ---")
r = c.execute("SELECT MIN(entry_mid), MAX(entry_mid), AVG(entry_mid), COUNT(*) FROM signal_features WHERE symbol='HYPEUSDT' AND fillable=1").fetchone()
print("entry_mid min/max/avg/n:", r)

print("\n--- distribution of |ret_1s| ---")
for lo,hi in [(0,0.001),(0.001,0.01),(0.01,0.05),(0.05,0.5),(0.5,5),(5,1000)]:
    n = c.execute(f"SELECT COUNT(*) FROM signal_features WHERE symbol='HYPEUSDT' AND fillable=1 AND ABS(ret_1s)>={lo} AND ABS(ret_1s)<{hi}").fetchone()[0]
    print(f"  |ret_1s| in [{lo},{hi}): n={n}")

print("\n--- mid_gap_bps range (known-good bps field for comparison) ---")
r = c.execute("SELECT MIN(mid_gap_bps), MAX(mid_gap_bps), AVG(mid_gap_bps) FROM signal_features WHERE symbol='HYPEUSDT' AND fillable=1").fetchone()
print(r)
