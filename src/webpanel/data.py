"""
Read/write helpers backing the non-simulator panel sections:
Аккаунты (webkey_slots), Dashboard (aggregates), Инциденты (panel_incidents),
Пользователи (panel access whitelist).

Stdlib sqlite3. Secrets (webkey/proxy blobs) are NEVER returned — only presence
flags and non-sensitive status columns.
"""
from __future__ import annotations

import logging

import json
import os
import re
import secrets
import sqlite3
import string
import time
import functools
import random
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB = str(REPO_ROOT / "data" / "stakan.db")
LIVE_DB = str(REPO_ROOT / "data" / "stakan-live.db")
ENV_FILE = REPO_ROOT / ".env"

# Webkey credential model (mirrors src/execution/webkey/credentials.py):
# the only user-provided secret is the webkey (WEB + 64 hex) — it's the value
# of the `u_id` / `uc_token` cookie on futures.mexc.com. visitor_id is
# auto-generated; mhash/chash/uid are derived at request time by the live
# client. We encrypt with Fernet(MASTER_KEY), the SAME format the live client
# decrypts, so a slot added here is usable for live trading without a terminal.
MAX_ACCOUNT_SLOTS = int(os.environ.get("STAKAN_MAX_SLOTS", "10"))
_WEBKEY_RE = re.compile(r"^WEB[0-9a-fA-F]{64}$")
WHITELIST_FILE = REPO_ROOT / "data" / "panel_whitelist.json"
ROTATION_FILE = REPO_ROOT / "data" / "rotation.json"

INCIDENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS panel_incidents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  severity TEXT NOT NULL,
  source TEXT,
  message TEXT NOT NULL,
  acked INTEGER NOT NULL DEFAULT 0
);
"""


def _ro(db: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _rw(db: str) -> sqlite3.Connection:
    """Read-write panel connection with a generous busy wait. The bot writes
    signal_features to this SAME stakan.db continuously (and the prune thread
    does a wal_checkpoint that can hold the writer >5s), so sqlite3's 5s default
    lets a panel control-write fail with 'database is locked'. Wait up to 8s.
    Keep the DEFAULT isolation_level: these functions SELECT-then-UPDATE on one
    connection; a snapshotting BEGIN would yield SQLITE_BUSY_SNAPSHOT, which no
    busy_timeout can fix. (2026-08-13.)"""
    c = sqlite3.connect(db, timeout=8.0)
    c.execute("PRAGMA busy_timeout=8000")
    return c


def _rw_retry(fn):
    """Retry a panel control-write that lost the writer-lock race. busy_timeout
    alone is not enough when the bot holds the lock past the timeout (prune /
    checkpoint), and disabling a slot / cutting leverage is a stop-the-loss
    action that must not fail on contention. Each attempt re-opens its own
    connection; sqlite3 rolls back the uncommitted txn on close, so a re-run is
    clean and these read-modify-write ops are idempotent."""
    @functools.wraps(fn)
    def _wrap(*a, **k):
        delay = 0.2
        for attempt in range(4):
            try:
                return fn(*a, **k)
            except sqlite3.OperationalError as e:
                m = str(e).lower()
                if ("locked" in m or "busy" in m) and attempt < 3:
                    time.sleep(delay + random.uniform(0.0, 0.05))
                    delay *= 2
                    continue
                raise
    return _wrap


# ---- Аккаунты ----
def accounts() -> list[dict]:
    try:
        conn = _ro(DB)
    except sqlite3.OperationalError:
        return []
    try:
        rows = conn.execute(
            "SELECT slot_id, label, enabled, live_enabled, assigned_pair, "
            "last_balance_usdt, last_latency_ms, last_health_check, last_error, "
            "(webkey_blob IS NOT NULL) AS has_key, (proxy_blob IS NOT NULL) AS has_proxy "
            "FROM webkey_slots ORDER BY slot_id"
        ).fetchall()
    finally:
        conn.close()
    def _num(v, cast):
        try:
            return cast(v) if v not in (None, "") else None
        except (ValueError, TypeError):
            return None
    out = []
    for r in rows:
        out.append({
            "slot_id": r["slot_id"], "label": r["label"] or f"slot {r['slot_id']}",
            "enabled": bool(r["enabled"]), "live_enabled": bool(r["live_enabled"]),
            "assigned_pair": r["assigned_pair"],
            "balance_usdt": _num(r["last_balance_usdt"], float),
            "latency_ms": _num(r["last_latency_ms"], int),
            "last_health_check": r["last_health_check"],
            "last_error": r["last_error"], "has_key": bool(r["has_key"]),
            "has_proxy": bool(r["has_proxy"]),
        })
    return out


@_rw_retry
def set_account(slot_id: int, *, enabled: bool | None = None,
                label: str | None = None, live_enabled: bool | None = None) -> None:
    conn = _rw(DB)
    try:
        # One upfront read: slot existence + assigned pair + credential presence.
        # A missing slot makes every UPDATE below a silent 0-row no-op (panel
        # shows "✓" while nothing changed) — refuse instead.
        _slot = conn.execute(
            "SELECT assigned_pair, (webkey_blob IS NOT NULL) FROM webkey_slots "
            "WHERE slot_id=?", (slot_id,)).fetchone()
        if _slot is None:
            raise AccountError(f"Слот {slot_id} не знайдено — зміну скасовано.")
        _ap, _has_key = _slot[0], bool(_slot[1])
        if enabled is not None:
            conn.execute("UPDATE webkey_slots SET enabled=?, updated_at=? WHERE slot_id=?",
                         (1 if enabled else 0, int(time.time()), slot_id))
        if label is not None:
            conn.execute("UPDATE webkey_slots SET label=?, updated_at=? WHERE slot_id=?",
                         (label, int(time.time()), slot_id))
        if live_enabled is not None:
            # Drives whether the bot routes live orders through this slot.
            _now = int(time.time())
            if live_enabled:
                # Mirrors Telegram's slot.is_complete gate: live on a slot with
                # no credential can never place an order.
                if not _has_key:
                    raise AccountError(
                        f"Слот {slot_id}: немає веб-ключа — вмикати live нічим. "
                        "Спершу підключіть акаунт."
                    )
                if not _ap:
                    raise AccountError(
                        f"Слот {slot_id}: пару не призначено — live не дасть жодної "
                        "угоди. Спершу призначте пару."
                    )
            _cur = conn.execute(
                "UPDATE webkey_slots SET live_enabled=?, updated_at=? WHERE slot_id=?",
                (1 if live_enabled else 0, _now, slot_id))
            if _cur.rowcount < 1:
                raise AccountError(
                    f"Слот {slot_id}: рядок не оновився — зміну скасовано.")
            # Complete panel go-live: PairStateManager.is_in_live checks
            # pair_states.state=='live'. Setting live_enabled alone would NOT
            # actually trade (pair stays shadow). Promote the slot's assigned
            # pair to 'live' when enabling, demote to 'shadow' when disabling
            # (so a turned-off slot never strands its pair live with no exec).
            if _ap:
                if live_enabled:
                    _cur = conn.execute(
                        "UPDATE pair_states SET state='live', state_since=?, updated_at=?, "
                        "last_state_change_reason=? WHERE symbol=?",
                        (_now, _now, "panel live toggle", _ap))
                    # 0 rows = no pair_states row for this symbol → the pair would
                    # stay OUT of live while the panel reported success. Raise
                    # BEFORE commit: sqlite3 rolls back on close, so live_enabled
                    # is not left on with a non-trading pair.
                    if _cur.rowcount < 1:
                        raise AccountError(
                            f"{_ap}: немає рядка в pair_states — live НЕ увімкнено "
                            f"(слот {slot_id} не змінено)."
                        )
                else:
                    # Only shadow the pair if NO other live slot still trades it.
                    # NOTE: deliberately NO rowcount check here — a failed demote
                    # must never block turning live OFF.
                    _demote_pair_if_orphaned(conn, _ap, slot_id, "panel live toggle off")
        conn.commit()
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Remote-bot RPC (full account integration: list/set/remove/add over SSH)
# ──────────────────────────────────────────────────────────────────────
# Each remote bot runs /usr/local/bin/stakan-account-rpc.py — the script reads
# a JSON command from stdin and returns a JSON result on stdout, using its own
# webpanel.data + MASTER_KEY (so credential encryption is correct for that bot).
# We invoke it over SSH from the primary. No HTTP ports exposed on the clone.
REMOTE_BOTS = ["clone1"]


def _demote_pair_if_orphaned(conn, symbol, keep_slot_id: int, reason: str) -> None:
    """Set pair_states.state='shadow' for `symbol` UNLESS another live_enabled
    slot (other than keep_slot_id) still trades it — otherwise disabling/
    reassigning/removing one slot would silently kill a sibling slot's live on
    the same pair. Same-conn (caller commits)."""
    if not symbol:
        return
    other = conn.execute(
        "SELECT 1 FROM webkey_slots WHERE assigned_pair=? AND live_enabled=1 "
        "AND slot_id!=? LIMIT 1", (symbol, keep_slot_id)).fetchone()
    if other:
        return  # a sibling slot still holds it live → keep it live
    now = int(time.time())
    conn.execute(
        "UPDATE pair_states SET state='shadow', state_since=?, updated_at=?, "
        "last_state_change_reason=? WHERE symbol=?", (now, now, reason, symbol))


def _remote_rpc(server: str, payload: dict, timeout: int = 8) -> dict:
    """SSH to `server` and run the account-RPC script, piping JSON in/out."""
    import subprocess
    try:
        proc = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes",
             server, "python3", "/usr/local/bin/stakan-account-rpc.py"],
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return {"ok": False, "error": f"ssh rc={proc.returncode}: {(proc.stderr or '')[:200]}"}
        return json.loads(proc.stdout or "{}")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ssh timeout"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def accounts_all() -> list[dict]:
    """Primary's accounts merged with every remote bot's accounts, each row
    tagged with a `server` field so the panel can route writes correctly.
    Remote rows include a `_stale` / `error` marker if the SSH call failed,
    so the operator sees the issue instead of a silent disappearance.
    """
    out = [{**a, "server": "primary"} for a in accounts()]
    for srv in REMOTE_BOTS:
        r = _remote_rpc(srv, {"op": "list"})
        if r.get("ok"):
            for a in r.get("accounts", []):
                out.append({**a, "server": srv})
        else:
            # Placeholder row so the table still shows the server name + reason.
            out.append({
                "server": srv, "slot_id": "—", "label": f"{srv} (unreachable)",
                "enabled": False, "live_enabled": False, "assigned_pair": None,
                "balance_usdt": None, "latency_ms": None, "last_error": r.get("error"),
                "has_key": False, "has_proxy": False, "_unreachable": True,
            })
    return out


def set_account_routed(server: str, slot_id: int, **kwargs) -> dict:
    if server == "primary":
        try:
            set_account(slot_id, **kwargs)
        except AccountError as e:
            # Surface the refusal as {"ok": False, "error": …} so app.py answers
            # 400 with the reason instead of a bare 500 (same shape the remote
            # RPC returns, and the same contract as set_pair_config_routed).
            return {"ok": False, "error": str(e)}
        return {"ok": True}
    return _remote_rpc(server, {"op": "set", "slot_id": slot_id, **kwargs})


@_rw_retry
def assign_pair(slot_id: int, pair: str | None) -> None:
    """Assign a pair to a slot (pair=None unassigns). Mirrors
    WebkeyStore.assign_pair: updates webkey_slots.assigned_pair AND auto-shadows
    the DISPLACED old pair via pair_states.state (else it's stranded live with no
    slot → silent [SKIP SHADOW]). The bot's _load_states_from_db (≤60s) reloads."""
    conn = _rw(DB)
    try:
        row = conn.execute(
            "SELECT assigned_pair, live_enabled, (webkey_blob IS NOT NULL) "
            "FROM webkey_slots WHERE slot_id=?", (slot_id,)).fetchone()
        if row is None:
            raise AccountError(f"Слот {slot_id} не знайдено — призначення скасовано.")
        old_pair, _slot_live, _has_key = row[0], bool(row[1]), bool(row[2])
        # Typo guard, same rule as the Telegram slot flow: the symbol must be in
        # live_pair_whitelist, else the slot ends up half-configured on a symbol
        # the bot will never trade. An absent/empty whitelist can't validate →
        # allow (never block the panel on a missing table).
        if pair:
            try:
                _wl = {r[0] for r in conn.execute("SELECT symbol FROM live_pair_whitelist")}
            except sqlite3.OperationalError:
                _wl = set()
            if _wl and pair not in _wl:
                raise AccountError(
                    f"{pair} немає в live_pair_whitelist — призначення скасовано "
                    "(перевірте символ)."
                )
        now = int(time.time())
        conn.execute("UPDATE webkey_slots SET assigned_pair=?, updated_at=? WHERE slot_id=?",
                     (pair, now, slot_id))
        if pair is None:
            # No pair = nothing this slot could execute. Clearing live here
            # keeps the slot out of the "live_enabled with no pair" state
            # that set_account() now refuses to create.
            conn.execute(
                "UPDATE webkey_slots SET live_enabled=0, updated_at=? WHERE slot_id=?",
                (now, slot_id))
        if old_pair and old_pair != pair:
            _demote_pair_if_orphaned(conn, old_pair, slot_id, "slot reassigned via panel")
        # The slot is ALREADY live: the pair it now points at must be live too,
        # else the slot is live_enabled with a shadow pair → silent no-trading.
        # We never auto-enable live for a non-live slot (that stays an explicit,
        # confirmed act); this only keeps an already-live slot consistent.
        if pair and _slot_live and _has_key:
            cur = conn.execute(
                "UPDATE pair_states SET state='live', state_since=?, updated_at=?, "
                "last_state_change_reason=? WHERE symbol=?",
                (now, now, f"panel assign to live slot {slot_id}", pair))
            if cur.rowcount < 1:
                # No pair_states row → cannot promote. Raise BEFORE commit so the
                # whole assignment rolls back instead of leaving a live slot
                # pointed at a pair that never trades.
                raise AccountError(
                    f"{pair}: немає рядка в pair_states — слот {slot_id} live, "
                    "але пару не можна перевести в live. Призначення скасовано."
                )
        conn.commit()
    finally:
        conn.close()


def assign_pair_routed(server: str, slot_id: int, pair: str | None) -> dict:
    if server == "primary":
        try:
            assign_pair(slot_id, pair)
        except AccountError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}
    return _remote_rpc(server, {"op": "assign_pair", "slot_id": slot_id, "pair": pair})


def remove_account_routed(server: str, slot_id: int) -> dict:
    if server == "primary":
        ok = remove_account(slot_id)
        return {"ok": bool(ok)}
    return _remote_rpc(server, {"op": "remove", "slot_id": slot_id})


def add_account_routed(server: str, *, webkey: str,
                       label: str | None = None,
                       slot_id: int | None = None) -> dict:
    if server == "primary":
        return {"ok": True, **add_account(webkey, label, slot_id)}
    return _remote_rpc(server,
        {"op": "add", "webkey": webkey, "label": label, "slot_id": slot_id})


def available_servers() -> list[str]:
    """Server names available in the panel: primary + every remote bot.
    Used by the UI to render the [Бот 1] [Бот 2] switcher."""
    return ["primary", *REMOTE_BOTS]


def pairs_routed(server: str) -> list[dict]:
    """Pairs from primary or from a remote bot via SSH-RPC."""
    if server == "primary":
        return pairs()
    r = _remote_rpc(server, {"op": "pairs_list"})
    if not r.get("ok"):
        return []
    return r.get("pairs", []) or []


def set_pair_config_routed(server: str, symbol: str, **kwargs) -> dict:
    if server == "primary":
        try:
            set_pair_config(symbol, **kwargs)
        except AccountError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}
    return _remote_rpc(server, {"op": "pairs_set", "symbol": symbol, **kwargs})


@_rw_retry
def set_slot_pair_sizing(symbol: str, slot_id: int, **kwargs) -> dict:
    """Upsert a per-(slot, pair) margin/leverage OVERRIDE into slot_pair_sizing.
    Only the provided keys are written (partial override preserved). Values are
    coerced (margin_*→float, leverage_*→int); non-numeric input is rejected.
    min<=max is validated on the EFFECTIVE row (incoming fields merged over the
    row already stored), so sequential single-field edits cannot leave an
    inverted override. Keys: margin_min_usdt, margin_max_usdt, leverage_min,
    leverage_max."""
    allowed = ("margin_min_usdt", "margin_max_usdt", "leverage_min", "leverage_max")
    fields = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not fields:
        return {"ok": False, "error": "no sizing fields"}
    # Coerce JSON payload values ("50", 50.0, …) to the column types before any
    # comparison — strings compare as text and would also be written as junk.
    for _k in list(fields):
        _cast = int if _k.startswith("leverage_") else float
        try:
            if isinstance(fields[_k], bool):
                raise ValueError("bool")
            fields[_k] = _cast(fields[_k])
        except (TypeError, ValueError, OverflowError):
            # OverflowError: int(float("inf")) — the coercion itself raises before
            # the finite check below could ever run (was an uncaught 500).
            return {"ok": False, "error": f"{_k}: очікується число"}
    # Sanity bounds. NaN would pass every min<=max comparison and silently
    # wipe the override; inf/absurd values reach MEXC as a broken order size.
    import math
    for _k, _v in fields.items():
        if not math.isfinite(_v):
            return {"ok": False, "error": f"{_k}: не число (NaN/inf)"}
        if _k.startswith("margin_") and not (0 < _v <= 100000):
            return {"ok": False, "error": f"{_k}: маржа поза межами 0–100000"}
        if _k.startswith("leverage_") and not (1 <= _v <= 125):
            return {"ok": False, "error": f"{_k}: плече поза межами 1–125"}
    now = int(time.time())
    conn = _rw(DB)
    try:
        # Effective row = what is already stored, overlaid with this request.
        # Validating only the request would let a single-field edit (e.g. just
        # margin_max) invert an existing pair in slot_pair_sizing.
        _cur = conn.execute(
            "SELECT margin_min_usdt, margin_max_usdt, leverage_min, leverage_max "
            "FROM slot_pair_sizing WHERE slot_id=? AND symbol=?",
            (slot_id, symbol)).fetchone()
        eff = dict(zip(allowed, _cur if _cur else (None, None, None, None)))
        eff.update(fields)

        def _f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        for _lo, _hi, _label in (("margin_min_usdt", "margin_max_usdt", "маржа"),
                                 ("leverage_min", "leverage_max", "плече")):
            _a, _b = _f(eff.get(_lo)), _f(eff.get(_hi))
            if _a is not None and _b is not None and _a > _b:
                return {"ok": False, "error": f"{_label}: min > max"}
        conn.execute("INSERT OR IGNORE INTO slot_pair_sizing (slot_id, symbol) VALUES (?, ?)",
                     (slot_id, symbol))
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        vals = list(fields.values()) + [now, slot_id, symbol]
        conn.execute(f"UPDATE slot_pair_sizing SET {sets} WHERE slot_id=? AND symbol=?", vals)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def set_slot_pair_sizing_routed(server: str, symbol: str, slot_id: int, **kwargs) -> dict:
    if server == "primary":
        return set_slot_pair_sizing(symbol, slot_id, **kwargs)
    return _remote_rpc(server, {"op": "slot_sizing_set", "symbol": symbol,
                                "slot_id": slot_id, **kwargs})


def _load_remote_instances() -> list[dict]:
    """Load all `data/instances/*.json` files written by remote bots' state
    exporters and pulled here by /usr/local/bin/stakan-clone-fetcher.sh.

    Each file shape (from scripts/state-export.py on the remote):
        {ts, server, accounts:[…], pairs:[…], pnl_24h:{…}, recent_trades:[…]}

    Enriches each instance with a `list` field (per-slot cards matching the
    primary's `accounts.list` shape) and `stale` / `age_sec` so the panel can
    render an outdated badge when the remote stops pushing.
    """
    out: list[dict] = []
    instances_dir = REPO_ROOT / "data" / "instances"
    if not instances_dir.exists():
        return out
    now = int(time.time())
    for f in sorted(instances_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        age = now - int(d.get("ts") or 0)
        d["age_sec"] = age
        d["stale"] = age > 180  # >3 min without a fresh push = offline
        # Build the per-slot card list, matching primary's accounts.list shape.
        slot_cards = []
        for a in d.get("accounts", []):
            if not a.get("has_key"):
                continue
            pn = (d.get("pnl_24h") or {}).get(f"slot{a['slot_id']}") or {}
            slot_cards.append({
                "slot_id":       a["slot_id"],
                "label":         a.get("label"),
                "assigned_pair": a.get("assigned_pair"),
                "live_enabled":  bool(a.get("live_enabled")),
                "balance_usdt":  a.get("last_balance_usdt"),
                "last_error":    a.get("last_error"),
                "pnl_24h_usdt":  pn.get("pnl"),
                "trades_24h":    pn.get("n", 0),
                "wins_24h":      pn.get("wins", 0),
            })
        d["list"] = slot_cards
        # Halted slots (configured but live_enabled=0) — for fee-guard banner.
        d["halted"] = [
            {
                "slot_id":       a["slot_id"],
                "label":         a.get("label"),
                "assigned_pair": a.get("assigned_pair"),
                "last_error":    a.get("last_error"),
            }
            for a in d.get("accounts", [])
            if a.get("has_key") and a.get("assigned_pair") and not a.get("live_enabled")
        ]
        out.append(d)
    return out


def _today_kyiv_cutoff() -> int:
    """Unix-epoch second of today's 00:00 in Europe/Kyiv (handles EET/EEST DST).

    Used by the dashboard so the per-slot 'today' counters reset at Kyiv midnight
    instead of being a floating 24h window. zoneinfo is stdlib (Python ≥3.9) and
    correctly switches between UTC+2 and UTC+3 across DST transitions.
    """
    import datetime
    from zoneinfo import ZoneInfo
    now = datetime.datetime.now(ZoneInfo("Europe/Kyiv"))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def slot_24h_pnl() -> dict:
    """Return {account_label: {pnl, n, wins}} since today's 00:00 Kyiv time.

    Name kept as `_24h` for historical reasons / API compat; cutoff is now
    Kyiv calendar-day. Keyed by `account_label` ("slot1", "slot2", …) which
    the IOC executor stamps on every live_trades row.
    """
    try:
        conn = _ro(LIVE_DB)
        cutoff = _today_kyiv_cutoff()
        rows = conn.execute(
            "SELECT account_label, "
            "       COALESCE(SUM(net_pnl_usdt), 0) AS pnl, "
            "       SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins, "
            "       COUNT(*) AS n "
            "  FROM live_trades "
            " WHERE opened_at >= ? AND account_label IS NOT NULL "
            " GROUP BY account_label",
            (cutoff,),
        ).fetchall()
        conn.close()
        return {
            r["account_label"]: {
                "pnl": float(r["pnl"] or 0),
                "wins": int(r["wins"] or 0),
                "n": int(r["n"] or 0),
            }
            for r in rows
        }
    except sqlite3.OperationalError:
        return {}


# ---- Аккаунты: подключение (запись зашифрованных кред) ----
class AccountError(Exception):
    """User-facing error for the connect-account form."""


def _master_key() -> str:
    mk = os.environ.get("MASTER_KEY")
    if not mk and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("MASTER_KEY="):
                mk = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not mk:
        raise AccountError("MASTER_KEY не найден (ни в окружении, ни в .env)")
    return mk


def _gen_visitor_id() -> str:
    """20-char alphanumeric visitor_id — matches credentials.generate_visitor_id()."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(20))


def _fernet():
    try:
        from cryptography.fernet import Fernet  # panel write-path only
    except ImportError as e:
        raise AccountError(
            "Пакет cryptography не встановлено у venv панелі — запусти "
            "`.venv/bin/pip install cryptography==43.0.3` і перезапусти stakan-panel."
        ) from e
    try:
        return Fernet(_master_key().encode())
    except Exception as e:  # noqa: BLE001 — surface a clean message to the form
        raise AccountError(f"Некорректный MASTER_KEY: {e}") from e


def _assert_key_can_decrypt_existing(conn: sqlite3.Connection, fernet) -> None:
    """Guard: prove MASTER_KEY matches the key used for already-stored slots.

    If a non-empty slot exists and we CAN'T decrypt it, the panel's key differs
    from the live bot's — writing new slots would create credentials the live
    client can't read. Refuse loudly instead of silently corrupting.
    """
    from cryptography.fernet import InvalidToken
    row = conn.execute(
        "SELECT webkey_blob FROM webkey_slots WHERE webkey_blob IS NOT NULL LIMIT 1"
    ).fetchone()
    if row is None:
        return  # nothing to compare against
    try:
        fernet.decrypt(row["webkey_blob"])
    except InvalidToken as e:
        raise AccountError(
            "MASTER_KEY не совпадает с ключом уже сохранённых слотов — "
            "новые креды были бы нечитаемы для live-клиента. Запись отменена."
        ) from e


def add_account(webkey: str, label: str | None = None,
                slot_id: int | None = None) -> dict:
    """Connect a MEXC account by webkey (the `u_id` cookie value, WEB+64hex).

    Fills the first EMPTY slot, or creates the next slot row (up to
    MAX_ACCOUNT_SLOTS). Never overwrites a slot that already holds a key
    (so the real live credential in slot 1 is safe by construction).
    Returns {"slot_id": N}.
    """
    webkey = (webkey or "").strip()
    if not _WEBKEY_RE.match(webkey):
        raise AccountError(
            "Веб-ключ должен быть WEB + 64 hex-символа (значение cookie `u_id` / "
            "`uc_token` на futures.mexc.com)."
        )
    label = (label or "").strip()[:50] or None

    fernet = _fernet()
    conn = _rw(DB)
    conn.row_factory = sqlite3.Row
    try:
        _assert_key_can_decrypt_existing(conn, fernet)

        rows = conn.execute(
            "SELECT slot_id, webkey_blob FROM webkey_slots ORDER BY slot_id"
        ).fetchall()
        by_id = {r["slot_id"]: r for r in rows}
        if slot_id is not None:
            # explicit slot chosen in the form — must exist-or-createable
            # and be EMPTY (never overwrite a live credential).
            want = int(slot_id)
            if want < 1 or want > MAX_ACCOUNT_SLOTS:
                raise AccountError(
                    f"Слот {want} поза діапазоном 1..{MAX_ACCOUNT_SLOTS}.")
            existing = by_id.get(want)
            if existing is not None and existing["webkey_blob"] is not None:
                raise AccountError(
                    f"Слот {want} вже зайнятий — спершу видаліть його ключ.")
            slot_id = want
            is_new_row = existing is None
        else:
            slot_id = next((r["slot_id"] for r in rows
                            if r["webkey_blob"] is None), None)
            is_new_row = False
            if slot_id is None:
                next_id = (max((r["slot_id"] for r in rows), default=0) + 1)
                if next_id > MAX_ACCOUNT_SLOTS:
                    raise AccountError(
                        f"Нет свободных слотов (лимит {MAX_ACCOUNT_SLOTS}). "
                        "Удалите неиспользуемый слот."
                    )
                slot_id = next_id
                is_new_row = True

        now = int(time.time())
        wk_blob = fernet.encrypt(webkey.encode("utf-8"))
        vis_blob = fernet.encrypt(_gen_visitor_id().encode("utf-8"))
        proxy_blob = None   # proxy support removed (direct mode); column kept NULL

        if is_new_row:
            conn.execute(
                "INSERT INTO webkey_slots "
                "(slot_id, label, enabled, webkey_blob, visitor_blob, proxy_blob, "
                " webkey_refreshed_at, created_at, updated_at) "
                "VALUES (?,?,0,?,?,?,?,?,?)",
                (slot_id, label, wk_blob, vis_blob, proxy_blob, now, now, now),
            )
        else:
            conn.execute(
                "UPDATE webkey_slots SET label=?, webkey_blob=?, visitor_blob=?, "
                "proxy_blob=?, webkey_refreshed_at=?, updated_at=? WHERE slot_id=?",
                (label, wk_blob, vis_blob, proxy_blob, now, now, slot_id),
            )
        conn.commit()
        return {"slot_id": slot_id}
    finally:
        conn.close()


@_rw_retry
def remove_account(slot_id: int) -> bool:
    """Clear creds from a slot (keep the row). Returns False if already empty."""
    conn = _rw(DB)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT webkey_blob, assigned_pair FROM webkey_slots WHERE slot_id=?", (slot_id,)
        ).fetchone()
        if not row or row["webkey_blob"] is None:
            return False
        _old_pair = row["assigned_pair"]
        conn.execute(
            "UPDATE webkey_slots SET webkey_blob=NULL, visitor_blob=NULL, proxy_blob=NULL, "
            "enabled=0, live_enabled=0, assigned_pair=NULL, last_health_check=NULL, "
            "last_latency_ms=NULL, last_balance_usdt=NULL, last_error=NULL, "
            "webkey_refreshed_at=NULL, label=NULL, updated_at=? WHERE slot_id=?",
            (int(time.time()), slot_id),
        )
        # Auto-shadow the displaced pair (unless a sibling live slot still trades
        # it) — else it is stranded live with no credential/slot to execute it.
        _demote_pair_if_orphaned(conn, _old_pair, slot_id, "webkey removed via panel")
        conn.commit()
        return True
    finally:
        conn.close()


# ---- Инциденты ----
def _ensure_incidents() -> None:
    conn = _rw(DB)
    try:
        conn.executescript(INCIDENTS_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def incidents(limit: int = 200) -> list[dict]:
    _ensure_incidents()
    conn = _ro(DB)
    try:
        rows = conn.execute(
            "SELECT id, ts, severity, source, message, acked FROM panel_incidents "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@_rw_retry
def add_incident(severity: str, message: str, source: str = "panel") -> int:
    _ensure_incidents()
    conn = _rw(DB)
    try:
        cur = conn.execute(
            "INSERT INTO panel_incidents(ts,severity,source,message) VALUES(?,?,?,?)",
            (int(time.time()), severity, source, message))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@_rw_retry
def ack_incident(incident_id: int) -> None:
    _ensure_incidents()
    conn = _rw(DB)
    try:
        conn.execute("UPDATE panel_incidents SET acked=1 WHERE id=?", (incident_id,))
        conn.commit()
    finally:
        conn.close()


# ---- Dashboard aggregate ----
def live_trades_summary() -> dict:
    """All-time + today (Kyiv) totals. Dashboard KPIs show both rows so the
    operator sees lifetime score alongside today's session."""
    try:
        conn = _ro(LIVE_DB)
    except sqlite3.OperationalError:
        return {"trades": 0, "net_pnl_usdt": 0.0, "open": 0,
                "trades_today": 0, "net_pnl_today_usdt": 0.0}
    try:
        r = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(net_pnl_usdt),0) pnl, "
            "SUM(CASE WHEN closed_at IS NULL THEN 1 ELSE 0 END) opn FROM live_trades"
        ).fetchone()
        cutoff = _today_kyiv_cutoff()
        t = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(net_pnl_usdt),0) pnl "
            "  FROM live_trades WHERE opened_at >= ?", (cutoff,)
        ).fetchone()
    finally:
        conn.close()
    return {
        "trades":             r["n"],
        "net_pnl_usdt":       r["pnl"],
        "open":               r["opn"] or 0,
        "trades_today":       t["n"],
        "net_pnl_today_usdt": t["pnl"],
    }


def dashboard(sim_summary: dict | None) -> dict:
    accs = accounts()
    inc = incidents(limit=500)
    pnl24 = slot_24h_pnl()
    def _f(v):
        try:
            return float(v) if v not in (None, "") else 0.0
        except (ValueError, TypeError):
            return 0.0
    # Remote (clone) data arrives via JSON and is best-effort: a malformed or
    # partial payload must NEVER blank the whole dashboard. Fold it defensively
    # so any error degrades to primary-only instead of 500ing /api/dashboard.
    try:
        ri = _load_remote_instances()
    except Exception:
        logging.exception("dashboard: _load_remote_instances failed")
        ri = []
    try:
        _clone_accs = [x for inst in ri for x in (inst.get("accounts") or [])]
        _acc_live_extra = sum(1 for x in _clone_accs if x.get("live_enabled"))
        _acc_bal_extra = sum(_f(x.get("last_balance_usdt")) for x in _clone_accs)
    except Exception:
        logging.exception("dashboard: clone account fold failed; primary only")
        _clone_accs, _acc_live_extra, _acc_bal_extra = [], 0, 0.0
    _acc_total = len(accs) + len(_clone_accs)
    _acc_live = sum(1 for a in accs if a["live_enabled"]) + _acc_live_extra
    _acc_balance = sum(_f(a.get("balance_usdt")) for a in accs) + _acc_bal_extra
    # Top-KPI live totals = primary + every clone (coerce every term; JSON strings possible).
    _live = live_trades_summary()
    try:
        for inst in ri:
            _ls = inst.get("live_summary") or {}
            _live["trades"] = int(_f(_live.get("trades")) + _f(_ls.get("trades")))
            _live["net_pnl_usdt"] = _f(_live.get("net_pnl_usdt")) + _f(_ls.get("net_pnl_usdt"))
            _live["trades_today"] = int(_f(_live.get("trades_today")) + _f(_ls.get("trades_today")))
            _live["net_pnl_today_usdt"] = _f(_live.get("net_pnl_today_usdt")) + _f(_ls.get("net_pnl_today_usdt"))
    except Exception:
        logging.exception("dashboard: clone live_summary fold failed; primary totals only")
        _live = live_trades_summary()
    return {
        "accounts": {
            "total": _acc_total,
            # "enabled" drives the KPI's first number — show LIVE-enabled accounts
            # across ALL bots (the meaningful "actively trading" count).
            "enabled": _acc_live,
            "live": _acc_live,
            # Kept for backward-compat. Dashboard now renders per-slot from `list`.
            "balance_usdt": _acc_balance,
            # Per-slot breakdown so the dashboard can show separate cards
            # (one per account) instead of a single combined balance, each
            # enriched with that slot's last-24h PnL.
            "list": [
                {
                    "slot_id": a["slot_id"],
                    "label": a["label"],
                    "assigned_pair": a["assigned_pair"],
                    "live_enabled": a["live_enabled"],
                    "has_key": a["has_key"],
                    "balance_usdt": a["balance_usdt"],
                    "last_error": a["last_error"],
                    "pnl_24h_usdt": pnl24.get(f"slot{a['slot_id']}", {}).get("pnl"),
                    "trades_24h":   pnl24.get(f"slot{a['slot_id']}", {}).get("n", 0),
                    "wins_24h":     pnl24.get(f"slot{a['slot_id']}", {}).get("wins", 0),
                }
                for a in accs if a["has_key"]
            ],
            # Slots configured for live (has webkey + assigned pair) but with
            # live_enabled=False — drives the dashboard's fee-guard banner.
            # Could be the IOC fee guard, a manual disable, or any other halt;
            # the banner shows last_error so the operator sees the reason.
            "halted": [
                {
                    "slot_id": a["slot_id"],
                    "label": a["label"],
                    "assigned_pair": a["assigned_pair"],
                    "last_error": a["last_error"],
                }
                for a in accs
                if a["has_key"] and a["assigned_pair"] and not a["live_enabled"]
            ],
        },
        "incidents": {
            "total": len(inc),
            "open": sum(1 for i in inc if not i["acked"]),
        },
        "live": _live,
        "sim": sim_summary,
        "recent_incidents": inc[:6],
        "recent_live_trades": live_trades(25),   # primary (Основа) only
        # State of every remote bot (clones) pulled by the fetcher timer — each
        # carries its own recent_trades, rendered as a separate "Клон" block.
        "remote_instances": ri,
    }


# ---- Пользователи / whitelist ----
def whitelist() -> list[dict]:
    if not WHITELIST_FILE.exists():
        return []
    try:
        raw = json.loads(WHITELIST_FILE.read_text())
    except Exception:
        return []
    # Shape guard: this feeds the OUTERMOST middleware. A hand-edited file
    # holding bare strings used to raise AttributeError on every request
    # (including /login) — i.e. a typo could lock the operator out.
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and e.get("ip")]


def whitelist_ips() -> set[str]:
    return {e.get("ip") for e in whitelist() if e.get("ip")}


def add_whitelist(ip: str, label: str = "") -> list[dict]:
    wl = whitelist()
    if not any(e.get("ip") == ip for e in wl):
        wl.append({"ip": ip, "label": label, "added": int(time.time())})
        WHITELIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        WHITELIST_FILE.write_text(json.dumps(wl, indent=2))
    return wl


def remove_whitelist(ip: str) -> list[dict]:
    wl = [e for e in whitelist() if e.get("ip") != ip]
    WHITELIST_FILE.write_text(json.dumps(wl, indent=2))
    return wl


# ---- Очереди (rotation plan: account → pairs) ----
def rotation() -> list[dict]:
    """Per-account rotation plan. One entry per webkey slot."""
    saved = {}
    if ROTATION_FILE.exists():
        try:
            for e in json.loads(ROTATION_FILE.read_text()):
                saved[e["slot_id"]] = e
        except Exception:
            pass
    # always reflect current accounts (so new slots appear)
    out = []
    for a in accounts():
        e = saved.get(a["slot_id"], {})
        out.append({
            "slot_id": a["slot_id"],
            "label": a["label"],
            "account_enabled": a["enabled"],
            "pairs": e.get("pairs", []),
            "mode": e.get("mode", "sequential"),   # sequential | random
            "enabled": e.get("enabled", False),
        })
    return out


def set_rotation(plan: list[dict]) -> list[dict]:
    norm = [{"slot_id": int(e["slot_id"]), "pairs": list(e.get("pairs", [])),
             "mode": e.get("mode", "sequential"), "enabled": bool(e.get("enabled", False))}
            for e in plan]
    ROTATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROTATION_FILE.write_text(json.dumps(norm, indent=2))
    return rotation()


# ---- Live trades ----
def live_trades(limit: int = 100) -> list[dict]:
    try:
        conn = _ro(LIVE_DB)
        rows = conn.execute(
            # notional_usdt = ЗАЛИТО, margin×leverage = ЗАМОВЛЕНО. Праймер
            # тягне ці рядки через RPC і показує колонку «Залито».
            "SELECT id, symbol, direction, leverage, margin_usdt, net_pnl_usdt, "
            "roi_pct, opened_at, closed_at, exit_reason, duration_sec, "
            "mode, account_label, notional_usdt, entry_filled_pct "
            "FROM live_trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def available_pairs() -> list[str]:
    try:
        conn = _ro(DB)
        rows = conn.execute(
            "SELECT symbol FROM live_pair_whitelist ORDER BY symbol"
        ).fetchall()
        conn.close()
        return [r["symbol"] for r in rows]
    except sqlite3.OperationalError:
        return []


def _pair_today_stats() -> dict[str, dict]:
    """Per-symbol trade counts/PnL since today's 00:00 Kyiv, computed at query
    time from live_trades + shadow_trades. Replaces pair_states.last_24h_*
    which are rolling-24h written by the bot (so they didn't reset at Kyiv
    midnight as the operator expects on the dashboard).

    Returns {symbol: {n, wins, pnl}}. A pair gets stats from live_trades if
    it has live rows today, otherwise from shadow_trades — so live pairs show
    their real activity and shadow pairs still show simulated edge.
    """
    cutoff = _today_kyiv_cutoff()
    out: dict[str, dict] = {}

    # Live trades first — they take precedence.
    try:
        live = _ro(LIVE_DB)
        for r in live.execute(
            "SELECT symbol, COUNT(*) AS n, "
            "       SUM(CASE WHEN net_pnl_usdt>0 THEN 1 ELSE 0 END) AS wins, "
            "       COALESCE(SUM(net_pnl_usdt), 0) AS pnl "
            "  FROM live_trades WHERE opened_at >= ? GROUP BY symbol",
            (cutoff,),
        ).fetchall():
            out[r["symbol"]] = {"n": int(r["n"]), "wins": int(r["wins"] or 0),
                                "pnl": float(r["pnl"] or 0)}
        live.close()
    except sqlite3.OperationalError:
        pass

    # Shadow trades for pairs that didn't trade live today.
    try:
        sh = _ro(DB)
        for r in sh.execute(
            "SELECT symbol, COUNT(*) AS n, "
            "       SUM(CASE WHEN net_pnl_usdt>0 THEN 1 ELSE 0 END) AS wins, "
            "       COALESCE(SUM(net_pnl_usdt), 0) AS pnl "
            "  FROM shadow_trades WHERE opened_at >= ? GROUP BY symbol",
            (cutoff,),
        ).fetchall():
            if r["symbol"] not in out:
                out[r["symbol"]] = {"n": int(r["n"]), "wins": int(r["wins"] or 0),
                                    "pnl": float(r["pnl"] or 0)}
        sh.close()
    except sqlite3.OperationalError:
        pass
    return out


def _pair_alltime_stats() -> dict[str, dict]:
    """Per-symbol ALL-TIME counts/wins/PnL from live_trades (real money,
    real money). NO shadow fallback (a never-live pair must show empty, not its
    shadow PnL). NO time cutoff. Returns {symbol: {n, wins, pnl}}."""
    out: dict[str, dict] = {}
    try:
        live = _ro(LIVE_DB)
        for r in live.execute(
            "SELECT symbol, COUNT(*) AS n, "
            "       SUM(CASE WHEN net_pnl_usdt>0 THEN 1 ELSE 0 END) AS wins, "
            "       COALESCE(SUM(net_pnl_usdt), 0) AS pnl "
            "  FROM live_trades GROUP BY symbol"
        ).fetchall():
            out[r["symbol"]] = {"n": int(r["n"]), "wins": int(r["wins"] or 0),
                                "pnl": float(r["pnl"] or 0)}
        live.close()
    except sqlite3.OperationalError:
        pass
    # LIVE money ONLY — no shadow fallback. Pairs that never traded live are
    # absent here, so the "live PnL весь час" column shows "—" for them (a
    # shadow fallback would mislabel shadow PnL as live; bug fixed 2026-06-24).
    return out


def _pair_alltime_shadow_stats() -> dict[str, dict]:
    """Per-symbol ALL-TIME counts/PnL from shadow_trades (SIMULATION, not real
    money). Kept strictly separate from _pair_alltime_stats so the UI can show
    live vs shadow profit distinctly. Returns {symbol: {n, wins, pnl}}."""
    out: dict[str, dict] = {}
    try:
        conn = _ro(DB)
        for r in conn.execute(
            "SELECT symbol, COUNT(*) AS n, "
            "       SUM(CASE WHEN net_pnl_usdt>0 THEN 1 ELSE 0 END) AS wins, "
            "       COALESCE(SUM(net_pnl_usdt), 0) AS pnl "
            "  FROM shadow_trades GROUP BY symbol"
        ).fetchall():
            out[r["symbol"]] = {"n": int(r["n"]), "wins": int(r["wins"] or 0),
                                "pnl": float(r["pnl"] or 0)}
        conn.close()
    except sqlite3.OperationalError:
        pass
    return out


# ---- Pairs management ----
def _bot_slot_ids() -> list[int]:
    """The account slots the bot has (usually [1, 2]). Used so the pairs UI can
    show a per-slot sizing row for every slot."""
    try:
        conn = _ro(DB)
        ids = [r[0] for r in conn.execute("SELECT slot_id FROM webkey_slots ORDER BY slot_id")]
        conn.close()
        return ids or [1, 2]
    except sqlite3.OperationalError:
        return [1, 2]


def _slot_pair_sizing_map() -> dict:
    """{symbol: {slot_id: {margin_min_usdt, margin_max_usdt, leverage_min,
    leverage_max}}} from slot_pair_sizing. Absent (slot,pair) = inherit pair YAML;
    a present row may have some fields NULL (partial override)."""
    out: dict = {}
    try:
        conn = _ro(DB)
        rows = conn.execute(
            "SELECT slot_id, symbol, margin_min_usdt, margin_max_usdt, "
            "leverage_min, leverage_max FROM slot_pair_sizing"
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        out.setdefault(r["symbol"], {})[r["slot_id"]] = {
            "margin_min_usdt": r["margin_min_usdt"],
            "margin_max_usdt": r["margin_max_usdt"],
            "leverage_min": r["leverage_min"],
            "leverage_max": r["leverage_max"],
        }
    return out


def pairs() -> list[dict]:
    try:
        conn = _ro(DB)
        rows = conn.execute("""
            SELECT ps.symbol, ps.state, ps.state_since,
                   ps.last_24h_trades, ps.last_24h_winrate, ps.last_24h_pnl,
                   ps.total_live_trades, ps.total_live_pnl,
                   ps.last_state_change_reason, ps.pause_reason
            FROM pair_states ps
        """).fetchall()
        conn.close()
        result = [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []

    # Config comes from the pair YAML via the shared resolver — the SAME
    # ConfigLoader the trading loop and Telegram /get_config read. We no longer
    # query the pair_configs config columns at all: sizing (margin/leverage) is
    # resolved from YAML here; runtime state is ps.state (pair_states); the dead
    # pc.* columns (mode/min_confidence/min_max_mexc_lag_pct/ioc_offset/max_hold)
    # were never rendered by the UI and are gone from this query.
    from src.config_writer import shared_loader
    _szl = shared_loader()
    for row in result:
        _e = _szl.get(row["symbol"]).execution
        row["margin_min_usdt"] = _e.margin_min_usdt
        row["margin_max_usdt"] = _e.margin_max_usdt
        row["leverage_min"] = _e.leverage_min
        row["leverage_max"] = _e.leverage_max

    # Per-(slot, pair) sizing OVERRIDE (slot_pair_sizing). Each pair gets a
    # `slots` list [{slot_id, margin_min_usdt, margin_max_usdt, leverage_min,
    # leverage_max}] where a field is None = inherit the pair YAML above. Lets
    # the UI show + edit margin/leverage per slot (two accounts, same pair).
    _slots = _bot_slot_ids()
    _szmap = _slot_pair_sizing_map()
    for row in result:
        _ov = _szmap.get(row["symbol"], {})
        row["slots"] = [
            {
                "slot_id": sid,
                "margin_min_usdt": (_ov.get(sid) or {}).get("margin_min_usdt"),
                "margin_max_usdt": (_ov.get(sid) or {}).get("margin_max_usdt"),
                "leverage_min": (_ov.get(sid) or {}).get("leverage_min"),
                "leverage_max": (_ov.get(sid) or {}).get("leverage_max"),
            }
            for sid in _slots
        ]

    # Overlay the per-pair "today" stats (Kyiv-day) onto last_24h_* fields the
    # UI reads. We keep the field names for template compat — the labels just
    # mean "since 00:00 Kyiv" now, matching slot_24h_pnl().
    today = _pair_today_stats()
    for row in result:
        st = today.get(row["symbol"])
        if st is None:
            row["last_24h_trades"] = 0
            row["last_24h_winrate"] = None
            row["last_24h_pnl"] = 0.0
        else:
            row["last_24h_trades"] = st["n"]
            row["last_24h_winrate"] = (st["wins"] / st["n"]) if st["n"] else None
            row["last_24h_pnl"] = st["pnl"]

    # All-time per-pair LIVE stats (real money from live_trades, NO shadow).
    allt = _pair_alltime_stats()
    for row in result:
        a = allt.get(row["symbol"])
        if a is None:
            row["total_trades"] = 0
            row["total_winrate"] = None
            row["total_pnl"] = 0.0
        else:
            row["total_trades"] = a["n"]
            row["total_winrate"] = (a["wins"] / a["n"]) if a["n"] else None
            row["total_pnl"] = a["pnl"]

    # All-time per-pair SHADOW stats (simulation from shadow_trades) — kept
    # SEPARATE from live so the overview never conflates real money with sim.
    sh = _pair_alltime_shadow_stats()
    for row in result:
        s = sh.get(row["symbol"])
        row["total_shadow_trades"] = s["n"] if s else 0
        row["total_shadow_pnl"] = s["pnl"] if s else 0.0

    # Sort: live first, then shadow, then others; within group by today PnL desc.
    state_rank = {"live": 0, "shadow": 1, "discovered": 2}
    result.sort(key=lambda r: (state_rank.get(r["state"], 3), -(r["last_24h_pnl"] or 0)))
    return result


@_rw_retry
def set_pair_config(symbol: str, **kwargs) -> None:
    # mode = operational state → DB; margin/leverage = static config → pair YAML
    # (single source of truth, read by ConfigLoader).
    sizing = {k: v for k, v in kwargs.items()
              if k in ("margin_min_usdt", "margin_max_usdt", "leverage_min", "leverage_max")}
    if sizing:
        from src.config_writer import set_pair_execution
        set_pair_execution(symbol, **sizing)
    if "mode" in kwargs:
        if kwargs["mode"] == "live":
            # Go-live is NOT a webpanel action — it needs a coordinated state
            # (pair_states.state=live + slot live_enabled + live_pool rebuild),
            # all driven by the Telegram slot flow. A raw state=live here would
            # leave a pair in live-state with no configured slot.
            raise AccountError(
                "Go-live is managed via Telegram (slot flow: assign + enable "
                "live + promote). The webpanel only demotes to shadow."
            )
        # mode='shadow' → demote via pair_states.state (the live authority) —
        # the same canonical write as set_all_pairs_shadow / demote_pair_to_shadow.
        now = int(time.time())
        conn = _rw(DB)
        try:
            conn.execute(
                "UPDATE pair_states SET state='shadow', state_since=?, updated_at=?, "
                "last_state_change_reason=? WHERE symbol=?",
                (now, now, f"webpanel demote {symbol}", symbol),
            )
            conn.commit()
        finally:
            conn.close()


def set_all_pairs_shadow_all(servers: list[str] | None = None) -> dict:
    """KILL ALL across the CHOSEN bots (default: every bot).

    `set_all_pairs_shadow()` only ever touched the PRIMARY database, so a clone
    kept trading live while the panel reported success — the one defect that can
    cost money at the exact moment the operator is trying to stop.

    `servers` narrows the blast radius: the two bots trade different pairs on
    different accounts, so "stop the clone" must not also stop the primary.
    Unknown names are dropped rather than defaulted — a typo must never resolve
    to some other bot. Passing an explicit empty list stops NOTHING and says so;
    omitting the argument keeps the original all-bots behaviour, so no existing
    caller changes meaning silently.

    Local demote runs FIRST and is committed before any SSH, so an unreachable
    clone can never delay stopping the primary. Returns per-server results so the
    UI can never render a clean success when a clone was not stopped.
    """
    known = available_servers()
    targets = known if servers is None else [s for s in servers if s in known]
    out: dict = {}
    if "primary" in targets:
        out["primary"] = {"ok": True, "demoted": set_all_pairs_shadow()}
    for srv in targets:
        if srv == "primary":
            continue
        try:
            out[srv] = _remote_rpc(srv, {"op": "kill_all"})
        except Exception as e:  # never let a dead clone mask the local stop
            out[srv] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return out


@_rw_retry
def set_all_pairs_shadow() -> int:
    """Webpanel 'kill all' — demote every LIVE pair to SHADOW *state*.

    pair_states.state is the live authority (PairStateManager.is_in_live reads
    state, NOT pair_configs.mode); the old `mode='shadow' WHERE mode='live'`
    flip was a no-op for live trading. This writes the same pair_states columns
    as WebkeyStore.demote_pair_to_shadow (the canonical demote); the bot reloads
    pair_states within ≤60s.

    BLOCKS NEW live entries. Does NOT close open positions — they exit via the
    normal exit rules. (Different from the Telegram /kill_all, which closes ALL
    open positions AND disables trading globally.)
    """
    now = int(time.time())
    conn = _rw(DB)
    try:
        cur = conn.execute(
            "UPDATE pair_states SET state='shadow', state_since=?, updated_at=?, "
            "last_state_change_reason='webpanel kill-all' WHERE state='live'",
            (now, now),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def open_positions() -> list[dict]:
    try:
        conn = _ro(LIVE_DB)
        rows = conn.execute(
            "SELECT id, symbol, direction, leverage, margin_usdt, "
            "entry_price, opened_at, mode, account_label "
            "FROM live_trades WHERE closed_at IS NULL ORDER BY opened_at DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
