"""
Deep per-pair analyzer — generates a per-pair strategy recommendation
by combining ALL available data sources:

  1. shadow_trades — our own simulated trades (entry, exit, PnL, MFE, MAE)
  2. signals — detector firings
  3. historical_candles — 7-day 1m OHLCV from MEXC + Binance
  4. live_orderbook_snapshots — top-5 orderbook depth, spreads
  5. pair_states — current state, paused_until

Output: a structured config recommendation with rationale.

Usage:
    python scripts/deep_analyze.py ZECUSDT
    python scripts/deep_analyze.py --all
    python scripts/deep_analyze.py ZECUSDT --hours 168 --out /tmp/zec.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, stdev


DEFAULT_PAIRS = [
    "ZECUSDT",
    "TAOUSDT",
    "1000PEPEUSDT",
    "ENAUSDT",
    "PENGUUSDT",
    "BCHUSDT",
]


def fetch_shadow_trades(conn: sqlite3.Connection, symbol: str, hours: int) -> list[dict]:
    cutoff = int(time.time()) - hours * 3600
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM shadow_trades
         WHERE symbol = ?
           AND opened_at >= ?
           AND closed_at IS NOT NULL
         ORDER BY opened_at
        """,
        (symbol, cutoff),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_historical_candles(
    conn: sqlite3.Connection, symbol: str, hours: int, exchange: str = "mexc"
) -> list[dict]:
    cutoff_ms = (int(time.time()) - hours * 3600) * 1000
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT open_time, open, high, low, close, volume, quote_volume, trades
              FROM historical_candles
             WHERE symbol = ?
               AND exchange = ?
               AND open_time >= ?
             ORDER BY open_time
            """,
            (symbol, exchange, cutoff_ms),
        )
    except sqlite3.OperationalError:
        return []  # table doesn't exist yet
    return [
        {
            "open_time": r[0], "open": r[1], "high": r[2], "low": r[3],
            "close": r[4], "volume": r[5], "quote_volume": r[6], "trades": r[7],
        }
        for r in cur.fetchall()
    ]


def fetch_orderbook_snapshots(
    conn: sqlite3.Connection, symbol: str, hours: int, exchange: str = "mexc"
) -> list[dict]:
    cutoff_ms = (int(time.time()) - hours * 3600) * 1000
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT ts_ms, mid_price, spread_pct, bid_depth_5, ask_depth_5
              FROM live_orderbook_snapshots
             WHERE symbol = ?
               AND exchange = ?
               AND ts_ms >= ?
             ORDER BY ts_ms
            """,
            (symbol, exchange, cutoff_ms),
        )
    except sqlite3.OperationalError:
        return []
    return [
        {"ts_ms": r[0], "mid": r[1], "spread_pct": r[2],
         "bid_depth": r[3], "ask_depth": r[4]}
        for r in cur.fetchall()
    ]


def fetch_signals(conn: sqlite3.Connection, symbol: str, hours: int) -> list[dict]:
    cutoff = int(time.time()) - hours * 3600
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT * FROM signals
             WHERE symbol = ? AND ts >= ?
             ORDER BY ts
            """,
            (symbol, cutoff),
        )
    except sqlite3.OperationalError:
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ============================================================
# Analysis modules
# ============================================================

def analyze_volatility(candles: list[dict]) -> dict:
    """Volatility profile — distribution of 1m candle ranges."""
    if not candles:
        return {"available": False}

    pcts = []
    big_candles_per_hour = defaultdict(int)  # hour_utc → count of >0.3% candles

    for c in candles:
        if c["open"] <= 0:
            continue
        rng_pct = (c["high"] - c["low"]) / c["open"] * 100
        pcts.append(rng_pct)
        if rng_pct >= 0.3:
            hour_utc = (c["open_time"] // 1000 // 3600) % 24
            big_candles_per_hour[hour_utc] += 1

    if not pcts:
        return {"available": False}

    pcts_sorted = sorted(pcts)
    n = len(pcts_sorted)
    return {
        "available": True,
        "n_candles": n,
        "avg_range_pct": round(mean(pcts), 4),
        "median_range_pct": round(median(pcts), 4),
        "p75_range_pct": round(pcts_sorted[int(n * 0.75)], 4),
        "p95_range_pct": round(pcts_sorted[int(n * 0.95)], 4),
        "p99_range_pct": round(pcts_sorted[int(n * 0.99)], 4),
        "big_candles_per_hour_utc": dict(big_candles_per_hour),
        "big_candles_total": sum(big_candles_per_hour.values()),
    }


def analyze_hour_of_day(trades: list[dict]) -> dict:
    """Per-hour-UTC win rate and PnL for 24h cycle."""
    if not trades:
        return {"available": False}

    hourly = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        if not t.get("opened_at"):
            continue
        hour = (t["opened_at"] // 3600) % 24
        h = hourly[hour]
        h["trades"] += 1
        h["pnl"] += t.get("net_pnl_usdt") or 0.0
        if (t.get("net_pnl_usdt") or 0) > 0:
            h["wins"] += 1

    out = {}
    best_hours = []
    worst_hours = []
    for hour, d in hourly.items():
        if d["trades"] == 0:
            continue
        wr = 100.0 * d["wins"] / d["trades"]
        avg_pnl = d["pnl"] / d["trades"]
        out[hour] = {
            "trades": d["trades"],
            "win_pct": round(wr, 1),
            "total_pnl": round(d["pnl"], 2),
            "avg_pnl": round(avg_pnl, 4),
        }
        if d["pnl"] > 0 and d["trades"] >= 5:
            best_hours.append((hour, d["pnl"]))
        elif d["pnl"] < -1.0 and d["trades"] >= 5:
            worst_hours.append((hour, d["pnl"]))

    best_hours.sort(key=lambda x: -x[1])
    worst_hours.sort(key=lambda x: x[1])

    return {
        "available": True,
        "by_hour": out,
        "best_hours_utc": [h for h, _ in best_hours[:5]],
        "worst_hours_utc": [h for h, _ in worst_hours[:5]],
    }


def analyze_detector_performance(trades: list[dict]) -> dict:
    """Win rate per detector source."""
    if not trades:
        return {"available": False}

    by_det = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        det = t.get("detector_source") or "unknown"
        d = by_det[det]
        d["n"] += 1
        pnl = t.get("net_pnl_usdt") or 0
        d["pnl"] += pnl
        if pnl > 0:
            d["wins"] += 1

    out = {}
    for det, d in by_det.items():
        if d["n"] == 0:
            continue
        out[det] = {
            "trades": d["n"],
            "win_pct": round(100.0 * d["wins"] / d["n"], 1),
            "total_pnl": round(d["pnl"], 2),
            "avg_pnl": round(d["pnl"] / d["n"], 4),
        }
    return {"available": True, "by_detector": out}


def analyze_exit_reasons(trades: list[dict]) -> dict:
    """Win rate and PnL by exit reason."""
    if not trades:
        return {"available": False}

    by_exit = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        ex = t.get("exit_reason") or "unknown"
        d = by_exit[ex]
        d["n"] += 1
        pnl = t.get("net_pnl_usdt") or 0
        d["pnl"] += pnl
        if pnl > 0:
            d["wins"] += 1

    out = {}
    for ex, d in by_exit.items():
        if d["n"] == 0:
            continue
        out[ex] = {
            "trades": d["n"],
            "win_pct": round(100.0 * d["wins"] / d["n"], 1),
            "total_pnl": round(d["pnl"], 2),
        }
    return {"available": True, "by_exit": out}


def analyze_mfe_mae(trades: list[dict]) -> dict:
    """
    Max favorable / adverse excursion analysis.

    Uses peak_roi_pct and trough_roi_pct (ROI on margin, with leverage applied)
    rather than raw price movement — these are the actually meaningful numbers
    for tuning SL/TP.

    Tells us:
    - Where most trades top out (helps tune TP)
    - Where most losers bottom (helps tune SL)
    """
    if not trades:
        return {"available": False}

    # peak_roi_pct = highest ROI reached (positive); trough_roi_pct = lowest (negative)
    peaks = [t.get("peak_roi_pct") or 0 for t in trades]
    troughs = [t.get("trough_roi_pct") or 0 for t in trades]

    if not peaks:
        return {"available": False}

    peaks_sorted = sorted(peaks, reverse=True)  # high to low
    troughs_sorted = sorted(troughs)  # low (negative) to high
    n = len(peaks_sorted)

    return {
        "available": True,
        "n_trades": n,
        # MFE — peak ROI reached (top 25% of trades reach this+)
        "mfe_p25": round(peaks_sorted[int(n * 0.25)], 3),
        "mfe_p50": round(peaks_sorted[int(n * 0.50)], 3),
        "mfe_p75": round(peaks_sorted[int(n * 0.75)], 3),
        # MAE — worst trough ROI (worst 25% drop to this)
        "mae_p25": round(troughs_sorted[int(n * 0.25)], 3),
        "mae_p50": round(troughs_sorted[int(n * 0.50)], 3),
        "mae_p75": round(troughs_sorted[int(n * 0.75)], 3),
    }


def analyze_orderbook(snapshots: list[dict]) -> dict:
    """Spread distribution, depth profile."""
    if not snapshots:
        return {"available": False}

    spreads = [s["spread_pct"] for s in snapshots if s["spread_pct"] is not None]
    bid_depths = [s["bid_depth"] for s in snapshots if s["bid_depth"] is not None]
    ask_depths = [s["ask_depth"] for s in snapshots if s["ask_depth"] is not None]

    if not spreads:
        return {"available": False}

    spreads_sorted = sorted(spreads)
    n = len(spreads_sorted)

    return {
        "available": True,
        "n_snapshots": n,
        "spread_p50_pct": round(spreads_sorted[int(n * 0.50)], 4),
        "spread_p95_pct": round(spreads_sorted[int(n * 0.95)], 4),
        "avg_bid_depth_5": round(mean(bid_depths), 2) if bid_depths else None,
        "avg_ask_depth_5": round(mean(ask_depths), 2) if ask_depths else None,
    }


# ============================================================
# Recommendation engine
# ============================================================

def generate_recommendation(symbol: str, analyses: dict) -> dict:
    """
    Combine analyses into actionable config recommendation.

    Returns a dict with:
    - pair: symbol
    - active_hours: [hour_utc, ...] when to trade
    - paused_hours: hours to skip
    - sl_pct: recommended stop loss
    - trailing_activation_pct: when to start trailing
    - trailing_distance_pct: trailing distance
    - quick_scalp_pct / quick_scalp_sec: quick exit threshold
    - min_volatility_pct: skip flat market
    - prefer_detector: best detector for this pair
    - rationale: human-readable explanation
    """
    rec = {
        "pair": symbol,
        "rationale": [],
    }

    # --- Active hours ---
    hod = analyses.get("hour_of_day", {})
    if hod.get("available") and hod.get("by_hour"):
        # Pick hours with avg_pnl > 0 and ≥ 5 trades
        good_hours = []
        bad_hours = []
        for h, d in hod["by_hour"].items():
            if d["trades"] >= 5:
                if d["total_pnl"] > 0:
                    good_hours.append(int(h))
                elif d["total_pnl"] < 0:
                    bad_hours.append(int(h))
        rec["active_hours"] = sorted(good_hours)
        rec["paused_hours"] = sorted(bad_hours)
        if good_hours:
            rec["rationale"].append(
                f"Profitable hours UTC: {sorted(good_hours)}"
            )
        if bad_hours:
            rec["rationale"].append(
                f"Loss-making hours UTC: {sorted(bad_hours)} → AVOID"
            )

    # --- SL/TP from MFE/MAE (in ROI terms — already includes leverage) ---
    mfe_mae = analyses.get("mfe_mae", {})
    if mfe_mae.get("available"):
        # MAE is trough ROI (negative). p50 = median worst, p25 = quarter that went lowest.
        mae_p50 = mfe_mae.get("mae_p50", -2.0)
        mae_p25 = mfe_mae.get("mae_p25", -3.0)
        # SL: between p25 and p50 — protects against majority of losers
        # but doesn't kill quick mean-revertors. Bound: -5% to -0.5%.
        proposed_sl = (mae_p25 + mae_p50) / 2
        rec["sl_pct"] = round(max(-5.0, min(-0.5, proposed_sl)), 2)
        rec["rationale"].append(
            f"Trough ROI p25={mae_p25}%, p50={mae_p50}% → recommend SL={rec['sl_pct']}%"
        )

        # MFE is peak ROI (positive). p50 = median peak — set quick_scalp at 70% of this.
        mfe_p50 = mfe_mae.get("mfe_p50", 1.5)
        # Bounds: 0.3% to 5%
        rec["quick_scalp_pct"] = round(max(0.3, min(5.0, mfe_p50 * 0.7)), 2)
        rec["trailing_activation_pct"] = round(max(0.3, min(3.0, mfe_p50 * 0.5)), 2)
        rec["trailing_distance_pct"] = round(rec["trailing_activation_pct"] * 0.4, 2)
        rec["rationale"].append(
            f"Peak ROI p50={mfe_p50}% → quick_scalp@{rec['quick_scalp_pct']}%, "
            f"trailing activate@{rec['trailing_activation_pct']}% dist={rec['trailing_distance_pct']}%"
        )

    # --- Volatility filter ---
    vol = analyses.get("volatility", {})
    if vol.get("available"):
        # If avg 1m range < 0.04%, market is too flat for our edge
        median_range = vol.get("median_range_pct", 0.05)
        rec["min_volatility_pct"] = round(median_range * 0.7, 4)
        rec["rationale"].append(
            f"Median 1m range = {median_range}% → min_volatility={rec['min_volatility_pct']}%"
        )
        big_per_hour = vol.get("big_candles_per_hour_utc", {})
        if big_per_hour:
            top_vol_hours = sorted(big_per_hour.items(), key=lambda x: -x[1])[:5]
            rec["rationale"].append(
                f"Most volatile hours UTC: {[h for h, _ in top_vol_hours]}"
            )

    # --- Best detector ---
    det = analyses.get("detector", {})
    if det.get("available") and det.get("by_detector"):
        # Best by total_pnl if ≥ 20 trades
        viable = [
            (name, d) for name, d in det["by_detector"].items()
            if d["trades"] >= 20
        ]
        if viable:
            viable.sort(key=lambda x: -x[1]["total_pnl"])
            best = viable[0]
            rec["prefer_detector"] = best[0]
            rec["rationale"].append(
                f"Best detector for {symbol}: {best[0]} ({best[1]['win_pct']}% win, "
                f"${best[1]['total_pnl']} PnL on {best[1]['trades']} trades)"
            )
            # Detectors UNDERPERFORMING for THIS pair (negative PnL)
            # Note: this is per-pair, not a global verdict.
            # A detector losing on ZEC may win on TAO.
            underperforming = [
                {"name": name, "trades": d["trades"], "win_pct": d["win_pct"],
                 "pnl": d["total_pnl"]}
                for name, d in viable if d["total_pnl"] < -1.0
            ]
            if underperforming:
                rec["underperforming_detectors"] = underperforming
                names = [u["name"] for u in underperforming]
                rec["rationale"].append(
                    f"Underperforming for {symbol} (consider per-pair filter): {names} "
                    f"— other pairs may differ, run --all to compare"
                )
        # Add note if data is thin
        total_trades = sum(d["trades"] for d in det["by_detector"].values())
        if total_trades < 100:
            rec["rationale"].append(
                f"⚠️ Only {total_trades} trades analyzed — recommendations preliminary"
            )

    # --- Spread filter ---
    ob = analyses.get("orderbook", {})
    if ob.get("available"):
        rec["typical_spread_pct"] = ob.get("spread_p50_pct")
        rec["rationale"].append(
            f"Typical spread: {ob['spread_p50_pct']}%, p95={ob['spread_p95_pct']}%"
        )

    # --- Final verdict ---
    trades = analyses.get("shadow_summary", {})
    if trades.get("n_trades", 0) > 0:
        net = trades.get("total_pnl", 0)
        wr = trades.get("win_pct", 0)
        if net > 0 and wr >= 50:
            rec["verdict"] = "✅ Edge present — proceed to live (small)"
        elif net > 0 and wr < 50:
            rec["verdict"] = "🟡 Marginal — refine config first"
        elif net < -5:
            rec["verdict"] = "❌ Negative edge — pause until config improves"
        else:
            rec["verdict"] = "⚪ Inconclusive — need more data"

    return rec


# ============================================================
# Per-pair runner
# ============================================================

def shadow_summary(trades: list[dict]) -> dict:
    if not trades:
        return {"n_trades": 0}
    n = len(trades)
    wins = sum(1 for t in trades if (t.get("net_pnl_usdt") or 0) > 0)
    pnl = sum((t.get("net_pnl_usdt") or 0) for t in trades)
    return {
        "n_trades": n,
        "win_pct": round(100.0 * wins / n, 1),
        "total_pnl": round(pnl, 2),
        "avg_pnl_per_trade": round(pnl / n, 4),
    }


def analyze_pair(conn: sqlite3.Connection, symbol: str, hours: int) -> dict:
    trades = fetch_shadow_trades(conn, symbol, hours)
    candles_mexc = fetch_historical_candles(conn, symbol, hours, "mexc")
    snapshots = fetch_orderbook_snapshots(conn, symbol, hours, "mexc")

    analyses = {
        "shadow_summary": shadow_summary(trades),
        "volatility": analyze_volatility(candles_mexc),
        "hour_of_day": analyze_hour_of_day(trades),
        "detector": analyze_detector_performance(trades),
        "exit_reason": analyze_exit_reasons(trades),
        "mfe_mae": analyze_mfe_mae(trades),
        "orderbook": analyze_orderbook(snapshots),
    }
    rec = generate_recommendation(symbol, analyses)

    return {
        "symbol": symbol,
        "analyzed_at": int(time.time()),
        "lookback_hours": hours,
        "data_sources": {
            "shadow_trades": len(trades),
            "historical_candles": len(candles_mexc),
            "orderbook_snapshots": len(snapshots),
        },
        "analyses": analyses,
        "recommendation": rec,
    }


def print_report(result: dict) -> None:
    """Pretty-print analysis to stdout."""
    sym = result["symbol"]
    src = result["data_sources"]
    rec = result["recommendation"]

    print()
    print("=" * 70)
    print(f"  DEEP ANALYSIS: {sym}")
    print(f"  Lookback: {result['lookback_hours']}h")
    print("=" * 70)
    print()
    print(f"Data sources:")
    print(f"  Shadow trades:     {src['shadow_trades']}")
    print(f"  Historical candles: {src['historical_candles']}")
    print(f"  Orderbook snapshots: {src['orderbook_snapshots']}")
    print()

    summary = result["analyses"].get("shadow_summary", {})
    if summary.get("n_trades"):
        print(f"Shadow performance: {summary['n_trades']} trades, "
              f"{summary['win_pct']}% win, ${summary['total_pnl']} PnL")
    print()

    vol = result["analyses"].get("volatility", {})
    if vol.get("available"):
        print(f"Volatility (1m candle range):")
        print(f"  median: {vol['median_range_pct']}%  p95: {vol['p95_range_pct']}%")
        print(f"  big candles (>0.3%): {vol['big_candles_total']} in {result['lookback_hours']}h")
        print()

    mfe_mae = result["analyses"].get("mfe_mae", {})
    if mfe_mae.get("available"):
        print(f"ROI excursion (peak/trough ROI on margin):")
        print(f"  Peak ROI:    p50={mfe_mae['mfe_p50']:>6.2f}%  p75={mfe_mae['mfe_p75']:>6.2f}%")
        print(f"  Trough ROI:  p50={mfe_mae['mae_p50']:>6.2f}%  p25={mfe_mae['mae_p25']:>6.2f}%")
        print()

    hod = result["analyses"].get("hour_of_day", {})
    if hod.get("available"):
        print(f"Best hours UTC: {hod.get('best_hours_utc')}")
        print(f"Worst hours UTC: {hod.get('worst_hours_utc')}")
        print()

    det = result["analyses"].get("detector", {})
    if det.get("available"):
        print(f"Detector performance:")
        for name, d in sorted(det["by_detector"].items(), key=lambda x: -x[1]["total_pnl"]):
            print(f"  {name:30s}  {d['trades']:>4} trades  "
                  f"{d['win_pct']:>5.1f}% win  ${d['total_pnl']:>+8.2f}")
        print()

    print("─" * 70)
    print("RECOMMENDATION")
    print("─" * 70)
    if "verdict" in rec:
        print(f"Verdict: {rec['verdict']}")
        print()
    if "active_hours" in rec:
        print(f"Active hours UTC:  {rec['active_hours']}")
    if "paused_hours" in rec:
        print(f"Paused hours UTC:  {rec['paused_hours']}")
    if "sl_pct" in rec:
        print(f"Stop loss:         {rec['sl_pct']}%")
    if "quick_scalp_pct" in rec:
        print(f"Quick scalp:       {rec['quick_scalp_pct']}%")
    if "trailing_activation_pct" in rec:
        print(f"Trailing activate: {rec['trailing_activation_pct']}%")
        print(f"Trailing distance: {rec['trailing_distance_pct']}%")
    if "min_volatility_pct" in rec:
        print(f"Min volatility:    {rec['min_volatility_pct']}%")
    if "prefer_detector" in rec:
        print(f"Best detector:     {rec['prefer_detector']}")
    if "underperforming_detectors" in rec:
        print(f"Underperforming detectors (per-pair, not global):")
        for u in rec["underperforming_detectors"]:
            print(f"  - {u['name']:30s}  {u['trades']:>3} trades, "
                  f"{u['win_pct']:>4.1f}% win, ${u['pnl']:+.2f}")
    print()
    print("Rationale:")
    for line in rec.get("rationale", []):
        print(f"  • {line}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Deep per-pair analyzer")
    parser.add_argument("symbol", nargs="?", default=None,
                        help="Pair to analyze (e.g. ZECUSDT)")
    parser.add_argument("--all", action="store_true",
                        help="Analyze all 6 standard pairs")
    parser.add_argument("--hours", type=int, default=24,
                        help="Lookback hours (default 24)")
    parser.add_argument("--db", default="/app/data/stakan.db",
                        help="DB path")
    parser.add_argument("--out", default=None,
                        help="Output JSON file (in addition to stdout)")
    args = parser.parse_args()

    if not args.symbol and not args.all:
        parser.error("Provide a symbol or use --all")

    conn = sqlite3.connect(args.db)
    pairs = DEFAULT_PAIRS if args.all else [args.symbol.upper()]

    all_results = []
    for sym in pairs:
        result = analyze_pair(conn, sym, args.hours)
        print_report(result)
        all_results.append(result)

    # Cross-pair detector matrix (when running --all)
    if len(all_results) > 1:
        print()
        print("=" * 78)
        print("  CROSS-PAIR DETECTOR PERFORMANCE MATRIX")
        print("=" * 78)
        print()

        # Collect all detector names
        all_detectors: set[str] = set()
        for r in all_results:
            det = r["analyses"].get("detector", {})
            if det.get("available"):
                all_detectors.update(det["by_detector"].keys())

        # Print header
        det_list = sorted(all_detectors)
        print(f"{'pair':<14}", end="")
        for d in det_list:
            print(f"  {d[:18]:<18}", end="")
        print()
        print("-" * (14 + 20 * len(det_list)))

        # Print one row per pair with PnL per detector
        for r in all_results:
            sym = r["symbol"]
            print(f"{sym:<14}", end="")
            det = r["analyses"].get("detector", {})
            if det.get("available"):
                by_det = det["by_detector"]
                for d in det_list:
                    if d in by_det:
                        info = by_det[d]
                        cell = f"${info['total_pnl']:+.1f}/{info['trades']}t"
                        print(f"  {cell:<18}", end="")
                    else:
                        print(f"  {'—':<18}", end="")
            print()
        print()
        print("Format: $PnL/Ntrades. '—' = no trades.")
        print("Use this to identify which detector works for which pair.")
        print()

    if args.out:
        Path(args.out).write_text(json.dumps(all_results, indent=2))
        print(f"\n💾 Saved JSON: {args.out}")


if __name__ == "__main__":
    main()
