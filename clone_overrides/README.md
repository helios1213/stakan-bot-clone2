# clone_overrides

Files that live OUTSIDE the bot tree on the clone host but are part of this
clone's setup. Tracked here so a full disk-wipe is recoverable from git.

## Contents
- `usr_local_bin/stakan-account-rpc.py`  → deploy to `/usr/local/bin/` (SSH-RPC target the primary panel calls: list/set/remove/add accounts, pairs_list/pairs_set, slot_sizing_set).
- `usr_local_bin/stakan-state-export.py`  → deploy to `/usr/local/bin/` (writes `/root/state-export.json`: accounts, pairs, recent_trades(25), pnl_24h, live_summary). Run by the timer below.
- `systemd/stakan-state-export.service` + `.timer` → deploy to `/etc/systemd/system/`, then `systemctl daemon-reload && systemctl enable --now stakan-state-export.timer`.

⚠️ **Спершу бот, потім таймер.** Експортер і RPC працюють від root: якщо запустити таймер ДО першого старту бота, `sqlite3.connect` створить порожній `data/stakan-live.db` з власником root, і бот (uid 1000) впаде з `attempt to write a readonly database` (спіймано на clone2 2026-09-15; лік — `chown 1000:1000` порожнього файлу).

## Redeploy after a wipe
```
cp clone_overrides/usr_local_bin/*.py /usr/local/bin/ && chmod +x /usr/local/bin/stakan-*.py
cp clone_overrides/systemd/stakan-state-export.* /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now stakan-state-export.timer
```
