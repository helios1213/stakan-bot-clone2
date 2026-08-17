"""
Control the live trading bot from the panel — start / stop / restart.

The bot runs as a docker compose service (`stakan-bot`), NOT a host process, so
these drive `docker compose` on REPO_ROOT. The previous host-process model
(scan /proc → os.kill(PID1, SIGTERM) + Popen a host duplicate on start) was
incompatible with docker: SIGTERM to the container's PID 1 shut the bot down but
left the container "Up" with a dead bot inside (docker's restart:unless-stopped
never fired because the process didn't exit as docker expects), and `start`
would have spawned a rogue non-docker duplicate on the same DB. 2026-08-17: an
operator's Stop/Restart click took the primary down for ~2h this way.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE = "stakan-bot"
_COMPOSE = ["docker", "compose"]
_STOP_GRACE = "30"  # seconds for graceful shutdown (close positions) before SIGKILL


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        _COMPOSE + args, cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )


def _state() -> tuple[bool, str, int]:
    """(running, health, pid) for the service container. running=False if the
    container is absent OR reports unhealthy — so a dead-inside/unhealthy bot
    shows as NOT running instead of a false green (the old failure mode)."""
    try:
        cid = (_run(["ps", "-q", SERVICE], timeout=20).stdout or "").strip().splitlines()
        if not cid:
            return (False, "none", 0)
        insp = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.State.Running}}|{{.State.Health.Status}}|{{.State.Pid}}", cid[0]],
            capture_output=True, text=True, timeout=20,
        )
        parts = (insp.stdout or "").strip().split("|")
        running = len(parts) > 0 and parts[0] == "true"
        health = parts[1] if len(parts) > 1 else ""
        try:
            pid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        except ValueError:
            pid = 0
        return (running, health, pid)
    except Exception:
        return (False, "error", 0)


def bot_pids() -> list[int]:
    """Container's main PID (host-namespace), for display. Empty if not running."""
    running, _health, pid = _state()
    return [pid] if running and pid > 0 else []


def is_running() -> bool:
    """True iff the container is running AND not unhealthy."""
    running, health, _pid = _state()
    return running and health != "unhealthy"


def _result(p: subprocess.CompletedProcess, key: str) -> dict:
    ok = p.returncode == 0
    out = {"ok": ok, key: ok, "pids": bot_pids()}
    if not ok:
        out["error"] = (p.stderr or p.stdout or "").strip()[:300]
    return out


def start() -> dict:
    """docker compose up -d (starts the stopped container; creates if missing)."""
    if is_running():
        return {"ok": True, "already_running": True, "pids": bot_pids()}
    return _result(_run(["up", "-d", SERVICE]), "started")


def stop() -> dict:
    """docker compose stop — graceful (SIGTERM, then SIGKILL after grace). docker
    will NOT auto-restart a deliberately-stopped container, so the bot stays down
    until Start."""
    return _result(_run(["stop", "-t", _STOP_GRACE, SERVICE]), "stopped")


def restart() -> dict:
    """docker compose restart — single atomic op (no host duplicate)."""
    return _result(_run(["restart", "-t", _STOP_GRACE, SERVICE]), "restarted")
