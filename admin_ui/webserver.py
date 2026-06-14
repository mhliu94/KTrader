import os
import secrets
from hmac import compare_digest
from typing import Dict
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

from .config import load_config
from .i18n import SUPPORTED_LANGS, resolve_lang, t
from .kafka.consumer_live_updates import LiveTradingUpdatesConsumer
from .store import StrategyStore
from .templates import render_layout, render_login_page, render_strategy_page

app = FastAPI(title="Admin UI")

store = StrategyStore()

APP_CONFIG: Dict = {}
AUTH_USERS: Dict = {}
SESSIONS: Dict[str, str] = {}
LIVE_UPDATES_CONSUMER = None


def _build_next_path(request: Request) -> str:
    path = request.url.path or "/strategies"
    q = request.url.query
    return f"{path}?{q}" if q else path


def _current_user(request: Request) -> str | None:
    token = request.cookies.get("auth_token")
    if not token:
        return None
    return SESSIONS.get(token)


def _verify_credentials(username: str, password: str) -> bool:
    rec = AUTH_USERS.get(username)
    if not rec:
        return False
    stored = str(rec.get("password", ""))
    return compare_digest(stored, password)


def _require_auth_page(request: Request):
    user = _current_user(request)
    if user:
        return user, None
    next_path = _build_next_path(request)
    return None, RedirectResponse(url=f"/login?next={quote(next_path)}", status_code=303)


def _require_auth_api(request: Request):
    user = _current_user(request)
    if user:
        return user, None
    return None, JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)


@app.on_event("startup")
def on_startup() -> None:
    global APP_CONFIG, AUTH_USERS, LIVE_UPDATES_CONSUMER

    config_path = os.getenv("ADMIN_DASHBOARD_CONFIG", "./admin_ui/sample/config.json")
    APP_CONFIG = load_config(config_path)
    AUTH_USERS = APP_CONFIG["auth"]["users"]

    LIVE_UPDATES_CONSUMER = LiveTradingUpdatesConsumer(APP_CONFIG["kafka"], store)
    LIVE_UPDATES_CONSUMER.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    if LIVE_UPDATES_CONSUMER:
        LIVE_UPDATES_CONSUMER.stop()


@app.get("/set-lang")
def set_lang(lang: str, next: str = "/strategies"):
    if lang not in SUPPORTED_LANGS:
        lang = "en"
    resp = RedirectResponse(url=next, status_code=302)
    resp.set_cookie("lang", lang, max_age=180 * 24 * 3600, httponly=False, samesite="lax")
    return resp


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    return RedirectResponse(url="/strategies", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None, err: str | None = None) -> HTMLResponse:
    lang = resolve_lang(request)
    if _current_user(request):
        return RedirectResponse(url=(next or "/strategies"), status_code=302)
    next_path = next or "/strategies"
    inner = render_login_page(lang, next_path=next_path, error=(err or ""))
    return HTMLResponse(render_layout(lang, "strategies", inner, current_user=""))


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/strategies"),
):
    lang = resolve_lang(request)
    user = str(username).strip()
    pw = str(password)
    if not _verify_credentials(user, pw):
        return RedirectResponse(url=f"/login?next={quote(next)}&err={quote(t(lang,'login_failed'))}", status_code=303)

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = user
    resp = RedirectResponse(url=(next or "/strategies"), status_code=303)
    secure_cookie = bool(APP_CONFIG.get("server", {}).get("ssl_enabled", False))
    resp.set_cookie("auth_token", token, max_age=12 * 3600, httponly=True, samesite="lax", secure=secure_cookie)
    return resp


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    token = request.cookies.get("auth_token")
    if token:
        SESSIONS.pop(token, None)
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("auth_token")
    return resp


@app.get("/strategies", response_class=HTMLResponse)
def strategy_monitor(request: Request) -> HTMLResponse:
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    inner = render_strategy_page(
        lang=lang,
        strategies=store.get_all(),
        summary=store.summary(),
        topic_name=APP_CONFIG["kafka"]["live_trading_updates_topic"],
    )
    return HTMLResponse(render_layout(lang, "strategies", inner, current_user=(user or "")))


@app.get("/api/strategies")
def api_strategies(request: Request) -> JSONResponse:
    _, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    return JSONResponse(
        {
            "ok": True,
            "summary": store.summary().__dict__,
            "strategies": [item.__dict__ for item in store.get_all().values()],
            "topic": APP_CONFIG["kafka"]["live_trading_updates_topic"],
        }
    )


def run() -> None:
    config_path = os.getenv("ADMIN_DASHBOARD_CONFIG", "./admin_ui/sample/config.json")
    cfg = load_config(config_path)
    server_cfg = cfg["server"]
    kwargs = {
        "host": server_cfg["host"],
        "port": int(server_cfg["port"]),
    }
    if server_cfg.get("ssl_enabled"):
        kwargs["ssl_certfile"] = server_cfg["ssl_certfile"]
        kwargs["ssl_keyfile"] = server_cfg["ssl_keyfile"]
    uvicorn.run("admin_ui.webserver:app", **kwargs)


if __name__ == "__main__":
    run()
