#!/usr/bin/env python3
"""Migrate Stage 17→18: pair detector config from .env + DB columns → YAML files.

What this does:
  1. Reads /root/stakan-bot/.env — extracts STATIC_GAP_* values as global defaults.
  2. Reads pair_configs DB — extracts per-pair gap_* overrides + NIZAR adaptive vars.
  3. Writes config/global.yaml — defaults for fields with no per-pair value.
  4. Writes config/pairs/<SYMBOL>.yaml — one file per pair with overrides only.
  5. Writes config/.env.new — minimal new .env without migrated keys.
  6. Does NOT touch DB. Does NOT touch original .env. Reversible.

Run after Stage 18 code patch is deployed and verified.
Then manually:
  - Review generated files
  - Replace /root/stakan-bot/.env with .env.new
  - Restart bot
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Keys that move from .env into config/global.yaml as detector defaults.
# Format: env_key → (yaml_path, type_converter)
DETECTOR_ENV_TO_YAML = {
    "STATIC_GAP_ENABLED":           ("detector.enabled", lambda v: v == "1"),
    "STATIC_GAP_INTERVAL_SEC":      ("detector.scan_interval_sec", float),
    "STATIC_GAP_COOLDOWN_SEC":      ("detector.cooldown_sec", float),
    "STATIC_GAP_NIZAR_MODE":        ("detector.nizar_mode", lambda v: v == "1"),
    "STATIC_GAP_NIZAR_MIN_TICKS":   ("detector.min_ticks", int),
    "STATIC_GAP_MIN_TICKS":         ("detector.min_ticks", int),  # legacy fallback
    "STATIC_GAP_NIZAR_MIN_EXEC_TICKS": ("detector.min_exec_ticks", int),
    "STATIC_GAP_MIN_EXEC_TICKS":    ("detector.min_exec_ticks", int),  # legacy fallback
    "STATIC_GAP_BOTH_SIDES":        ("detector.both_sides", lambda v: v == "1"),
    "STATIC_GAP_INVERT":            ("detector.invert", lambda v: v == "1"),
}

# DB columns in pair_configs (gap_*) that move to per-pair YAML overrides.
# Format: db_column → (yaml_path, type_converter, is_bool)
DB_GAP_COLS_TO_YAML = {
    "gap_min_ticks":          ("detector.min_ticks", int, False),
    "gap_min_exec_ticks":     ("detector.min_exec_ticks", int, False),
    "gap_cooldown_sec":       ("detector.cooldown_sec", float, False),
}

# Keys that STAY in .env (not migrated). Anything else is dropped.
ENV_KEEP_KEYS = {
    # Secrets / infra
    "MASTER_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID",
    "WEBKEY_MEMBER_ID", "WEBKEY_VISITOR_ID", "WEBKEY_CHASH", "WEBKEY_MHASH",
    "LOG_LEVEL", "LOG_FILE", "DB_PATH", "LIVE_DB_PATH",
    # Global trading safety
    "LIVE_TRADING_ENABLED", "LIVE_MIN_BALANCE",
    "LIVE_DAILY_LOSS_KILL", "LIVE_MAX_CONSEC_LOSSES",
    "LIVE_MAX_PER_SYMBOL", "LIVE_MAX_TOTAL", "LIVE_MAX_MARGIN",
    "VOLATILITY_MIN_IMPULSE_PCT",
    # IOC execution — user wants global
    "IOC_MAX_ATTEMPTS", "IOC_RETRY_DELAY_MS", "IOC_PASSIVE_TICK_OFFSET",
    # Realism / misc
    "SHADOW_REALISM_PROFILE",
}


def parse_env(path: Path) -> dict[str, str]:
    """Parse .env file → dict. Skips comments and blank lines."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def set_nested(d: dict, dotted_key: str, value: Any) -> None:
    """Set d[a][b][c] = value when dotted_key is 'a.b.c'."""
    parts = dotted_key.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def build_global_yaml(env: dict[str, str]) -> dict:
    """Extract detector defaults from env."""
    out: dict = {}
    for env_key, (yaml_path, conv) in DETECTOR_ENV_TO_YAML.items():
        if env_key not in env:
            continue
        try:
            set_nested(out, yaml_path, conv(env[env_key]))
        except Exception as e:
            print(f"  warning: failed to convert {env_key}={env[env_key]}: {e}",
                  file=sys.stderr)
    return out


def build_pair_yaml(symbol: str, db_path: Path, env: dict[str, str]) -> dict | None:
    """Read pair_configs row for symbol, extract gap_* overrides.

    Also pulls NIZAR_<SYMBOL>_ADAPTIVE_* env vars into adaptive_exit.
    Returns None if pair has zero per-pair overrides (no YAML needed).
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM pair_configs WHERE symbol = ?", (symbol,)
        ).fetchone()
    finally:
        con.close()

    if row is None:
        return None

    out: dict = {}

    # Detector overrides from DB
    for db_col, (yaml_path, conv, is_bool) in DB_GAP_COLS_TO_YAML.items():
        try:
            v = row[db_col]
        except (KeyError, IndexError):
            continue
        if v is None:
            continue
        if is_bool:
            v = bool(v)
        else:
            v = conv(v)
        set_nested(out, yaml_path, v)

    # Adaptive exit from env (legacy NIZAR_<SYMBOL>_ADAPTIVE_*)
    adaptive_map = {
        f"NIZAR_{symbol}_ADAPTIVE_MIN_HOLD_MS":   ("adaptive_exit.min_hold_ms", int),
        f"NIZAR_{symbol}_ADAPTIVE_STALL_MS":      ("adaptive_exit.stall_ms", int),
        f"NIZAR_{symbol}_ADAPTIVE_REVERSAL_TICKS": ("adaptive_exit.reversal_ticks", float),
    }
    for env_key, (yaml_path, conv) in adaptive_map.items():
        if env_key in env:
            try:
                set_nested(out, yaml_path, conv(env[env_key]))
            except Exception as e:
                print(f"  warning: failed to convert {env_key}: {e}", file=sys.stderr)

    return out if out else None


def write_yaml(path: Path, data: dict, header: str = "") -> None:
    """Write YAML preserving readable structure."""
    import yaml as _yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        if header:
            f.write(header)
        _yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False, indent=2)


def build_new_env(env: dict[str, str]) -> str:
    """Build minimal new .env keeping only ENV_KEEP_KEYS."""
    lines = [
        "# =========================================================",
        "# stakan-bot environment (post Stage 18 migration)",
        "# =========================================================",
        "# Pair-specific strategy config now lives in:",
        "#   config/global.yaml  — global defaults",
        "#   config/pairs/<SYMBOL>.yaml  — per-pair overrides",
        "#",
        "# This file keeps ONLY secrets, infra paths, and global",
        "# trading safety knobs that apply identically to all pairs.",
        "# =========================================================",
        "",
    ]
    # Group by purpose for readability
    groups = [
        ("# --- Secrets ---", ["MASTER_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"]),
        ("# --- WebKey credentials ---",
         ["WEBKEY_MEMBER_ID", "WEBKEY_VISITOR_ID", "WEBKEY_CHASH", "WEBKEY_MHASH"]),
        ("# --- Logging & DB paths ---",
         ["LOG_LEVEL", "LOG_FILE", "DB_PATH", "LIVE_DB_PATH"]),
        ("# --- Global trading safety ---",
         ["LIVE_TRADING_ENABLED", "LIVE_MIN_BALANCE", "LIVE_DAILY_LOSS_KILL",
          "LIVE_MAX_CONSEC_LOSSES", "LIVE_MAX_PER_SYMBOL", "LIVE_MAX_TOTAL",
          "LIVE_MAX_MARGIN", "VOLATILITY_MIN_IMPULSE_PCT"]),
        ("# --- IOC execution (global, same for all pairs) ---",
         ["IOC_MAX_ATTEMPTS", "IOC_RETRY_DELAY_MS", "IOC_PASSIVE_TICK_OFFSET"]),
        ("# --- Realism profile ---", ["SHADOW_REALISM_PROFILE"]),
    ]
    for header, keys in groups:
        any_in_group = False
        for k in keys:
            if k in env:
                if not any_in_group:
                    lines.append(header)
                    any_in_group = True
                lines.append(f"{k}={env[k]}")
        if any_in_group:
            lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="/root/stakan-bot/.env",
                    help="Path to current .env")
    ap.add_argument("--db", default="/root/stakan-bot/data/stakan.db",
                    help="Path to stakan.db (for pair_configs)")
    ap.add_argument("--output", default="/root/stakan-bot/config",
                    help="Output directory for global.yaml + pairs/")
    ap.add_argument("--new-env", default="/root/stakan-bot/.env.new",
                    help="Where to write the minimal new .env")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be written, don't actually write")
    args = ap.parse_args()

    env_path = Path(args.env)
    db_path = Path(args.db)
    out_dir = Path(args.output)

    if not env_path.exists():
        sys.exit(f"Error: .env not found at {env_path}")
    if not db_path.exists():
        sys.exit(f"Error: DB not found at {db_path}")

    print(f"Reading .env from {env_path}")
    env = parse_env(env_path)
    print(f"  {len(env)} env vars parsed")

    # Global defaults
    print(f"\nBuilding global.yaml from STATIC_GAP_* env vars...")
    global_yaml = build_global_yaml(env)
    print(f"  {len([k for k in DETECTOR_ENV_TO_YAML if k in env])} keys migrated to global defaults")

    # Per-pair from DB
    print(f"\nReading pair_configs from {db_path}")
    con = sqlite3.connect(db_path)
    try:
        symbols = [r[0] for r in con.execute("SELECT symbol FROM pair_configs").fetchall()]
    finally:
        con.close()
    print(f"  {len(symbols)} pairs in DB")

    pair_yamls: dict[str, dict] = {}
    for sym in symbols:
        py = build_pair_yaml(sym, db_path, env)
        if py is not None:
            pair_yamls[sym] = py

    print(f"  {len(pair_yamls)} pairs have overrides → will get YAML files")
    print(f"  {len(symbols) - len(pair_yamls)} pairs use globals only (no YAML needed)")

    # New .env
    new_env = build_new_env(env)
    new_env_lines = new_env.count("\n")

    # Output
    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN — nothing written. Preview:")
        print("=" * 60)
        print(f"\n--- {out_dir / 'global.yaml'} ---")
        import yaml as _yaml
        print(_yaml.safe_dump(global_yaml, sort_keys=False, default_flow_style=False, indent=2))
        for sym, py in sorted(pair_yamls.items()):
            print(f"--- {out_dir / 'pairs' / (sym + '.yaml')} ---")
            print(_yaml.safe_dump(py, sort_keys=False, default_flow_style=False, indent=2))
        print(f"--- {args.new_env} ({new_env_lines} lines, was {len(env)} env vars) ---")
        print(new_env)
        return

    # Write
    global_path = out_dir / "global.yaml"
    write_yaml(
        global_path, global_yaml,
        header="# Global detector defaults — used when a pair has no per-pair override.\n"
               "# Auto-generated by migrate_to_yaml.py from .env STATIC_GAP_* keys.\n"
               "# Edit freely — changes take effect within 30s (no restart).\n\n",
    )
    print(f"\n✓ Wrote {global_path}")

    for sym, py in sorted(pair_yamls.items()):
        pair_path = out_dir / "pairs" / f"{sym}.yaml"
        write_yaml(
            pair_path, py,
            header=f"# Per-pair config for {sym}.\n"
                   f"# Only fields that differ from config/global.yaml.\n"
                   f"# Changes take effect within 30s (no restart).\n\n",
        )
        print(f"✓ Wrote {pair_path}")

    new_env_path = Path(args.new_env)
    new_env_path.write_text(new_env + "\n")
    print(f"\n✓ Wrote {new_env_path} ({new_env_lines} lines, was {len(env)} env vars)")

    print(f"\nNext steps:")
    print(f"  1. Review files in {out_dir}/")
    print(f"  2. Compare: diff {env_path} {new_env_path}")
    print(f"  3. Replace .env: mv {new_env_path} {env_path}")
    print(f"  4. Restart bot: docker compose up -d --force-recreate stakan-bot")


if __name__ == "__main__":
    main()
