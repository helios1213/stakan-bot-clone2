#!/usr/bin/env python3
"""Account RPC handler — invoked over SSH from the primary panel.

Reads a JSON command from stdin, performs the action via webpanel.data
(which uses THIS bot's MASTER_KEY from .env for credential encryption),
writes the result as JSON to stdout. Returning `{"ok": True, ...}` on
success or `{"ok": False, "error": "..."}` on failure.

Supported ops:
  {"op": "list"}                              → list accounts
  {"op": "set",     "slot_id": N, ...fields…} → edit (enabled/label/live_enabled)
  {"op": "remove",  "slot_id": N}             → wipe slot
  {"op": "add",     "webkey": "WEB...", "label": "...", "proxy": "..."}  → new

  {"op": "pairs_list"}                                → list pairs
  {"op": "pairs_set",  "symbol": "X", ...fields…}     → edit pair config
                                                        (mode/margin_*/leverage_*)
"""
import sys, json, os
from pathlib import Path

sys.path.insert(0, "/root/stakan-bot")
ENV = Path("/root/stakan-bot/.env")
if ENV.exists():
    for ln in ENV.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, _, v = ln.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.webpanel import data  # noqa: E402

def handle(req: dict) -> dict:
    op = req.get("op")
    if op == "list":
        return {"ok": True, "accounts": data.accounts()}
    if op == "set":
        sid = int(req["slot_id"])
        kwargs = {k: req[k] for k in ("enabled", "label", "live_enabled") if k in req}
        data.set_account(sid, **kwargs)
        return {"ok": True}
    if op == "remove":
        sid = int(req["slot_id"])
        data.remove_account(sid)
        return {"ok": True}
    if op == "add":
        res = data.add_account(
            webkey=req.get("webkey", ""),
            label=req.get("label") or None,
        )
        return {"ok": True, **res}
    if op == "pairs_list":
        return {"ok": True, "pairs": data.pairs()}
    if op == "pairs_set":
        sym = req["symbol"]
        kwargs = {k: v for k, v in req.items() if k not in ("op", "symbol")}
        data.set_pair_config(sym, **kwargs)
        return {"ok": True}
    if op == "slot_sizing_set":
        sym = req["symbol"]; sid = int(req["slot_id"])
        kwargs = {k: v for k, v in req.items() if k not in ("op", "symbol", "slot_id")}
        return data.set_slot_pair_sizing(sym, sid, **kwargs)
    if op == "assign_pair":
        data.assign_pair(int(req["slot_id"]), req.get("pair"))
        return {"ok": True}
    return {"ok": False, "error": f"unknown op: {op!r}"}

try:
    req = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(handle(req), default=str))
except Exception as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
