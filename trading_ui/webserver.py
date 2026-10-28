import asyncio
import json
import logging
import os
import secrets
import threading
from decimal import InvalidOperation
from datetime import datetime, timezone
from hmac import compare_digest
from typing import AsyncIterator, Dict, Tuple

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from urllib.parse import quote
from .app_logging import configure_logging
from .services.market_data import MarketDataStore, HistoricalCloseStore, QuoteRow
from .templates import render_market_data_page, render_market_insights_page
from .models import AccountSnapshot

from .config import load_config, load_account_metas, load_symbols
from .i18n import resolve_lang, SUPPORTED_LANGS, t
from .store import AccountStore
from .templates import (
    ACCOUNT_SORT_NUMERIC_ID,
    render_layout,
    render_account_details_page,
    render_control_panel_page,
    render_currency_conversion_page,
    render_live_account_details_page,
    render_login_page,
    resolve_account_sort,
    render_trading_status_page,
)
from .services.operations_log import OperationsLog
from .services.activity_reporting import ActivityReporting, reporting_db_path
from .services.system_snapshots import SystemSnapshotRecorder
from .services.orders import (
    validate_order_inputs,
    validate_quick_order_inputs,
    validate_limit_order_inputs,
    validate_cancel_open_orders_inputs,
    validate_algo_start_inputs,
    build_algo_stop_command,
    validate_currency_conversion_inputs,
    validate_trading_status_inputs,
)
from .services.fast_trading import FastTradingStrategyManager
from .services.smart_selling_runtime import SmartSellingRuntime
from .kafka.consumer_smart_selling import SmartSellingConsumer
from .kafka.consumer_account_details import AccountDetailsConsumer
from .kafka.consumer_market_data import PriceBookConsumer
from .kafka.producer_trading_commands import TradingCommandsProducer

configure_logging()
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="Trading UI")

store = AccountStore()

APP_CONFIG: Dict = {}
ACCOUNT_METAS: Dict = {}
SYMBOLS = []
AUTH_USERS: Dict = {}
SESSIONS: Dict[str, str] = {}

ACCOUNT_CONSUMER = None
MARKET_DATA_CONSUMER = None
COMMANDS_PRODUCER = None
FAST_TRADING_MANAGER = None
SMART_SELLING_MANAGER = None
SMART_SELLING_CONSUMER = None
ALGO_CONTROL_LOCK = threading.RLock()
OPERATIONS_LOG = None
ACTIVITY_REPORTING = None
SYSTEM_SNAPSHOTS = None

MD_STORE = MarketDataStore()
HIST_CLOSE_STORE = HistoricalCloseStore("./market_data/historical_prices.csv")

ACCOUNT_SORT_COOKIE = "account_details_sort"
ACCOUNT_SECURITY_COOKIE = "account_details_security"
ACCOUNT_SORT_COOKIE_MAX_AGE = 180 * 24 * 3600
ACCOUNT_DETAILS_QUOTE_REFRESH_SECONDS = 5.0


def _filter_configured_snapshots(accounts: Dict[str, AccountSnapshot]) -> Dict[str, AccountSnapshot]:
    return {account_id: snap for account_id, snap in accounts.items() if account_id in ACCOUNT_METAS}


def _normalized_configured_symbols() -> list[str]:
    symbols = []
    seen = set()
    for raw_symbol in SYMBOLS:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)
    return symbols


def _market_insights_symbols() -> list[str]:
    return sorted(set(_normalized_configured_symbols()) | set(MD_STORE.get_book_symbols()))


def _extract_fast_trading_config(body: Dict) -> object:
    raw_config = body.get("fast_trading_config")
    if raw_config is not None and not isinstance(raw_config, dict):
        return raw_config

    config = dict(raw_config or {})
    aliases = {
        "price_limit": ("fast_trading_price_limit", "price_limit"),
        "account_ids": ("fast_trading_account_ids", "account_ids"),
        "book_levels": ("fast_trading_book_levels", "book_levels"),
        "target_percentage": ("fast_trading_target_percentage", "target_percentage"),
        "cycle_seconds": ("fast_trading_cycle_seconds", "cycle_seconds"),
        "test_mode": ("fast_trading_test_mode", "test_mode"),
    }
    found_alias = False
    for config_key, candidates in aliases.items():
        if config_key in config:
            continue
        for candidate in candidates:
            if candidate in body:
                config[config_key] = body[candidate]
                found_alias = True
                break
    if raw_config is None and not found_alias:
        return None
    return config


def get_served_snapshots() -> Tuple[Dict, str]:
    return _filter_configured_snapshots(store.get_all()), "kafka"


def _render_account_details_content(lang: str, account_sort: str, security: str) -> str:
    accounts, src = get_served_snapshots()
    held_symbols = sorted({
        str(position.symbol or "").strip().upper()
        for account in accounts.values()
        for position in account.positions
        if str(position.symbol or "").strip() and position.qty
    })
    return render_account_details_page(
        lang=lang,
        store=store,
        accounts=accounts,
        source_label=src,
        account_metas=ACCOUNT_METAS,
        account_details_topic=APP_CONFIG["kafka"]["account_details_topic"],
        symbols=_normalized_configured_symbols(),
        account_sort=account_sort,
        security=security,
        quotes=MD_STORE.get_stock_quotes(held_symbols),
    )


def _account_details_sse_event(lang: str, account_sort: str, security: str, version: int) -> str:
    payload = json.dumps(
        {"html": _render_account_details_content(lang, account_sort, security)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {version}\ndata: {payload}\n\n"


async def _account_details_event_stream(
    lang: str,
    account_sort: str,
    security: str,
    request: Request | None = None,
) -> AsyncIterator[str]:
    token, version, updates = store.subscribe_updates()
    try:
        if request is not None and (
            _current_user(request) is None or await request.is_disconnected()
        ):
            return
        last_event = _account_details_sse_event(lang, account_sort, security, version)
        yield last_event
        while True:
            try:
                next_version = await asyncio.wait_for(
                    updates.get(), timeout=ACCOUNT_DETAILS_QUOTE_REFRESH_SECONDS
                )
            except TimeoutError:
                if request is not None and (
                    _current_user(request) is None or await request.is_disconnected()
                ):
                    return
                # Quotes can change without a new account snapshot.
                event = _account_details_sse_event(lang, account_sort, security, version)
                if event != last_event:
                    last_event = event
                    yield event
                else:
                    yield ": keep-alive\n\n"
                continue

            if request is not None and (
                _current_user(request) is None or await request.is_disconnected()
            ):
                return
            version = next_version
            last_event = _account_details_sse_event(lang, account_sort, security, version)
            yield last_event
    finally:
        store.unsubscribe_updates(token)


def _build_next_path(request: Request) -> str:
    path = request.url.path or "/account-details"
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


def _has_lmh_feature_access(user: str | None) -> bool:
    return str(user or "").strip() == "lmh"


def _can_convert_currency(user: str | None) -> bool:
    return _has_lmh_feature_access(user)


def _can_manage_trading(user: str | None) -> bool:
    return _has_lmh_feature_access(user)


def _publish_fast_trading_command(command: Dict, key: str) -> None:
    with ALGO_CONTROL_LOCK:
        if not FAST_TRADING_MANAGER or not FAST_TRADING_MANAGER.has_running_strategy(
            command.get("account_id"), command.get("symbol")
        ):
            return
        if SMART_SELLING_MANAGER:
            if SMART_SELLING_MANAGER.has_active_session():
                raise ValueError("Stop Smart selling before starting another algorithm")
            SMART_SELLING_MANAGER.track_external_order(command)
        _publish_trading_command(command, key)


def _publish_control_command(command: Dict, key: str) -> None:
    """Manual order instructions cannot interfere with an algorithm's accounts."""
    places_order = command.get("type") in {
        "LIMIT_ORDER",
        "LIMIT_ORDER_FOK",
        "MARKET_ORDER",
        "DELAYED_MARKET_ORDER",
    }
    with ALGO_CONTROL_LOCK:
        account_id = command.get("account_id")
        if (
            places_order
            or command.get("type") == "CURRENCY_CONVERSION"
            or str(command.get("type", "")).startswith("CANCEL_")
        ):
            if any(
                manager and manager.owns_account(account_id)
                for manager in (FAST_TRADING_MANAGER, SMART_SELLING_MANAGER)
            ):
                raise ValueError(
                    "An algorithm session owns this account; "
                    "stop it and await outstanding order confirmations first"
                )
        if SMART_SELLING_MANAGER and places_order:
            SMART_SELLING_MANAGER.track_external_order(command)
        _publish_trading_command(command, key)


def _publish_trading_command(command: Dict, key: str) -> None:
    """Transport only: callers check ownership and journal intent before handoff."""
    assert COMMANDS_PRODUCER is not None
    COMMANDS_PRODUCER.publish_order(command, key=key)


def _require_idle_algorithms():
    if any(
        manager and manager.has_active_session()
        for manager in (FAST_TRADING_MANAGER, SMART_SELLING_MANAGER)
    ):
        raise ValueError(
            "Stop the existing algorithm session and await outstanding order confirmations "
            "before starting another"
        )


def _start_fast_strategy(command):
    with ALGO_CONTROL_LOCK:
        _require_idle_algorithms()
        return FAST_TRADING_MANAGER.start_strategy(command)


def _start_smart_parent(settings):
    with ALGO_CONTROL_LOCK:
        _require_idle_algorithms()
        return SMART_SELLING_MANAGER.start_parent(settings)


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
    try:
        _start_services()
    except BaseException:
        on_shutdown()
        raise


def _start_services() -> None:
    global APP_CONFIG, ACCOUNT_METAS, SYMBOLS, AUTH_USERS, ACCOUNT_CONSUMER, MARKET_DATA_CONSUMER, COMMANDS_PRODUCER, FAST_TRADING_MANAGER, OPERATIONS_LOG, HIST_CLOSE_STORE
    global SMART_SELLING_MANAGER, SMART_SELLING_CONSUMER, ACTIVITY_REPORTING, SYSTEM_SNAPSHOTS

    server_started_at = datetime.now(timezone.utc)
    config_path = os.getenv("ACCOUNT_DASHBOARD_CONFIG", "./trading_ui/sample/config.json")
    APP_CONFIG = load_config(config_path)
    ACCOUNT_METAS = load_account_metas(APP_CONFIG)
    SYMBOLS = load_symbols(APP_CONFIG)
    AUTH_USERS = APP_CONFIG["auth"]["users"]
    HIST_CLOSE_STORE = HistoricalCloseStore(APP_CONFIG["market_data"]["historical_prices_csv"])
    LOGGER.info("market-data historical_prices_csv=%s", APP_CONFIG["market_data"]["historical_prices_csv"])
    OPERATIONS_LOG = OperationsLog(
        log_dir=os.getenv("OPERATIONS_LOG_DIR", "./logs"),
        server_started_at=server_started_at,
        snapshots_provider=get_served_snapshots,
    )
    LOGGER.info("operations log path=%s", OPERATIONS_LOG.path)

    # Archive every configured trade account, including accounts hidden in the UI.
    snapshot_metas = load_account_metas(APP_CONFIG, include_hidden=True)
    db_path = reporting_db_path(APP_CONFIG, config_path)
    ACTIVITY_REPORTING = ActivityReporting(db_path)
    SYSTEM_SNAPSHOTS = SystemSnapshotRecorder(
        db_path, accounts_provider=lambda: snapshot_metas,
        snapshots_provider=lambda: (store.get_all(), "kafka"),
    )
    ACCOUNT_CONSUMER = AccountDetailsConsumer(APP_CONFIG["kafka"], store, snapshot_metas)
    ACCOUNT_CONSUMER.start()
    SYSTEM_SNAPSHOTS.start()

    COMMANDS_PRODUCER = TradingCommandsProducer(APP_CONFIG["kafka"])
    FAST_TRADING_MANAGER = FastTradingStrategyManager(
        market_data_store=MD_STORE,
        account_metas_provider=lambda: ACCOUNT_METAS,
        account_snapshots_provider=lambda: get_served_snapshots()[0],
        operations_log=OPERATIONS_LOG,
        reporting=ACTIVITY_REPORTING,
        publish_command=_publish_fast_trading_command,
        fast_trading_settings=APP_CONFIG.get("fast_trading"),
    )
    if APP_CONFIG["smart_selling"]["enabled"]:
        SMART_SELLING_MANAGER = SmartSellingRuntime(
            APP_CONFIG["smart_selling"],
            reporting=ACTIVITY_REPORTING,
            account_metas_provider=lambda: ACCOUNT_METAS,
            snapshots_provider=lambda: get_served_snapshots()[0],
            publish_command=_publish_trading_command,
            fast_active=lambda: FAST_TRADING_MANAGER.has_active_session(),
            fast_conflict=lambda account, symbol: FAST_TRADING_MANAGER.has_strategy(
                account, symbol
            ),
        )
        SMART_SELLING_CONSUMER = SmartSellingConsumer(APP_CONFIG, SMART_SELLING_MANAGER)
        SMART_SELLING_CONSUMER.start()
        SMART_SELLING_MANAGER.start()
    MARKET_DATA_CONSUMER = PriceBookConsumer(
        APP_CONFIG["kafka"],
        MD_STORE,
        payload_callback=SMART_SELLING_MANAGER.on_price_book if SMART_SELLING_MANAGER else None,
        health_callback=(
            SMART_SELLING_MANAGER.on_price_book_health if SMART_SELLING_MANAGER else None
        ),
    )
    MARKET_DATA_CONSUMER.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    global SYSTEM_SNAPSHOTS, FAST_TRADING_MANAGER, SMART_SELLING_MANAGER
    global ACCOUNT_CONSUMER, MARKET_DATA_CONSUMER, SMART_SELLING_CONSUMER, COMMANDS_PRODUCER, ACTIVITY_REPORTING
    # Also used after partial startup. One failing service must not strand others.
    for service, method, args in (
        (SYSTEM_SNAPSHOTS, "stop", ()),
        (FAST_TRADING_MANAGER, "stop_all", ()),
        (SMART_SELLING_MANAGER, "stop_all", ()),
        (ACCOUNT_CONSUMER, "stop", ()),
        (MARKET_DATA_CONSUMER, "stop", ()),
        (SMART_SELLING_CONSUMER, "stop", ()),
        (SMART_SELLING_MANAGER, "close", ()),
        (COMMANDS_PRODUCER, "flush", (2.0,)),
        (ACTIVITY_REPORTING, "close", ()),
    ):
        if service is not None:
            try:
                getattr(service, method)(*args)
            except Exception:
                LOGGER.exception("Failed shutting down %s.%s", type(service).__name__, method)
    SYSTEM_SNAPSHOTS = FAST_TRADING_MANAGER = SMART_SELLING_MANAGER = None
    ACCOUNT_CONSUMER = MARKET_DATA_CONSUMER = SMART_SELLING_CONSUMER = None
    COMMANDS_PRODUCER = ACTIVITY_REPORTING = None


@app.get("/set-lang")
def set_lang(lang: str, next: str = "/account-details"):
    if lang not in SUPPORTED_LANGS:
        lang = "en"
    resp = RedirectResponse(url=next, status_code=302)
    resp.set_cookie("lang", lang, max_age=180 * 24 * 3600, httponly=False, samesite="lax")
    return resp


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    return RedirectResponse(url="/account-details", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None, err: str | None = None) -> HTMLResponse:
    lang = resolve_lang(request)
    if _current_user(request):
        return RedirectResponse(url=(next or "/account-details"), status_code=302)
    next_path = next or "/account-details"
    inner = render_login_page(lang, next_path=next_path, error=(err or ""))
    return HTMLResponse(render_layout(lang, "account-details", inner, current_user=""))


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/account-details"),
):
    lang = resolve_lang(request)
    user = str(username).strip()
    pw = str(password)
    if not _verify_credentials(user, pw):
        return RedirectResponse(url=f"/login?next={quote(next)}&err={quote(t(lang,'login_failed'))}", status_code=303)

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = user
    resp = RedirectResponse(url=(next or "/account-details"), status_code=303)
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


@app.get("/account-details", response_class=HTMLResponse)
def account_details(
    request: Request,
    sort_by: str | None = None,
    security: str | None = None,
) -> HTMLResponse:
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    preferred_sort = sort_by if sort_by is not None else request.cookies.get(ACCOUNT_SORT_COOKIE)
    preferred_security = security if security is not None else request.cookies.get(ACCOUNT_SECURITY_COOKIE)
    configured_symbols = _normalized_configured_symbols()
    effective_sort, selected_security, _ = resolve_account_sort(
        configured_symbols,
        preferred_sort or ACCOUNT_SORT_NUMERIC_ID,
        preferred_security,
    )
    account_content = _render_account_details_content(lang, effective_sort, selected_security)
    inner = render_live_account_details_page(account_content, lang=lang, symbols=configured_symbols)
    response = HTMLResponse(render_layout(lang, "account-details", inner, current_user=(user or ""), can_convert_currency=_can_convert_currency(user), can_manage_trading=_can_manage_trading(user)))
    secure_cookie = bool(APP_CONFIG.get("server", {}).get("ssl_enabled", False))
    response.set_cookie(
        ACCOUNT_SORT_COOKIE,
        effective_sort,
        max_age=ACCOUNT_SORT_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure_cookie,
    )
    response.set_cookie(
        ACCOUNT_SECURITY_COOKIE,
        selected_security,
        max_age=ACCOUNT_SORT_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure_cookie,
    )
    return response


@app.get("/control-panel", response_class=HTMLResponse)
def control_panel(request: Request, ok: str | None = None, err: str | None = None) -> HTMLResponse:
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    inner = render_control_panel_page(
        lang,
        ACCOUNT_METAS,
        _normalized_configured_symbols(),
        error=(err or ""),
        ok=(ok or ""),
    )
    return HTMLResponse(render_layout(lang, "control-panel", inner, current_user=(user or ""), can_convert_currency=_can_convert_currency(user), can_manage_trading=_can_manage_trading(user)))


@app.get("/currency-conversion", response_class=HTMLResponse)
def currency_conversion(request: Request, ok: str | None = None, err: str | None = None) -> HTMLResponse:
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    if not _can_convert_currency(user):
        return RedirectResponse(url=f"/control-panel?err={quote(t(lang, 'conversion_forbidden'))}", status_code=303)
    inner = render_currency_conversion_page(lang, ACCOUNT_METAS, error=(err or ""), ok=(ok or ""))
    return HTMLResponse(render_layout(
        lang,
        "currency-conversion",
        inner,
        current_user=(user or ""),
        can_convert_currency=True,
        can_manage_trading=True,
    ))


@app.get("/trading-status", response_class=HTMLResponse)
def trading_status(request: Request, ok: str | None = None, err: str | None = None) -> HTMLResponse:
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    if not _can_manage_trading(user):
        return RedirectResponse(url=f"/control-panel?err={quote(t(lang, 'trading_status_forbidden'))}", status_code=303)
    accounts, _src = get_served_snapshots()
    inner = render_trading_status_page(lang, accounts, ACCOUNT_METAS, error=(err or ""), ok=(ok or ""))
    return HTMLResponse(render_layout(
        lang,
        "trading-status",
        inner,
        current_user=(user or ""),
        can_convert_currency=True,
        can_manage_trading=True,
    ))


# ---------------------------
# Market order submit
# ---------------------------

@app.post("/submit-order")
def submit_order(
    request: Request,
    account_id: str = Form(...),
    symbol: str = Form(...),
    side: str = Form("BUY"),
    shares: str | None = Form(None),
    dollar_amount: str | None = Form(None),
):
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)

    cmd, err = validate_order_inputs(
        account_id=account_id,
        symbol=symbol,
        side=side,
        shares_raw=shares,
        dollars_raw=dollar_amount,
        account_metas=ACCOUNT_METAS,
        invalid_account=t(lang, "invalid_account"),
        invalid_symbol=t(lang, "invalid_symbol"),
        invalid_side=t(lang, "invalid_side"),
        both_shares_and_dollars=t(lang, "both_shares_and_dollars"),
        neither_shares_nor_dollars=t(lang, "neither_shares_nor_dollars"),
        shares_positive=t(lang, "shares_positive"),
        dollars_positive=t(lang, "dollars_positive"),
    )

    if err:
        # Redirect with error message (URL-encoded)
        return RedirectResponse(url=f"/control-panel?err={quote(err)}", status_code=303)

    try:
        assert COMMANDS_PRODUCER is not None and cmd is not None
        _publish_control_command(cmd, key=cmd.get("account_id", "MARKET_ORDER"))
        ok = f"{t(lang,'published_cmd')}={cmd['command_id']}"
        return RedirectResponse(url=f"/control-panel?ok={quote(ok)}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/control-panel?err={quote(str(e))}", status_code=303)


@app.post("/submit-quick-order")
async def submit_quick_order(request: Request):
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    form = await request.form()

    account_ids = [str(x) for x in form.getlist("account_ids")]
    symbol = str(form.get("symbol", "")).strip().upper()
    side = str(form.get("side", "BUY"))
    dollars_raw = str(form.get("dollar_amount", "") or "").strip()
    if not dollars_raw:
        dollars_raw = "10000"

    market_last = None
    quote_row = MD_STORE.get_for_symbols([symbol]).get(symbol)
    if quote_row is not None and quote_row.error is None:
        market_last = quote_row.last

    if not account_ids:
        return RedirectResponse(url=f"/control-panel?err={quote(t(lang, 'quick_pick_account'))}", status_code=303)

    published_count = 0
    first_cmd_id = None
    for account_id in account_ids:
        cmd, err = validate_quick_order_inputs(
            account_id=account_id,
            symbol=symbol,
            side=side,
            dollars_raw=dollars_raw,
            market_last=market_last,
            account_metas=ACCOUNT_METAS,
            invalid_account=t(lang, "invalid_account"),
            invalid_symbol=t(lang, "invalid_symbol"),
            invalid_side=t(lang, "invalid_side"),
            dollars_positive=t(lang, "dollars_positive"),
            no_last_price=t(lang, "quick_no_last_price"),
            dollars_too_low=t(lang, "quick_dollars_too_low"),
        )
        if err:
            return RedirectResponse(url=f"/control-panel?err={quote(err)}", status_code=303)

        try:
            assert COMMANDS_PRODUCER is not None and cmd is not None
            _publish_control_command(cmd, key=cmd.get("account_id", "MARKET_ORDER"))
            published_count += 1
            if first_cmd_id is None:
                first_cmd_id = str(cmd.get("command_id", ""))
        except Exception as e:
            return RedirectResponse(url=f"/control-panel?err={quote(str(e))}", status_code=303)

    ok = f"{t(lang,'quick_submitted')} {published_count}. {t(lang,'published_cmd')}={first_cmd_id or ''}"
    return RedirectResponse(url=f"/control-panel?ok={quote(ok)}", status_code=303)


@app.post("/submit-limit-order")
async def submit_limit_order(request: Request):
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    form = await request.form()

    account_ids = [str(x) for x in form.getlist("account_ids")]
    symbol = str(form.get("symbol", "")).strip().upper()
    side = str(form.get("side", "BUY"))
    shares_raw = str(form.get("shares", "") or "").strip()
    limit_price_raw = str(form.get("limit_price", "") or "").strip()
    through_market_pct_raw = str(form.get("through_market_pct", "") or "").strip()

    market_last = None
    if not limit_price_raw:
        quote_row = MD_STORE.get_for_symbols([symbol]).get(symbol)
        if quote_row is not None and quote_row.error is None:
            market_last = quote_row.last

    if not account_ids:
        return RedirectResponse(url=f"/control-panel?err={quote(t(lang, 'quick_pick_account'))}", status_code=303)

    published_count = 0
    first_cmd_id = None
    for account_id in account_ids:
        cmd, err = validate_limit_order_inputs(
            account_id=account_id,
            symbol=symbol,
            side=side,
            shares_raw=shares_raw,
            limit_price_raw=limit_price_raw,
            through_market_pct_raw=through_market_pct_raw,
            market_last=market_last,
            account_metas=ACCOUNT_METAS,
            invalid_account=t(lang, "invalid_account"),
            invalid_symbol=t(lang, "invalid_symbol"),
            invalid_side=t(lang, "invalid_side"),
            shares_positive=t(lang, "shares_positive"),
            price_positive=t(lang, "price_positive"),
            no_last_price=t(lang, "no_last_price"),
        )
        if err:
            return RedirectResponse(url=f"/control-panel?err={quote(err)}", status_code=303)

        try:
            assert COMMANDS_PRODUCER is not None and cmd is not None
            _publish_control_command(cmd, key=cmd.get("account_id", "LIMIT_ORDER"))
            published_count += 1
            if first_cmd_id is None:
                first_cmd_id = str(cmd.get("command_id", ""))
        except Exception as e:
            return RedirectResponse(url=f"/control-panel?err={quote(str(e))}", status_code=303)

    ok = f"{t(lang,'limit_submitted')} {published_count}. {t(lang,'published_cmd')}={first_cmd_id or ''}"
    return RedirectResponse(url=f"/control-panel?ok={quote(ok)}", status_code=303)


@app.post("/submit-cancel-open-orders")
async def submit_cancel_open_orders(request: Request):
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    form = await request.form()
    raw_account_ids = [str(x).strip() for x in form.getlist("account_ids")]
    symbol = str(form.get("symbol", "") or "").strip().upper()

    if "__ALL__" in raw_account_ids:
        account_ids = sorted(ACCOUNT_METAS.keys(), key=lambda aid: ACCOUNT_METAS[aid].num_id)
    else:
        account_ids = []
        seen = set()
        for account_id in raw_account_ids:
            if not account_id or account_id in seen:
                continue
            seen.add(account_id)
            account_ids.append(account_id)

    if not account_ids:
        return RedirectResponse(url=f"/control-panel?err={quote(t(lang, 'quick_pick_account'))}", status_code=303)

    published_count = 0
    first_cmd_id = None
    for account_id in account_ids:
        cmd, err = validate_cancel_open_orders_inputs(
            account_id=account_id,
            symbol=symbol or None,
            account_metas=ACCOUNT_METAS,
            invalid_account=t(lang, "invalid_account"),
        )
        if err:
            return RedirectResponse(url=f"/control-panel?err={quote(err)}", status_code=303)

        try:
            assert COMMANDS_PRODUCER is not None and cmd is not None
            _publish_control_command(cmd, key=cmd.get("account_id", "CANCEL_OPEN_ORDERS"))
            published_count += 1
            if first_cmd_id is None:
                first_cmd_id = str(cmd.get("command_id", ""))
        except Exception as e:
            return RedirectResponse(url=f"/control-panel?err={quote(str(e))}", status_code=303)

    ok = f"{t(lang,'cancel_orders_submitted')} {published_count}. {t(lang,'published_cmd')}={first_cmd_id or ''}"
    return RedirectResponse(url=f"/control-panel?ok={quote(ok)}", status_code=303)


@app.post("/submit-currency-conversion")
def submit_currency_conversion(
    request: Request,
    account_id: str = Form(...),
    source_currency: str = Form(...),
    target_currency: str = Form(...),
    source_amount: str = Form(...),
):
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    if not _can_convert_currency(user):
        return RedirectResponse(url=f"/control-panel?err={quote(t(lang, 'conversion_forbidden'))}", status_code=303)

    cmd, err = validate_currency_conversion_inputs(
        account_id=account_id,
        source_currency_raw=source_currency,
        target_currency_raw=target_currency,
        source_amount_raw=source_amount,
        account_metas=ACCOUNT_METAS,
        invalid_account=t(lang, "invalid_account"),
        invalid_currency=t(lang, "invalid_currency_pair"),
        same_currency=t(lang, "same_currency"),
        amount_positive=t(lang, "conversion_amount_positive"),
    )

    if err:
        return RedirectResponse(url=f"/currency-conversion?err={quote(err)}", status_code=303)

    try:
        assert COMMANDS_PRODUCER is not None and cmd is not None
        _publish_control_command(cmd, key=cmd.get("account_id", "CURRENCY_CONVERSION"))
        ok = f"{t(lang,'conversion_submitted')} {t(lang,'published_cmd')}={cmd['command_id']}"
        return RedirectResponse(url=f"/currency-conversion?ok={quote(ok)}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/currency-conversion?err={quote(str(e))}", status_code=303)


@app.post("/submit-trading-status")
async def submit_trading_status(request: Request):
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    if not _can_manage_trading(user):
        return RedirectResponse(url=f"/control-panel?err={quote(t(lang, 'trading_status_forbidden'))}", status_code=303)

    form = await request.form()
    disable_all = str(form.get("disable_all", "")).strip().lower() in ("1", "true", "yes", "on")
    trading_enabled_raw = str(form.get("trading_enabled", ""))
    account_ids = list(ACCOUNT_METAS.keys()) if disable_all else [str(form.get("account_id", ""))]

    published_count = 0
    first_cmd_id = None
    for account_id in account_ids:
        cmd, err = validate_trading_status_inputs(
            account_id=account_id,
            trading_enabled_raw=trading_enabled_raw,
            account_metas=ACCOUNT_METAS,
            invalid_account=t(lang, "invalid_account"),
            invalid_status=t(lang, "invalid_trading_status"),
        )
        if err:
            return RedirectResponse(url=f"/trading-status?err={quote(err)}", status_code=303)
        try:
            assert COMMANDS_PRODUCER is not None and cmd is not None
            _publish_control_command(cmd, key=cmd.get("account_id", "SET_TRADING_ENABLED"))
            published_count += 1
            if first_cmd_id is None:
                first_cmd_id = str(cmd.get("command_id", ""))
        except Exception as e:
            return RedirectResponse(url=f"/trading-status?err={quote(str(e))}", status_code=303)

    ok_key = "trading_disabled_all" if disable_all else "trading_status_submitted"
    ok = f"{t(lang, ok_key)} {published_count}. {t(lang,'published_cmd')}={first_cmd_id or ''}"
    return RedirectResponse(url=f"/trading-status?ok={quote(ok)}", status_code=303)


# ---------------------------
# Algo trading submit (NEW)
# ---------------------------

@app.post("/submit-algo")
def submit_algo(
    request: Request,
    trading_mode: str = Form(...),
    symbol: str = Form(...),
    fast_trading_config: str | None = Form(None),
):
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)

    cmd, err = validate_algo_start_inputs(
        trading_mode=trading_mode,
        symbol=symbol,
        fast_trading_config_raw=fast_trading_config,
        account_metas=ACCOUNT_METAS,
        invalid_mode=t(lang, "invalid_mode"),
        invalid_symbol=t(lang, "invalid_symbol"),
        invalid_account=t(lang, "invalid_account"),
        fast_config_required=t(lang, "fast_config_required"),
        fast_price_limit_positive=t(lang, "fast_price_limit_positive"),
        fast_accounts_required=t(lang, "fast_accounts_required"),
        fast_account_duplicate=t(lang, "fast_account_duplicate"),
        fast_book_levels_invalid=t(lang, "fast_book_levels_invalid"),
        fast_target_percentage_invalid=t(lang, "fast_target_percentage_invalid"),
        fast_cycle_seconds_invalid=t(lang, "fast_cycle_seconds_invalid"),
    )

    if err:
        return RedirectResponse(url=f"/control-panel?err={quote(err)}", status_code=303)

    try:
        assert COMMANDS_PRODUCER is not None and cmd is not None
        assert FAST_TRADING_MANAGER is not None
        strategy_key = _start_fast_strategy(cmd)
        ok = f"{t(lang,'algo_submitted')} {strategy_key}. {t(lang,'published_cmd')}={cmd['command_id']}"
        return RedirectResponse(url=f"/control-panel?ok={quote(ok)}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/control-panel?err={quote(str(e))}", status_code=303)


# ---------------------------
# APIs
# ---------------------------

@app.get("/api/account-details/stream")
def api_account_details_stream(request: Request):
    _, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    effective_sort, selected_security, _ = resolve_account_sort(
        _normalized_configured_symbols(),
        request.cookies.get(ACCOUNT_SORT_COOKIE) or ACCOUNT_SORT_NUMERIC_ID,
        request.cookies.get(ACCOUNT_SECURITY_COOKIE),
    )
    return StreamingResponse(
        _account_details_event_stream(lang, effective_sort, selected_security, request=request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/account-orders/quick-market")
async def api_submit_account_quick_order(request: Request) -> JSONResponse:
    _, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    form = await request.form()

    account_ids = [str(value) for value in form.getlist("account_id")]
    if len(account_ids) != 1:
        return JSONResponse(
            {"ok": False, "error": t(lang, "invalid_account")},
            status_code=400,
        )

    account_id = account_ids[0]
    symbol = str(form.get("symbol", "")).strip().upper()
    side = str(form.get("side", "BUY"))
    shares_raw = str(form.get("shares", "") or "").strip()

    try:
        cmd, err = validate_order_inputs(
            account_id=account_id,
            symbol=symbol,
            side=side,
            shares_raw=shares_raw,
            dollars_raw=None,
            account_metas=ACCOUNT_METAS,
            invalid_account=t(lang, "invalid_account"),
            invalid_symbol=t(lang, "invalid_symbol"),
            invalid_side=t(lang, "invalid_side"),
            both_shares_and_dollars=t(lang, "shares_positive"),
            neither_shares_nor_dollars=t(lang, "shares_positive"),
            shares_positive=t(lang, "shares_positive"),
            dollars_positive=t(lang, "dollars_positive"),
        )
    except (TypeError, ValueError, OverflowError):
        cmd, err = None, t(lang, "shares_positive")

    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if COMMANDS_PRODUCER is None or cmd is None:
        return JSONResponse(
            {"ok": False, "error": t(lang, "publish_failed")},
            status_code=503,
        )

    try:
        _publish_control_command(cmd, key=account_id)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc) or t(lang, "publish_failed")},
            status_code=500,
        )

    command_id = str(cmd.get("command_id", ""))
    message = f"{t(lang, 'order_submitted')}. {t(lang, 'published_cmd')}={command_id}"
    return JSONResponse(
        {"ok": True, "message": message, "command_id": command_id},
        status_code=200,
    )


@app.post("/api/account-orders/limit")
async def api_submit_account_limit_order(request: Request) -> JSONResponse:
    _, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    form = await request.form()

    account_ids = [str(value) for value in form.getlist("account_id")]
    if len(account_ids) != 1:
        return JSONResponse(
            {"ok": False, "error": t(lang, "invalid_account")},
            status_code=400,
        )

    account_id = account_ids[0]
    symbol = str(form.get("symbol", "")).strip().upper()
    side = str(form.get("side", "BUY"))
    shares_raw = str(form.get("shares", "") or "").strip()
    limit_price_raw = str(form.get("limit_price", "") or "").strip()
    through_market_pct_raw = str(form.get("through_market_pct", "") or "").strip()

    market_last = None
    if not limit_price_raw:
        quote_row = MD_STORE.get_for_symbols([symbol]).get(symbol)
        if quote_row is not None and quote_row.error is None:
            market_last = quote_row.last

    try:
        cmd, err = validate_limit_order_inputs(
            account_id=account_id,
            symbol=symbol,
            side=side,
            shares_raw=shares_raw,
            limit_price_raw=limit_price_raw,
            through_market_pct_raw=through_market_pct_raw,
            market_last=market_last,
            account_metas=ACCOUNT_METAS,
            invalid_account=t(lang, "invalid_account"),
            invalid_symbol=t(lang, "invalid_symbol"),
            invalid_side=t(lang, "invalid_side"),
            shares_positive=t(lang, "shares_positive"),
            price_positive=t(lang, "price_positive"),
            no_last_price=t(lang, "no_last_price"),
        )
    except (TypeError, ValueError, OverflowError):
        cmd, err = None, t(lang, "price_positive")

    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if COMMANDS_PRODUCER is None or cmd is None:
        return JSONResponse(
            {"ok": False, "error": t(lang, "publish_failed")},
            status_code=503,
        )

    try:
        _publish_control_command(cmd, key=account_id)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc) or t(lang, "publish_failed")},
            status_code=500,
        )

    command_id = str(cmd.get("command_id", ""))
    message = f"{t(lang, 'order_submitted')}. {t(lang, 'published_cmd')}={command_id}"
    return JSONResponse(
        {"ok": True, "message": message, "command_id": command_id},
        status_code=200,
    )


@app.get("/api/accounts")
def api_accounts(request: Request) -> JSONResponse:
    user, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    accounts, src = get_served_snapshots()

    def account_num(acct):
        meta = ACCOUNT_METAS.get(acct.account_id)
        if meta:
            return meta.num_id
        if acct.account_num_id is not None:
            return acct.account_num_id
        return 999

    sorted_accounts = sorted(
        accounts.values(),
        key=lambda x: (account_num(x), x.account_id),
    )
    return JSONResponse(
        {
            "source": src,
            "lang": lang,
            "accounts": [
                {
                    **a.to_dict(),
                    "account_num_id": (account_num(a) if account_num(a) != 999 else None),
                }
                for a in sorted_accounts
            ],
        }
    )


@app.post("/api/submit-order")
async def api_submit_order(request: Request) -> JSONResponse:
    user, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    body = await request.json()

    account_id = str(body.get("account_id", ""))
    symbol = str(body.get("symbol", ""))
    side = str(body.get("side", "BUY"))

    shares_raw = None if body.get("shares") is None else str(body.get("shares"))
    dollars_raw = None if body.get("dollar_amount") is None else str(body.get("dollar_amount"))

    cmd, err = validate_order_inputs(
        account_id=account_id,
        symbol=symbol,
        side=side,
        shares_raw=shares_raw,
        dollars_raw=dollars_raw,
        account_metas=ACCOUNT_METAS,
        invalid_account=t(lang, "invalid_account"),
        invalid_symbol=t(lang, "invalid_symbol"),
        invalid_side=t(lang, "invalid_side"),
        both_shares_and_dollars=t(lang, "both_shares_and_dollars"),
        neither_shares_nor_dollars=t(lang, "neither_shares_nor_dollars"),
        shares_positive=t(lang, "shares_positive"),
        dollars_positive=t(lang, "dollars_positive"),
    )

    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    try:
        assert COMMANDS_PRODUCER is not None
        assert cmd is not None
        _publish_control_command(cmd, key=cmd.get("account_id", "MARKET_ORDER"))
        return JSONResponse({"ok": True, "command": cmd})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/submit-algo")
async def api_submit_algo(request: Request) -> JSONResponse:
    user, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    body = await request.json()

    trading_mode = str(body.get("trading_mode", ""))
    symbol = str(body.get("symbol", ""))

    fast_trading_config_raw = _extract_fast_trading_config(body)

    cmd, err = validate_algo_start_inputs(
        trading_mode=trading_mode,
        symbol=symbol,
        fast_trading_config_raw=fast_trading_config_raw,
        account_metas=ACCOUNT_METAS,
        invalid_mode=t(lang, "invalid_mode"),
        invalid_symbol=t(lang, "invalid_symbol"),
        invalid_account=t(lang, "invalid_account"),
        fast_config_required=t(lang, "fast_config_required"),
        fast_price_limit_positive=t(lang, "fast_price_limit_positive"),
        fast_accounts_required=t(lang, "fast_accounts_required"),
        fast_account_duplicate=t(lang, "fast_account_duplicate"),
        fast_book_levels_invalid=t(lang, "fast_book_levels_invalid"),
        fast_target_percentage_invalid=t(lang, "fast_target_percentage_invalid"),
        fast_cycle_seconds_invalid=t(lang, "fast_cycle_seconds_invalid"),
    )

    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    try:
        assert COMMANDS_PRODUCER is not None
        assert cmd is not None
        assert FAST_TRADING_MANAGER is not None
        strategy_key = _start_fast_strategy(cmd)
        return JSONResponse({"ok": True, "command": cmd, "strategy_key": strategy_key})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/smart-selling")
def api_smart_selling_status(request: Request):
    _, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    return JSONResponse(
        {
            "ok": True,
            "parents": SMART_SELLING_MANAGER.list_parents() if SMART_SELLING_MANAGER else [],
            "available": SMART_SELLING_MANAGER is not None,
        }
    )


def _smart_origin_allowed(request):
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    return not origin or origin.rstrip("/") == expected


@app.post("/api/smart-selling")
async def api_start_smart_selling(request: Request):
    _, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    if not _smart_origin_allowed(request):
        return JSONResponse(
            {"ok": False, "error": "Cross-origin submission is not allowed"}, status_code=403
        )
    if SMART_SELLING_MANAGER is None:
        return JSONResponse({"ok": False, "error": "Smart selling is unavailable"}, status_code=503)
    try:
        payload = await request.json()
        parent = _start_smart_parent(payload)
        return JSONResponse({"ok": True, "parent": parent})
    except (ValueError, TypeError, InvalidOperation) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/smart-selling/{parent_id}/stop")
def api_stop_smart_selling(parent_id: str, request: Request):
    _, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    if not _smart_origin_allowed(request):
        return JSONResponse(
            {"ok": False, "error": "Cross-origin submission is not allowed"}, status_code=403
        )
    if SMART_SELLING_MANAGER is None:
        return JSONResponse({"ok": False, "error": "Smart selling is unavailable"}, status_code=503)
    try:
        with ALGO_CONTROL_LOCK:
            return JSONResponse({"ok": True, "parent": SMART_SELLING_MANAGER.stop_parent(parent_id)})
    except KeyError:
        return JSONResponse({"ok": False, "error": "Unknown Smart selling parent"}, status_code=404)


@app.post("/submit-algo-stop")
def submit_algo_stop(
    request: Request,
    reason: str | None = Form(None),
):
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    # Scope comes from the server, never from submitted mode/account fields.
    # Stop local strategies first even if Kafka is unavailable.
    # Keep new starts behind the stop notification as well as the local stop.
    with ALGO_CONTROL_LOCK:
        try:
            if FAST_TRADING_MANAGER is None and SMART_SELLING_MANAGER is None:
                raise RuntimeError(t(lang, "algo_stop_unavailable"))
            stopped_count = FAST_TRADING_MANAGER.stop_all() if FAST_TRADING_MANAGER else 0
            smart_count = SMART_SELLING_MANAGER.stop_all() if SMART_SELLING_MANAGER else 0
        except Exception as e:
            return RedirectResponse(url=f"/control-panel?err={quote(str(e))}", status_code=303)

        ok = t(lang, "algo_stop_submitted").format(count=stopped_count)
        if smart_count:
            ok += (
                f" 已请求停止 {smart_count} 个智能卖出任务，等待订单终态确认。"
                if lang == "zh"
                else f" Stop requested for {smart_count} Smart selling parent(s); awaiting final order confirmations."
            )
        cmd = build_algo_stop_command(reason=reason)
        try:
            if COMMANDS_PRODUCER is None:
                raise RuntimeError(t(lang, "algo_stop_notification_failed"))
            _publish_control_command(cmd, key="ALGO_STOP")
        except Exception:
            LOGGER.exception("All local algorithm strategies stopped, but stop notification failed")
            err = t(lang, "algo_stop_notification_failed")
            return RedirectResponse(
                url=f"/control-panel?ok={quote(ok)}&err={quote(err)}", status_code=303
            )
        ok += f" {t(lang,'published_cmd')}={cmd['command_id']}"
        return RedirectResponse(url=f"/control-panel?ok={quote(ok)}", status_code=303)


@app.get("/submit-algo")
def submit_algo_get() -> RedirectResponse:
    # If browser submits GET or user navigates here, avoid 405
    return RedirectResponse(url="/control-panel", status_code=302)


@app.get("/submit-order")
def submit_order_get() -> RedirectResponse:
    return RedirectResponse(url="/control-panel", status_code=302)


@app.get("/submit-quick-order")
def submit_quick_order_get() -> RedirectResponse:
    return RedirectResponse(url="/control-panel", status_code=302)


@app.get("/submit-limit-order")
def submit_limit_order_get() -> RedirectResponse:
    return RedirectResponse(url="/control-panel", status_code=302)


@app.get("/submit-cancel-open-orders")
def submit_cancel_open_orders_get() -> RedirectResponse:
    return RedirectResponse(url="/control-panel", status_code=302)


@app.get("/submit-currency-conversion")
def submit_currency_conversion_get() -> RedirectResponse:
    return RedirectResponse(url="/currency-conversion", status_code=302)


@app.get("/submit-trading-status")
def submit_trading_status_get() -> RedirectResponse:
    return RedirectResponse(url="/trading-status", status_code=302)


@app.get("/submit-algo-stop")
def submit_algo_stop_get():
    return RedirectResponse(url="/control-panel", status_code=302)


@app.get("/market-data", response_class=HTMLResponse)
def market_data(request: Request) -> HTMLResponse:
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    inner = render_market_data_page(lang=lang, symbols=_normalized_configured_symbols())
    return HTMLResponse(render_layout(lang, "market-data", inner, current_user=(user or ""), can_convert_currency=_can_convert_currency(user), can_manage_trading=_can_manage_trading(user)))


@app.get("/market-insights", response_class=HTMLResponse)
def market_insights(request: Request) -> HTMLResponse:
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    max_levels = int(APP_CONFIG.get("market_data", {}).get("market_insights_max_levels", 20))
    inner = render_market_insights_page(
        lang=lang,
        symbols=_market_insights_symbols(),
        max_levels=max_levels,
    )
    return HTMLResponse(render_layout(lang, "market-insights", inner, current_user=(user or ""), can_convert_currency=_can_convert_currency(user), can_manage_trading=_can_manage_trading(user)))


@app.get("/api/market-data")
async def api_market_data(request: Request, symbol: str | None = None) -> JSONResponse:
    user, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    clean_symbol = str(symbol or "").strip().upper()
    requested_symbols = [clean_symbol] if clean_symbol else _normalized_configured_symbols()
    rows = MD_STORE.get_for_symbols(requested_symbols)
    enriched_rows: Dict[str, QuoteRow] = {}
    for symbol, row in rows.items():
        prev_close = HIST_CLOSE_STORE.get_prev_close(symbol)

        change = None
        change_pct = None
        if prev_close is not None and row.last is not None:
            change = row.last - prev_close
            if prev_close != 0:
                change_pct = (change / prev_close) * 100.0

        enriched_rows[symbol] = QuoteRow(
            symbol=row.symbol,
            prev_close=prev_close,
            last=row.last,
            change=change,
            change_pct=change_pct,
            short_interest=row.short_interest,
            volume=row.volume,
            asof_epoch=row.asof_epoch,
            error=row.error,
        )

    return JSONResponse(
        {
            "rows": {
                k: {
                    "symbol": v.symbol,
                    "prev_close": v.prev_close,
                    "last": v.last,
                    "change": v.change,
                    "change_pct": v.change_pct,
                    "short_interest": v.short_interest,
                    "volume": v.volume,
                    "asof_epoch": v.asof_epoch,
                    "error": v.error,
                }
                for k, v in enriched_rows.items()
            }
        }
    )


@app.get("/api/market-insights")
async def api_market_insights(request: Request, symbol: str, depth: int = 20) -> JSONResponse:
    user, gate = _require_auth_api(request)
    if gate is not None:
        return gate

    clean_symbol = str(symbol).strip().upper()
    if not clean_symbol:
        return JSONResponse({"ok": False, "error": t(resolve_lang(request), "invalid_symbol")}, status_code=400)

    max_levels = int(APP_CONFIG.get("market_data", {}).get("market_insights_max_levels", 20))
    requested_depth = max(1, min(int(depth), max_levels))
    book = MD_STORE.get_book(clean_symbol, requested_depth)

    return JSONResponse(
        {
            "symbol": book.symbol,
            "symbols": _market_insights_symbols(),
            "depth_limit": book.depth_limit,
            "asof_epoch": book.asof_epoch,
            "error": book.error,
            "bids": [
                {
                    "price": level.price,
                    "quantity": level.quantity,
                    "order_count": level.order_count,
                }
                for level in book.bids
            ],
            "asks": [
                {
                    "price": level.price,
                    "quantity": level.quantity,
                    "order_count": level.order_count,
                }
                for level in book.asks
            ],
        }
    )


if __name__ == "__main__":
    import uvicorn

    config_path = os.getenv("ACCOUNT_DASHBOARD_CONFIG", "./trading_ui/sample/config.json")
    cfg = load_config(config_path)
    server_cfg = cfg.get("server", {})

    uvicorn_kwargs: Dict = {
        "host": str(server_cfg.get("host", "0.0.0.0")),
        "port": int(server_cfg.get("port", 8000)),
        "log_config": None,
    }
    if bool(server_cfg.get("ssl_enabled", False)):
        uvicorn_kwargs["ssl_certfile"] = str(server_cfg.get("ssl_certfile", ""))
        uvicorn_kwargs["ssl_keyfile"] = str(server_cfg.get("ssl_keyfile", ""))
        LOGGER.info("https enabled cert=%s", uvicorn_kwargs["ssl_certfile"])
    else:
        LOGGER.info("https disabled (HTTP)")

    uvicorn.run("trading_ui.webserver:app", **uvicorn_kwargs)
