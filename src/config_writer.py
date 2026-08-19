"""Write per-pair config values back into the pair YAML.

The pair YAML (config/pairs/<SYMBOL>.yaml) is the single source of truth for
static pair config (read by ConfigLoader, hot-reloaded ≤30s). The sizing UIs
(Telegram cmd_sizing, webpanel) used to write margin/leverage into the DB
pair_configs columns — which the runtime no longer reads. They now route the
write here so it lands where it's actually read.

Updates keys in the `execution:` block in place (preserves comments/structure),
appends a missing key to the block. Atomic write (temp + os.replace).
"""
from __future__ import annotations

import os
import re
import tempfile

# Default config dir = <repo_root>/config, resolved relative to THIS file so it
# works in every context without an env override: bot container (config_writer
# at /app/src → /app/config), host admin-panel and clone SSH-RPC (config_writer
# at /root/stakan-bot/src → /root/stakan-bot/config). STAKAN_CONFIG_DIR still
# overrides if explicitly set. (Was hardcoded "/app/config" — correct only in
# the container; on a host it pointed at a non-existent dir, so shared_loader
# silently returned global defaults.)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.environ.get("STAKAN_CONFIG_DIR", os.path.join(_REPO_ROOT, "config"))


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


_loader = None


def shared_loader():
    """Module-level cached ConfigLoader for the sizing UIs.

    A fresh ConfigLoader().load() per call re-read every pair file AND logged
    "ConfigLoader: loaded …" each time — N times per sizing menu (one per pair)
    plus once per webpanel refresh, all as blocking file-I/O on the bot's event
    loop. Instead we keep one loader and let maybe_reload() pick up external
    edits on its TTL; set_pair_execution() force-refreshes it so a write made
    through the UI is visible on the very next read (no TTL lag)."""
    global _loader
    from src.config_loader import ConfigLoader
    if _loader is None:
        _loader = ConfigLoader(CONFIG_DIR)
        _loader.load()
    else:
        _loader.maybe_reload()
    return _loader


def read_pair_sizing(symbol: str) -> dict:
    """Read margin/leverage for a pair from its YAML (the single source of
    truth). Returns global defaults if the pair has no YAML file. Reflects a
    UI write immediately (set_pair_execution refreshes the shared loader)."""
    e = shared_loader().get(symbol).execution
    return {
        # both key spellings — cmd_sizing uses margin_min, cmd_slot/webpanel
        # use the DB-column spelling margin_min_usdt.
        "margin_min": e.margin_min_usdt,
        "margin_max": e.margin_max_usdt,
        "margin_min_usdt": e.margin_min_usdt,
        "margin_max_usdt": e.margin_max_usdt,
        "leverage_min": e.leverage_min,
        "leverage_max": e.leverage_max,
    }


def pair_config_exists(symbol: str) -> bool:
    """True if the pair has its own YAML file (i.e. explicit sizing config),
    not just global defaults. Used by the go-live guard."""
    return os.path.exists(os.path.join(CONFIG_DIR, "pairs", f"{symbol}.yaml"))


def set_pair_execution(symbol: str, **kwargs) -> bool:
    """Update keys in the execution block of <symbol>.yaml. Returns True if the
    file existed and was written, False if there's no pair file / no execution
    block (caller should treat that as 'pair not configured')."""
    if not kwargs:
        return False
    # ЄДИНЕ ДЖЕРЕЛО РОЗМІРУ = slot_pair_sizing (DB оверайд). Сайзинг
    # НІКОЛИ не пишеться в yaml — відкидаємо margin_/leverage_ ключі
    # (Ship 2). Якщо лишились лише сайзинг-ключі — нічого не пишемо.
    kwargs = {k: v for k, v in kwargs.items()
              if not (k.startswith("margin_") or k.startswith("leverage_"))}
    if not kwargs:
        return False
    path = os.path.join(CONFIG_DIR, "pairs", f"{symbol}.yaml")
    if not os.path.exists(path):
        return False
    with open(path) as f:
        lines = f.readlines()

    # Locate the execution: block (top-level key → next top-level key/EOF).
    exec_start = exec_end = None
    for i, ln in enumerate(lines):
        if re.match(r"^execution:", ln):
            exec_start = i
            continue
        if exec_start is not None and re.match(r"^[^\s#]", ln):
            exec_end = i
            break
    if exec_start is None:
        return False
    if exec_end is None:
        exec_end = len(lines)

    remaining = dict(kwargs)
    # Update existing keys in place.
    for i in range(exec_start + 1, exec_end):
        m = re.match(r"^(\s+)([A-Za-z_]+):", lines[i])
        if m and m.group(2) in remaining:
            key = m.group(2)
            lines[i] = f"{m.group(1)}{key}: {_fmt(remaining.pop(key))}\n"
    # Append any missing keys at the end of the execution block.
    insert_at = exec_end
    for key, val in remaining.items():
        lines.insert(insert_at, f"  {key}: {_fmt(val)}\n")
        insert_at += 1

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
        os.replace(tmp, path)
        # Keep the bot (container uid 1000) able to read+write even when the
        # HOST webpanel (runs as root) is the writer — else a panel sizing edit
        # leaves a root:root 600 file that locks out the bot reload + Telegram
        # edits (recurring permission bug, fixed 2026-06-26).
        try:
            os.chmod(path, 0o644)
            os.chown(path, 1000, 1000)
        except (PermissionError, OSError):
            pass
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    # Refresh the shared loader so the next read_pair_sizing() reflects this
    # write immediately, without waiting for the maybe_reload() TTL.
    global _loader
    if _loader is not None:
        _loader.load()
    return True
