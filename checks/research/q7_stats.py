import sqlite3, time
t0=time.time()
c = sqlite3.connect('file:/app/data/stakan.db?mode=ro', uri=True)

cols = ['binance_impulse_bps','mexc_impulse_bps','book_imbalance','gap_age_ms','spread_bps','gap_ticks','mid_gap_bps']
sel = []
for col in cols:
    sel.append(f"MIN({col})")
    sel.append(f"MAX({col})")
    sel.append(f"AVG({col})")

hist_specs = [
    ('binance_impulse_bps', [-5,-2,-1,-0.5,0,0.5,1,2,5]),
    ('mexc_impulse_bps', [-5,-2,-1,-0.5,0,0.5,1,2,5]),
    ('book_imbalance', [-0.5,-0.2,-0.1,0,0.1,0.2,0.5]),
    ('gap_age_ms', [10,25,50,100,200,500,1000]),
    ('spread_bps', [0.5,1,2,3,5,10]),
]
hist_slices = {}  # col -> (start_idx, labels)
for col, edges in hist_specs:
    start = len(sel)
    sel.append(f"SUM(CASE WHEN {col}<{edges[0]} THEN 1 ELSE 0 END)")
    for i in range(len(edges)-1):
        sel.append(f"SUM(CASE WHEN {col}>={edges[i]} AND {col}<{edges[i+1]} THEN 1 ELSE 0 END)")
    sel.append(f"SUM(CASE WHEN {col}>={edges[-1]} THEN 1 ELSE 0 END)")
    labels = [f"<{edges[0]}"] + [f"[{edges[i]},{edges[i+1]})" for i in range(len(edges)-1)] + [f">={edges[-1]}"]
    hist_slices[col] = (start, labels)

sql = "SELECT " + ", ".join(sel) + " FROM signal_features WHERE symbol='HYPEUSDT' AND fillable=1"
row = c.execute(sql).fetchone()
print("single-pass query done, time:", time.time()-t0)

for i,col in enumerate(cols):
    mn,mx,av = row[i*3], row[i*3+1], row[i*3+2]
    print(f"{col}: min={mn} max={mx} avg={av}")

for col, (start, labels) in hist_slices.items():
    print(f"\n{col} histogram:")
    for j, lbl in enumerate(labels):
        print(f"  {lbl:16s} n={row[start+j]}")

print("total time:", time.time()-t0)
