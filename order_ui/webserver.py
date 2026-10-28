"""Run from KTrader: python -m order_ui.webserver --config /path/to/config.json."""

import argparse
from contextlib import asynccontextmanager
from hmac import compare_digest
import logging
from pathlib import Path
import secrets
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from trading_ui.services.activity_reporting import ActivityReporting, DEFAULT_REPORTING_DB

from .config import default_config_path, load_config
from .consumer import OrderStatusConsumer
from .events import STATUSES
from .store import OrderStore

STATIC = Path(__file__).parent / "static"
COOKIE = "ktrader_order_ui_session"
SESSION_SECONDS = 12 * 3600


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=4096)


class TagBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    symbol: str = Field(min_length=1, max_length=64)


def create_app(config_path=None, *, consumer_factory=OrderStatusConsumer, config=None):
    sessions = {}

    @asynccontextmanager
    async def lifespan(app):
        cfg = config if config is not None else load_config(config_path or default_config_path())
        app.state.config = cfg
        reporting = ActivityReporting(cfg.get("reporting_db_path", DEFAULT_REPORTING_DB))
        app.state.reporting = reporting
        app.state.store = OrderStore(cfg["accounts"], cfg["timezone"], reporting=reporting)
        consumer = consumer_factory(cfg["kafka"], app.state.store)
        app.state.consumer = consumer
        try:
            consumer.start()
            yield
        finally:
            consumer.stop()
            reporting.close()
            sessions.clear()

    app = FastAPI(title="KTrader Orders", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def user(request):
        token = request.cookies.get(COOKIE, "")
        record = sessions.get(token)
        if record and record[1] > time.time():
            return record[0]
        sessions.pop(token, None)
        raise HTTPException(401, "Sign in with your KTrader credentials.")

    def same_origin(request):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Requests must come from this order UI.")
        if request.headers.get("x-order-ui-request") != "1":
            raise HTTPException(403, "Missing order UI request header.")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.post("/api/login")
    def login(request: Request, body: LoginBody):
        same_origin(request)
        username = body.username.strip()
        record = app.state.config["users"].get(username)
        if record is None or not compare_digest(record["password"].encode(), body.password.encode()):
            raise HTTPException(401, "Incorrect username or password.")
        now = time.time()
        for token, session in list(sessions.items()):
            if session[1] <= now:
                sessions.pop(token, None)
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        token = secrets.token_urlsafe(32)
        sessions[token] = (username, now + SESSION_SECONDS)
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=SESSION_SECONDS,
                            secure=bool(app.state.config["server"]["ssl_certfile"]) or request.url.scheme == "https")
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        same_origin(request)
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/orders")
    def orders(request: Request):
        username = user(request)
        cfg = app.state.config
        snapshot = app.state.store.snapshot()
        tags = app.state.reporting.order_tags(snapshot["orders"])
        for order in snapshot["orders"]:
            order["tags"] = tags.get((order["account_id"], order["order_id"]), [])
        return {**snapshot, "username": username,
                "reporting": app.state.reporting.snapshot(cfg["accounts"]),
                "refresh_interval_seconds": cfg["refresh_interval_seconds"], "statuses": STATUSES,
                "accounts": [{"id": meta.id, "num_id": meta.num_id, "broker": meta.broker}
                             for meta in sorted(cfg["accounts"].values(), key=lambda meta: meta.num_id)]}

    @app.post("/api/tags", status_code=201)
    def start_tag(request: Request, body: TagBody):
        user(request)
        same_origin(request)
        try:
            tag_id = app.state.reporting.start_tag(body.name, body.symbol)
        except ValueError as exc:
            raise HTTPException(409 if "active tag" in str(exc) else 422, str(exc)) from exc
        return {"id": tag_id}

    @app.post("/api/tags/{tag_id}/stop")
    def stop_tag(request: Request, tag_id: str):
        user(request)
        same_origin(request)
        try:
            app.state.reporting.stop_tag(tag_id)
        except KeyError as exc:
            raise HTTPException(404, "Tag not found.") from exc
        return {"ok": True}

    return app


app = create_app()


def run():
    parser = argparse.ArgumentParser(description="Display today's KTrader orders in a separate server.")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="The same config.json used by trading_ui")
    parser.add_argument("--host", help="Override order_ui.server.host")
    parser.add_argument("--port", type=int, help="Override order_ui.server.port")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.host is not None:
        cfg["server"]["host"] = args.host
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        cfg["server"]["port"] = args.port
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn
    server = cfg["server"]
    uvicorn.run(create_app(config=cfg), host=server["host"], port=server["port"],
                ssl_certfile=server["ssl_certfile"] or None, ssl_keyfile=server["ssl_keyfile"] or None)


if __name__ == "__main__":
    run()
