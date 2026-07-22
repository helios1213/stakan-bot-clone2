#!/usr/bin/env python3
"""
microstructure_edge_analyzer.py
================================

Деталей не показує deep_analyze: я ВИКОРИСТОВУЮ orderbook snapshots і
candles ЯК ФАКТОРИ для пояснення why each trade won or lost.

For each closed shadow_trade, joins:
  1. live_orderbook_snapshots — spread/depth on Binance & MEXC at entry time
  2. historical_candles      — 1m candle range/volume covering entry minute
  3. trade outcome           — net_pnl, exit_reason, ROI

Then groups by various conditions (spread regime, depth regime, vol regime)
and shows win rate / PnL within each bucket.

Goal: find conditions that flip the bot from -EV to +EV.

Usage:
    python3 microstructure_edge_analyzer.py [--db PATH] [--hours N] [--symbol SYM]

Output: text report. No DB writes.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from typing import Iterable

DEFAULT_DB = "/root/stakan-bot/data/stakan.db"
DEFAULT_PAIRS = ["ZECUSDT", "TAOUSDT", "1000PEPEUSDT", "ENAUSDT", "BCHUSDT", "PENGUUSDT"]


# ============================================================
# Helpers
# ============================================================

def fmt_money(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}${x:.2f}"


def fmt_pct(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}%"


def safe_pct(num: float, den: float) -> float:
    return (num / den * 100) if den else 0.0


def quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    f, c = int(k), min(int(k) + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def winrate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if (t.get("net_pnl") or 0) > 0)
    return wins / len(trades) * 100


def total_pnl(trades: list[dict]) -> float:
    return sum((t.get("net_pnl") or 0) for t in trades)


def profit_factor(trades: list[dict]) -> float:
    gp = sum((t["net_pnl"] or 0) for t in trades if (t["net_pnl"] or 0) > 0)
    gl = sum(abs(t["net_pnl"] or 0) for t in trades if (t["net_pnl"] or 0) < 0)
    if gl == 0:
        return float("inf") if gp > 0 else 0.0
    return gp / gl


# ============================================================
# Data loading
# ============================================================

def load_trades(conn: sqlite3.Connection, hours: int, symbols: list[str]) -> list[dict]:
    cutoff = int(time.time()) - hours * 3600
    placeholders = ",".join("?" * len(symbols))
    rows = conn.execute(
        f"""SELECT id, symbol, direction, opened_at, closed_at,
                   entry_price, exit_price, exit_reason,
                   net_pnl_usdt, roi_pct, mfe_pct, mae_pct,
                   peak_roi_pct, trough_roi_pct,
                   detector_source, confidence,
                   notional_usdt, margin_usdt, leverage,
                   binance_price_at_entry, mexc_price_at_entry,
                   mexc_lag_at_entry_pct
              FROM shadow_trades
             WHERE opened_at >= ?
               AND closed_at IS NOT NULL
               AND symbol IN ({placeholders})
             ORDER BY opened_at ASC""",
        (cutoff, *symbols),
    ).fetchall()
    return [
        {
            "id": r[0], "symbol": r[1], "direction": r[2],
            "opened_at": r[3], "closed_at": r[4],
            "entry_price": r[5], "exit_price": r[6], "exit_reason": r[7],
            "net_pnl": r[8], "roi_pct": r[9],
            "mfe_pct": r[10], "mae_pct": r[11],
            "peak_roi": r[12], "trough_roi": r[13],
            "detector": r[14], "confidence": r[15],
            "notional": r[16], "margin": r[17], "leverage": r[18],
            "bin_price_entry": r[19], "mex_price_entry": r[20],
            "mex_lag_entry_pct": r[21],
        }
        for r in rows
    ]


def attach_orderbook(conn: sqlite3.Connection, trades: list[dict]) -> int:
    """
    For each trade, find nearest orderbook snapshot for each exchange
    within ±10 seconds of entry. Adds keys:
      ob_bin_spread_pct, ob_bin_bid_depth, ob_bin_ask_depth
      ob_mex_spread_pct, ob_mex_bid_depth, ob_mex_ask_depth

    Returns count of trades successfully matched (both exchanges).
    """
    matched = 0
    for t in trades:
        sym = t["symbol"]
        opened_ms = t["opened_at"] * 1000
        for exch_label, exch_db in (("bin", "binance"), ("mex", "mexc")):
            row = conn.execute(
                """SELECT spread_pct, bid_depth_5, ask_depth_5,
                          bid1_price, ask1_price, ts_ms
                     FROM live_orderbook_snapshots
                    WHERE exchange=? AND symbol=?
                      AND ts_ms BETWEEN ? AND ?
                    ORDER BY ABS(ts_ms - ?) ASC
                    LIMIT 1""",
                (exch_db, sym, opened_ms - 10_000, opened_ms + 10_000, opened_ms),
            ).fetchone()
            if row:
                t[f"ob_{exch_label}_spread_pct"] = row[0]
                t[f"ob_{exch_label}_bid_depth"] = row[1]
                t[f"ob_{exch_label}_ask_depth"] = row[2]
                t[f"ob_{exch_label}_bid1"] = row[3]
                t[f"ob_{exch_label}_ask1"] = row[4]
                t[f"ob_{exch_label}_dt_ms"] = row[5] - opened_ms
            else:
                t[f"ob_{exch_label}_spread_pct"] = None
                t[f"ob_{exch_label}_bid_depth"] = None
                t[f"ob_{exch_label}_ask_depth"] = None
        if t.get("ob_bin_spread_pct") is not None and t.get("ob_mex_spread_pct") is not None:
            matched += 1
    return matched


def attach_candles(conn: sqlite3.Connection, trades: list[dict]) -> int:
    """
    For each trade, find the 1m Binance candle covering entry time.
    Adds keys: candle_range_pct, candle_volume_usdt, candle_body_pct
    """
    matched = 0
    for t in trades:
        sym = t["symbol"]
        # candle covering entry
        minute_start_ms = (t["opened_at"] // 60 * 60) * 1000
        row = conn.execute(
            """SELECT open, high, low, close, quote_volume
                 FROM historical_candles
                WHERE exchange='binance' AND symbol=? AND interval='1m'
                  AND open_time = ?""",
            (sym, minute_start_ms),
        ).fetchone()
        if row:
            o, h, l, c, qv = row
            mid = (h + l) / 2 if h and l else None
            t["candle_range_pct"] = ((h - l) / mid * 100) if mid and mid > 0 else None
            t["candle_body_pct"] = (abs(c - o) / o * 100) if o else None
            t["candle_volume_usdt"] = qv
            matched += 1
        else:
            t["candle_range_pct"] = None
            t["candle_body_pct"] = None
            t["candle_volume_usdt"] = None
    return matched


# ============================================================
# Bucketing analysis
# ============================================================

def bucket_summary(name: str, buckets: dict[str, list[dict]]) -> None:
    """
    Print a table of bucket → (n trades, win rate, total PnL, profit factor).
    """
    print(f"\n=== {name} ===")
    if not any(buckets.values()):
        print("  (no data)")
        return

    print(f"{'bucket':<28} {'n':>5} {'WR':>6} {'PnL':>10} {'PF':>6} {'avg_ROI':>9}")
    print("-" * 70)

    # Maintain provided order
    for bucket_label, trades in buckets.items():
        if not trades:
            print(f"{bucket_label:<28} {'0':>5}  {'-':>5}  {'-':>9}  {'-':>5}  {'-':>8}")
            continue
        n = len(trades)
        wr = winrate(trades)
        pnl = total_pnl(trades)
        pf = profit_factor(trades)
        rois = [t["roi_pct"] for t in trades if t["roi_pct"] is not None]
        avg_roi = sum(rois) / len(rois) if rois else 0
        pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"{bucket_label:<28} {n:>5}  {wr:>4.1f}%  {fmt_money(pnl):>9}  {pf_str:>5}  {avg_roi:>+7.3f}%")


def split_by_pct_threshold(
    trades: list[dict],
    field: str,
    thresholds: list[tuple[str, float, str]],
) -> dict[str, list[dict]]:
    """
    thresholds = [("label", upper_bound, op), ...]
    op: "<", "<=", ">", ">=", "between" (with extra arg as tuple)
    Simple version: list of (label, max_value) — first match wins, last is catchall.
    """
    out = {label: [] for label, _, _ in thresholds}
    for t in trades:
        v = t.get(field)
        if v is None:
            continue
        for label, bound, op in thresholds:
            if op == "<" and v < bound:
                out[label].append(t); break
            if op == "<=" and v <= bound:
                out[label].append(t); break
            if op == ">" and v > bound:
                out[label].append(t); break
            if op == ">=" and v >= bound:
                out[label].append(t); break
            if op == "*":  # catchall
                out[label].append(t); break
    return out


# ============================================================
# Analyses
# ============================================================

def analyze_overall(trades: list[dict]) -> None:
    print("\n" + "=" * 70)
    print("  OVERALL")
    print("=" * 70)
    n = len(trades)
    if n == 0:
        print("  no closed trades in window")
        return
    wr = winrate(trades)
    pnl = total_pnl(trades)
    pf = profit_factor(trades)
    print(f"  Trades: {n}")
    print(f"  Win rate: {wr:.1f}%")
    print(f"  Total PnL: {fmt_money(pnl)}")
    pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
    print(f"  Profit factor: {pf_str}")

    # Coverage stats — how many trades have orderbook / candle data
    with_ob = sum(1 for t in trades if t.get("ob_bin_spread_pct") is not None)
    with_candle = sum(1 for t in trades if t.get("candle_range_pct") is not None)
    print(f"\n  Coverage:")
    print(f"    with orderbook snapshot: {with_ob} / {n} ({safe_pct(with_ob, n):.1f}%)")
    print(f"    with 1m candle data:     {with_candle} / {n} ({safe_pct(with_candle, n):.1f}%)")


def analyze_spread_regime(trades: list[dict]) -> None:
    """
    Hypothesis: bot wins more often when MEXC spread is tight at entry.
    Wide spread suggests momentum mid-event — bot might be late.
    """
    print("\n" + "=" * 70)
    print("  HYPOTHESIS 1: SPREAD REGIME at entry (MEXC side)")
    print("=" * 70)
    print("  Theory: tight spread = quiet market = lag arb works.")
    print("           wide spread = active market = MEXC may catch up faster than bot.")

    trades_with_spread = [t for t in trades if t.get("ob_mex_spread_pct") is not None]
    if not trades_with_spread:
        print("\n  no orderbook data available")
        return

    # Compute percentiles
    spreads = [t["ob_mex_spread_pct"] for t in trades_with_spread]
    p25 = quantile(spreads, 0.25)
    p50 = quantile(spreads, 0.50)
    p75 = quantile(spreads, 0.75)
    p90 = quantile(spreads, 0.90)

    print(f"\n  MEXC spread distribution at entry:")
    print(f"    p25={p25*100 if p25 else 0:.4f} bps  "
          f"p50={p50*100 if p50 else 0:.4f} bps  "
          f"p75={p75*100 if p75 else 0:.4f} bps  "
          f"p90={p90*100 if p90 else 0:.4f} bps")

    if p50 is None or p75 is None:
        return

    buckets = split_by_pct_threshold(
        trades_with_spread,
        "ob_mex_spread_pct",
        [
            (f"tight (≤p25={p25*100:.3f}bps)",  p25, "<="),
            (f"normal (≤p50={p50*100:.3f}bps)", p50, "<="),
            (f"wide   (≤p75={p75*100:.3f}bps)", p75, "<="),
            (f"very wide (>p75)",                 p75, ">"),
        ],
    )
    bucket_summary("Win rate by MEXC spread regime", buckets)


def analyze_depth_regime(trades: list[dict]) -> None:
    """
    Hypothesis: when MEXC ask depth is shallow, our entry slippage is worse.
    """
    print("\n" + "=" * 70)
    print("  HYPOTHESIS 2: MEXC DEPTH at entry side")
    print("=" * 70)
    print("  Theory: thin book on entry side = high real slippage = -EV.")

    # entry side depth: longs hit asks, shorts hit bids
    for t in trades:
        if t["direction"] == "long":
            t["entry_depth"] = t.get("ob_mex_ask_depth")
        else:
            t["entry_depth"] = t.get("ob_mex_bid_depth")
        # depth ratio: how big is our notional vs available depth
        if t.get("entry_depth") and t.get("notional"):
            # entry_depth is in base asset units * price ≈ rough USD notional capacity
            # Actually it's sum of qty * price — we want relative size
            t["depth_ratio"] = t["notional"] / max(t["entry_depth"], 1)
        else:
            t["depth_ratio"] = None

    trades_with_depth = [t for t in trades if t.get("entry_depth") is not None]
    if not trades_with_depth:
        print("\n  no orderbook data available")
        return

    depths = [t["entry_depth"] for t in trades_with_depth]
    p25 = quantile(depths, 0.25)
    p50 = quantile(depths, 0.50)
    p75 = quantile(depths, 0.75)

    print(f"\n  MEXC entry-side top-5 depth (sum):")
    print(f"    p25={p25:.0f}  p50={p50:.0f}  p75={p75:.0f}")

    if p25 is None or p75 is None:
        return

    buckets = split_by_pct_threshold(
        trades_with_depth,
        "entry_depth",
        [
            (f"thin   (≤p25={p25:.0f})",  p25, "<="),
            (f"medium (≤p75={p75:.0f})", p75, "<="),
            (f"deep   (>p75)",            p75, ">"),
        ],
    )
    bucket_summary("Win rate by MEXC entry-side depth", buckets)


def analyze_volatility_regime(trades: list[dict]) -> None:
    """
    Hypothesis: bot has different edge in different volatility regimes.
    """
    print("\n" + "=" * 70)
    print("  HYPOTHESIS 3: VOLATILITY REGIME (1m candle range)")
    print("=" * 70)
    print("  Theory: in high-vol minutes, MEXC catches up faster — bot late.")

    trades_with_vol = [t for t in trades if t.get("candle_range_pct") is not None]
    if not trades_with_vol:
        print("\n  no candle data available")
        return

    ranges = [t["candle_range_pct"] for t in trades_with_vol]
    p25 = quantile(ranges, 0.25)
    p50 = quantile(ranges, 0.50)
    p75 = quantile(ranges, 0.75)
    p90 = quantile(ranges, 0.90)

    print(f"\n  1m candle range distribution at entry:")
    print(f"    p25={p25:.3f}%  p50={p50:.3f}%  p75={p75:.3f}%  p90={p90:.3f}%")

    if p25 is None or p75 is None:
        return

    buckets = split_by_pct_threshold(
        trades_with_vol,
        "candle_range_pct",
        [
            (f"calm   (≤p25={p25:.3f}%)",  p25, "<="),
            (f"normal (≤p50={p50:.3f}%)",  p50, "<="),
            (f"active (≤p75={p75:.3f}%)",  p75, "<="),
            (f"explosive (>p75)",           p75, ">"),
        ],
    )
    bucket_summary("Win rate by 1m candle range", buckets)


def analyze_lag_at_entry(trades: list[dict]) -> None:
    """
    Hypothesis: bigger MEXC lag = more profit potential.
    """
    print("\n" + "=" * 70)
    print("  HYPOTHESIS 4: MEXC LAG at entry")
    print("=" * 70)
    print("  Theory: bigger lag % = more catch-up profit potential.")

    lags = [abs(t["mex_lag_entry_pct"]) for t in trades if t.get("mex_lag_entry_pct") is not None]
    if not lags:
        print("\n  no lag data available")
        return

    p25 = quantile(lags, 0.25)
    p50 = quantile(lags, 0.50)
    p75 = quantile(lags, 0.75)

    print(f"\n  Absolute MEXC lag distribution:")
    print(f"    p25={p25:.4f}%  p50={p50:.4f}%  p75={p75:.4f}%")

    # Use absolute value for bucketing
    for t in trades:
        v = t.get("mex_lag_entry_pct")
        t["abs_lag"] = abs(v) if v is not None else None

    if p25 is None or p75 is None:
        return

    trades_with_lag = [t for t in trades if t.get("abs_lag") is not None]
    buckets = split_by_pct_threshold(
        trades_with_lag,
        "abs_lag",
        [
            (f"small  (≤p25={p25:.4f}%)", p25, "<="),
            (f"medium (≤p75={p75:.4f}%)", p75, "<="),
            (f"big    (>p75)",            p75, ">"),
        ],
    )
    bucket_summary("Win rate by MEXC lag at entry", buckets)


def analyze_confidence(trades: list[dict]) -> None:
    """
    Hypothesis: higher detector confidence = better outcome.
    """
    print("\n" + "=" * 70)
    print("  HYPOTHESIS 5: DETECTOR CONFIDENCE")
    print("=" * 70)
    print("  Theory: high confidence trades should outperform low confidence.")

    confs = [t["confidence"] for t in trades if t.get("confidence") is not None]
    if not confs:
        print("\n  no confidence data")
        return

    buckets = split_by_pct_threshold(
        [t for t in trades if t.get("confidence") is not None],
        "confidence",
        [
            ("low    (<0.5)",   0.5, "<"),
            ("med    (0.5-0.7)", 0.7, "<"),
            ("high   (0.7-0.85)", 0.85, "<"),
            ("v.high (≥0.85)",   0.85, ">="),
        ],
    )
    bucket_summary("Win rate by detector confidence", buckets)


def analyze_combo_spread_x_vol(trades: list[dict]) -> None:
    """
    The big one: spread regime × volatility regime combined.
    Looking for the magic quadrant.
    """
    print("\n" + "=" * 70)
    print("  HYPOTHESIS 6: SPREAD × VOLATILITY combined")
    print("=" * 70)
    print("  Theory: edge is concentrated in specific (spread, vol) combos.")

    trades_full = [
        t for t in trades
        if t.get("ob_mex_spread_pct") is not None and t.get("candle_range_pct") is not None
    ]
    if not trades_full:
        print("\n  no combined data")
        return

    spreads = [t["ob_mex_spread_pct"] for t in trades_full]
    ranges = [t["candle_range_pct"] for t in trades_full]
    spread_p50 = quantile(spreads, 0.5)
    range_p50 = quantile(ranges, 0.5)

    if spread_p50 is None or range_p50 is None:
        return

    quads: dict[str, list[dict]] = {
        "tight spread + calm  ": [],
        "tight spread + active": [],
        "wide spread  + calm  ": [],
        "wide spread  + active": [],
    }
    for t in trades_full:
        s_tight = t["ob_mex_spread_pct"] <= spread_p50
        v_calm = t["candle_range_pct"] <= range_p50
        if s_tight and v_calm:
            quads["tight spread + calm  "].append(t)
        elif s_tight and not v_calm:
            quads["tight spread + active"].append(t)
        elif not s_tight and v_calm:
            quads["wide spread  + calm  "].append(t)
        else:
            quads["wide spread  + active"].append(t)

    print(f"\n  Median spread: {spread_p50*100:.4f} bps; median 1m range: {range_p50:.3f}%")
    bucket_summary("Win rate by (spread, vol) quadrant", quads)


def analyze_per_pair(trades: list[dict]) -> None:
    """
    Per-pair × spread regime — see if BCH responds differently than ZEC.
    """
    print("\n" + "=" * 70)
    print("  PER-PAIR × SPREAD REGIME")
    print("=" * 70)

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if t.get("ob_mex_spread_pct") is not None:
            by_sym[t["symbol"]].append(t)

    for sym, sym_trades in sorted(by_sym.items()):
        if len(sym_trades) < 10:
            continue
        spreads = [t["ob_mex_spread_pct"] for t in sym_trades]
        sp50 = quantile(spreads, 0.5)
        if sp50 is None:
            continue
        tight = [t for t in sym_trades if t["ob_mex_spread_pct"] <= sp50]
        wide  = [t for t in sym_trades if t["ob_mex_spread_pct"] >  sp50]
        print(f"\n  {sym} (n={len(sym_trades)}, median spread={sp50*100:.4f}bps)")
        for label, group in (("tight (≤median)", tight), ("wide  (>median)", wide)):
            if not group:
                continue
            n = len(group); wr = winrate(group); pnl = total_pnl(group); pf = profit_factor(group)
            pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
            print(f"    {label:<18} n={n:<4} WR={wr:5.1f}%  PnL={fmt_money(pnl):>9}  PF={pf_str}")


def analyze_exit_reason_breakdown(trades: list[dict]) -> None:
    """
    Where does PnL come from? Where does it leak?
    """
    print("\n" + "=" * 70)
    print("  EXIT REASON BREAKDOWN")
    print("=" * 70)

    by_reason: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_reason[t["exit_reason"] or "unknown"].append(t)

    print(f"{'reason':<25} {'n':>5} {'WR':>6} {'PnL':>10} {'avg_ROI':>9}")
    print("-" * 60)
    for reason, group in sorted(by_reason.items(), key=lambda kv: -total_pnl(kv[1])):
        n = len(group); wr = winrate(group); pnl = total_pnl(group)
        rois = [t["roi_pct"] for t in group if t.get("roi_pct") is not None]
        avg_roi = sum(rois) / len(rois) if rois else 0
        print(f"{reason:<25} {n:>5}  {wr:>4.1f}%  {fmt_money(pnl):>9}  {avg_roi:>+7.3f}%")


# ============================================================
# Main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--hours", type=int, default=24,
                        help="Lookback window in hours (default: 24)")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_PAIRS,
                        help=f"Symbols to analyze (default: {','.join(DEFAULT_PAIRS)})")
    parser.add_argument("--min-trades", type=int, default=50,
                        help="Skip analyses if fewer trades (default: 50)")
    args = parser.parse_args()

    print("=" * 70)
    print("  MICROSTRUCTURE EDGE ANALYZER")
    print("=" * 70)
    print(f"  DB:       {args.db}")
    print(f"  Lookback: {args.hours}h")
    print(f"  Symbols:  {', '.join(args.symbols)}")
    print()

    try:
        conn = sqlite3.connect(args.db)
    except sqlite3.OperationalError as e:
        print(f"ERROR: cannot open DB: {e}", file=sys.stderr)
        return 1

    print("Loading trades...")
    try:
        trades = load_trades(conn, args.hours, args.symbols)
    except sqlite3.OperationalError as e:
        print(f"ERROR: query failed: {e}", file=sys.stderr)
        return 1
    print(f"  loaded {len(trades)} closed trades")

    if len(trades) < args.min_trades:
        print(f"\n  not enough trades ({len(trades)} < {args.min_trades}) for meaningful analysis")
        print("  try --hours 48 or wider window")
        return 0

    print("Joining orderbook snapshots...")
    ob_matched = attach_orderbook(conn, trades)
    print(f"  matched {ob_matched} / {len(trades)} ({safe_pct(ob_matched, len(trades)):.1f}%)")

    print("Joining 1m candles...")
    cd_matched = attach_candles(conn, trades)
    print(f"  matched {cd_matched} / {len(trades)} ({safe_pct(cd_matched, len(trades)):.1f}%)")

    # Run analyses
    analyze_overall(trades)
    analyze_exit_reason_breakdown(trades)
    analyze_spread_regime(trades)
    analyze_depth_regime(trades)
    analyze_volatility_regime(trades)
    analyze_lag_at_entry(trades)
    analyze_confidence(trades)
    analyze_combo_spread_x_vol(trades)
    analyze_per_pair(trades)

    print("\n" + "=" * 70)
    print("  HOW TO READ THIS")
    print("=" * 70)
    print("""
  Look for buckets where:
    - WR ≥ 55%, PnL clearly positive, PF ≥ 1.5  → ADD this filter to bot
    - WR ≤ 35%, PnL strongly negative           → SKIP this regime in bot

  If "tight spread" bucket is +EV but "wide spread" is -EV — implement a
  spread filter in pair_configs (skip trade if current spread > Nx median).

  If "calm vol" bucket is +EV but "explosive" is -EV — that's why bot is
  losing in 13-14 UTC active hours. Implement vol gate.

  If MEXC lag-at-entry "small lag" loses but "big lag" wins — raise
  min_mexc_lag_pct in pair config to filter out marginal signals.

  If exit_reason='stop_loss' dominates losses but mfe_pct shows trades
  often went +0.5% before reversing — quick_scalp at 0.4% would help.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
