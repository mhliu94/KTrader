import os
import secrets
from hmac import compare_digest
from typing import Dict, Tuple

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from urllib.parse import quote
from .services.market_data import MarketDataStore, HistoricalCloseStore, QuoteRow
from .templates import render_market_data_page, render_market_insights_page
from .models import AccountSnapshot

from .config import load_config, load_account_metas, load_symbols
from .i18n import resolve_lang, SUPPORTED_LANGS, t
from .store import AccountStore
from .templates import (
    render_layout,
    render_account_details_page,
    render_control_panel_page,
    render_currency_conversion_page,
    render_login_page,
    render_trading_status_page,
)
from .services.fallback import load_fallback_snapshots
from .services.orders import (
    validate_order_inputs,
    validate_delayed_order_inputs,
    validate_limit_order_inputs,
    validate_cancel_open_orders_inputs,
    validate_algo_start_inputs,
    validate_algo_stop_inputs,
    validate_currency_conversion_inputs,
    validate_trading_status_inputs,
)
from .kafka.consumer_account_details import AccountDetailsConsumer
from .kafka.consumer_market_data import PriceBookConsumer
from .kafka.producer_trading_commands import TradingCommandsProducer

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

MD_STORE = MarketDataStore()
HIST_CLOSE_STORE = HistoricalCloseStore("./market_data/historical_prices.csv")


def _filter_configured_snapshots(accounts: Dict[str, AccountSnapshot]) -> Dict[str, AccountSnapshot]:
    return {account_id: snap for account_id, snap in accounts.items() if account_id in ACCOUNT_METAS}


def get_served_snapshots() -> Tuple[Dict, str]:
    if store.kafka_seen_any():
        return _filter_configured_snapshots(store.get_all()), "kafka"
    fallback_path = APP_CONFIG["fallback"]["file"]
    snaps = load_fallback_snapshots(fallback_path)
    return _filter_configured_snapshots(snaps), f"fallback_file:{fallback_path}"


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
    global APP_CONFIG, ACCOUNT_METAS, SYMBOLS, AUTH_USERS, ACCOUNT_CONSUMER, MARKET_DATA_CONSUMER, COMMANDS_PRODUCER, HIST_CLOSE_STORE

    config_path = os.getenv("ACCOUNT_DASHBOARD_CONFIG", "./trading_ui/sample/config.json")
    APP_CONFIG = load_config(config_path)
    ACCOUNT_METAS = load_account_metas(APP_CONFIG)
    SYMBOLS = load_symbols(APP_CONFIG)
    AUTH_USERS = APP_CONFIG["auth"]["users"]
    HIST_CLOSE_STORE = HistoricalCloseStore(APP_CONFIG["market_data"]["historical_prices_csv"])
    print(f"[market-data] historical_prices_csv='{APP_CONFIG['market_data']['historical_prices_csv']}'")

    ACCOUNT_CONSUMER = AccountDetailsConsumer(APP_CONFIG["kafka"], store, ACCOUNT_METAS)
    ACCOUNT_CONSUMER.start()

    MARKET_DATA_CONSUMER = PriceBookConsumer(APP_CONFIG["kafka"], MD_STORE)
    MARKET_DATA_CONSUMER.start()

    COMMANDS_PRODUCER = TradingCommandsProducer(APP_CONFIG["kafka"])


@app.on_event("shutdown")
def on_shutdown() -> None:
    if ACCOUNT_CONSUMER:
        ACCOUNT_CONSUMER.stop()
    if MARKET_DATA_CONSUMER:
        MARKET_DATA_CONSUMER.stop()
    if COMMANDS_PRODUCER:
        COMMANDS_PRODUCER.flush(2.0)


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
def account_details(request: Request) -> HTMLResponse:
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    accounts, src = get_served_snapshots()
    inner = render_account_details_page(
        lang=lang,
        store=store,
        accounts=accounts,
        source_label=src,
        account_metas=ACCOUNT_METAS,
        account_details_topic=APP_CONFIG["kafka"]["account_details_topic"],
    )
    return HTMLResponse(render_layout(lang, "account-details", inner, current_user=(user or ""), can_convert_currency=_can_convert_currency(user), can_manage_trading=_can_manage_trading(user)))


@app.get("/control-panel", response_class=HTMLResponse)
def control_panel(request: Request, ok: str | None = None, err: str | None = None) -> HTMLResponse:
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    inner = render_control_panel_page(lang, ACCOUNT_METAS, SYMBOLS, error=(err or ""), ok=(ok or ""))
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
        symbols=SYMBOLS,
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
        COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("account_id", "MARKET_ORDER"))
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
    symbol = str(form.get("symbol", ""))
    side = str(form.get("side", "BUY"))
    dollars_raw = str(form.get("dollar_amount", "") or "").strip()
    if not dollars_raw:
        dollars_raw = "10000"

    if not account_ids:
        return RedirectResponse(url=f"/control-panel?err={quote(t(lang, 'quick_pick_account'))}", status_code=303)

    published_count = 0
    first_cmd_id = None
    for account_id in account_ids:
        cmd, err = validate_order_inputs(
            account_id=account_id,
            symbol=symbol,
            side=side,
            shares_raw=None,
            dollars_raw=dollars_raw,
            account_metas=ACCOUNT_METAS,
            symbols=SYMBOLS,
            invalid_account=t(lang, "invalid_account"),
            invalid_symbol=t(lang, "invalid_symbol"),
            invalid_side=t(lang, "invalid_side"),
            both_shares_and_dollars=t(lang, "both_shares_and_dollars"),
            neither_shares_nor_dollars=t(lang, "neither_shares_nor_dollars"),
            shares_positive=t(lang, "shares_positive"),
            dollars_positive=t(lang, "dollars_positive"),
        )
        if err:
            return RedirectResponse(url=f"/control-panel?err={quote(err)}", status_code=303)

        try:
            assert COMMANDS_PRODUCER is not None and cmd is not None
            COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("account_id", "MARKET_ORDER"))
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
    symbol = str(form.get("symbol", ""))
    side = str(form.get("side", "BUY"))
    shares_raw = str(form.get("shares", "") or "").strip()
    limit_price_raw = str(form.get("limit_price", "") or "").strip()
    through_market_pct_raw = str(form.get("through_market_pct", "") or "").strip()

    market_last = None
    if not limit_price_raw:
        quote = MD_STORE.get_for_symbols([symbol]).get(symbol)
        if quote is not None and quote.error is None:
            market_last = quote.last

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
            symbols=SYMBOLS,
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
            COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("account_id", "LIMIT_ORDER"))
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
            symbols=SYMBOLS,
            invalid_account=t(lang, "invalid_account"),
            invalid_symbol=t(lang, "invalid_symbol"),
        )
        if err:
            return RedirectResponse(url=f"/control-panel?err={quote(err)}", status_code=303)

        try:
            assert COMMANDS_PRODUCER is not None and cmd is not None
            COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("account_id", "CANCEL_OPEN_ORDERS"))
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
        COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("account_id", "CURRENCY_CONVERSION"))
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
            COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("account_id", "SET_TRADING_ENABLED"))
            published_count += 1
            if first_cmd_id is None:
                first_cmd_id = str(cmd.get("command_id", ""))
        except Exception as e:
            return RedirectResponse(url=f"/trading-status?err={quote(str(e))}", status_code=303)

    ok_key = "trading_disabled_all" if disable_all else "trading_status_submitted"
    ok = f"{t(lang, ok_key)} {published_count}. {t(lang,'published_cmd')}={first_cmd_id or ''}"
    return RedirectResponse(url=f"/trading-status?ok={quote(ok)}", status_code=303)


@app.post("/submit-delayed-order")
def submit_delayed_order(
    request: Request,
    account_id: str = Form(...),
    symbol: str = Form(...),
    side: str = Form("BUY"),
    shares: str | None = Form(None),
    dollar_amount: str | None = Form(None),
    delay_choice: str = Form(...),
    execute_at: str | None = Form(None),
):
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)

    cmd, err = validate_delayed_order_inputs(
        account_id=account_id,
        symbol=symbol,
        side=side,
        shares_raw=shares,
        dollars_raw=dollar_amount,
        delay_choice_raw=delay_choice,
        execute_at_raw=execute_at,
        account_metas=ACCOUNT_METAS,
        symbols=SYMBOLS,
        invalid_account=t(lang, "invalid_account"),
        invalid_symbol=t(lang, "invalid_symbol"),
        invalid_side=t(lang, "invalid_side"),
        both_shares_and_dollars=t(lang, "both_shares_and_dollars"),
        neither_shares_nor_dollars=t(lang, "neither_shares_nor_dollars"),
        shares_positive=t(lang, "shares_positive"),
        dollars_positive=t(lang, "dollars_positive"),
        invalid_delay_choice=t(lang, "invalid_delay_choice"),
        future_time_required=t(lang, "future_time_required"),
        future_time_must_be_future=t(lang, "future_time_must_be_future"),
    )

    if err:
        return RedirectResponse(url=f"/control-panel?err={quote(err)}", status_code=303)

    try:
        assert COMMANDS_PRODUCER is not None and cmd is not None
        COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("account_id", "DELAYED_MARKET_ORDER"))
        ok = f"{t(lang,'published_cmd')}={cmd['command_id']}"
        return RedirectResponse(url=f"/control-panel?ok={quote(ok)}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/control-panel?err={quote(str(e))}", status_code=303)


# ---------------------------
# Algo trading submit (NEW)
# ---------------------------

@app.post("/submit-algo")
def submit_algo(
    request: Request,
    trading_mode: str = Form(...),
    symbol: str = Form(...),
    max_volume: str | None = Form("-1"),
    market_volume_target: str | None = Form("-1"),
    end_time_et: str | None = Form("2099-12-31T00:00"),
    abs_pos_change_limit: str | None = Form("-1"),
    price_target: str | None = Form(...),
    single_order_notional_limit: str | None = Form("-1"),
    order_rate_limit_per_minute: str | None = Form("-1"),
    fast_trading_config: str | None = Form(None),
):
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)

    cmd, err = validate_algo_start_inputs(
        trading_mode=trading_mode,
        symbol=symbol,
        max_volume_raw=max_volume,
        market_volume_target_raw=market_volume_target,
        end_time_et_raw=end_time_et,
        abs_pos_change_limit_raw=abs_pos_change_limit,
        price_target_raw=price_target,
        single_order_notional_limit_raw=single_order_notional_limit,
        order_rate_limit_per_minute_raw=order_rate_limit_per_minute,
        fast_trading_config_raw=fast_trading_config,
        symbols=SYMBOLS,
        account_metas=ACCOUNT_METAS,
        invalid_mode=t(lang, "invalid_mode"),
        invalid_symbol=t(lang, "invalid_symbol"),
        invalid_account=t(lang, "invalid_account"),
        number_required=t(lang, "number_required"),
        end_time_required=t(lang, "end_time_required"),
        fast_config_required=t(lang, "fast_config_required"),
        fast_group_required=t(lang, "fast_group_required"),
        fast_price_limit_positive=t(lang, "fast_price_limit_positive"),
        fast_group_accounts_required=t(lang, "fast_group_accounts_required"),
        fast_account_duplicate=t(lang, "fast_account_duplicate"),
        fast_allocation_positive=t(lang, "fast_allocation_positive"),
        fast_allocation_total=t(lang, "fast_allocation_total"),
    )

    if err:
        return RedirectResponse(url=f"/control-panel?err={quote(err)}", status_code=303)

    try:
        assert COMMANDS_PRODUCER is not None and cmd is not None
        COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("symbol", "ALGO"))
        ok = f"{t(lang,'published_cmd')}={cmd['command_id']}"
        return RedirectResponse(url=f"/control-panel?ok={quote(ok)}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/control-panel?err={quote(str(e))}", status_code=303)


# ---------------------------
# APIs
# ---------------------------

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
        symbols=SYMBOLS,
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
        COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("account_id", "MARKET_ORDER"))
        return JSONResponse({"ok": True, "command": cmd})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/submit-delayed-order")
async def api_submit_delayed_order(request: Request) -> JSONResponse:
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
    delay_choice_raw = None if body.get("delay_choice") is None else str(body.get("delay_choice"))
    execute_at_raw = None if body.get("execute_at") is None else str(body.get("execute_at"))

    cmd, err = validate_delayed_order_inputs(
        account_id=account_id,
        symbol=symbol,
        side=side,
        shares_raw=shares_raw,
        dollars_raw=dollars_raw,
        delay_choice_raw=delay_choice_raw,
        execute_at_raw=execute_at_raw,
        account_metas=ACCOUNT_METAS,
        symbols=SYMBOLS,
        invalid_account=t(lang, "invalid_account"),
        invalid_symbol=t(lang, "invalid_symbol"),
        invalid_side=t(lang, "invalid_side"),
        both_shares_and_dollars=t(lang, "both_shares_and_dollars"),
        neither_shares_nor_dollars=t(lang, "neither_shares_nor_dollars"),
        shares_positive=t(lang, "shares_positive"),
        dollars_positive=t(lang, "dollars_positive"),
        invalid_delay_choice=t(lang, "invalid_delay_choice"),
        future_time_required=t(lang, "future_time_required"),
        future_time_must_be_future=t(lang, "future_time_must_be_future"),
    )

    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    try:
        assert COMMANDS_PRODUCER is not None
        assert cmd is not None
        COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("account_id", "DELAYED_MARKET_ORDER"))
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

    max_volume_raw = None if body.get("max_volume") is None else str(body.get("max_volume"))
    mvt_raw = None if body.get("market_volume_target") is None else str(body.get("market_volume_target"))
    raw_end_time = body.get("end_time_et", body.get("end_time_bjt"))
    end_time_raw = None if raw_end_time is None else str(raw_end_time)
    abs_lim_raw = None if body.get("abs_pos_change_limit") is None else str(body.get("abs_pos_change_limit"))
    price_target_raw = None if body.get("price_target") is None else str(body.get("price_target"))
    single_order_notional_limit_raw = None if body.get("single_order_notional_limit") is None else str(body.get("single_order_notional_limit"))
    order_rate_limit_per_minute_raw = None if body.get("order_rate_limit_per_minute") is None else str(body.get("order_rate_limit_per_minute"))
    fast_trading_config_raw = body.get("fast_trading_config")
    if fast_trading_config_raw is None:
        fast_trading_config_raw = body.get("fast_trading_groups")

    cmd, err = validate_algo_start_inputs(
        trading_mode=trading_mode,
        symbol=symbol,
        max_volume_raw=max_volume_raw,
        market_volume_target_raw=mvt_raw,
        end_time_et_raw=end_time_raw,
        abs_pos_change_limit_raw=abs_lim_raw,
        price_target_raw=price_target_raw,
        single_order_notional_limit_raw=single_order_notional_limit_raw,
        order_rate_limit_per_minute_raw=order_rate_limit_per_minute_raw,
        fast_trading_config_raw=fast_trading_config_raw,
        symbols=SYMBOLS,
        account_metas=ACCOUNT_METAS,
        invalid_mode=t(lang, "invalid_mode"),
        invalid_symbol=t(lang, "invalid_symbol"),
        invalid_account=t(lang, "invalid_account"),
        number_required=t(lang, "number_required"),
        end_time_required=t(lang, "end_time_required"),
        fast_config_required=t(lang, "fast_config_required"),
        fast_group_required=t(lang, "fast_group_required"),
        fast_price_limit_positive=t(lang, "fast_price_limit_positive"),
        fast_group_accounts_required=t(lang, "fast_group_accounts_required"),
        fast_account_duplicate=t(lang, "fast_account_duplicate"),
        fast_allocation_positive=t(lang, "fast_allocation_positive"),
        fast_allocation_total=t(lang, "fast_allocation_total"),
    )

    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    try:
        assert COMMANDS_PRODUCER is not None
        assert cmd is not None
        COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("symbol", "ALGO"))
        return JSONResponse({"ok": True, "command": cmd})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/submit-algo-stop")
async def submit_algo_stop(
    request: Request,
    trading_mode: str = Form(...),
    reason: str | None = Form(None),
):
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    form = await request.form()
    account_ids = [str(x) for x in form.getlist("account_ids")]

    cmd, err = validate_algo_stop_inputs(
        trading_mode=trading_mode,
        account_ids=account_ids,
        reason_raw=reason,
        account_metas=ACCOUNT_METAS,
        invalid_mode=t(lang, "invalid_mode"),
        invalid_account=t(lang, "invalid_account"),
    )

    if err:
        return RedirectResponse(url=f"/control-panel?err={quote(err)}", status_code=303)

    try:
        assert COMMANDS_PRODUCER is not None and cmd is not None
        COMMANDS_PRODUCER.publish_order(cmd, key=cmd.get("trading_mode", "ALGO_STOP"))
        ok = f"{t(lang,'published_cmd')}={cmd['command_id']}"
        return RedirectResponse(url=f"/control-panel?ok={quote(ok)}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/control-panel?err={quote(str(e))}", status_code=303)


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
    inner = render_market_data_page(lang=lang, symbols=SYMBOLS)
    return HTMLResponse(render_layout(lang, "market-data", inner, current_user=(user or ""), can_convert_currency=_can_convert_currency(user), can_manage_trading=_can_manage_trading(user)))


@app.get("/market-insights", response_class=HTMLResponse)
def market_insights(request: Request) -> HTMLResponse:
    user, gate = _require_auth_page(request)
    if gate is not None:
        return gate
    lang = resolve_lang(request)
    max_levels = int(APP_CONFIG.get("market_data", {}).get("market_insights_max_levels", 20))
    inner = render_market_insights_page(lang=lang, symbols=SYMBOLS, max_levels=max_levels)
    return HTMLResponse(render_layout(lang, "market-insights", inner, current_user=(user or ""), can_convert_currency=_can_convert_currency(user), can_manage_trading=_can_manage_trading(user)))


@app.get("/api/market-data")
async def api_market_data(request: Request) -> JSONResponse:
    user, gate = _require_auth_api(request)
    if gate is not None:
        return gate
    rows = MD_STORE.get_for_symbols(SYMBOLS)
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
    if clean_symbol not in SYMBOLS:
        return JSONResponse({"ok": False, "error": "Unknown symbol"}, status_code=404)

    max_levels = int(APP_CONFIG.get("market_data", {}).get("market_insights_max_levels", 20))
    requested_depth = max(1, min(int(depth), max_levels))
    book = MD_STORE.get_book(clean_symbol, requested_depth)

    return JSONResponse(
        {
            "symbol": book.symbol,
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
    }
    if bool(server_cfg.get("ssl_enabled", False)):
        uvicorn_kwargs["ssl_certfile"] = str(server_cfg.get("ssl_certfile", ""))
        uvicorn_kwargs["ssl_keyfile"] = str(server_cfg.get("ssl_keyfile", ""))
        print(f"[https] enabled cert='{uvicorn_kwargs['ssl_certfile']}'")
    else:
        print("[https] disabled (HTTP)")

    uvicorn.run("trading_ui.webserver:app", **uvicorn_kwargs)
