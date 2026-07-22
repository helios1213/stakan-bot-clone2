"""
Pair Deep-Dive Analysis Tool

Use this after collecting 24h+ shadow data to deeply understand a specific
pair's behavior and find optimal config.

Usage:
  python scripts/analyze_pair.py ZECUSDT [--hours 24]

Outputs:
  1. Overall stats (trades, win%, PnL, avg ROI, duration)
  2. Exit reason distribution (which mechanisms drive profit/loss)
  3. ROI distribution (where do wins/losses cluster?)
  4. Duration distribution (are we exiting too fast?)
  5. MFE/MAE analysis (could trailing capture more?)
  6. Time-of-day patterns (when does this pair work?)
  7. Detector breakdown (which signal source is best for this pair?)
  8. Optimal SL/TP simulation (what config would have been ideal?)

Goal: find the right per-pair config to maximize PnL.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = "/app/data/stakan.db"


def fmt_table(rows: list[dict], headers: list[str]) -> str:
    """Format rows as aligned text table."""
    if not rows:
        return "  (no data)"
    widths = {h: len(h) for h in headers}
    for r in rows:
        for h in headers:
            v = str(r.get(h, ''))
            widths[h] = max(widths[h], len(v))
    out = []
    out.append("  " + "  ".join(h.ljust(widths[h]) for h in headers))
    out.append("  " + "  ".join('-' * widths[h] for h in headers))
    for r in rows:
        out.append("  " + "  ".join(str(r.get(h, '')).ljust(widths[h]) for h in headers))
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="Deep-dive analysis on a single pair")
    parser.add_argument("symbol", help="Pair symbol, e.g. ZECUSDT")
    parser.add_argument("--hours", type=int, default=24, help="Lookback window in hours")
    parser.add_argument("--db", default=DB_PATH, help="Path to stakan.db")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: Database not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    where = """WHERE symbol = ?
                 AND opened_at > strftime('%s','now') - ?
                 AND closed_at IS NOT NULL"""
    params = (args.symbol, args.hours * 3600)

    print("=" * 70)
    print(f"DEEP-DIVE: {args.symbol} (last {args.hours}h)")
    print("=" * 70)

    # 1. Overall stats
    print("\n[1] OVERALL STATS")
    cur.execute(f"""
        SELECT
          COUNT(*) AS trades,
          ROUND(100.0 * SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_pct,
          ROUND(SUM(pnl_usdt), 2) AS pnl_total,
          ROUND(AVG(pnl_usdt), 4) AS pnl_avg,
          ROUND(AVG(roi_pct), 3) AS roi_avg,
          ROUND(AVG(duration_ms), 0) AS dur_avg_ms,
          ROUND(SUM(CASE WHEN pnl_usdt > 0 THEN pnl_usdt ELSE 0 END), 2) AS gross_wins,
          ROUND(SUM(CASE WHEN pnl_usdt < 0 THEN pnl_usdt ELSE 0 END), 2) AS gross_losses
        FROM shadow_trades {where}
    """, params)
    row = cur.fetchone()
    if not row or row['trades'] == 0:
        print(f"  No trades for {args.symbol} in last {args.hours}h")
        return
    d = dict(row)
    pf = d['gross_wins'] / abs(d['gross_losses']) if d['gross_losses'] < 0 else float('inf')
    print(f"  Trades:          {d['trades']}")
    print(f"  Win rate:        {d['win_pct']}%")
    print(f"  Net PnL:         ${d['pnl_total']}")
    print(f"  Avg PnL/trade:   ${d['pnl_avg']}")
    print(f"  Avg ROI:         {d['roi_avg']}%")
    print(f"  Avg duration:    {d['dur_avg_ms']}ms")
    print(f"  Gross wins:      ${d['gross_wins']}")
    print(f"  Gross losses:    ${d['gross_losses']}")
    print(f"  Profit factor:   {pf:.2f}")

    # 2. Exit reason distribution
    print("\n[2] EXIT REASON BREAKDOWN")
    cur.execute(f"""
        SELECT
          exit_reason,
          COUNT(*) AS n,
          ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM shadow_trades {where}), 1) AS pct,
          ROUND(AVG(duration_ms), 0) AS avg_ms,
          ROUND(AVG(roi_pct), 2) AS avg_roi,
          ROUND(SUM(pnl_usdt), 2) AS pnl
        FROM shadow_trades {where}
        GROUP BY exit_reason
        ORDER BY pnl DESC
    """, params + params)
    rows = [dict(r) for r in cur.fetchall()]
    print(fmt_table(rows, ['exit_reason', 'n', 'pct', 'avg_ms', 'avg_roi', 'pnl']))

    # 3. ROI distribution
    print("\n[3] ROI DISTRIBUTION (buckets of 1%)")
    cur.execute(f"""
        SELECT
          CAST(roi_pct AS INTEGER) AS roi_bucket,
          COUNT(*) AS n,
          ROUND(SUM(pnl_usdt), 2) AS pnl
        FROM shadow_trades {where}
        GROUP BY roi_bucket
        ORDER BY roi_bucket
    """, params)
    rows = [dict(r) for r in cur.fetchall()]
    # Visualize
    if rows:
        max_n = max(r['n'] for r in rows)
        for r in rows:
            bar = '█' * int(40 * r['n'] / max_n)
            sign = '+' if r['roi_bucket'] >= 0 else ''
            print(f"  {sign}{r['roi_bucket']:>4}% [{r['n']:>4}]  pnl=${r['pnl']:>7}  {bar}")

    # 4. Duration distribution
    print("\n[4] DURATION DISTRIBUTION (seconds)")
    cur.execute(f"""
        SELECT
          CASE
            WHEN duration_ms < 1000 THEN '<1s'
            WHEN duration_ms < 2000 THEN '1-2s'
            WHEN duration_ms < 3000 THEN '2-3s'
            WHEN duration_ms < 5000 THEN '3-5s'
            WHEN duration_ms < 10000 THEN '5-10s'
            WHEN duration_ms < 30000 THEN '10-30s'
            WHEN duration_ms < 60000 THEN '30-60s'
            ELSE '60s+'
          END AS bucket,
          COUNT(*) AS n,
          ROUND(AVG(roi_pct), 2) AS avg_roi,
          ROUND(SUM(pnl_usdt), 2) AS pnl
        FROM shadow_trades {where}
        GROUP BY bucket
        ORDER BY MIN(duration_ms)
    """, params)
    rows = [dict(r) for r in cur.fetchall()]
    print(fmt_table(rows, ['bucket', 'n', 'avg_roi', 'pnl']))

    # 5. MFE / MAE analysis — could trailing capture more?
    print("\n[5] MFE/MAE — opportunity left on the table")
    cur.execute(f"""
        SELECT
          ROUND(AVG(mfe_pct), 4) AS avg_mfe,
          ROUND(AVG(mae_pct), 4) AS avg_mae,
          ROUND(AVG(CASE WHEN pnl_usdt > 0 THEN mfe_pct END), 4) AS avg_mfe_wins,
          ROUND(AVG(CASE WHEN pnl_usdt < 0 THEN mae_pct END), 4) AS avg_mae_losses,
          ROUND(MAX(mfe_pct), 4) AS max_mfe,
          ROUND(MIN(mae_pct), 4) AS min_mae
        FROM shadow_trades {where}
    """, params)
    row = dict(cur.fetchone())
    print(f"  Avg MFE (best price seen):       {row['avg_mfe']}%")
    print(f"  Avg MAE (worst price seen):      {row['avg_mae']}%")
    print(f"  Avg MFE for wins:                {row['avg_mfe_wins']}%")
    print(f"  Avg MAE for losses:              {row['avg_mae_losses']}%")
    print(f"  Best move ever captured (MFE):   {row['max_mfe']}%")
    print(f"  Worst drawdown ever (MAE):       {row['min_mae']}%")

    # 6. Time-of-day pattern
    print("\n[6] HOUR-OF-DAY PERFORMANCE (UTC)")
    cur.execute(f"""
        SELECT
          CAST(strftime('%H', opened_at, 'unixepoch') AS INTEGER) AS hour,
          COUNT(*) AS n,
          ROUND(SUM(pnl_usdt), 2) AS pnl,
          ROUND(100.0 * SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_pct
        FROM shadow_trades {where}
        GROUP BY hour
        ORDER BY hour
    """, params)
    rows = [dict(r) for r in cur.fetchall()]
    print(fmt_table(rows, ['hour', 'n', 'pnl', 'win_pct']))

    # 7. Detector breakdown
    print("\n[7] DETECTOR BREAKDOWN (signal source)")
    cur.execute(f"""
        SELECT
          detector_source,
          COUNT(*) AS n,
          ROUND(100.0 * SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_pct,
          ROUND(AVG(roi_pct), 2) AS avg_roi,
          ROUND(SUM(pnl_usdt), 2) AS pnl
        FROM shadow_trades {where}
        GROUP BY detector_source
        ORDER BY pnl DESC
    """, params)
    rows = [dict(r) for r in cur.fetchall()]
    print(fmt_table(rows, ['detector_source', 'n', 'win_pct', 'avg_roi', 'pnl']))

    # 8. Direction breakdown
    print("\n[8] DIRECTION (LONG vs SHORT)")
    cur.execute(f"""
        SELECT
          direction,
          COUNT(*) AS n,
          ROUND(100.0 * SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_pct,
          ROUND(AVG(roi_pct), 2) AS avg_roi,
          ROUND(SUM(pnl_usdt), 2) AS pnl
        FROM shadow_trades {where}
        GROUP BY direction
        ORDER BY pnl DESC
    """, params)
    rows = [dict(r) for r in cur.fetchall()]
    print(fmt_table(rows, ['direction', 'n', 'win_pct', 'avg_roi', 'pnl']))

    # 9. Optimal SL simulation
    print("\n[9] WHAT-IF: OPTIMAL SL/TP COMBOS")
    print("  Simulating: if SL had been X and TP at Y%, what would PnL have been?")
    cur.execute(f"""
        SELECT mfe_pct, mae_pct, roi_pct, margin_usdt
        FROM shadow_trades {where}
    """, params)
    trades = [dict(r) for r in cur.fetchall()]

    sl_options = [-1.0, -1.5, -2.0, -2.5, -3.0]
    tp_options = [1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 999.0]

    sim_results = []
    for sl in sl_options:
        for tp in tp_options:
            sim_pnl = 0.0
            sim_wins = 0
            sim_losses = 0
            for t in trades:
                # SL/TP work on price-pct basis (mfe/mae are price excursions)
                # ROI is based on margin (mfe × leverage = ROI on margin roughly)
                # Approximate: did MFE reach TP first, or MAE reach SL first?
                # Use roi-based comparison via mfe×N and mae×N
                # Actually: roi_pct already accounts for leverage
                # Simulate: if SL%on margin then trades < SL → loss=SL margin
                #            if MFE × leverage ≥ TP → take profit
                # Without detailed history we approximate with realized roi
                margin = t['margin_usdt'] or 25
                sim_roi = t['roi_pct']
                # if mae adjusted indicates earlier SL hit
                # mfe/mae are price %, not roi. roi = price_pct × leverage roughly
                # We just check if final roi exceeded TP threshold or SL threshold
                if sim_roi >= tp:
                    sim_pnl += margin * tp / 100
                    sim_wins += 1
                elif sim_roi <= sl:
                    sim_pnl += margin * sl / 100
                    sim_losses += 1
                else:
                    # Trade exited normally — keep as-is
                    sim_pnl += margin * sim_roi / 100
                    if sim_roi > 0:
                        sim_wins += 1
                    elif sim_roi < 0:
                        sim_losses += 1
            sim_results.append({
                'sl': sl, 'tp': tp, 'pnl': round(sim_pnl, 2),
                'wins': sim_wins, 'losses': sim_losses,
            })

    # Top 5 best combos
    sim_results.sort(key=lambda x: x['pnl'], reverse=True)
    print(f"\n  TOP 5 SL/TP combos by simulated PnL:")
    for r in sim_results[:5]:
        print(f"    sl={r['sl']:>5}%  tp={r['tp']:>5}%  → ${r['pnl']:>7}  ({r['wins']}W / {r['losses']}L)")

    # 10. Slippage stats
    print("\n[10] SLIPPAGE")
    cur.execute(f"""
        SELECT
          ROUND(AVG(entry_slippage_pct), 4) AS avg_entry_slip,
          ROUND(AVG(exit_slippage_pct), 4) AS avg_exit_slip,
          ROUND(MAX(entry_slippage_pct), 4) AS max_entry_slip,
          ROUND(MIN(entry_slippage_pct), 4) AS min_entry_slip
        FROM shadow_trades {where}
    """, params)
    row = dict(cur.fetchone())
    print(f"  Avg entry slippage:   {row['avg_entry_slip']}%")
    print(f"  Avg exit slippage:    {row['avg_exit_slip']}%")
    print(f"  Range entry slip:     [{row['min_entry_slip']}%, {row['max_entry_slip']}%]")

    print("\n" + "=" * 70)
    print("DONE.")
    print("=" * 70)


if __name__ == "__main__":
    main()
