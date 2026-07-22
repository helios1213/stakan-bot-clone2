#!/usr/bin/env python3
"""
Launch the local admin panel.

    .venv/bin/python run_panel.py            # http://127.0.0.1:8777
    STAKAN_PANEL_PORT=9000 .venv/bin/python run_panel.py
    STAKAN_PANEL_ALLOW_IPS=0.0.0.0 ...       # allow any IP (LAN access)

Whitelist: STAKAN_PANEL_ALLOW_IPS (comma-separated, default 127.0.0.1,::1).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Load .env before uvicorn imports the app (so PANEL_PASSWORD etc. are set)
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "src.webpanel.app:app",
        host=os.environ.get("STAKAN_PANEL_HOST", "127.0.0.1"),
        port=int(os.environ.get("STAKAN_PANEL_PORT", "8777")),
        log_level="info",
    )
