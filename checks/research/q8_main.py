import sqlite3, time
t0 = time.time()
c = sqlite3.connect('file:/app/data/stakan.db?mode=ro', uri=True)

# NOTE: ret_500ms/ret_1s/ret_2s/ret_3s/ret_5s are ALREADY IN BPS (confirmed via raw-sample cross-check
# against mid_gap_bps, which is unambiguously bps). Do NOT multiply by 1e4.
SIGNED = "(CASE WHEN direction='long' THEN {r} WHEN direction='short' THEN -{r} ELSE NULL END)"

sel = []      # list of sql expressions
meta = []     # list of (group_label, bucket_label, kind) kind in {'n','sum','sumsq'}

def add_bucket(group_label, bucket_label, cond, retcol='ret_1s'):
    s = SIGNED.format(r=retcol)
    full = f"(fillable=1 AND ({cond}) AND {retcol} IS NOT NULL)"
    sel.append(f"SUM(CASE WHEN {full} THEN 1 ELSE 0 END)")
    meta.append((group_label, bucket_label, 'n'))
    sel.append(f"SUM(CASE WHEN {full} THEN {s} ELSE 0 END)")
    meta.append((group_label, bucket_label, 'sum'))
    sel.append(f"SUM(CASE WHEN {full} THEN {s}*{s} ELSE 0 END)")
    meta.append((group_label, bucket_label, 'sumsq'))

def add_bucket_multi(group_label, bucket_label, cond, retcols=('ret_1s','ret_2s','ret_3s')):
    for rc in retcols:
        add_bucket(f"{group_label}::{rc}", bucket_label, cond, rc)

# === 1. BASELINE: fillable vs not-fillable, all horizons ===
add_bucket_multi("baseline", "fillable=1", "1=1", retcols=('ret_500ms','ret_1s','ret_2s','ret_3s','ret_5s'))
for rc in ('ret_500ms','ret_1s','ret_2s','ret_3s','ret_5s'):
    s = SIGNED.format(r=rc)
    full = f"(fillable=0 AND {rc} IS NOT NULL)"
    sel.append(f"SUM(CASE WHEN {full} THEN 1 ELSE 0 END)")
    meta.append((f"baseline_notfillable::{rc}", "fillable=0", 'n'))
    sel.append(f"SUM(CASE WHEN {full} THEN {s} ELSE 0 END)")
    meta.append((f"baseline_notfillable::{rc}", "fillable=0", 'sum'))
    sel.append(f"SUM(CASE WHEN {full} THEN {s}*{s} ELSE 0 END)")
    meta.append((f"baseline_notfillable::{rc}", "fillable=0", 'sumsq'))

# === 2. direction ===
add_bucket_multi("direction", "long", "direction='long'")
add_bucket_multi("direction", "short", "direction='short'")

# === 3. gap_ticks in [3,25) ===
gt_edges = [3,5,7,9,12,15,20,25]
for i in range(len(gt_edges)-1):
    lo,hi = gt_edges[i], gt_edges[i+1]
    add_bucket_multi("gap_ticks", f"[{lo},{hi})", f"gap_ticks>={lo} AND gap_ticks<{hi}")

# === 4. binance_impulse_bps raw buckets ===
bi_edges = [-2,-0.5,0,0.5,1,2]
labels = [f"<{bi_edges[0]}"] + [f"[{bi_edges[i]},{bi_edges[i+1]})" for i in range(len(bi_edges)-1)] + [f">={bi_edges[-1]}"]
conds = [f"binance_impulse_bps<{bi_edges[0]}"] + [f"binance_impulse_bps>={bi_edges[i]} AND binance_impulse_bps<{bi_edges[i+1]}" for i in range(len(bi_edges)-1)] + [f"binance_impulse_bps>={bi_edges[-1]}"]
for lbl,cond in zip(labels,conds):
    add_bucket_multi("binance_impulse_bps_raw", lbl, cond)

# === 5. binance_impulse_bps SIGNED-to-direction (confirm/fade) ===
bi_signed = "(CASE WHEN direction='long' THEN binance_impulse_bps WHEN direction='short' THEN -binance_impulse_bps ELSE NULL END)"
for lbl,lo,hi in [("fade<-2", None,-2), ("fade[-2,-0.5)",-2,-0.5), ("neutral[-0.5,0.5)",-0.5,0.5), ("confirm[0.5,2)",0.5,2), ("confirm>=2",2,None)]:
    if lo is None:
        cond = f"{bi_signed}<{hi}"
    elif hi is None:
        cond = f"{bi_signed}>={lo}"
    else:
        cond = f"{bi_signed}>={lo} AND {bi_signed}<{hi}"
    add_bucket_multi("binance_impulse_SIGNED", lbl, cond)

# === 6. mexc_impulse_bps SIGNED-to-direction ===
mi_signed = "(CASE WHEN direction='long' THEN mexc_impulse_bps WHEN direction='short' THEN -mexc_impulse_bps ELSE NULL END)"
for lbl,lo,hi in [("already_closed<-2", None,-2), ("already_closed[-2,-0.5)",-2,-0.5), ("flat[-0.5,0.5)",-0.5,0.5), ("widening[0.5,2)",0.5,2), ("widening>=2",2,None)]:
    if lo is None:
        cond = f"{mi_signed}<{hi}"
    elif hi is None:
        cond = f"{mi_signed}>={lo}"
    else:
        cond = f"{mi_signed}>={lo} AND {mi_signed}<{hi}"
    add_bucket_multi("mexc_impulse_SIGNED", lbl, cond)

# === 7. gap_age_ms ===
for lbl,cond in [("fresh<10ms","gap_age_ms<10"), ("[10,100)ms","gap_age_ms>=10 AND gap_age_ms<100"),
                  ("[100,500)ms","gap_age_ms>=100 AND gap_age_ms<500"), ("[500,1000)ms","gap_age_ms>=500 AND gap_age_ms<1000"),
                  ("stale>=1000ms","gap_age_ms>=1000")]:
    add_bucket_multi("gap_age_ms", lbl, cond)

# === 8. spread_bps ===
for lbl,cond in [("<0.5","spread_bps<0.5"), ("[0.5,1)","spread_bps>=0.5 AND spread_bps<1"),
                  ("[1,2)","spread_bps>=1 AND spread_bps<2"), (">=2","spread_bps>=2")]:
    add_bucket_multi("spread_bps", lbl, cond)

# === INTERACTIONS ===
# I1: binance confirm/fade x gap_age freshness
for age_lbl, age_cond in [("fresh<10ms","gap_age_ms<10"), ("stale>=1000ms","gap_age_ms>=1000")]:
    for bi_lbl, bi_cond in [("confirm(>0.5)", f"{bi_signed}>0.5"), ("neutral", f"{bi_signed}>=-0.5 AND {bi_signed}<=0.5"), ("fade(<-0.5)", f"{bi_signed}<-0.5")]:
        add_bucket_multi("I1_biSigned_x_gapAge", f"{age_lbl} & {bi_lbl}", f"({age_cond}) AND ({bi_cond})")

# I2: binance confirm x mexc already-moved
for mi_lbl, mi_cond in [("mexc_widening(>0.5)", f"{mi_signed}>0.5"), ("mexc_flat", f"{mi_signed}>=-0.5 AND {mi_signed}<=0.5"), ("mexc_closed(<-0.5)", f"{mi_signed}<-0.5")]:
    for bi_lbl, bi_cond in [("bi_confirm(>0.5)", f"{bi_signed}>0.5"), ("bi_fade(<-0.5)", f"{bi_signed}<-0.5")]:
        add_bucket_multi("I2_miSigned_x_biSigned", f"{mi_lbl} & {bi_lbl}", f"({mi_cond}) AND ({bi_cond})")

# I3: gap_ticks bucket x binance confirm/fade (book_imbalance dropped - constant field)
for gt_lbl, gt_cond in [("gt[3,8)","gap_ticks>=3 AND gap_ticks<8"), ("gt[8,15)","gap_ticks>=8 AND gap_ticks<15"), ("gt[15,25)","gap_ticks>=15 AND gap_ticks<25")]:
    for bi_lbl, bi_cond in [("bi_confirm(>0.5)", f"{bi_signed}>0.5"), ("bi_neutral", f"{bi_signed}>=-0.5 AND {bi_signed}<=0.5"), ("bi_fade(<-0.5)", f"{bi_signed}<-0.5")]:
        add_bucket_multi("I3_gapTicks_x_biSigned", f"{gt_lbl} & {bi_lbl}", f"({gt_cond}) AND ({bi_cond})")

# === Best-combo hypothesis: fresh + confirm + not-yet-closed ===
best_cond = f"(gap_age_ms<10) AND ({bi_signed}>0.5) AND ({mi_signed}<0.5)"
add_bucket_multi("BEST_combo", "fresh&confirm&mexc_not_moved", best_cond)
worst_cond = f"(gap_age_ms>=1000) AND ({bi_signed}<-0.5)"
add_bucket_multi("WORST_combo", "stale&fade", worst_cond)

print(f"total expressions: {len(sel)}  ({len(meta)} meta entries)")
sql = "SELECT " + ", ".join(sel) + " FROM signal_features WHERE symbol='HYPEUSDT'"
t1 = time.time()
row = c.execute(sql).fetchone()
print("query executed in", time.time()-t1, "s (total incl connect:", time.time()-t0, ")")

# reorganize into dict: group_label -> bucket_label -> {n,sum,sumsq}
from collections import defaultdict
data = defaultdict(dict)
for (glabel, blabel, kind), val in zip(meta, row):
    data[(glabel,blabel)][kind] = val

# print grouped by group_label prefix (strip ::retcol suffix for horizon grouping)
groups = defaultdict(list)  # base_group -> set of bucket labels in order
seen_order = []
for (glabel, blabel) in data.keys():
    if glabel not in seen_order:
        seen_order.append(glabel)

for glabel in seen_order:
    print(f"\n--- {glabel} ---")
    for (g2,blabel), vals in data.items():
        if g2 != glabel: continue
        n, sm, sq = vals.get('n',0), vals.get('sum',0), vals.get('sumsq',0)
        if not n:
            print(f"  {blabel:36s} n=0")
            continue
        mean = sm/n
        var = sq/n - mean*mean
        sd = var**0.5 if var>0 else 0
        se = sd/(n**0.5) if n>0 else 0
        t = mean/se if se>0 else 0
        print(f"  {blabel:36s} n={n:8d} mean={mean:+8.4f}bps sd={sd:8.3f}bps t={t:+6.2f}")

print("\nDONE total time:", time.time()-t0)
