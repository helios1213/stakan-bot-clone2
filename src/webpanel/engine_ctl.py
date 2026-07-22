"""
Control the live trading bot (python -m src.main) from the panel — start / stop /
restart. Detection scans /proc for the python src.main process.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PY = "python3"
MODULE = "src.main"
LOG = REPO_ROOT / "logs" / "stakan.log"


def bot_pids() -> list[int]:
    """PIDs of the python -m src.main process(es)."""
    pids = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cl = (d / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except (OSError, ValueError):
            continue
        if "src.main" in cl and "bash" not in cl and "/proc" not in cl:
            pids.append(int(d.name))
    return pids


def is_running() -> bool:
    return len(bot_pids()) > 0


def start() -> dict:
    if is_running():
        return {"ok": True, "already_running": True, "pids": bot_pids()}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOG, "ab")
    p = subprocess.Popen(
        [PY, "-m", MODULE], cwd=str(REPO_ROOT),
        stdout=f, stderr=f, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "started_pid": p.pid}


def stop() -> dict:
    pids = bot_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(15):
        if not is_running():
            break
        time.sleep(0.2)
    return {"ok": True, "stopped": pids}


def restart() -> dict:
    stop()
    return start()
