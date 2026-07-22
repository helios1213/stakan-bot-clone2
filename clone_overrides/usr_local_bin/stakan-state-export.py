#!/usr/bin/env python3
"""Periodic state exporter — writes a JSON snapshot of this bot's DB state so
the central panel on the primary server can aggregate it. Stdlib only."""
import sqlite3, json, time, os, sys
import datetime
from zoneinfo import ZoneInfo

SERVER_NAME = sys.argv[1] if len(sys.argv) > 1 else "clone1"
SHADOW = "/root/stakan-bot/data/stakan.db"
LIVE   = "/root/stakan-bot/data/stakan-live.db"
OUT    = "/root/state-export.json"


def today_kyiv_cutoff() -> int:
    """Epoch of today's 00:00 in Europe/Kyiv (DST-aware via stdlib zoneinfo)."""
    now = datetime.datetime.now(ZoneInfo("Europe/Kyiv"))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def rows(conn, q, *args):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(q, args)]

out = {"ts": int(time.time()), "server": SERVER_NAME,
       "accounts": [], "pairs": [], "recent_trades": [], "pnl_24h": {}}
try:
    db = sqlite3.connect(SHADOW)
    out["accounts"] = rows(db,
        "SELECT slot_id,label,assigned_pair,live_enabled,last_balance_usdt,"
        "last_latency_ms,last_health_check,last_error,"
        "(webkey_blob IS NOT NULL) AS has_key "
        "FROM webkey_slots ORDER BY slot_id")
    out["pairs"] = rows(db,
        "SELECT symbol,state,last_24h_trades,last_24h_winrate,last_24h_pnl "
        "FROM pair_states ORDER BY symbol")
    db.close()
except Exception as e:
    out["error_shadow"] = str(e)
try:
    live = sqlite3.connect(LIVE)
    # Reset at Kyiv midnight (was: rolling 24h). Field name kept for compat.
    cutoff = today_kyiv_cutoff()
    for r in rows(live,
        "SELECT account_label,COALESCE(SUM(net_pnl_usdt),0) AS pnl,"
        "SUM(CASE WHEN net_pnl_usdt>0 THEN 1 ELSE 0 END) AS wins,"
        "COUNT(*) AS n FROM live_trades "
        "WHERE opened_at>=? AND account_label IS NOT NULL "
        "GROUP BY account_label", cutoff):
        out["pnl_24h"][r["account_label"]] = {
            "pnl": float(r["pnl"] or 0),
            "wins": int(r["wins"] or 0),
            "n":    int(r["n"] or 0)}
    out["recent_trades"] = rows(live,
        "SELECT id,opened_at,closed_at,symbol,direction,leverage,margin_usdt,"
        "net_pnl_usdt,roi_pct,exit_reason,duration_sec,account_label "
        "FROM live_trades ORDER BY id DESC LIMIT 25")
    _ls = rows(live, "SELECT COUNT(*) AS n, COALESCE(SUM(net_pnl_usdt),0) AS pnl FROM live_trades")[0]
    _lst = rows(live, "SELECT COUNT(*) AS n, COALESCE(SUM(net_pnl_usdt),0) AS pnl FROM live_trades WHERE opened_at>=?", cutoff)[0]
    out["live_summary"] = {"trades": int(_ls["n"] or 0), "net_pnl_usdt": float(_ls["pnl"] or 0),
                           "trades_today": int(_lst["n"] or 0), "net_pnl_today_usdt": float(_lst["pnl"] or 0)}
    live.close()
except Exception as e:
    out["error_live"] = str(e)

tmp = OUT + ".tmp"
with open(tmp, "w") as f: json.dump(out, f, default=str)
os.replace(tmp, OUT)
