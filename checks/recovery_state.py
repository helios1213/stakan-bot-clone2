# -*- coding: utf-8 -*-
"""Поточний стан монітора відновлення по слотах."""
import datetime
import sqlite3

c = sqlite3.connect("file:/root/stakan-bot/data/stakan.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row


def ts(v):
    return datetime.datetime.utcfromtimestamp(v).strftime("%m-%d %H:%M") if v else "—"


for r in c.execute(
        "SELECT slot_id, assigned_pair, enabled, live_enabled, webkey_blob IS NOT NULL AS has_key,"
        "       webkey_refreshed_at, open_throttle_until, recovery_mode, recovery_cap_usdt,"
        "       recovery_buffer_usdt, recovery_baseline_ts, recovery_target_usdt"
        "  FROM webkey_slots ORDER BY slot_id"):
    d = dict(r)
    print(f"слот {d['slot_id']}  пара={d['assigned_pair']}  ключ={'є' if d['has_key'] else 'НЕМА'}  "
          f"enabled={d['enabled']} live={d['live_enabled']}")
    print(f"    ключ оновлено   {ts(d['webkey_refreshed_at'])}")
    print(f"    засувка до      {ts(d['open_throttle_until'])}")
    print(f"    recovery_mode   {d['recovery_mode']}")
    cap = d['recovery_cap_usdt']
    print(f"    cap (підлога)   {cap:,.0f}" + ("  → ВИМКНЕНО" if cap and cap > 1e5 else ""))
    print(f"    buf (ціль)      {d['recovery_buffer_usdt']}  → стоп, коли акаунт підніметься до -{d['recovery_buffer_usdt']}")
    print(f"    армлено         {ts(d['recovery_baseline_ts'])}   ціль {d['recovery_target_usdt']}")
    if d['recovery_baseline_ts'] and d['webkey_refreshed_at'] \
            and d['webkey_refreshed_at'] > d['recovery_baseline_ts']:
        print("    ⚠️ базова лінія СТАРІША за ключ — армлена для іншого акаунта")
    print()
