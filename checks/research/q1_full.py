import sqlite3, time, math

t0 = time.time()
c = sqlite3.connect('file:/app/data/stakan.db?mode=ro', uri=True)
cur = c.execute("""
SELECT direction, gap_ticks, long_gap_ticks, short_gap_ticks, mid_gap_bps,
       binance_impulse_bps, mexc_impulse_bps, spread_bps, book_imbalance,
       gap_age_ms, hour, entry_mid, entry_exec, fillable,
       ret_500ms, ret_1s, ret_2s, ret_3s, ret_5s, ts
FROM signal_features WHERE symbol='HYPEUSDT'
""")
rows = cur.fetchall()
print("fetched", len(rows), "in", round(time.time()-t0,1), "s")

cols = ['direction','gap_ticks','long_gap_ticks','short_gap_ticks','mid_gap_bps',
        'binance_impulse_bps','mexc_impulse_bps','spread_bps','book_imbalance',
        'gap_age_ms','hour','entry_mid','entry_exec','fillable',
        'ret_500ms','ret_1s','ret_2s','ret_3s','ret_5s','ts']
idx = {n:i for i,n in enumerate(cols)}

def g(r,name):
    return r[idx[name]]

def signed(r, retname):
    v = g(r, retname)
    if v is None: return None
    d = g(r,'direction')
    if d == 'long': return v
    elif d == 'short': return -v
    return None

def stats(vals):
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0: return (0, None, None, None)
    mean = sum(vals)/n
    if n > 1:
        var = sum((v-mean)**2 for v in vals)/(n-1)
        sd = var**0.5
        se = sd/(n**0.5)
        t = mean/se if se>0 else 0
    else:
        sd, t = None, None
    return (n, mean, sd, t)

all_hype = rows
fillable = [r for r in rows if g(r,'fillable')==1]
notfill = [r for r in rows if g(r,'fillable')==0]
print(f"\n=== OVERVIEW ===")
print("total HYPEUSDT signals:", len(all_hype))
print("fillable=1:", len(fillable), " fillable=0:", len(notfill))
for k,v in [('long', None), ('short', None)]:
    n = sum(1 for r in fillable if g(r,'direction')==k)
    print(f"  direction={k} fillable n={n}")

print(f"\n=== ADVERSE SELECTION: fillable=1 vs fillable=0 signed forward return (bps) ===")
for rh in ['ret_500ms','ret_1s','ret_2s','ret_3s','ret_5s']:
    n1,m1,sd1,t1 = stats([signed(r,rh) for r in fillable])
    n0,m0,sd0,t0_ = stats([signed(r,rh) for r in notfill])
    print(f"{rh}: fillable n={n1} mean={m1*1e4 if m1 is not None else None:.3f}bps t={t1:.2f} | NOTfillable n={n0} mean={(m0*1e4 if m0 is not None else 0):.3f}bps t={t0_ if t0_ else 0:.2f}" if m1 is not None else f"{rh}: no data")

print(f"\n=== ENTRY SLIP: entry_exec vs entry_mid (signed, bps of mid) ===")
slip_vals = []
for r in fillable:
    em, ee, d = g(r,'entry_mid'), g(r,'entry_exec'), g(r,'direction')
    if em is None or ee is None or em==0: continue
    raw = (ee-em)/em*1e4
    # slip cost: for long, paying more than mid is bad (positive slip=cost); for short, paying less than mid... invert
    signed_slip = raw if d=='long' else -raw
    slip_vals.append(signed_slip)
n,m,sd,t = stats(slip_vals)
print(f"signed entry slip (bps, positive=cost): n={n} mean={m:.4f} sd={sd:.4f} t={t:.2f}")

print(f"\n=== BASELINE fillable=1 signed forward return, ALL horizons ===")
for rh in ['ret_500ms','ret_1s','ret_2s','ret_3s','ret_5s']:
    n,m,sd,t = stats([signed(r,rh) for r in fillable])
    print(f"{rh}: n={n} mean={m*1e4:.4f}bps sd={sd*1e4:.2f}bps t={t:.2f}")

print("\nDone part 1:", round(time.time()-t0,1), "s")

# Save rows to a pickle-like using marshal for fast reload in next scripts (avoid re-scan)
import pickle
with open('/app/data/_hype_signal_cache.pkl','wb') as f:
    pickle.dump({'cols':cols,'rows':rows}, f)
print("cached to /app/data/_hype_signal_cache.pkl")
