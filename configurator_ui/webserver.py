"""Run with python -m configurator_ui.webserver --config /path/to/config.json."""

import argparse
import hashlib
from hmac import compare_digest
import os
from pathlib import Path
import secrets
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from trading_ui.config import DEFAULT_FAST_TRADING_AGGRESSION_LEVELS, DEFAULT_FAST_TRADING_MINIMUM_CYCLE_SECONDS_BY_MEDIUM
from .storage import ConfigRepository, ConflictError, decode_json

COOKIE = "ktrader_configurator_session"
SESSION_SECONDS = 12 * 3600
STATIC = Path(__file__).parent / "static"


def default_config_path() -> Path:
    configured = os.getenv("ACCOUNT_DASHBOARD_CONFIG")
    if configured:
        return Path(configured)
    sample = Path(__file__).resolve().parents[1] / "trading_ui" / "sample"
    local = sample / "config.local.json"
    return local if local.exists() else sample / "config.json"


def create_app(config_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="KTrader Configurator", docs_url=None, redoc_url=None, openapi_url=None)
    repository = ConfigRepository(config_path or default_config_path())
    app.state.repository = repository
    sessions: dict[str, tuple[float, bytes, str]] = {}
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def fingerprint() -> bytes:
        return hashlib.sha256(repository.admin_password().encode()).digest()

    def session(request: Request) -> tuple[float, bytes, str]:
        token = request.cookies.get(COOKIE, "")
        record = sessions.get(token)
        if record is not None and record[0] > time.time() and compare_digest(record[1], fingerprint()):
            return record
        sessions.pop(token, None)
        raise HTTPException(401, "Sign in with the admin account from the main configuration.")

    async def mutation_body(request: Request, *, csrf: str | None = None):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Requests must come from this configurator.")
        if request.headers.get("x-configurator-request") != "1":
            raise HTTPException(403, "Missing configurator request header.")
        if csrf is not None and not compare_digest(request.headers.get("x-csrf-token", "").encode(), csrf.encode()):
            raise HTTPException(403, "Session token is invalid. Reload the page.")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "Send application/json.")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1024 * 1024:
                raise HTTPException(413, "Configuration must be smaller than 1 MiB.")
        try:
            body = decode_json(bytes(raw))
            if not isinstance(body, dict):
                raise ValueError("Request must be a JSON object.")
            return body
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.exception_handler(OSError)
    async def file_error(request: Request, exc: OSError):
        return JSONResponse({"detail": "Unable to access a configuration file. Check the configured paths and file permissions."}, status_code=400)

    @app.exception_handler(ValueError)
    async def validation_error(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(ConflictError)
    async def conflict_error(request: Request, exc: ConflictError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.post("/api/login")
    async def login(request: Request):
        body = await mutation_body(request)
        password = body.get("password")
        if body.get("username") != "admin" or not isinstance(password, str) or not compare_digest(password.encode(), repository.admin_password().encode()):
            raise HTTPException(401, "Incorrect admin username or password.")
        now = time.time()
        for token, record in list(sessions.items()):
            if record[0] <= now:
                sessions.pop(token, None)
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        token = secrets.token_urlsafe(32)
        sessions[token] = (now + SESSION_SECONDS, fingerprint(), secrets.token_urlsafe(32))
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=request.url.scheme == "https", samesite="strict")
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        record = session(request)
        await mutation_body(request, csrf=record[2])
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/documents")
    def documents(request: Request):
        record = session(request)
        return {
            "documents": repository.read(), "csrf_token": record[2],
            "fast_trading_defaults": {
                "aggression_levels": DEFAULT_FAST_TRADING_AGGRESSION_LEVELS,
                "minimum_cycle_seconds_by_medium": DEFAULT_FAST_TRADING_MINIMUM_CYCLE_SECONDS_BY_MEDIUM,
            },
        }

    @app.put("/api/documents/{name}")
    async def save(name: str, request: Request):
        record = session(request)
        body = await mutation_body(request, csrf=record[2])
        if name not in ("config", "accounts"):
            raise HTTPException(404, "Unknown configuration document.")
        if "document" not in body or not isinstance(body.get("revision"), str):
            raise HTTPException(400, "document and revision are required.")
        result = repository.save(name, body["document"], body["revision"])
        # Keep the editing session usable after an admin password change; expire other sessions.
        current_fingerprint = fingerprint()
        if not compare_digest(record[1], current_fingerprint):
            sessions.clear()
            sessions[request.cookies[COOKIE]] = (record[0], current_fingerprint, record[2])
        return {"ok": True, **result}

    return app


app = create_app()


def run() -> None:
    parser = argparse.ArgumentParser(description="Edit KTrader configuration and account files.")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--ssl-certfile")
    parser.add_argument("--ssl-keyfile")
    args = parser.parse_args()
    application = create_app(args.config)
    application.state.repository.admin_password()
    import uvicorn
    uvicorn.run(application, host=args.host, port=args.port, ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile)


if __name__ == "__main__":
    run()
