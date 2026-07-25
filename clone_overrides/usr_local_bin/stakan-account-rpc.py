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
  {"op": "add",     "webkey": "WEB...", "label": "..."}  → new
                    (NO proxy arg — data.add_account(webkey, label) only)

  {"op": "pairs_list"}                                → list pairs
  {"op": "pairs_set",  "symbol": "X", ...fields…}     → edit pair config
                                                        (mode/margin_*/leverage_*)
  {"op": "slot_sizing_set", "symbol": "X", "slot_id": N, ...fields…}
                                                      → per-(slot,pair) sizing
  {"op": "assign_pair", "slot_id": N, "pair": "X"|null} → assign / unassign
"""
import sys, json, os
from pathlib import Path

# The panel parses THIS process's stdout as JSON (data._remote_rpc does
# json.loads(proc.stdout)). A stray print()/library banner on stdout corrupts
# it and makes a SUCCESSFUL rpc look like a failure to the operator. Grab the
# real stdout handle once, then point sys.stdout at stderr so every accidental
# write lands on the SSH stderr channel instead. Done BEFORE the imports below
# so import-time chatter is covered too.
_RESULT_OUT = sys.stdout
sys.stdout = sys.stderr

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
        # Parity with the primary path (data.remove_account_routed): the helper
        # returns False when the slot was ALREADY empty — report that instead
        # of a fake success (the panel turns ok=False into a 400).
        ok = data.remove_account(sid)
        if not ok:
            # Give the panel a distinguishable reason: "already empty" must not
            # look identical to an SSH/transport failure.
            return {"ok": False, "error": f"slot {sid} already empty"}
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
    if op == "kill_all":
        # Panel KILL ALL fans out here: demote every LIVE pair on THIS bot.
        return {"ok": True, "demoted": data.set_all_pairs_shadow()}
    return {"ok": False, "error": f"unknown op: {op!r}"}

try:
    req = json.loads(sys.stdin.read() or "{}")
    _result = handle(req)
except Exception as e:
    _result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
try:
    _payload = json.dumps(_result, default=str)
except Exception as e:  # unserialisable result → still answer with valid JSON
    _payload = json.dumps({"ok": False, "error": f"json encode failed: {e}"})
_RESULT_OUT.write(_payload + "\n")
_RESULT_OUT.flush()
