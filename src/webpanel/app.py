"""
FastAPI app for the live-trading admin panel.

Access control:
  1. IP whitelist (env STAKAN_PANEL_ALLOW_IPS, default 127.0.0.1,::1)
  2. Password auth (env PANEL_PASSWORD) — login form → session cookie
"""
from __future__ import annotations

import hmac
import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Body, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.webpanel import data, engine_ctl

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

ALLOW_IPS = {
    ip.strip() for ip in
    os.environ.get("STAKAN_PANEL_ALLOW_IPS", "127.0.0.1,::1,testclient").split(",")
    if ip.strip()
}
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
SESSION_TTL = int(os.environ.get("PANEL_SESSION_TTL", str(24 * 3600)))  # 24h default
_COOKIE = "panel_session"

# In-memory session store: token → expiry timestamp
_sessions: dict[str, float] = {}


def _valid_session(token: str) -> bool:
    exp = _sessions.get(token)
    if exp is None:
        return False
    if time.time() > exp:
        _sessions.pop(token, None)
        return False
    return True


def _create_session() -> str:
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def _check_password(candidate: str) -> bool:
    if not PANEL_PASSWORD:
        return True
    return hmac.compare_digest(candidate, PANEL_PASSWORD)


app = FastAPI(title="stakan panel", docs_url=None)

_PUBLIC = {"/login", "/logout"}


@app.middleware("http")
async def ip_whitelist(request: Request, call_next):
    client = request.client.host if request.client else ""
    allow = ALLOW_IPS | data.whitelist_ips()
    if allow and client not in allow and "0.0.0.0" not in allow:
        return JSONResponse({"detail": f"IP {client} not in whitelist"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def auth_required(request: Request, call_next):
    if not PANEL_PASSWORD or request.url.path in _PUBLIC:
        return await call_next(request)
    token = request.cookies.get(_COOKIE, "")
    if _valid_session(token):
        return await call_next(request)
    # API calls → 401 JSON; page calls → redirect to /login
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


# ---- login / logout ----
@app.get("/login")
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if not _check_password(password):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Невірний пароль"}, status_code=401
        )
    token = _create_session()
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(
        _COOKIE, token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(_COOKIE, "")
    _sessions.pop(token, None)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(_COOKIE)
    return resp


# ---- pages ----
@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ---- API: Аккаунты (primary + remote bots) ----
# Every write endpoint accepts an optional `server` field in the payload —
# "primary" (default) hits the local DB; anything else routes via SSH-RPC to
# the named clone. GET returns the merged list with each row tagged `server`.
@app.get("/api/accounts")
async def api_accounts():
    return {"accounts": data.accounts_all()}


@app.post("/api/accounts/connect")
async def api_connect_account(payload: dict = Body(...)):
    server = payload.get("server") or "primary"
    try:
        res = data.add_account_routed(
            server,
            webkey=payload.get("webkey", ""),
            label=payload.get("label"),
        )
    except data.AccountError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "remote add failed")
    return {"ok": True, "slot_id": res.get("slot_id"), "server": server,
            "accounts": data.accounts_all()}


@app.post("/api/accounts/{slot_id}/remove")
async def api_remove_account(slot_id: int, payload: dict = Body(default={})):
    server = (payload or {}).get("server") or "primary"
    res = data.remove_account_routed(server, slot_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "remote remove failed")
    return {"ok": True, "server": server, "accounts": data.accounts_all()}


@app.post("/api/accounts/{slot_id}")
async def api_set_account(slot_id: int, payload: dict = Body(...)):
    server = payload.get("server") or "primary"
    kwargs = {k: payload[k] for k in ("enabled", "label", "live_enabled") if k in payload}
    res = data.set_account_routed(server, slot_id, **kwargs)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "remote set failed")
    return {"ok": True, "server": server, "accounts": data.accounts_all()}


# ---- API: Dashboard ----
@app.get("/api/dashboard")
async def api_dashboard():
    return data.dashboard(None)


# ---- API: Инциденты ----
@app.get("/api/incidents")
async def api_incidents():
    return {"incidents": data.incidents()}


@app.post("/api/incidents")
async def api_add_incident(payload: dict = Body(...)):
    iid = data.add_incident(
        payload.get("severity", "info"),
        payload.get("message", ""),
        payload.get("source", "panel"),
    )
    return {"id": iid, "incidents": data.incidents()}


@app.post("/api/incidents/{incident_id}/ack")
async def api_ack_incident(incident_id: int):
    data.ack_incident(incident_id)
    return {"ok": True, "incidents": data.incidents()}


# ---- API: Пользователи (panel access whitelist) ----
@app.get("/api/users")
async def api_users(request: Request):
    return {
        "whitelist": data.whitelist(),
        "env_ips": sorted(ALLOW_IPS),
        "your_ip": request.client.host if request.client else None,
    }


@app.post("/api/users")
async def api_add_user(payload: dict = Body(...)):
    return {"whitelist": data.add_whitelist(payload["ip"], payload.get("label", ""))}


@app.post("/api/users/remove")
async def api_remove_user(payload: dict = Body(...)):
    return {"whitelist": data.remove_whitelist(payload["ip"])}


# ---- API: Bot engine control ----
@app.get("/api/bot")
async def api_bot():
    return {"running": engine_ctl.is_running(), "pids": engine_ctl.bot_pids()}


@app.post("/api/bot/start")
async def api_bot_start():
    return engine_ctl.start()


@app.post("/api/bot/stop")
async def api_bot_stop():
    return engine_ctl.stop()


@app.post("/api/bot/restart")
async def api_bot_restart():
    return engine_ctl.restart()


# ---- API: Live trades ----
@app.get("/api/live/trades")
async def api_live_trades():
    return {"trades": data.live_trades(100)}


@app.get("/api/live/summary")
async def api_live_summary():
    return data.live_trades_summary()


# ---- API: Пары ----
# GET /api/pairs?server=primary|clone1 — pairs for that bot. `positions` is
# always primary-only (used by the dashboard, not the pair editor).
# POST body includes optional `server`; default = primary.
@app.get("/api/pairs")
async def api_pairs(server: str = "primary"):
    return {
        "pairs": data.pairs_routed(server),
        "positions": data.open_positions(),
        "servers": data.available_servers(),
        "current": server,
    }


@app.post("/api/pairs/{symbol}")
async def api_set_pair(symbol: str, payload: dict = Body(...)):
    server = payload.pop("server", None) or "primary"
    res = data.set_pair_config_routed(server, symbol, **payload)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "remote set failed")
    return {"ok": True, "server": server}


# ---- API: Открытые позиции ----
@app.get("/api/positions")
async def api_positions():
    return {"positions": data.open_positions()}


# ---- API: KILL ALL ----
@app.post("/api/bot/killall")
async def api_kill_all():
    count = data.set_all_pairs_shadow()
    return {"ok": True, "demoted": count}


# ---- API: Очереди (rotation plan) ----
@app.get("/api/rotation")
async def api_rotation():
    return {
        "rotation": data.rotation(),
        "available_pairs": data.available_pairs(),
    }


@app.post("/api/rotation")
async def api_set_rotation(payload: dict = Body(...)):
    return {"rotation": data.set_rotation(payload.get("plan", []))}


_static = HERE / "static"
if _static.exists():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")
