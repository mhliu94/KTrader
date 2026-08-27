import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from .services.market_data import QuoteRow
from .i18n import t
from .models import AccountMeta, AccountSnapshot
from .store import AccountStore


ACCOUNT_STALE_AFTER = timedelta(minutes=5)
ACCOUNT_SORT_NUMERIC_ID = "numeric_id"
ACCOUNT_SORT_USD_CASH = "usd_cash"
ACCOUNT_SORT_SECURITY = "security"


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _is_snapshot_stale(ts: str | None, now: datetime | None = None) -> bool:
    """Return whether a snapshot timestamp is more than five minutes old."""
    if not ts:
        return False

    try:
        timestamp = str(ts).strip()
        if timestamp.endswith(("Z", "z")):
            timestamp = f"{timestamp[:-1]}+00:00"
        updated_at = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return False

    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)

    return reference_time.astimezone(timezone.utc) - updated_at.astimezone(timezone.utc) > ACCOUNT_STALE_AFTER


def _account_server_label(meta: AccountMeta) -> str:
    server_name = meta.machine_alias or meta.ip_address or meta.id
    return f"#{meta.num_id} {server_name}"


def _account_server_title(meta: AccountMeta) -> str:
    parts = [meta.id, meta.broker]
    if meta.machine_alias:
        parts.append(meta.machine_alias)
    if meta.ip_address:
        parts.append(meta.ip_address)
    return " | ".join(part for part in parts if part)


def render_layout(
    lang: str,
    active_tab: str,
    inner_html: str,
    flash: str = "",
    current_user: str = "",
    can_convert_currency: bool = False,
    can_manage_trading: bool = False,
) -> str:
    next_path = f"/{active_tab}"

    tab1_cls = "tab active" if active_tab == "account-details" else "tab"
    tab2_cls = "tab active" if active_tab == "control-panel" else "tab"
    tab3_cls = "tab active" if active_tab == "market-data" else "tab"
    tab4_cls = "tab active" if active_tab == "market-insights" else "tab"
    tab5_cls = "tab active" if active_tab == "currency-conversion" else "tab"
    tab6_cls = "tab active" if active_tab == "trading-status" else "tab"

    flash_html = ""
    if flash:
        flash_html = f"<div class='flash'>{html_escape(flash)}</div>"

    lang_toggle = (
        f"<a class='lang' href='/set-lang?lang=en&next={html_escape(next_path)}'>{html_escape(t(lang,'lang_en'))}</a>"
        f"<span class='lang-sep'>|</span>"
        f"<a class='lang' href='/set-lang?lang=zh&next={html_escape(next_path)}'>{html_escape(t(lang,'lang_zh'))}</a>"
    )


    conversion_tab_html = ""
    if can_convert_currency:
        conversion_tab_html = (
            f'<a class="{tab5_cls}" href="/currency-conversion">'
            f'{html_escape(t(lang,"currency_conversion"))}</a>'
        )

    trading_status_tab_html = ""
    if can_manage_trading:
        trading_status_tab_html = (
            f'<a class="{tab6_cls}" href="/trading-status">'
            f'{html_escape(t(lang,"trading_status"))}</a>'
        )

    user_html = ""
    if current_user:
        user_html = (
            f"<span class='user'>{html_escape(t(lang,'signed_in_as'))}: <b>{html_escape(current_user)}</b></span>"
            f"<a class='logout' href='/logout'>{html_escape(t(lang,'logout'))}</a>"
        )

    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{html_escape(t(lang, "title"))}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; background: #fafafa; }}
    .topbar {{ display:flex; align-items:center; justify-content:space-between; margin-bottom: 12px; }}
    .right {{ display:flex; align-items:center; gap: 16px; }}
    h1 {{ margin: 0; font-size: 22px; }}
    .tabs {{ display:flex; gap:10px; margin: 14px 0 16px 0; }}
    .tab {{
      display:inline-block; padding:10px 14px; border-radius: 12px;
      border: 1px solid #e5e5e5; background: white; text-decoration:none; color:#222;
      box-shadow: 0 1px 2px rgba(0,0,0,0.04);
      font-weight: 600;
    }}
    .tab.active {{ outline: 2px solid rgba(0,0,0,0.12); }}
    .flash {{
      margin: 0 0 14px 0; padding: 12px 14px; border-radius: 12px;
      background: #fff; border: 1px solid #e5e5e5;
      box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px; }}
    .card {{ background: white; border: 1px solid #e5e5e5; border-radius: 12px; padding: 16px;
             box-shadow: 0 1px 2px rgba(0,0,0,0.04); }}
    .hdr {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom: 6px; }}
    .acct {{ font-size: 18px; font-weight: 700; }}
    .ts {{ font-size: 12px; color: #888; }}
    .meta {{ font-size: 12px; color: #666; margin-bottom: 10px; }}
    .cash {{ margin: 8px 0 12px 0; }}
    .portfolio-card {{ margin-bottom: 16px; }}
    .portfolio-card .hdr {{ align-items:flex-start; gap:16px; }}
    .portfolio-card .ts {{ max-width:520px; text-align:right; line-height:1.4; }}
    .portfolio-cash {{ display:flex; flex-wrap:wrap; gap:10px; margin:12px 0 16px 0; }}
    .cash-total {{
      min-width:150px; padding:10px 12px; border:1px solid #e5e7eb; border-radius:10px;
      background:#f8fafc; box-sizing:border-box;
    }}
    .cash-total-label {{ color:#64748b; font-size:11px; font-weight:700; letter-spacing:.04em; text-transform:uppercase; }}
    .cash-total-value {{ margin-top:3px; color:#0f172a; font-size:18px; font-weight:700; font-variant-numeric:tabular-nums; }}
    .portfolio-note {{ color:#666; font-size:12px; margin-top:4px; }}
    .portfolio-note.warn-note {{ color:#9a3412; }}
    .section-heading {{ margin:20px 0 10px 2px; color:#334155; font-size:14px; font-weight:700; }}
    .portfolio-card .section-heading {{ margin-top:12px; }}
    .account-list-heading {{ display:flex; flex-wrap:wrap; justify-content:space-between; align-items:end; gap:12px; margin:20px 2px 10px; }}
    .account-list-heading .section-heading {{ margin:0; }}
    form.account-sort-form {{ display:flex; flex-direction:row; flex-wrap:wrap; align-items:end; gap:8px; margin:0; }}
    .account-sort-field {{ display:flex; flex-direction:column; gap:4px; min-width:190px; }}
    .account-sort-field select {{ padding:7px 10px; }}
    .account-sort-form .btn {{ padding:8px 12px; }}
    .table-scroll {{ overflow-x:auto; }}
    .status-row {{ display:flex; align-items:center; gap:8px; margin: 8px 0 12px 0; font-size: 13px; }}
    .status-pill {{ display:inline-block; padding: 3px 8px; border-radius: 999px; font-weight: 700; font-size: 12px; }}
    .status-on {{ color:#166534; background:#dcfce7; border:1px solid #86efac; }}
    .status-off {{ color:#991b1b; background:#fee2e2; border:1px solid #fecaca; }}
    .status-unknown {{ color:#475569; background:#f1f5f9; border:1px solid #cbd5e1; }}
    .status-stale {{ color:#9a3412; background:#ffedd5; border:1px solid #fdba74; }}
    table.pos {{ width: 100%; border-collapse: collapse; }}
    table.pos th, table.pos td {{ border-top: 1px solid #eee; padding: 8px; font-size: 13px; }}
    table.pos thead th {{ border-top: none; color: #444; }}
    code {{ background: #f2f2f2; padding: 2px 6px; border-radius: 6px; }}

    /* form */
    form {{ display:flex; flex-direction:column; gap: 10px; }}
    label {{ font-size: 13px; color:#333; font-weight:600; }}
    select, input {{
      padding: 10px 12px; border-radius: 10px; border: 1px solid #ddd; background: white; font-size: 14px;
    }}
    .row {{ display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .help {{ color:#666; font-size: 12px; }}
    .btn {{
      display:inline-block;
      padding: 10px 14px;
      border-radius: 12px;
      border: 1px solid #e5e5e5;
      font-weight: 700;
      cursor: pointer;
      width: fit-content;
      color: white;
    }}
    
    .btn-blue  {{ background: #2563eb; }}   /* blue */
    .btn-green {{ background: #16a34a; }}   /* green */
    .btn-red   {{ background: #dc2626; }}   /* red */
    
    .btn-blue:hover  {{ background:#1d4ed8; }}
    .btn-green:hover {{ background:#15803d; }}
    .btn-red:hover   {{ background:#b91c1c; }}
    .btn:disabled {{ opacity: 0.45; cursor: default; }}
    .warn {{ color:#a33; font-size: 12px; }}

    /* language toggle */
    .lang {{ text-decoration:none; color:#222; font-weight:700; }}
    .lang-sep {{ color:#888; }}

    .divider {{ height: 1px; background: #eee; margin: 18px 0; }}
    .user {{ color:#333; font-size: 13px; }}
    .logout {{ text-decoration:none; color:#2563eb; font-weight:700; }}
    .choice-grid {{ display:flex; flex-wrap:wrap; gap: 8px; }}
    .choice-input {{ position:absolute; opacity:0; pointer-events:none; }}
    .choice-btn {{
      display:inline-block; padding: 8px 12px; border-radius: 10px; border:1px solid #d9d9d9;
      background:#fff; color:#222; font-size:13px; font-weight:700; cursor:pointer;
      user-select:none;
    }}
    .choice-input:checked + .choice-btn {{
      background:#dbeafe; color:#1e40af; border-color:#93c5fd;
    }}
    .choice-input:focus-visible + .choice-btn {{
      outline:3px solid rgba(37,99,235,0.35); outline-offset:2px;
    }}
    .choice-btn.active {{
      background:#dbeafe; color:#1e40af; border-color:#93c5fd;
    }}
    .toolbar {{ display:flex; flex-wrap:wrap; gap:12px; align-items:end; margin-bottom:16px; }}
    .toolbar-field {{ min-width: 180px; }}
    .split-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:16px; }}
    .control-panel-grid {{
      display:grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap:16px;
      align-items:start;
      max-width:1280px;
    }}
    .control-panel-column {{ display:flex; flex-direction:column; gap:16px; min-width:0; }}
    .control-panel-section {{ min-width:0; }}
    .inline-actions {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
    form.inline-form {{ display:inline-flex; flex-direction:row; gap:8px; margin:0; }}
    .status-list {{ display:flex; flex-direction:column; gap:10px; }}
    .status-item {{ display:flex; justify-content:space-between; gap:12px; align-items:center; padding:10px 0; border-top:1px solid #eee; }}
    .status-item:first-child {{ border-top:none; }}
    .status-title {{ font-weight:700; }}
    .status-meta {{ color:#666; font-size:12px; margin-top:3px; }}
    .account-card-actions {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }}
    .account-order-form {{ display:flex; flex-direction:column; gap:12px; }}
    .account-order-account {{ font-size:13px; color:#475569; }}
    .account-order-message {{ min-height:18px; font-size:13px; }}
    .account-order-message.success {{ color:#15803d; }}
    .account-order-message.error {{ color:#b91c1c; }}
    body.modal-open {{ overflow:hidden; }}
    .modal-backdrop {{
      position:fixed; inset:0; z-index:1000; padding:20px;
      display:flex; align-items:center; justify-content:center;
      background:rgba(15,23,42,0.48);
    }}
    .modal-backdrop[hidden] {{ display:none; }}
    .modal-panel {{
      width:min(920px, calc(100vw - 32px)); max-height:calc(100vh - 40px); overflow:auto;
      background:#fff; border-radius:12px; border:1px solid #e5e5e5;
      box-shadow:0 24px 80px rgba(15,23,42,0.24); padding:16px;
    }}
    .modal-header {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:12px; }}
    .modal-close {{ border:1px solid #ddd; background:#fff; color:#222; border-radius:10px; width:36px; height:36px; cursor:pointer; font-size:20px; line-height:1; }}
    .modal-actions {{ display:flex; flex-wrap:wrap; gap:8px; justify-content:flex-end; margin-top:14px; }}
    .fast-account-list {{ display:flex; flex-direction:column; gap:8px; margin-top:8px; }}
    .fast-account-row {{ display:grid; grid-template-columns:auto minmax(0, 1fr); gap:8px; align-items:center; }}
    .fast-account-label {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:13px; font-weight:600; color:#333; }}
    @media (max-width: 640px) {{
      body {{ margin:16px; }}
      .topbar {{ align-items:flex-start; gap:12px; }}
      .right {{ gap:10px; }}
      .tabs {{ overflow-x:auto; padding:2px 0 4px 0; }}
      .tab {{ flex:0 0 auto; }}
      .grid {{ grid-template-columns:minmax(0, 1fr); }}
      .card {{ min-width:0; }}
      .portfolio-card .hdr {{ flex-direction:column; gap:4px; }}
      .portfolio-card .ts {{ max-width:none; text-align:left; }}
      .portfolio-cash {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(110px, 1fr)); }}
      .cash-total {{ min-width:0; }}
      .modal-actions {{ justify-content:stretch; }}
      .modal-actions .btn {{ width:100%; }}
    }}
    @media (max-width: 960px) {{
      .control-panel-grid {{ grid-template-columns: 1fr; max-width: 920px; }}
    }}
    @media (max-width: 640px) {{
      .control-panel-grid .row {{ grid-template-columns: 1fr; }}
    }}
    .book-table {{ width:100%; border-collapse: collapse; }}
    .book-table th, .book-table td {{ border-top:1px solid #eee; padding:8px; font-size:13px; }}
    .book-table thead th {{ border-top:none; }}
    .book-empty {{ color:#666; font-size:13px; padding:10px 0 0 0; }}
  </style>
</head>
<body>
  <div class="topbar">
    <h1>{html_escape(t(lang, "title"))}</h1>
    <div class="right">
      <div>{user_html}</div>
      <div>{lang_toggle}</div>
    </div>
  </div>

  <div class="tabs">
    <a class="{tab1_cls}" href="/account-details">{html_escape(t(lang,"tab_account"))}</a>
    <a class="{tab2_cls}" href="/control-panel">{html_escape(t(lang,"tab_control"))}</a>
    <a class="{tab3_cls}" href="/market-data">{html_escape(t(lang,"tab_market"))}</a>
    <a class="{tab4_cls}" href="/market-insights">{html_escape(t(lang,"tab_insights"))}</a>
    {conversion_tab_html}
    {trading_status_tab_html}
  </div>

  {flash_html}
  {inner_html}

</body>
</html>
"""


def _aggregate_portfolio(
    accounts: Dict[str, AccountSnapshot],
    account_metas: Dict[str, AccountMeta],
):
    """Aggregate the latest configured-account snapshots for portfolio display."""
    cash_totals: Dict[str, float] = {}
    position_totals = {}
    included_accounts = 0

    for account_id in account_metas:
        account = accounts.get(account_id)
        if account is None:
            continue

        included_accounts += 1
        balances: Dict[str, float] = {}
        for raw_currency, raw_amount in account.cash_by_currency.items():
            currency = str(raw_currency or "").strip().upper()
            if not currency:
                continue
            balances[currency] = float(raw_amount or 0.0)
        if "USD" not in balances:
            balances["USD"] = float(account.cash or 0.0)
        for currency, amount in balances.items():
            cash_totals[currency] = cash_totals.get(currency, 0.0) + amount

        symbols_seen = set()
        for position in account.positions:
            symbol = str(position.symbol or "").strip().upper()
            if not symbol:
                continue
            qty = float(position.qty or 0.0)
            if not qty:
                continue

            aggregate = position_totals.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "qty": 0.0,
                    "priced_value": 0.0,
                    "priced_qty": 0.0,
                    "all_priced": True,
                    "directions": set(),
                    "account_count": 0,
                },
            )
            aggregate["qty"] += qty
            if qty > 0:
                aggregate["directions"].add(1)
            elif qty < 0:
                aggregate["directions"].add(-1)

            if position.avg_price is None:
                aggregate["all_priced"] = False
            else:
                abs_qty = abs(qty)
                aggregate["priced_qty"] += abs_qty
                aggregate["priced_value"] += abs_qty * float(position.avg_price)

            if symbol not in symbols_seen:
                aggregate["account_count"] += 1
                symbols_seen.add(symbol)

    positions = []
    for aggregate in position_totals.values():
        avg_price = None
        if (
            aggregate["all_priced"]
            and len(aggregate["directions"]) == 1
            and aggregate["priced_qty"]
        ):
            avg_price = aggregate["priced_value"] / aggregate["priced_qty"]
        positions.append(
            {
                "symbol": aggregate["symbol"],
                "qty": aggregate["qty"],
                "avg_price": avg_price,
                "account_count": aggregate["account_count"],
            }
        )

    return {
        "cash": dict(sorted(cash_totals.items())),
        "positions": sorted(positions, key=lambda position: position["symbol"]),
        "included_accounts": included_accounts,
        "total_accounts": len(account_metas),
    }


def _account_usd_cash(account: AccountSnapshot | None) -> float:
    if account is None:
        return 0.0
    for raw_currency, raw_amount in account.cash_by_currency.items():
        if str(raw_currency or "").strip().upper() == "USD":
            return float(raw_amount or 0.0)
    return float(account.cash or 0.0)


def _account_security_holding(account: AccountSnapshot | None, security: str) -> float:
    if account is None:
        return 0.0
    normalized_security = str(security or "").strip().upper()
    return sum(
        float(position.qty or 0.0)
        for position in account.positions
        if str(position.symbol or "").strip().upper() == normalized_security
    )


def _sort_account_metas(
    account_metas: Dict[str, AccountMeta],
    accounts: Dict[str, AccountSnapshot],
    sort_by: str,
    security: str = "",
) -> List[AccountMeta]:
    items = list(account_metas.values())
    if sort_by == ACCOUNT_SORT_USD_CASH:
        return sorted(
            items,
            key=lambda meta: (
                accounts.get(meta.id) is None,
                -_account_usd_cash(accounts.get(meta.id)),
                meta.num_id,
                meta.id,
            ),
        )
    if sort_by == ACCOUNT_SORT_SECURITY:
        return sorted(
            items,
            key=lambda meta: (
                accounts.get(meta.id) is None,
                -_account_security_holding(accounts.get(meta.id), security),
                -_account_usd_cash(accounts.get(meta.id)),
                meta.num_id,
                meta.id,
            ),
        )
    return sorted(items, key=lambda meta: (meta.num_id, meta.id))


def _configured_securities(symbols: List[str] | None) -> List[str]:
    configured = []
    seen = set()
    for raw_symbol in symbols or []:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        configured.append(symbol)
        seen.add(symbol)
    return configured


def resolve_account_sort(
    symbols: List[str] | None,
    account_sort: str | None,
    security: str | None,
) -> tuple[str, str, List[str]]:
    sort_modes = {ACCOUNT_SORT_NUMERIC_ID, ACCOUNT_SORT_USD_CASH, ACCOUNT_SORT_SECURITY}
    effective_sort = account_sort if account_sort in sort_modes else ACCOUNT_SORT_NUMERIC_ID
    securities = _configured_securities(symbols)
    requested_security = str(security or "").strip().upper()
    selected_security = requested_security if requested_security in securities else (securities[0] if securities else "")
    if effective_sort == ACCOUNT_SORT_SECURITY and not selected_security:
        effective_sort = ACCOUNT_SORT_NUMERIC_ID
    return effective_sort, selected_security, securities


def render_account_details_page(
    lang: str,
    store: AccountStore,
    accounts: Dict[str, AccountSnapshot],
    source_label: str,
    account_metas: Dict[str, AccountMeta],
    account_details_topic: str,
    symbols: List[str] | None = None,
    account_sort: str = ACCOUNT_SORT_NUMERIC_ID,
    security: str | None = None,
) -> str:
    effective_sort, selected_security, securities = resolve_account_sort(symbols, account_sort, security)

    items = _sort_account_metas(account_metas, accounts, effective_sort, selected_security)
    portfolio = _aggregate_portfolio(accounts, account_metas)

    cash_totals = []
    for currency, amount in portfolio["cash"].items():
        cash_totals.append(
            "<div class='cash-total'>"
            f"<div class='cash-total-label'>{html_escape(currency)}</div>"
            f"<div class='cash-total-value'>{amount:,.2f}</div>"
            "</div>"
        )

    portfolio_position_rows = []
    for position in portfolio["positions"]:
        avg_price = position["avg_price"]
        portfolio_position_rows.append(
            "<tr>"
            f"<td><b>{html_escape(position['symbol'])}</b></td>"
            f"<td style='text-align:right'>{position['qty']:,.2f}</td>"
            f"<td style='text-align:right'>{'—' if avg_price is None else f'{avg_price:,.4f}'}</td>"
            f"<td style='text-align:right'>{position['account_count']:,}</td>"
            "</tr>"
        )

    coverage_note = (
        f"{html_escape(t(lang, 'snapshot_coverage'))}: "
        f"<b>{portfolio['included_accounts']} / {portfolio['total_accounts']}</b>"
    )
    if portfolio["included_accounts"] < portfolio["total_accounts"]:
        coverage_note += (
            f"<div class='portfolio-note warn-note'>{html_escape(t(lang, 'portfolio_incomplete'))}</div>"
        )

    portfolio_html = (
        "<section id='portfolio-summary' class='card portfolio-card'>"
        "<div class='hdr'>"
        f"<div class='acct'>{html_escape(t(lang, 'portfolio'))}</div>"
        f"<div class='ts'>{coverage_note}</div>"
        "</div>"
        f"<div class='meta'>{html_escape(t(lang, 'portfolio_summary'))}</div>"
        f"<div class='section-heading'>{html_escape(t(lang, 'cash_holdings'))}</div>"
        f"<div class='portfolio-cash'>{''.join(cash_totals) if cash_totals else '<em>—</em>'}</div>"
        f"<div class='section-heading'>{html_escape(t(lang, 'positions'))}</div>"
        "<div class='table-scroll'><table class='pos'>"
        f"<thead><tr><th>{html_escape(t(lang, 'symbol'))}</th>"
        f"<th style='text-align:right'>{html_escape(t(lang, 'qty'))}</th>"
        f"<th style='text-align:right' title='{html_escape(t(lang, 'weighted_avg_px'))}'>{html_escape(t(lang, 'avg_px'))}</th>"
        f"<th style='text-align:right'>{html_escape(t(lang, 'holding_accounts'))}</th></tr></thead>"
        f"<tbody>{''.join(portfolio_position_rows) if portfolio_position_rows else '<tr><td colspan=4><em>—</em></td></tr>'}</tbody>"
        "</table></div>"
        "</section>"
    )

    cards: List[str] = []
    for meta in items:
        acct = accounts.get(meta.id)
        updated_at = str(acct.ts or "") if acct is not None else ""
        acct_title = f"#{meta.num_id} - {html_escape(meta.id)}"
        meta_parts = [
            f"{html_escape(t(lang,'broker'))}: {html_escape(meta.broker)}",
            f"{html_escape(t(lang,'trading_medium'))}: {html_escape(meta.trading_medium)}",
        ]
        if meta.machine_alias:
            meta_parts.append(f"{html_escape(t(lang,'machine_alias'))}: {html_escape(meta.machine_alias)}")
        if meta.ip_address:
            ip_address = html_escape(meta.ip_address)
            if meta.monitor:
                ip_address = (
                    f"<a href='https://{ip_address}' target='_blank' rel='noopener noreferrer'>"
                    f"{ip_address}</a>"
                )
            meta_parts.append(f"{html_escape(t(lang,'ip_address'))}: {ip_address}")
        if meta.broker_id:
            meta_parts.append(f"{html_escape(t(lang,'broker_id'))}={html_escape(meta.broker_id)}")
        meta_line = " · ".join(meta_parts)

        pos_rows = []
        if acct is not None:
            for p in sorted(acct.positions, key=lambda x: x.symbol):
                pos_rows.append(
                    "<tr>"
                    f"<td>{html_escape(p.symbol)}</td>"
                    f"<td style='text-align:right'>{p.qty:,.2f}</td>"
                    f"<td style='text-align:right'>{'' if p.avg_price is None else f'{p.avg_price:,.4f}'}</td>"
                    "</tr>"
                )

        if acct is None:
            trading_cls = "status-unknown"
            trading_text = t(lang, "trading_unknown")
            cash_text = "—"
            ts_text = t(lang, "no_snapshot")
            stale_status = ""
        else:
            trading_cls = "status-on" if acct.trading_enabled else "status-off"
            trading_text = t(lang, "trading_on" if acct.trading_enabled else "trading_off")
            cash_text = f"${_account_usd_cash(acct):,.2f}"
            ts_text = acct.ts or ""
            stale_status = ""
            if _is_snapshot_stale(acct.ts):
                stale_status = (
                    f"<span class='status-pill status-stale' "
                    f"title='{html_escape(t(lang, 'stale_account_help'))}'>"
                    f"{html_escape(t(lang, 'stale'))}</span>"
                )

        pos_table = (
            "<table class='pos'>"
            f"<thead><tr><th>{html_escape(t(lang,'symbol'))}</th>"
            f"<th style='text-align:right'>{html_escape(t(lang,'qty'))}</th>"
            f"<th style='text-align:right'>{html_escape(t(lang,'avg_px'))}</th></tr></thead>"
            f"<tbody>{''.join(pos_rows) if pos_rows else '<tr><td colspan=3><em>—</em></td></tr>'}</tbody>"
            "</table>"
        )

        cards.append(
            "<div class='card account-card' "
            f"data-updated-at='{html_escape(updated_at)}' "
            f"data-stale-label='{html_escape(t(lang, 'stale'))}' "
            f"data-stale-title='{html_escape(t(lang, 'stale_account_help'))}'>"
            f"<div class='hdr'><div class='acct'>{acct_title}</div>"
            f"<div class='ts'>{html_escape(ts_text)}</div></div>"
            f"<div class='meta'>{meta_line}</div>"
            f"<div class='status-row'>{html_escape(t(lang,'trading'))}: "
            f"<span class='status-pill {trading_cls}'>{html_escape(trading_text)}</span>"
            f"{stale_status}</div>"
            f"<div class='cash'>{html_escape(t(lang,'cash'))} (USD): <b>{html_escape(cash_text)}</b></div>"
            f"{pos_table}"
            "<div class='account-card-actions'>"
            f"<button class='btn btn-blue' type='button' data-account-order='market' "
            f"data-account-id='{html_escape(meta.id)}' data-account-label='#{meta.num_id} - {html_escape(meta.id)}'>"
            f"{html_escape(t(lang, 'market_order'))}</button>"
            f"<button class='btn btn-green' type='button' data-account-order='limit' "
            f"data-account-id='{html_escape(meta.id)}' data-account-label='#{meta.num_id} - {html_escape(meta.id)}'>"
            f"{html_escape(t(lang, 'limit_order'))}</button>"
            "</div>"
            "</div>"
        )

    sort_options = "".join(
        f"<option value='{value}'{' selected' if effective_sort == value else ''}>"
        f"{html_escape(t(lang, label))}</option>"
        for value, label in (
            (ACCOUNT_SORT_NUMERIC_ID, "sort_numeric_id"),
            (ACCOUNT_SORT_USD_CASH, "sort_usd_cash"),
            (ACCOUNT_SORT_SECURITY, "sort_security_holdings"),
        )
    )
    security_options = "".join(
        f"<option value='{html_escape(symbol)}'{' selected' if selected_security == symbol else ''}>"
        f"{html_escape(symbol)}</option>"
        for symbol in securities
    )
    if not security_options:
        security_options = "<option value=''>—</option>"
    security_hidden = "" if effective_sort == ACCOUNT_SORT_SECURITY else " hidden"
    sort_controls = f"""
    <form class="account-sort-form" method="get" action="/account-details">
      <div class="account-sort-field">
        <label for="account-sort">{html_escape(t(lang, 'sort_accounts'))}</label>
        <select id="account-sort" name="sort_by">{sort_options}</select>
      </div>
      <div class="account-sort-field" id="account-sort-security-field"{security_hidden}>
        <label for="account-sort-security">{html_escape(t(lang, 'sort_security'))}</label>
        <select id="account-sort-security" name="security">{security_options}</select>
      </div>
      <button class="btn btn-blue" type="submit">{html_escape(t(lang, 'apply_sort'))}</button>
    </form>
    <script>
      (function() {{
        const sort = document.getElementById("account-sort");
        const securityField = document.getElementById("account-sort-security-field");
        if (!sort || !securityField) return;
        function syncSecurityField() {{ securityField.hidden = sort.value !== "{ACCOUNT_SORT_SECURITY}"; }}
        sort.addEventListener("change", syncSecurityField);
        syncSecurityField();
      }})();
    </script>
    """

    # NOTE REMOVED: no source/topic line, users don't care
    inner = f"""
  {portfolio_html}
  <div class="account-list-heading">
    <div class="section-heading">{html_escape(t(lang, 'trading_accounts'))}</div>
    {sort_controls}
  </div>
  <div id="trading-account-grid" class="grid">
    {''.join(cards) if cards else f"<div><em>{html_escape(t(lang,'no_data'))}</em></div>"}
  </div>
"""
    return inner


def _render_account_order_controls(lang: str, symbols: List[str] | None) -> str:
    configured_symbols = sorted(_configured_securities(symbols))
    quick_symbol_buttons = "".join(
        (
            f"<input class='choice-input' type='radio' id='account-quick-symbol-{idx}' "
            f"name='symbol' value='{html_escape(symbol)}'{' checked' if idx == 1 else ''}>"
            f"<label class='choice-btn' for='account-quick-symbol-{idx}'>{html_escape(symbol)}</label>"
        )
        for idx, symbol in enumerate(configured_symbols, start=1)
    )
    limit_symbol_options = "".join(
        f"<option value='{html_escape(symbol)}'>{html_escape(symbol)}</option>"
        for symbol in configured_symbols
    )
    limit_share_buttons = "".join(
        f"<button class='choice-btn' type='button' data-account-limit-shares='{shares}'>{shares}</button>"
        for shares in (3000, 5000, 10000, 20000, 30000, 50000)
    )

    market_modal = f"""
    <div class="modal-backdrop" id="account-quick-order-modal" hidden>
      <div class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="account-quick-order-title">
        <div class="modal-header">
          <div>
            <div class="acct" id="account-quick-order-title">{html_escape(t(lang, 'quick_market_order'))}</div>
            <div class="account-order-account">
              {html_escape(t(lang, 'account'))}: <b data-account-order-label>—</b>
            </div>
          </div>
          <button class="modal-close" type="button" data-account-order-close
                  aria-label="{html_escape(t(lang, 'fast_config_cancel'))}">&times;</button>
        </div>
        <form class="account-order-form" id="account-quick-order-form" method="post"
              action="/api/account-orders/quick-market"
              data-failure-message="{html_escape(t(lang, 'publish_failed'))}">
          <input type="hidden" name="account_id" value="">
          <div>
            <label>{html_escape(t(lang, 'quick_symbol'))}</label>
            <div class="choice-grid">{quick_symbol_buttons}</div>
          </div>
          <div>
            <label>{html_escape(t(lang, 'quick_side'))}</label>
            <div class="choice-grid">
              <input class="choice-input" type="radio" id="account-quick-side-buy" name="side" value="BUY" checked>
              <label class="choice-btn" for="account-quick-side-buy">{html_escape(t(lang, 'buy'))}</label>
              <input class="choice-input" type="radio" id="account-quick-side-sell" name="side" value="SELL">
              <label class="choice-btn" for="account-quick-side-sell">{html_escape(t(lang, 'sell'))}</label>
            </div>
          </div>
          <div>
            <label for="account-quick-dollar-amount">{html_escape(t(lang, 'quick_dollars'))}</label>
            <input id="account-quick-dollar-amount" name="dollar_amount" placeholder="10000" inputmode="decimal">
          </div>
          <div class="help">
            {html_escape(t(lang, 'quick_selected_cash'))}: <b data-account-order-cash>—</b>
            &nbsp;|&nbsp;
            {html_escape(t(lang, 'quick_selected_last'))}: <b data-account-order-last>—</b>
          </div>
          <div class="account-order-message" data-account-order-message aria-live="polite"></div>
          <div class="modal-actions">
            <button class="btn" type="button" data-account-order-close style="background:#64748b;">
              {html_escape(t(lang, 'fast_config_cancel'))}
            </button>
            <button class="btn btn-blue" type="submit">{html_escape(t(lang, 'submit_order'))}</button>
          </div>
        </form>
      </div>
    </div>
    """

    limit_modal = f"""
    <div class="modal-backdrop" id="account-limit-order-modal" hidden>
      <div class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="account-limit-order-title">
        <div class="modal-header">
          <div>
            <div class="acct" id="account-limit-order-title">{html_escape(t(lang, 'limit_order'))}</div>
            <div class="account-order-account">
              {html_escape(t(lang, 'account'))}: <b data-account-order-label>—</b>
            </div>
          </div>
          <button class="modal-close" type="button" data-account-order-close
                  aria-label="{html_escape(t(lang, 'fast_config_cancel'))}">&times;</button>
        </div>
        <form class="account-order-form" id="account-limit-order-form" method="post"
              action="/api/account-orders/limit"
              data-failure-message="{html_escape(t(lang, 'publish_failed'))}">
          <input type="hidden" name="account_id" value="">
          <div class="row">
            <div>
              <label for="account-limit-symbol">{html_escape(t(lang, 'limit_symbol'))}</label>
              <select id="account-limit-symbol" name="symbol" required>{limit_symbol_options}</select>
            </div>
            <div>
              <label>{html_escape(t(lang, 'limit_side'))}</label>
              <div class="choice-grid">
                <input class="choice-input" type="radio" id="account-limit-side-buy" name="side" value="BUY" checked>
                <label class="choice-btn" for="account-limit-side-buy">{html_escape(t(lang, 'buy'))}</label>
                <input class="choice-input" type="radio" id="account-limit-side-sell" name="side" value="SELL">
                <label class="choice-btn" for="account-limit-side-sell">{html_escape(t(lang, 'sell'))}</label>
              </div>
            </div>
          </div>
          <div class="row">
            <div>
              <label for="account-limit-shares">{html_escape(t(lang, 'shares'))}</label>
              <input id="account-limit-shares" name="shares" placeholder="e.g. 100" inputmode="numeric" required>
              <div class="choice-grid" style="margin-top:8px;">{limit_share_buttons}</div>
              <div class="help">{html_escape(t(lang, 'limit_quick_shares'))}</div>
            </div>
            <div>
              <label for="account-limit-price">{html_escape(t(lang, 'limit_price'))}</label>
              <input id="account-limit-price" name="limit_price" placeholder="e.g. 123.45" inputmode="decimal">
              <div class="help">{html_escape(t(lang, 'limit_price_precedence'))}</div>
            </div>
          </div>
          <div>
            <label>{html_escape(t(lang, 'through_market_pct'))}</label>
            <div class="choice-grid">
              <input class="choice-input" type="radio" id="account-limit-through-none" name="through_market_pct" value="" checked>
              <label class="choice-btn" for="account-limit-through-none">{html_escape(t(lang, 'none'))}</label>
              <input class="choice-input" type="radio" id="account-limit-through-1" name="through_market_pct" value="1">
              <label class="choice-btn" for="account-limit-through-1">1%</label>
              <input class="choice-input" type="radio" id="account-limit-through-5" name="through_market_pct" value="5">
              <label class="choice-btn" for="account-limit-through-5">5%</label>
              <input class="choice-input" type="radio" id="account-limit-through-10" name="through_market_pct" value="10">
              <label class="choice-btn" for="account-limit-through-10">10%</label>
              <input class="choice-input" type="radio" id="account-limit-through-20" name="through_market_pct" value="20">
              <label class="choice-btn" for="account-limit-through-20">20%</label>
            </div>
          </div>
          <div class="help">
            {html_escape(t(lang, 'quick_selected_cash'))}: <b data-account-order-cash>—</b>
            &nbsp;|&nbsp;
            {html_escape(t(lang, 'quick_selected_last'))}: <b data-account-order-last>—</b>
          </div>
          <div class="account-order-message" data-account-order-message aria-live="polite"></div>
          <div class="modal-actions">
            <button class="btn" type="button" data-account-order-close style="background:#64748b;">
              {html_escape(t(lang, 'fast_config_cancel'))}
            </button>
            <button class="btn btn-blue" type="submit">{html_escape(t(lang, 'submit_limit_order'))}</button>
          </div>
        </form>
      </div>
    </div>
    """

    controller_script = """
    <script>
      (function() {
        const root = document.getElementById("account-details-live-root");
        const marketModal = document.getElementById("account-quick-order-modal");
        const limitModal = document.getElementById("account-limit-order-modal");
        if (!root || !marketModal || !limitModal) return;

        const modals = { market: marketModal, limit: limitModal };
        let activeModal = null;
        let lastFocused = null;
        let activeAccountId = "";
        let activeOrderKind = "";
        let accountsCache = null;
        let marketCache = null;
        let summaryRequest = null;
        let nextContextVersion = 1;

        function formatMoney(value) {
          const number = Number(value);
          if (value === null || value === undefined || !Number.isFinite(number)) return "—";
          return "$" + number.toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
          });
        }

        function formatPrice(value) {
          const number = Number(value);
          if (value === null || value === undefined || !Number.isFinite(number)) return "—";
          return number.toLocaleString(undefined, {
            minimumFractionDigits: 4,
            maximumFractionDigits: 4
          });
        }

        function usdCash(account) {
          if (!account) return null;
          const balances = account.cash_by_currency || {};
          const usdKey = Object.keys(balances).find(function(key) {
            return String(key).trim().toUpperCase() === "USD";
          });
          return usdKey === undefined ? account.cash : balances[usdKey];
        }

        function selectedSymbol(modal) {
          const checked = modal.querySelector("input[name='symbol']:checked");
          if (checked) return checked.value;
          const select = modal.querySelector("select[name='symbol']");
          return select ? select.value : "";
        }

        function renderSummary(modal) {
          if (!modal) return;
          const accountInput = modal.querySelector("input[name='account_id']");
          const accountId = accountInput ? accountInput.value : "";
          const accounts = accountsCache && Array.isArray(accountsCache.accounts) ? accountsCache.accounts : [];
          const account = accounts.find(function(row) { return row.account_id === accountId; });
          const cash = modal.querySelector("[data-account-order-cash]");
          if (cash) cash.textContent = formatMoney(usdCash(account));

          const rows = marketCache && marketCache.rows ? marketCache.rows : {};
          const quote = rows[selectedSymbol(modal)] || null;
          const last = modal.querySelector("[data-account-order-last]");
          if (last) last.textContent = quote && !quote.error ? formatPrice(quote.last) : "—";
        }

        function refreshSummary() {
          if (!activeModal) return Promise.resolve();
          if (summaryRequest) return summaryRequest;
          summaryRequest = Promise.all([
            fetch("/api/accounts", { cache: "no-store", credentials: "same-origin" }),
            fetch("/api/market-data", { cache: "no-store", credentials: "same-origin" })
          ]).then(async function(responses) {
            if (responses[0].ok) accountsCache = await responses[0].json();
            if (responses[1].ok) marketCache = await responses[1].json();
          }).catch(function() {
            // Keep the last successful values through transient network failures.
          }).finally(function() {
            summaryRequest = null;
            renderSummary(activeModal);
          });
          return summaryRequest;
        }

        function clearMessage(modal) {
          const message = modal.querySelector("[data-account-order-message]");
          if (!message) return;
          message.textContent = "";
          message.classList.remove("success", "error");
        }

        function openModal(kind, accountId, accountLabel, trigger) {
          const modal = modals[kind];
          if (!modal || !accountId) return;
          if (activeModal && activeModal !== modal) activeModal.hidden = true;
          const form = modal.querySelector("form");
          if (!form) return;
          Array.from(form.elements).forEach(function(control) { control.disabled = false; });
          form.reset();
          form.dataset.contextVersion = String(nextContextVersion++);
          const accountInput = form.querySelector("input[name='account_id']");
          if (accountInput) accountInput.value = accountId;
          const label = modal.querySelector("[data-account-order-label]");
          if (label) label.textContent = accountLabel || accountId;
          clearMessage(modal);
          modal.querySelectorAll("[data-account-limit-shares]").forEach(function(button) {
            button.classList.remove("active");
          });
          lastFocused = trigger;
          activeAccountId = accountId;
          activeOrderKind = kind;
          activeModal = modal;
          modal.hidden = false;
          document.body.classList.add("modal-open");
          renderSummary(modal);
          refreshSummary();
          const closeButton = modal.querySelector(".modal-close");
          if (closeButton) closeButton.focus();
        }

        function closeModal() {
          if (!activeModal) return;
          activeModal.hidden = true;
          activeModal = null;
          document.body.classList.remove("modal-open");
          if (lastFocused && lastFocused.isConnected) {
            lastFocused.focus();
            return;
          }
          const replacement = Array.from(root.querySelectorAll("[data-account-order]")).find(function(button) {
            return button.dataset.accountOrder === activeOrderKind && button.dataset.accountId === activeAccountId;
          });
          if (replacement) replacement.focus();
        }

        async function submitOrder(form) {
          const modal = form.closest(".modal-backdrop");
          if (!modal) return;
          const contextVersion = form.dataset.contextVersion || "";
          const formData = new FormData(form);
          const controls = Array.from(form.elements);
          const message = modal.querySelector("[data-account-order-message]");
          controls.forEach(function(control) { control.disabled = true; });
          clearMessage(modal);
          try {
            const response = await fetch(form.action, {
              method: "POST",
              body: formData,
              credentials: "same-origin",
              headers: { Accept: "application/json" }
            });
            let payload = {};
            try { payload = await response.json(); } catch (_error) {}
            if (!response.ok || !payload.ok) {
              throw new Error(payload.error || form.dataset.failureMessage || "Request failed");
            }
            if (form.dataset.contextVersion !== contextVersion) return;
            if (message) {
              message.textContent = payload.message || "";
              message.classList.add("success");
            }
            refreshSummary();
          } catch (error) {
            if (form.dataset.contextVersion !== contextVersion) return;
            if (message) {
              message.textContent = error && error.message ? error.message : (form.dataset.failureMessage || "Request failed");
              message.classList.add("error");
            }
          } finally {
            if (form.dataset.contextVersion === contextVersion) {
              controls.forEach(function(control) { control.disabled = false; });
            }
          }
        }

        root.addEventListener("click", function(event) {
          if (!(event.target instanceof Element)) return;
          const trigger = event.target.closest("[data-account-order]");
          if (trigger && root.contains(trigger)) {
            openModal(trigger.dataset.accountOrder || "", trigger.dataset.accountId || "", trigger.dataset.accountLabel || "", trigger);
            return;
          }
          if (event.target.closest("[data-account-order-close]")) {
            closeModal();
            return;
          }
          const sharesButton = event.target.closest("[data-account-limit-shares]");
          if (sharesButton && activeModal === limitModal) {
            const shares = limitModal.querySelector("input[name='shares']");
            if (shares) shares.value = sharesButton.dataset.accountLimitShares || "";
            limitModal.querySelectorAll("[data-account-limit-shares]").forEach(function(button) {
              button.classList.toggle("active", button === sharesButton);
            });
            if (shares) shares.focus();
            return;
          }
          if (activeModal && event.target === activeModal) closeModal();
        });

        root.addEventListener("input", function(event) {
          if (!(event.target instanceof Element)) return;
          if (event.target.matches("#account-limit-shares")) {
            const current = event.target.value.trim();
            limitModal.querySelectorAll("[data-account-limit-shares]").forEach(function(button) {
              button.classList.toggle("active", button.dataset.accountLimitShares === current);
            });
          }
        });

        root.addEventListener("change", function(event) {
          if (activeModal && event.target instanceof Element && event.target.matches("[name='symbol']")) {
            renderSummary(activeModal);
          }
        });

        [marketModal, limitModal].forEach(function(modal) {
          const form = modal.querySelector("form");
          if (form) form.addEventListener("submit", function(event) {
            event.preventDefault();
            submitOrder(form);
          });
        });

        document.addEventListener("keydown", function(event) {
          if (!activeModal) return;
          if (event.key === "Escape") {
            closeModal();
            return;
          }
          if (event.key !== "Tab") return;
          const focusable = Array.from(activeModal.querySelectorAll(
            "button:not([disabled]), input:not([type='hidden']):not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]"
          ));
          if (!focusable.length) return;
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (event.shiftKey && (document.activeElement === first || !activeModal.contains(document.activeElement))) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && (document.activeElement === last || !activeModal.contains(document.activeElement))) {
            event.preventDefault();
            first.focus();
          }
        });

        const summaryTimer = window.setInterval(function() {
          if (activeModal) refreshSummary();
        }, 5000);
        window.addEventListener("pagehide", function() {
          window.clearInterval(summaryTimer);
        }, { once: true });
      })();
    </script>
    """
    return market_modal + limit_modal + controller_script


def render_live_account_details_page(
    inner_html: str,
    lang: str | None = None,
    symbols: List[str] | None = None,
) -> str:
    order_controls = _render_account_order_controls(lang, symbols) if lang is not None else ""
    return f"""
  <div id="account-details-live-root">
    {inner_html}
    {order_controls}
  </div>
  <script>
    (function() {{
      const root = document.getElementById("account-details-live-root");
      if (!root || !window.EventSource) return;

      const updates = new EventSource("/api/account-details/stream");
      function refreshStaleStatuses() {{
        root.querySelectorAll(".account-card[data-updated-at]").forEach(function(card) {{
          let timestamp = (card.getAttribute("data-updated-at") || "").trim();
          if (!timestamp) return;
          if (!/(?:[zZ]|[+-]\d{{2}}:?\d{{2}})$/.test(timestamp)) timestamp += "Z";
          const updatedAt = Date.parse(timestamp);
          if (Number.isNaN(updatedAt)) return;

          const statusRow = card.querySelector(".status-row");
          if (!statusRow) return;
          const existing = statusRow.querySelector(".status-stale");
          const isStale = Date.now() - updatedAt > 5 * 60 * 1000;
          if (!isStale && existing) existing.remove();
          if (isStale && !existing) {{
            const badge = document.createElement("span");
            badge.className = "status-pill status-stale";
            badge.title = card.getAttribute("data-stale-title") || "";
            badge.textContent = card.getAttribute("data-stale-label") || "";
            statusRow.appendChild(badge);
          }}
        }});
      }}

      updates.onmessage = function(event) {{
        try {{
          const payload = JSON.parse(event.data);
          if (!payload || typeof payload.html !== "string") return;

          const template = document.createElement("template");
          template.innerHTML = payload.html;
          const nextPortfolio = template.content.querySelector("#portfolio-summary");
          const nextGrid = template.content.querySelector("#trading-account-grid");
          const currentPortfolio = root.querySelector("#portfolio-summary");
          const currentGrid = root.querySelector("#trading-account-grid");
          if (nextPortfolio && currentPortfolio) currentPortfolio.replaceWith(nextPortfolio);
          if (nextGrid && currentGrid) currentGrid.replaceWith(nextGrid);
          refreshStaleStatuses();
        }} catch (_error) {{
          // EventSource reconnects automatically; keep the last complete snapshot visible.
        }}
      }};

      refreshStaleStatuses();
      const staleTimer = window.setInterval(refreshStaleStatuses, 15000);
      function closeUpdates() {{
        window.clearInterval(staleTimer);
        updates.close();
      }}
      window.addEventListener("pagehide", closeUpdates, {{ once: true }});
      window.addEventListener("beforeunload", closeUpdates, {{ once: true }});
    }})();
  </script>
"""


def render_login_page(lang: str, next_path: str, error: str = "") -> str:
    msg = ""
    if error:
        msg = f"<div class='warn'>{html_escape(error)}</div>"
    return f"""
  <div class="card" style="max-width: 460px;">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'login_title'))}</div>
      <div class="ts">-</div>
    </div>
    {msg}
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{html_escape(next_path)}" />
      <div>
        <label for="username">{html_escape(t(lang,'username'))}</label>
        <input id="username" name="username" type="text" autocomplete="username" required />
      </div>
      <div>
        <label for="password">{html_escape(t(lang,'password'))}</label>
        <input id="password" name="password" type="password" required />
      </div>
      <button class="btn btn-blue" type="submit">{html_escape(t(lang,'login'))}</button>
    </form>
  </div>
"""


def _currency_conversion_account_options(account_metas: Dict[str, AccountMeta]) -> str:
    sorted_account_ids = sorted(account_metas.keys(), key=lambda aid: account_metas[aid].num_id)
    tiger_account_ids = [aid for aid in sorted_account_ids if account_metas[aid].broker.strip().lower() == "tiger"]
    conversion_account_ids = tiger_account_ids or sorted_account_ids
    return "\n".join(
        (
            f"<option value='{html_escape(aid)}'>"
            f"#{account_metas[aid].num_id} {html_escape(aid)} ({html_escape(account_metas[aid].broker)})"
            "</option>"
        )
        for aid in conversion_account_ids
    )


def _render_currency_conversion_panel(lang: str, account_metas: Dict[str, AccountMeta], error: str = "", ok: str = "") -> str:
    conversion_account_opts = _currency_conversion_account_options(account_metas)
    msg = ""
    if error:
        msg = f"<div class='warn'>{html_escape(error)}</div>"
    elif ok:
        msg = f"<div class='help'>{html_escape(ok)}</div>"

    return f"""
  <section class="card control-panel-section currency-conversion-panel">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'currency_conversion'))}</div>
      <div class="ts">USD/HKD</div>
    </div>

    {msg}

    <form method="post" action="/submit-currency-conversion" id="currency-conversion-form">
      <div>
        <label for="conversion_account_id">{html_escape(t(lang,'account'))}</label>
        <select id="conversion_account_id" name="account_id" required>
          {conversion_account_opts}
        </select>
      </div>

      <div class="row">
        <div>
          <label for="source_currency">{html_escape(t(lang,'source_currency'))}</label>
          <select id="source_currency" name="source_currency" required>
            <option value="HKD">HKD</option>
            <option value="USD">USD</option>
          </select>
        </div>
        <div>
          <label for="target_currency">{html_escape(t(lang,'target_currency'))}</label>
          <select id="target_currency" name="target_currency" required>
            <option value="USD">USD</option>
            <option value="HKD">HKD</option>
          </select>
        </div>
      </div>

      <div>
        <label for="source_amount">{html_escape(t(lang,'source_amount'))}</label>
        <input id="source_amount" name="source_amount" placeholder="2000" inputmode="decimal" required />
      </div>

      <div class="help">
        {html_escape(t(lang,'conversion_balances'))}: <b id="conversion-balances">-</b>
      </div>
      <div class="help">{html_escape(t(lang,'conversion_help'))}</div>

      <button class="btn btn-blue" type="submit">{html_escape(t(lang,'submit_conversion'))}</button>
    </form>
  </section>
"""


def _render_currency_conversion_script() -> str:
    return """
    <script>
      (function(){
        const form = document.getElementById("currency-conversion-form");
        if (!form) return;
        const accountEl = document.getElementById("conversion_account_id");
        const sourceEl = document.getElementById("source_currency");
        const targetEl = document.getElementById("target_currency");
        const balancesEl = document.getElementById("conversion-balances");
        let accountsCache = null;

        function fmtCurrency(currency, value) {
          const n = Number(value || 0);
          if (!isFinite(n)) return currency + " -";
          return currency + " " + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        }

        function keepOppositeCurrency(changedEl) {
          if (!sourceEl || !targetEl || sourceEl.value !== targetEl.value) return;
          if (changedEl === sourceEl) {
            targetEl.value = sourceEl.value === "HKD" ? "USD" : "HKD";
          } else {
            sourceEl.value = targetEl.value === "HKD" ? "USD" : "HKD";
          }
        }

        function renderBalances() {
          if (!balancesEl) return;
          const accounts = accountsCache && Array.isArray(accountsCache.accounts) ? accountsCache.accounts : [];
          const account = accounts.find((row) => row.account_id === (accountEl ? accountEl.value : ""));
          const cash = account && account.cash_by_currency ? account.cash_by_currency : {};
          balancesEl.textContent = [fmtCurrency("USD", cash.USD), fmtCurrency("HKD", cash.HKD)].join(" | ");
        }

        async function refreshBalances() {
          try {
            const resp = await fetch("/api/accounts", { cache: "no-store" });
            if (resp.ok) accountsCache = await resp.json();
          } catch (e) {
            // ignore transient errors
          }
          renderBalances();
        }

        if (accountEl) accountEl.addEventListener("change", renderBalances);
        if (sourceEl) sourceEl.addEventListener("change", function(){ keepOppositeCurrency(sourceEl); });
        if (targetEl) targetEl.addEventListener("change", function(){ keepOppositeCurrency(targetEl); });

        refreshBalances();
        setInterval(refreshBalances, 5000);
      })();
    </script>
"""


def render_currency_conversion_page(
    lang: str,
    account_metas: Dict[str, AccountMeta],
    error: str = "",
    ok: str = "",
) -> str:
    return f"""
  <div class="control-panel-grid currency-conversion-grid">
    <div class="control-panel-column">
      {_render_currency_conversion_panel(lang, account_metas, error=error, ok=ok)}
    </div>
  </div>

  {_render_currency_conversion_script()}
"""


def render_trading_status_page(
    lang: str,
    accounts: Dict[str, AccountSnapshot],
    account_metas: Dict[str, AccountMeta],
    error: str = "",
    ok: str = "",
) -> str:
    msg = ""
    if error:
        msg = f"<div class='warn'>{html_escape(error)}</div>"
    elif ok:
        msg = f"<div class='help'>{html_escape(ok)}</div>"

    rows: List[str] = []
    for account_id in sorted(account_metas.keys(), key=lambda aid: account_metas[aid].num_id):
        meta = account_metas[account_id]
        snapshot = accounts.get(account_id)
        has_snapshot = snapshot is not None
        enabled = bool(snapshot.trading_enabled) if snapshot is not None else False
        status_key = "trading_on" if enabled else ("trading_off" if has_snapshot else "trading_unknown")
        status_cls = "status-on" if enabled else ("status-off" if has_snapshot else "status-unknown")
        on_disabled = " disabled" if enabled else ""
        off_disabled = " disabled" if has_snapshot and not enabled else ""
        meta_parts = [meta.broker, meta.trading_medium]
        if meta.machine_alias:
            meta_parts.append(meta.machine_alias)
        if meta.ip_address:
            meta_parts.append(meta.ip_address)
        if meta.broker_id:
            meta_parts.append(meta.broker_id)
        meta_line = " · ".join(part for part in meta_parts if part)
        rows.append(
            f"""
        <div class="status-item trading-status-row" data-account-id="{html_escape(account_id)}">
          <div>
            <div class="status-title">#{meta.num_id} {html_escape(account_id)}</div>
            <div class="status-meta">{html_escape(meta_line)}</div>
            <div class="status-row">
              {html_escape(t(lang,'trading'))}:
              <span class="status-pill {status_cls}" data-role="status-pill">{html_escape(t(lang,status_key))}</span>
            </div>
          </div>
          <div class="inline-actions">
            <form class="inline-form" method="post" action="/submit-trading-status">
              <input type="hidden" name="account_id" value="{html_escape(account_id)}" />
              <input type="hidden" name="trading_enabled" value="true" />
              <button class="btn btn-green" type="submit" data-role="enable-btn"{on_disabled}>{html_escape(t(lang,'turn_on'))}</button>
            </form>
            <form class="inline-form" method="post" action="/submit-trading-status">
              <input type="hidden" name="account_id" value="{html_escape(account_id)}" />
              <input type="hidden" name="trading_enabled" value="false" />
              <button class="btn btn-red" type="submit" data-role="disable-btn"{off_disabled}>{html_escape(t(lang,'turn_off'))}</button>
            </form>
          </div>
        </div>
"""
        )

    return f"""
  <section class="card control-panel-section" style="max-width: 920px;">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'trading_status'))}</div>
      <div class="ts">{html_escape(t(lang,'current_status'))}</div>
    </div>

    {msg}

    <div class="inline-actions" style="margin-bottom: 12px;">
      <form class="inline-form" method="post" action="/submit-trading-status">
        <input type="hidden" name="disable_all" value="true" />
        <input type="hidden" name="trading_enabled" value="false" />
        <button class="btn btn-red" type="submit">{html_escape(t(lang,'turn_off_all'))}</button>
      </form>
    </div>

    <div class="status-list">
      {''.join(rows) if rows else f"<em>{html_escape(t(lang,'no_data'))}</em>"}
    </div>
  </section>

  <script>
    (function(){{
      const labels = {{
        on: "{html_escape(t(lang,'trading_on'))}",
        off: "{html_escape(t(lang,'trading_off'))}",
        unknown: "{html_escape(t(lang,'trading_unknown'))}"
      }};

      function applyStatus(row, enabled, known) {{
        const pill = row.querySelector("[data-role='status-pill']");
        const enableBtn = row.querySelector("[data-role='enable-btn']");
        const disableBtn = row.querySelector("[data-role='disable-btn']");
        if (pill) {{
          pill.textContent = known ? (enabled ? labels.on : labels.off) : labels.unknown;
          pill.classList.toggle("status-on", known && enabled);
          pill.classList.toggle("status-off", !known || !enabled);
        }}
        if (enableBtn) enableBtn.disabled = known && enabled;
        if (disableBtn) disableBtn.disabled = known && !enabled;
      }}

      async function refreshTradingStatus() {{
        try {{
          const resp = await fetch("/api/accounts", {{ cache: "no-store" }});
          if (!resp.ok) return;
          const payload = await resp.json();
          const accounts = Array.isArray(payload.accounts) ? payload.accounts : [];
          document.querySelectorAll(".trading-status-row").forEach((row) => {{
            const accountId = row.getAttribute("data-account-id") || "";
            const account = accounts.find((item) => item.account_id === accountId);
            applyStatus(row, !!(account && account.trading_enabled), !!account);
          }});
        }} catch (e) {{
          // ignore transient errors
        }}
      }}

      refreshTradingStatus();
      setInterval(refreshTradingStatus, 5000);
    }})();
  </script>
"""


def render_control_panel_page(
    lang: str,
    account_metas: Dict[str, AccountMeta],
    symbols: List[str],
    error: str = "",
    ok: str = "",
) -> str:
    sorted_account_ids = sorted(account_metas.keys(), key=lambda aid: account_metas[aid].num_id)
    account_opts = "\n".join(
        (
            f"<option value='{html_escape(aid)}'>"
            f"#{account_metas[aid].num_id} {html_escape(aid)} ({html_escape(account_metas[aid].broker)})"
            "</option>"
        )
        for aid in sorted_account_ids
    )
    symbol_opts = "\n".join(
        f"<option value='{html_escape(sym)}'>{html_escape(sym)}</option>"
        for sym in sorted(symbols)
    )
    quick_account_buttons = "\n".join(
        (
            f"<input class='choice-input' type='checkbox' id='quick-acct-{idx}' name='account_ids' value='{html_escape(aid)}' {'checked' if idx == 1 else ''}>"
            f"<label class='choice-btn' for='quick-acct-{idx}'>#{account_metas[aid].num_id}</label>"
        )
        for idx, aid in enumerate(sorted_account_ids, start=1)
    )
    limit_account_buttons = "\n".join(
        (
            f"<input class='choice-input' type='checkbox' id='limit-acct-{idx}' name='account_ids' value='{html_escape(aid)}' {'checked' if idx == 1 else ''}>"
            f"<label class='choice-btn' for='limit-acct-{idx}'>#{account_metas[aid].num_id}</label>"
        )
        for idx, aid in enumerate(sorted_account_ids, start=1)
    )
    cancel_account_buttons = (
        f"<input class='choice-input cancel-account-choice' type='checkbox' id='cancel-acct-all' name='account_ids' value='__ALL__' data-cancel-all checked>"
        f"<label class='choice-btn' for='cancel-acct-all'>{html_escape(t(lang,'all_accounts'))}</label>"
        + "\n"
        + "\n".join(
            (
                f"<input class='choice-input cancel-account-choice' type='checkbox' id='cancel-acct-{idx}' name='account_ids' value='{html_escape(aid)}' data-cancel-account>"
                f"<label class='choice-btn' for='cancel-acct-{idx}' title='{html_escape(_account_server_title(account_metas[aid]))}'>{html_escape(_account_server_label(account_metas[aid]))}</label>"
            )
            for idx, aid in enumerate(sorted_account_ids, start=1)
        )
    )
    sorted_symbols = sorted(symbols)
    quick_symbol_buttons = "\n".join(
        (
            f"<input class='choice-input' type='radio' id='quick-sym-{idx}' name='symbol' value='{html_escape(sym)}' {'checked' if idx == 1 else ''}>"
            f"<label class='choice-btn' for='quick-sym-{idx}'>{html_escape(sym)}</label>"
        )
        for idx, sym in enumerate(sorted_symbols, start=1)
    )
    quick_side_buttons = (
        f"<input class='choice-input' type='radio' id='quick-side-buy' name='side' value='BUY' checked>"
        f"<label class='choice-btn' for='quick-side-buy'>{html_escape(t(lang,'buy'))}</label>"
        f"<input class='choice-input' type='radio' id='quick-side-sell' name='side' value='SELL'>"
        f"<label class='choice-btn' for='quick-side-sell'>{html_escape(t(lang,'sell'))}</label>"
    )
    market_side_buttons = (
        f"<input class='choice-input' type='radio' id='market-side-buy' name='side' value='BUY' checked>"
        f"<label class='choice-btn' for='market-side-buy'>{html_escape(t(lang,'buy'))}</label>"
        f"<input class='choice-input' type='radio' id='market-side-sell' name='side' value='SELL'>"
        f"<label class='choice-btn' for='market-side-sell'>{html_escape(t(lang,'sell'))}</label>"
    )
    delayed_side_buttons = (
        f"<input class='choice-input' type='radio' id='delayed-side-buy' name='side' value='BUY' checked>"
        f"<label class='choice-btn' for='delayed-side-buy'>{html_escape(t(lang,'buy'))}</label>"
        f"<input class='choice-input' type='radio' id='delayed-side-sell' name='side' value='SELL'>"
        f"<label class='choice-btn' for='delayed-side-sell'>{html_escape(t(lang,'sell'))}</label>"
    )
    limit_side_buttons = (
        f"<input class='choice-input' type='radio' id='limit-side-buy' name='side' value='BUY' checked>"
        f"<label class='choice-btn' for='limit-side-buy'>{html_escape(t(lang,'buy'))}</label>"
        f"<input class='choice-input' type='radio' id='limit-side-sell' name='side' value='SELL'>"
        f"<label class='choice-btn' for='limit-side-sell'>{html_escape(t(lang,'sell'))}</label>"
    )
    limit_share_buttons = "\n".join(
        f"<button class='choice-btn' type='button' data-limit-shares='{shares}'>{shares}</button>"
        for shares in (3000, 5000, 10000, 20000, 30000, 50000)
    )
    limit_through_market_buttons = (
        f"<input class='choice-input' type='radio' id='limit-through-none' name='through_market_pct' value='' checked>"
        f"<label class='choice-btn' for='limit-through-none'>{html_escape(t(lang,'none'))}</label>"
        f"<input class='choice-input' type='radio' id='limit-through-1' name='through_market_pct' value='1'>"
        f"<label class='choice-btn' for='limit-through-1'>1%</label>"
        f"<input class='choice-input' type='radio' id='limit-through-5' name='through_market_pct' value='5'>"
        f"<label class='choice-btn' for='limit-through-5'>5%</label>"
        f"<input class='choice-input' type='radio' id='limit-through-10' name='through_market_pct' value='10'>"
        f"<label class='choice-btn' for='limit-through-10'>10%</label>"
        f"<input class='choice-input' type='radio' id='limit-through-20' name='through_market_pct' value='20'>"
        f"<label class='choice-btn' for='limit-through-20'>20%</label>"
    )
    algo_end_time_buttons = (
        f"<button class='choice-btn' type='button' data-minutes='1'>{html_escape(t(lang,'plus_1m'))}</button>"
        f"<button class='choice-btn' type='button' data-minutes='2'>{html_escape(t(lang,'plus_2m'))}</button>"
        f"<button class='choice-btn' type='button' data-minutes='5'>{html_escape(t(lang,'plus_5m'))}</button>"
        f"<button class='choice-btn' type='button' data-minutes='10'>{html_escape(t(lang,'plus_10m'))}</button>"
    )
    delayed_delay_buttons = (
        f"<input class='choice-input' type='radio' id='delay-min-1' name='delay_choice' value='1' checked>"
        f"<label class='choice-btn' for='delay-min-1'>{html_escape(t(lang,'delay_1m'))}</label>"
        f"<input class='choice-input' type='radio' id='delay-min-2' name='delay_choice' value='2'>"
        f"<label class='choice-btn' for='delay-min-2'>{html_escape(t(lang,'delay_2m'))}</label>"
        f"<input class='choice-input' type='radio' id='delay-min-5' name='delay_choice' value='5'>"
        f"<label class='choice-btn' for='delay-min-5'>{html_escape(t(lang,'delay_5m'))}</label>"
        f"<input class='choice-input' type='radio' id='delay-min-10' name='delay_choice' value='10'>"
        f"<label class='choice-btn' for='delay-min-10'>{html_escape(t(lang,'delay_10m'))}</label>"
        f"<input class='choice-input' type='radio' id='delay-custom' name='delay_choice' value='custom'>"
        f"<label class='choice-btn' for='delay-custom'>{html_escape(t(lang,'delay_custom'))}</label>"
    )
    stop_account_buttons = "\n".join(
        (
            f"<input class='choice-input' type='checkbox' id='stop-acct-{idx}' name='account_ids' value='{html_escape(aid)}'>"
            f"<label class='choice-btn' for='stop-acct-{idx}'>#{account_metas[aid].num_id}</label>"
        )
        for idx, aid in enumerate(sorted_account_ids, start=1)
    )

    fast_account_buttons = "\n".join(
        (
            f"<label class='fast-account-row'>"
            f"<input type='checkbox' data-fast-account-id='{html_escape(aid)}'>"
            f"<span class='fast-account-label'>#{account_metas[aid].num_id} "
            f"{html_escape(aid)} ({html_escape(account_metas[aid].broker)})</span>"
            f"</label>"
        )
        for aid in sorted_account_ids
    )
    fast_labels = {
        "modeE": t(lang, "mode_e"),
        "modeF": t(lang, "mode_f"),
        "fastSelectedMode": t(lang, "fast_selected_mode"),
        "fastBuyPriceLimit": t(lang, "fast_buy_price_limit"),
        "fastSellPriceLimit": t(lang, "fast_sell_price_limit"),
        "fastConfigRequired": t(lang, "fast_config_required"),
        "fastPriceLimitPositive": t(lang, "fast_price_limit_positive"),
        "fastAccountsRequired": t(lang, "fast_accounts_required"),
        "fastAggressionLevelInvalid": t(lang, "fast_aggression_level_invalid"),
    }
    fast_labels_json = json.dumps(fast_labels).replace("<", "\\u003c")
    fast_trading_modal = f"""
    <div class="modal-backdrop" id="fast-trading-modal" hidden>
      <div class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="fast-trading-title">
        <div class="modal-header">
          <div>
            <div class="acct" id="fast-trading-title">{html_escape(t(lang,'fast_config_title'))}</div>
            <div class="help" id="fast-trading-mode-label"></div>
          </div>
          <button class="modal-close" id="fast-trading-close" type="button" aria-label="{html_escape(t(lang,'fast_config_cancel'))}">&times;</button>
        </div>
        <div>
          <label id="fast-price-limit-label" for="fast-price-limit">{html_escape(t(lang,'fast_buy_price_limit'))}</label>
          <input id="fast-price-limit" type="number" min="0.01" step="0.01" inputmode="decimal">
        </div>
        <div style="margin-top:12px;">
          <label>{html_escape(t(lang,'fast_accounts'))}</label>
          <div class="fast-account-list" id="fast-trading-accounts">
            {fast_account_buttons}
          </div>
        </div>
        <div style="margin-top:12px;">
          <label>{html_escape(t(lang,'fast_aggression_level'))}</label>
          <div class="choice-grid">
            <input class="choice-input" type="radio" id="fast-aggression-1" name="fast_aggression_level" value="1" checked>
            <label class="choice-btn" for="fast-aggression-1">{html_escape(t(lang,'fast_aggression_1'))}</label>
            <input class="choice-input" type="radio" id="fast-aggression-2" name="fast_aggression_level" value="2">
            <label class="choice-btn" for="fast-aggression-2">{html_escape(t(lang,'fast_aggression_2'))}</label>
            <input class="choice-input" type="radio" id="fast-aggression-3" name="fast_aggression_level" value="3">
            <label class="choice-btn" for="fast-aggression-3">{html_escape(t(lang,'fast_aggression_3'))}</label>
          </div>
        </div>
        <label class="fast-account-row" style="margin-top:12px;">
          <input type="checkbox" id="fast-test-mode">
          <span class="fast-account-label">{html_escape(t(lang,'fast_test_mode'))}</span>
        </label>
        <div class="warn" id="fast-trading-error" style="margin-top:10px;"></div>
        <div class="modal-actions">
          <button class="btn" id="fast-cancel" type="button" style="background:#64748b;">{html_escape(t(lang,'fast_config_cancel'))}</button>
          <button class="btn btn-green" id="fast-submit-config" type="button">{html_escape(t(lang,'fast_config_submit'))}</button>
        </div>
      </div>
    </div>
    """
    fast_trading_script = """
    <script>
      (function(){
        const form = document.getElementById("algo-form");
        const configEl = document.getElementById("fast_trading_config");
        const modal = document.getElementById("fast-trading-modal");
        const genericConfigEl = document.getElementById("algo-generic-constraints");
        const modeLabelEl = document.getElementById("fast-trading-mode-label");
        const priceLabelEl = document.getElementById("fast-price-limit-label");
        const priceLimitEl = document.getElementById("fast-price-limit");
        const errorEl = document.getElementById("fast-trading-error");
        const cancelBtn = document.getElementById("fast-cancel");
        const closeBtn = document.getElementById("fast-trading-close");
        const submitBtn = document.getElementById("fast-submit-config");
        const testModeEl = document.getElementById("fast-test-mode");
        const labels = __FAST_LABELS__;

        if (!form || !configEl || !modal || !priceLimitEl) return;

        let confirmed = false;
        let lastFocused = null;

        function selectedMode() {
          const selected = form.querySelector("input[name='trading_mode']:checked");
          return selected ? selected.value : "";
        }

        function modeName(mode) {
          if (mode === "E") return labels.modeE || "E";
          if (mode === "F") return labels.modeF || "F";
          return mode;
        }

        function setError(message) {
          if (errorEl) errorEl.textContent = message || "";
        }

        function validateConfig() {
          const priceLimit = Number(priceLimitEl.value);
          if (!isFinite(priceLimit) || priceLimit <= 0) {
            return { ok: false, error: labels.fastPriceLimitPositive };
          }
          const accountIds = Array.from(modal.querySelectorAll("[data-fast-account-id]:checked"))
            .map(function(input) { return input.getAttribute("data-fast-account-id") || ""; })
            .filter(Boolean);
          if (!accountIds.length) return { ok: false, error: labels.fastAccountsRequired };
          const aggressionEl = modal.querySelector("input[name='fast_aggression_level']:checked");
          const aggressionLevel = aggressionEl ? Number(aggressionEl.value) : 0;
          if (![1, 2, 3].includes(aggressionLevel)) {
            return { ok: false, error: labels.fastAggressionLevelInvalid };
          }

          return {
            ok: true,
            payload: {
              price_limit: priceLimit,
              account_ids: accountIds,
              aggression_level: aggressionLevel,
              test_mode: Boolean(testModeEl && testModeEl.checked)
            }
          };
        }

        function openModal(mode) {
          lastFocused = document.activeElement;
          if (modeLabelEl) modeLabelEl.textContent = (labels.fastSelectedMode || "Mode") + ": " + modeName(mode);
          if (priceLabelEl) {
            priceLabelEl.textContent = mode === "F" ? labels.fastSellPriceLimit : labels.fastBuyPriceLimit;
          }
          setError("");
          modal.hidden = false;
          document.body.classList.add("modal-open");
          priceLimitEl.focus();
        }

        function closeModal() {
          modal.hidden = true;
          document.body.classList.remove("modal-open");
          setError("");
          if (lastFocused && typeof lastFocused.focus === "function") lastFocused.focus();
        }

        form.addEventListener("submit", function(event) {
          const mode = selectedMode();
          if (mode !== "E" && mode !== "F") {
            configEl.value = "";
            return;
          }
          if (confirmed) {
            confirmed = false;
            return;
          }
          event.preventDefault();
          openModal(mode);
        });

        function updateGenericVisibility() {
          const mode = selectedMode();
          if (genericConfigEl) genericConfigEl.hidden = mode === "E" || mode === "F";
        }
        form.querySelectorAll("input[name='trading_mode']").forEach(function(input) {
          input.addEventListener("change", updateGenericVisibility);
        });
        updateGenericVisibility();

        if (cancelBtn) cancelBtn.addEventListener("click", closeModal);
        if (closeBtn) closeBtn.addEventListener("click", closeModal);
        modal.addEventListener("click", function(event) {
          if (event.target === modal) closeModal();
        });
        document.addEventListener("keydown", function(event) {
          if (event.key === "Escape" && !modal.hidden) closeModal();
        });
        if (submitBtn) submitBtn.addEventListener("click", function() {
          const result = validateConfig();
          if (!result.ok) {
            setError(result.error || labels.fastConfigRequired);
            return;
          }
          configEl.value = JSON.stringify(result.payload);
          confirmed = true;
          closeModal();
          if (typeof form.requestSubmit === "function") {
            form.requestSubmit();
          } else {
            form.submit();
          }
        });

      })();
    </script>
    """.replace("__FAST_LABELS__", fast_labels_json)

    msg = ""
    if error:
        msg = f"<div class='warn'>{html_escape(error)}</div>"
    elif ok:
        msg = f"<div class='help'>{html_escape(ok)}</div>"

    default_end_time = "2099-12-31T00:00"

    # IMPORTANT:
    # - Separate forms, not nested
    # - All trade actions explicitly use method="post"
    return f"""
  <div class="control-panel-grid">
    <div class="control-panel-column">
      <section class="card control-panel-section">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'market_order'))}</div>
      <div class="ts">—</div>
    </div>

    {msg}

    <form method="post" action="/submit-order">
      <div class="row">
        <div>
          <label for="account_id">{html_escape(t(lang,'account'))}</label>
          <select id="account_id" name="account_id" required>
            {account_opts}
          </select>
        </div>
        <div>
          <label for="symbol">{html_escape(t(lang,'symbol'))}</label>
          <input id="symbol" name="symbol" list="symbol-suggestions" placeholder="e.g. AAPL" required />
        </div>
      </div>

      <div class="row">
        <div>
          <label>{html_escape(t(lang,'side'))}</label>
          <div class="choice-grid">
            {market_side_buttons}
          </div>
        </div>
        <div></div>
      </div>

      <div class="row">
        <div>
          <label for="shares">{html_escape(t(lang,'shares'))}</label>
          <input id="shares" name="shares" placeholder="e.g. 100" inputmode="numeric" />
          <div class="help">{html_escape(t(lang,'either_or'))}</div>
        </div>
        <div>
          <label for="dollar_amount">{html_escape(t(lang,'dollars'))}</label>
          <input id="dollar_amount" name="dollar_amount" placeholder="e.g. 2500" inputmode="decimal" />
          <div class="help">{html_escape(t(lang,'notional_hint'))}</div>
        </div>
      </div>

      <div class="help">
        {html_escape(t(lang,'selected_account_cash'))}: <b id="market-selected-cash">—</b>
      </div>

      <button class="btn btn-blue" type="submit">{html_escape(t(lang,'submit_order'))}</button>
      <div class="help">{html_escape(t(lang,'note_no_auth'))}</div>
    </form>
      </section>

      <section class="card control-panel-section">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'delayed_market_order'))}</div>
      <div class="ts">—</div>
    </div>

    <form method="post" action="/submit-delayed-order">
      <div class="row">
        <div>
          <label for="delayed_account_id">{html_escape(t(lang,'account'))}</label>
          <select id="delayed_account_id" name="account_id" required>
            {account_opts}
          </select>
        </div>
        <div>
          <label for="delayed_symbol">{html_escape(t(lang,'symbol'))}</label>
          <input id="delayed_symbol" name="symbol" list="symbol-suggestions" placeholder="e.g. AAPL" required />
        </div>
      </div>

      <div class="row">
        <div>
          <label>{html_escape(t(lang,'side'))}</label>
          <div class="choice-grid">
            {delayed_side_buttons}
          </div>
        </div>
        <div>
          <label>{html_escape(t(lang,'delay_when'))}</label>
          <div class="choice-grid">
            {delayed_delay_buttons}
          </div>
        </div>
      </div>

      <div class="row">
        <div>
          <label for="delayed_shares">{html_escape(t(lang,'shares'))}</label>
          <input id="delayed_shares" name="shares" placeholder="e.g. 100" inputmode="numeric" />
          <div class="help">{html_escape(t(lang,'either_or'))}</div>
        </div>
        <div>
          <label for="delayed_dollar_amount">{html_escape(t(lang,'dollars'))}</label>
          <input id="delayed_dollar_amount" name="dollar_amount" placeholder="e.g. 2500" inputmode="decimal" />
          <div class="help">{html_escape(t(lang,'notional_hint'))}</div>
        </div>
      </div>

      <div>
        <label for="execute_at">{html_escape(t(lang,'delay_future_time'))}</label>
        <input id="execute_at" name="execute_at" type="datetime-local" />
        <div class="help">{html_escape(t(lang,'delay_future_time_help'))}</div>
      </div>

      <div class="help">
        {html_escape(t(lang,'selected_account_cash'))}: <b id="delayed-selected-cash">—</b>
      </div>

      <button class="btn btn-blue" type="submit">{html_escape(t(lang,'submit_delayed_order'))}</button>
    </form>
      </section>

      <section class="card control-panel-section">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'quick_market_order'))}</div>
      <div class="ts">-</div>
    </div>

    <form method="post" action="/submit-quick-order">
      <div>
        <label>{html_escape(t(lang,'quick_accounts'))}</label>
        <div class="choice-grid">
          {quick_account_buttons}
        </div>
      </div>

      <div>
        <label>{html_escape(t(lang,'quick_symbol'))}</label>
        <div class="choice-grid">
          {quick_symbol_buttons}
        </div>
      </div>

      <div>
        <label>{html_escape(t(lang,'quick_side'))}</label>
        <div class="choice-grid">
          {quick_side_buttons}
        </div>
      </div>

      <div>
        <label for="quick_dollar_amount">{html_escape(t(lang,'quick_dollars'))}</label>
        <input id="quick_dollar_amount" name="dollar_amount" placeholder="10000" inputmode="decimal" />
      </div>

      <div class="help">
        {html_escape(t(lang,'quick_selected_cash'))}: <b id="quick-selected-cash">—</b>
        &nbsp;|&nbsp;
        {html_escape(t(lang,'quick_selected_last'))}: <b id="quick-selected-last">—</b>
      </div>

      <button class="btn btn-blue" type="submit">{html_escape(t(lang,'submit_order'))}</button>
    </form>
      </section>
    </div>

    <div class="control-panel-column">
      <section class="card control-panel-section">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'limit_order'))}</div>
      <div class="ts">-</div>
    </div>

    <form method="post" action="/submit-limit-order" id="limit-order-form">
      <div>
        <label>{html_escape(t(lang,'limit_accounts'))}</label>
        <div class="choice-grid">
          {limit_account_buttons}
        </div>
      </div>

      <div class="row">
        <div>
          <label for="limit_symbol">{html_escape(t(lang,'limit_symbol'))}</label>
          <select id="limit_symbol" name="symbol" required>
            {symbol_opts}
          </select>
        </div>
        <div>
          <label>{html_escape(t(lang,'limit_side'))}</label>
          <div class="choice-grid">
            {limit_side_buttons}
          </div>
        </div>
      </div>

      <div class="row">
        <div>
          <label for="limit_shares">{html_escape(t(lang,'shares'))}</label>
          <input id="limit_shares" name="shares" placeholder="e.g. 100" inputmode="numeric" required />
          <div class="choice-grid" style="margin-top:8px;" aria-label="{html_escape(t(lang,'limit_quick_shares'))}">
            {limit_share_buttons}
          </div>
        </div>
        <div>
          <label for="limit_price">{html_escape(t(lang,'limit_price'))}</label>
          <input id="limit_price" name="limit_price" placeholder="e.g. 182.50" inputmode="decimal" />
          <div class="help">{html_escape(t(lang,'limit_price_precedence'))}</div>
        </div>
      </div>

      <div>
        <label>{html_escape(t(lang,'through_market_pct'))}</label>
        <div class="choice-grid">
          {limit_through_market_buttons}
        </div>
      </div>

      <div class="help">
        {html_escape(t(lang,'selected_account_cash'))}: <b id="limit-selected-cash">-</b>
        &nbsp;|&nbsp;
        {html_escape(t(lang,'quick_selected_last'))}: <b id="limit-selected-last">-</b>
      </div>

      <button class="btn btn-blue" type="submit">{html_escape(t(lang,'submit_limit_order'))}</button>
    </form>
      </section>

      <section class="card control-panel-section">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'algo_title'))}</div>
      <div class="ts">—</div>
    </div>

    <form method="post" action="/submit-algo" id="algo-form">
      <input type="hidden" id="fast_trading_config" name="fast_trading_config" value="">
      <div class="row">
        <div>
          <label>{html_escape(t(lang,'trading_mode'))}</label>
          <div class="choice-grid">
            <input class="choice-input" type="radio" id="algo-mode-a" name="trading_mode" value="A" checked>
            <label class="choice-btn" for="algo-mode-a">{html_escape(t(lang,'mode_a'))}</label>
            <input class="choice-input" type="radio" id="algo-mode-b" name="trading_mode" value="B">
            <label class="choice-btn" for="algo-mode-b">{html_escape(t(lang,'mode_b'))}</label>
            <input class="choice-input" type="radio" id="algo-mode-c" name="trading_mode" value="C">
            <label class="choice-btn" for="algo-mode-c">{html_escape(t(lang,'mode_c'))}</label>
            <input class="choice-input" type="radio" id="algo-mode-d" name="trading_mode" value="D">
            <label class="choice-btn" for="algo-mode-d">{html_escape(t(lang,'mode_d'))}</label>
            <input class="choice-input" type="radio" id="algo-mode-e" name="trading_mode" value="E">
            <label class="choice-btn" for="algo-mode-e">{html_escape(t(lang,'mode_e'))}</label>
            <input class="choice-input" type="radio" id="algo-mode-f" name="trading_mode" value="F">
            <label class="choice-btn" for="algo-mode-f">{html_escape(t(lang,'mode_f'))}</label>
          </div>
        </div>
        <div>
          <label for="algo_symbol">{html_escape(t(lang,'symbol'))}</label>
          <select id="algo_symbol" name="symbol" required>
            {symbol_opts}
          </select>
        </div>
      </div>

      <div id="algo-generic-constraints">
      <div class="row">
        <div>
          <label for="max_volume">{html_escape(t(lang,'max_volume'))}</label>
          <input id="max_volume" name="max_volume" value="-1" inputmode="decimal" />
        </div>
        <div>
          <label for="mkt_vol_target">{html_escape(t(lang,'mkt_vol_target'))}</label>
          <input id="mkt_vol_target" name="market_volume_target" value="-1" inputmode="decimal" />
        </div>
      </div>

      <div class="row">
        <div>
          <label for="end_time_et">{html_escape(t(lang,'end_time_et'))}</label>
          <input id="end_time_et" name="end_time_et" type="datetime-local" value="{default_end_time}" />
          <div class="help">{html_escape(t(lang,'algo_end_quick'))}</div>
          <div class="choice-grid" id="algo-end-time-quick">
            {algo_end_time_buttons}
          </div>
          <div class="help">America/New_York</div>
        </div>
        <div>
          <label for="abs_pos_change_limit">{html_escape(t(lang,'abs_pos_change_limit'))}</label>
          <input id="abs_pos_change_limit" name="abs_pos_change_limit" value="-1" inputmode="decimal" />
        </div>
      </div>

      <div>
        <label for="price_target">{html_escape(t(lang,'price_target'))}</label>
        <input id="price_target" name="price_target" value="0" inputmode="decimal" />
      </div>

      <div class="row">
        <div>
          <label for="single_order_notional_limit">{html_escape(t(lang,'single_order_notional_limit'))}</label>
          <input id="single_order_notional_limit" name="single_order_notional_limit" value="-1" inputmode="decimal" />
        </div>
        <div>
          <label for="order_rate_limit_per_minute">{html_escape(t(lang,'order_rate_limit_per_minute'))}</label>
          <input id="order_rate_limit_per_minute" name="order_rate_limit_per_minute" value="-1" inputmode="decimal" />
        </div>
      </div>
      </div>

      <button class="btn btn-green" type="submit">{html_escape(t(lang,'algo_submit'))}</button>
    </form>
      </section>

      {fast_trading_modal}

      <section class="card control-panel-section">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'algo_stop_title'))}</div>
      <div class="ts">—</div>
    </div>

    <form method="post" action="/submit-algo-stop">
      <div class="row">
        <div>
          <label>{html_escape(t(lang,'trading_mode'))}</label>
          <div class="choice-grid">
            <input class="choice-input" type="radio" id="stop-mode-a" name="trading_mode" value="A" checked>
            <label class="choice-btn" for="stop-mode-a">A</label>
            <input class="choice-input" type="radio" id="stop-mode-b" name="trading_mode" value="B">
            <label class="choice-btn" for="stop-mode-b">B</label>
            <input class="choice-input" type="radio" id="stop-mode-c" name="trading_mode" value="C">
            <label class="choice-btn" for="stop-mode-c">C</label>
            <input class="choice-input" type="radio" id="stop-mode-d" name="trading_mode" value="D">
            <label class="choice-btn" for="stop-mode-d">D</label>
            <input class="choice-input" type="radio" id="stop-mode-e" name="trading_mode" value="E">
            <label class="choice-btn" for="stop-mode-e">E</label>
            <input class="choice-input" type="radio" id="stop-mode-f" name="trading_mode" value="F">
            <label class="choice-btn" for="stop-mode-f">F</label>
          </div>
        </div>
        <div>
          <label>{html_escape(t(lang,'algo_stop_accounts'))}</label>
          <div class="choice-grid">
            {stop_account_buttons}
          </div>
          <div class="help">{html_escape(t(lang,'algo_stop_accounts_help'))}</div>
        </div>
      </div>

      <div>
        <label for="stop_reason">{html_escape(t(lang,'stop_reason'))}</label>
        <input id="stop_reason" name="reason" placeholder="e.g. risk limit hit" />
      </div>

      <button class="btn btn-red" type="submit">{html_escape(t(lang,'algo_stop'))}</button>
    </form>
      </section>

      <section class="card control-panel-section">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'cancel_orders_title'))}</div>
      <div class="ts">-</div>
    </div>

    <form method="post" action="/submit-cancel-open-orders" id="cancel-open-orders-form">
      <div class="row">
        <div>
          <label>{html_escape(t(lang,'cancel_accounts'))}</label>
          <div class="choice-grid">
            {cancel_account_buttons}
          </div>
        </div>
        <div>
          <label for="cancel_symbol">{html_escape(t(lang,'cancel_symbol'))}</label>
          <input id="cancel_symbol" name="symbol" list="symbol-suggestions" placeholder="{html_escape(t(lang,'cancel_symbol_placeholder'))}" />
        </div>
      </div>
      <div class="help">{html_escape(t(lang,'cancel_orders_help'))}</div>
      <button class="btn btn-blue" type="submit">{html_escape(t(lang,'cancel_open_orders'))}</button>
    </form>
      </section>
    </div>
  </div>

    <script>
      (function(){{
        const cancelForm = document.getElementById("cancel-open-orders-form");
        if (cancelForm) {{
          const cancelAll = cancelForm.querySelector("input[data-cancel-all]");
          const cancelAccounts = Array.from(cancelForm.querySelectorAll("input[data-cancel-account]"));
          if (cancelAll) {{
            cancelAll.addEventListener("change", function(){{
              if (cancelAll.checked) {{
                cancelAccounts.forEach(function(input) {{ input.checked = false; }});
              }}
            }});
          }}
          cancelAccounts.forEach(function(input) {{
            input.addEventListener("change", function(){{
              if (input.checked && cancelAll) cancelAll.checked = false;
            }});
          }});
        }}

        const marketAccountEl = document.getElementById("account_id");
        const delayedAccountEl = document.getElementById("delayed_account_id");
        const marketCashEl = document.getElementById("market-selected-cash");
        const delayedCashEl = document.getElementById("delayed-selected-cash");
        let accountsCache = null;

        function fmtMoney(x) {{
          if (x === null || x === undefined) return "—";
          const n = Number(x);
          if (!isFinite(n)) return "—";
          return "$" + n.toLocaleString(undefined, {{
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
          }});
        }}

        function lookupCash(accountId) {{
          const accounts = accountsCache && Array.isArray(accountsCache.accounts) ? accountsCache.accounts : [];
          const account = accounts.find((row) => row.account_id === accountId);
          return account ? fmtMoney(account.cash) : "—";
        }}

        function renderSelectedAccountCash() {{
          if (marketCashEl) {{
            marketCashEl.textContent = lookupCash(marketAccountEl ? marketAccountEl.value : "");
          }}
          if (delayedCashEl) {{
            delayedCashEl.textContent = lookupCash(delayedAccountEl ? delayedAccountEl.value : "");
          }}
        }}

        async function refreshSelectedAccountCash() {{
          try {{
            const resp = await fetch("/api/accounts", {{ cache: "no-store" }});
            if (resp.ok) {{
              accountsCache = await resp.json();
            }}
          }} catch (e) {{
            // ignore transient errors
          }}
          renderSelectedAccountCash();
        }}

        if (marketAccountEl) marketAccountEl.addEventListener("change", renderSelectedAccountCash);
        if (delayedAccountEl) delayedAccountEl.addEventListener("change", renderSelectedAccountCash);

        refreshSelectedAccountCash();
        setInterval(refreshSelectedAccountCash, 5000);
      }})();
    </script>

    <script>
      (function(){{
        const cashEl = document.getElementById("quick-selected-cash");
        const lastEl = document.getElementById("quick-selected-last");
        const quickForm = document.querySelector("form[action='/submit-quick-order']");
        const accountInputs = quickForm ? Array.from(quickForm.querySelectorAll("input[name='account_ids']")) : [];
        const symbolInputs = quickForm ? Array.from(quickForm.querySelectorAll("input[name='symbol']")) : [];
        let accountsCache = null;
        let marketCache = null;

        function fmtMoney(x) {{
          if (x === null || x === undefined) return "—";
          const n = Number(x);
          if (!isFinite(n)) return "—";
          return "$" + n.toLocaleString(undefined, {{
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
          }});
        }}

        function fmtPrice(x) {{
          if (x === null || x === undefined) return "—";
          const n = Number(x);
          if (!isFinite(n)) return "—";
          return n.toLocaleString(undefined, {{
            minimumFractionDigits: 4,
            maximumFractionDigits: 4
          }});
        }}

        function selectedAccountId() {{
          const checked = accountInputs.find((el) => el.checked);
          return checked ? checked.value : "";
        }}

        function selectedSymbol() {{
          const checked = symbolInputs.find((el) => el.checked);
          return checked ? checked.value : "";
        }}

        function renderQuickSummary() {{
          const accountId = selectedAccountId();
          const symbol = selectedSymbol();

          if (cashEl) {{
            const accounts = accountsCache && Array.isArray(accountsCache.accounts) ? accountsCache.accounts : [];
            const account = accounts.find((row) => row.account_id === accountId);
            cashEl.textContent = account ? fmtMoney(account.cash) : "—";
          }}

          if (lastEl) {{
            const rows = marketCache && marketCache.rows ? marketCache.rows : {{}};
            const row = rows[symbol] || null;
            lastEl.textContent = row && !row.error ? fmtPrice(row.last) : "—";
          }}
        }}

        async function refreshQuickSummary() {{
          try {{
            const [accountsResp, marketResp] = await Promise.all([
              fetch("/api/accounts", {{ cache: "no-store" }}),
              fetch("/api/market-data", {{ cache: "no-store" }})
            ]);
            if (accountsResp.ok) {{
              accountsCache = await accountsResp.json();
            }}
            if (marketResp.ok) {{
              marketCache = await marketResp.json();
            }}
          }} catch (e) {{
            // ignore transient errors
          }}
          renderQuickSummary();
        }}

        accountInputs.forEach((el) => el.addEventListener("change", renderQuickSummary));
        symbolInputs.forEach((el) => el.addEventListener("change", renderQuickSummary));

        refreshQuickSummary();
        setInterval(refreshQuickSummary, 5000);
      }})();
    </script>

    <script>
      (function(){{
        const limitForm = document.getElementById("limit-order-form");
        const cashEl = document.getElementById("limit-selected-cash");
        const lastEl = document.getElementById("limit-selected-last");
        const symbolEl = document.getElementById("limit_symbol");
        const sharesEl = document.getElementById("limit_shares");
        const shareButtons = limitForm ? Array.from(limitForm.querySelectorAll("button[data-limit-shares]")) : [];
        const accountInputs = limitForm ? Array.from(limitForm.querySelectorAll("input[name='account_ids']")) : [];
        const sideInputs = limitForm ? Array.from(limitForm.querySelectorAll("input[name='side']")) : [];
        const symbolStorageKey = "trading_ui.limit_order.symbol";
        const accountStorageKey = "trading_ui.limit_order.account_ids";
        const sideStorageKey = "trading_ui.limit_order.side";
        let accountsCache = null;
        let marketCache = null;

        function storageGet(key) {{
          try {{
            return window.localStorage ? window.localStorage.getItem(key) : null;
          }} catch (e) {{
            return null;
          }}
        }}

        function storageSet(key, value) {{
          try {{
            if (window.localStorage) window.localStorage.setItem(key, value);
          }} catch (e) {{
            // ignore unavailable storage
          }}
        }}

        function restoreLimitSymbol() {{
          if (!symbolEl) return;
          const savedSymbol = storageGet(symbolStorageKey);
          if (!savedSymbol) return;
          const hasOption = Array.from(symbolEl.options).some((opt) => opt.value === savedSymbol);
          if (hasOption) symbolEl.value = savedSymbol;
        }}

        function persistLimitSymbol() {{
          if (!symbolEl || !symbolEl.value) return;
          storageSet(symbolStorageKey, symbolEl.value);
        }}

        function parseStoredAccountIds(raw) {{
          if (!raw) return [];
          try {{
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed)) {{
              return parsed.map((value) => String(value || "").trim()).filter(Boolean);
            }}
          }} catch (e) {{
            // fall back to legacy comma-separated values
          }}
          return String(raw).split(",").map((value) => value.trim()).filter(Boolean);
        }}

        function restoreLimitAccounts() {{
          if (!accountInputs.length) return;
          const savedAccounts = parseStoredAccountIds(storageGet(accountStorageKey));
          if (!savedAccounts.length) return;
          const savedSet = new Set(savedAccounts);
          let matched = false;
          accountInputs.forEach((input) => {{
            const shouldCheck = savedSet.has(input.value);
            input.checked = shouldCheck;
            if (shouldCheck) matched = true;
          }});
          if (!matched) {{
            accountInputs.forEach((input, idx) => {{
              input.checked = idx === 0;
            }});
          }}
        }}

        function persistLimitAccounts() {{
          const selectedAccounts = accountInputs.filter((input) => input.checked).map((input) => input.value);
          if (selectedAccounts.length) {{
            storageSet(accountStorageKey, JSON.stringify(selectedAccounts));
          }}
        }}

        function restoreLimitSide() {{
          const savedSide = storageGet(sideStorageKey);
          if (savedSide !== "BUY" && savedSide !== "SELL") return;
          const matchingInput = sideInputs.find((input) => input.value === savedSide);
          if (matchingInput) matchingInput.checked = true;
        }}

        function persistLimitSide() {{
          const selectedSide = sideInputs.find((input) => input.checked);
          if (selectedSide) storageSet(sideStorageKey, selectedSide.value);
        }}

        function syncShareButtons() {{
          if (!sharesEl) return;
          const current = String(sharesEl.value || "").trim();
          shareButtons.forEach((button) => {{
            button.classList.toggle("active", String(button.dataset.limitShares || "") === current);
          }});
        }}

        function fmtMoney(x) {{
          if (x === null || x === undefined) return "-";
          const n = Number(x);
          if (!isFinite(n)) return "-";
          return "$" + n.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
        }}

        function fmtPrice(x) {{
          if (x === null || x === undefined) return "-";
          const n = Number(x);
          if (!isFinite(n)) return "-";
          return n.toLocaleString(undefined, {{ minimumFractionDigits: 4, maximumFractionDigits: 4 }});
        }}

        function renderLimitSummary() {{
          const accounts = accountsCache && Array.isArray(accountsCache.accounts) ? accountsCache.accounts : [];
          const selected = accountInputs.filter((el) => el.checked).map((el) => el.value);
          if (cashEl) {{
            const parts = selected.map((accountId) => {{
              const account = accounts.find((row) => row.account_id === accountId);
              const label = account && account.account_num_id ? "#" + account.account_num_id : accountId;
              return label + " " + (account ? fmtMoney(account.cash) : "-");
            }});
            cashEl.textContent = parts.length ? parts.join(", ") : "-";
          }}
          if (lastEl) {{
            const rows = marketCache && marketCache.rows ? marketCache.rows : {{}};
            const row = rows[symbolEl ? symbolEl.value : ""] || null;
            lastEl.textContent = row && !row.error ? fmtPrice(row.last) : "-";
          }}
        }}

        async function refreshLimitSummary() {{
          try {{
            const [accountsResp, marketResp] = await Promise.all([
              fetch("/api/accounts", {{ cache: "no-store" }}),
              fetch("/api/market-data", {{ cache: "no-store" }})
            ]);
            if (accountsResp.ok) accountsCache = await accountsResp.json();
            if (marketResp.ok) marketCache = await marketResp.json();
          }} catch (e) {{
            // ignore transient errors
          }}
          renderLimitSummary();
        }}

        accountInputs.forEach((el) => el.addEventListener("change", function() {{
          persistLimitAccounts();
          renderLimitSummary();
        }}));
        sideInputs.forEach((el) => el.addEventListener("change", persistLimitSide));
        shareButtons.forEach((button) => {{
          button.addEventListener("click", function() {{
            if (!sharesEl) return;
            sharesEl.value = String(button.dataset.limitShares || "");
            syncShareButtons();
            sharesEl.focus();
          }});
        }});
        if (sharesEl) sharesEl.addEventListener("input", syncShareButtons);
        restoreLimitSymbol();
        restoreLimitAccounts();
        restoreLimitSide();
        if (symbolEl) symbolEl.addEventListener("change", function() {{
          persistLimitSymbol();
          renderLimitSummary();
        }});
        if (limitForm) limitForm.addEventListener("submit", function() {{
          persistLimitSymbol();
          persistLimitAccounts();
          persistLimitSide();
        }});
        syncShareButtons();
        refreshLimitSummary();
        setInterval(refreshLimitSummary, 5000);
      }})();
    </script>

    {fast_trading_script}

    <script>
      (function(){{
        const quickWrap = document.getElementById("algo-end-time-quick");
        const endTimeEl = document.getElementById("end_time_et");
        if (!quickWrap || !endTimeEl) return;

        function pad(value) {{
          return String(value).padStart(2, "0");
        }}

        function toDatetimeLocalValue(date) {{
          return (
            date.getFullYear() + "-" +
            pad(date.getMonth() + 1) + "-" +
            pad(date.getDate()) + "T" +
            pad(date.getHours()) + ":" +
            pad(date.getMinutes())
          );
        }}

        quickWrap.addEventListener("click", function(event) {{
          const target = event.target;
          if (!(target instanceof HTMLElement)) return;
          const minutesRaw = target.getAttribute("data-minutes");
          if (!minutesRaw) return;
          const minutes = Number(minutesRaw);
          if (!isFinite(minutes)) return;

          const next = new Date();
          next.setSeconds(0, 0);
          next.setMinutes(next.getMinutes() + minutes);
          endTimeEl.value = toDatetimeLocalValue(next);
        }});
      }})();
    </script>
  <datalist id="symbol-suggestions">
    {symbol_opts}
  </datalist>
"""


def _fmt_ts(epoch: int | None) -> str:
    if not epoch:
        return ""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    # second-resolution ISO (UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def render_market_data_page(lang: str, symbols: List[str]) -> str:
    rows = []
    for sym in sorted(symbols):
        s = html_escape(sym)
        rows.append(
            "<tr>"
            f"<td><b>{s}</b></td>"
            f"<td style='text-align:right' id='md-{s}-prev'>—</td>"
            f"<td style='text-align:right' id='md-{s}-last'>—</td>"
            f"<td style='text-align:right' id='md-{s}-chg'>—</td>"
            f"<td style='text-align:right' id='md-{s}-chgpx'>—</td>"
            f"<td style='text-align:right' id='md-{s}-shortint'>—</td>"
            f"<td style='text-align:right' id='md-{s}-vol'>—</td>"
            f"<td style='text-align:right' id='md-{s}-asof'>—</td>"
            "</tr>"
        )

    return f"""
  <div class="card" style="max-width: 980px;">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'md_title'))}</div>
      <div class="ts">{html_escape(t(lang,'md_note_live'))}</div>
    </div>

    <table class="pos">
      <thead>
        <tr>
          <th>{html_escape(t(lang,'symbol'))}</th>
          <th style="text-align:right">{html_escape(t(lang,'md_prev_close'))}</th>
          <th style="text-align:right">{html_escape(t(lang,'md_last'))}</th>
          <th style="text-align:right">{html_escape(t(lang,'md_change'))}</th>
          <th style="text-align:right">{html_escape(t(lang,'md_change_pct'))}</th>
          <th style="text-align:right">{html_escape(t(lang,'md_short_interest'))}</th>
          <th style="text-align:right">{html_escape(t(lang,'md_volume'))}</th>
          <th style="text-align:right">{html_escape(t(lang,'md_asof'))}</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows) if rows else '<tr><td colspan="8"><em>—</em></td></tr>'}
      </tbody>
    </table>
  </div>

  <script>
  (function(){{
    function fmtNum(x, digits) {{
      if (x === null || x === undefined) return "—";
      var n = Number(x);
      if (!isFinite(n)) return "—";
      return n.toLocaleString(undefined, {{
        minimumFractionDigits: digits,
        maximumFractionDigits: digits
      }});
    }}

    function fmtPct(x) {{
      if (x === null || x === undefined) return "—";
      var n = Number(x);
      if (!isFinite(n)) return "—";
      return n.toFixed(2) + "%";
    }}

    function fmtInt(x) {{
      if (x === null || x === undefined) return "—";
      var n = Number(x);
      if (!isFinite(n)) return "—";
      return Math.trunc(n).toLocaleString();
    }}

    function fmtAsOf(epoch) {{
      if (!epoch) return "—";
      var d = new Date(epoch * 1000);
      // show second-resolution in local time + timezone
      return d.toLocaleString(undefined, {{ hour12:false }});
    }}

    async function poll() {{
      try {{
        const r = await fetch("/api/market-data", {{ cache: "no-store" }});
        if (!r.ok) return;
        const j = await r.json();
        const rows = j.rows || {{}};

        for (const sym in rows) {{
          const row = rows[sym] || {{}};
          const esc = sym.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");

          const prevEl = document.getElementById("md-" + esc + "-prev");
          const lastEl = document.getElementById("md-" + esc + "-last");
          const chgEl  = document.getElementById("md-" + esc + "-chg");
          const chgpxEl= document.getElementById("md-" + esc + "-chgpx");
          const shortIntEl = document.getElementById("md-" + esc + "-shortint");
          const volEl  = document.getElementById("md-" + esc + "-vol");
          const asofEl = document.getElementById("md-" + esc + "-asof");

          if (prevEl) prevEl.textContent = fmtNum(row.prev_close, 4);

          if (row.error) {{
            const msg = "ERR: " + row.error;
            if (lastEl) lastEl.textContent = msg;
            if (chgEl) chgEl.textContent = "—";
            if (chgpxEl) chgpxEl.textContent = "—";
            if (shortIntEl) shortIntEl.textContent = "—";
            if (volEl) volEl.textContent = "—";
            if (asofEl) asofEl.textContent = "—";
            continue;
          }}

          if (lastEl) lastEl.textContent = fmtNum(row.last, 4);
          if (chgEl) chgEl.textContent = fmtNum(row.change, 4);
          if (chgpxEl) chgpxEl.textContent = fmtPct(row.change_pct);
          if (shortIntEl) shortIntEl.textContent = fmtPct(row.short_interest);
          if (volEl) volEl.textContent = fmtInt(row.volume);
          if (asofEl) asofEl.textContent = fmtAsOf(row.asof_epoch);
        }}
      }} catch (e) {{
        // ignore transient errors
      }}
    }}

    poll();
    setInterval(poll, 5000); // 5s live update without full page refresh
  }})();
  </script>
"""


def render_market_insights_page(lang: str, symbols: List[str], max_levels: int) -> str:
    options = []
    sorted_symbols = sorted(symbols)
    for symbol in sorted_symbols:
        options.append(f"<option value=\"{html_escape(symbol)}\">{html_escape(symbol)}</option>")

    depth_options = []
    depth_choices = [level for level in (5, 10, 20) if level <= max_levels]
    if max_levels not in depth_choices:
        depth_choices.append(max_levels)
    default_depth = max(depth_choices)
    for level in depth_choices:
        selected = " selected" if level == default_depth else ""
        depth_options.append(f"<option value=\"{level}\"{selected}>{level}</option>")

    no_book = html_escape(t(lang, "mi_no_book"))
    return f"""
  <div class="card" style="max-width: 1120px;">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'mi_title'))}</div>
      <div class="ts" id="mi-asof">{html_escape(t(lang,'mi_asof'))}: —</div>
    </div>
    <div class="toolbar">
      <div class="toolbar-field">
        <label for="mi-symbol">{html_escape(t(lang,'mi_symbol'))}</label>
        <select id="mi-symbol">
          {''.join(options)}
        </select>
      </div>
      <div class="toolbar-field">
        <label for="mi-depth">{html_escape(t(lang,'mi_depth'))}</label>
        <select id="mi-depth">
          {''.join(depth_options)}
        </select>
      </div>
    </div>

    <div class="split-grid">
      <div class="card">
        <div class="hdr">
          <div class="acct">{html_escape(t(lang,'mi_bid'))}</div>
          <div class="ts" id="mi-bid-count">0</div>
        </div>
        <table class="book-table">
          <thead>
            <tr>
              <th style="text-align:right">{html_escape(t(lang,'mi_price'))}</th>
              <th style="text-align:right">{html_escape(t(lang,'mi_qty'))}</th>
              <th style="text-align:right">{html_escape(t(lang,'mi_cum_qty'))}</th>
              <th style="text-align:right">{html_escape(t(lang,'mi_orders'))}</th>
            </tr>
          </thead>
          <tbody id="mi-bids-body"></tbody>
        </table>
      </div>

      <div class="card">
        <div class="hdr">
          <div class="acct">{html_escape(t(lang,'mi_ask'))}</div>
          <div class="ts" id="mi-ask-count">0</div>
        </div>
        <table class="book-table">
          <thead>
            <tr>
              <th style="text-align:right">{html_escape(t(lang,'mi_price'))}</th>
              <th style="text-align:right">{html_escape(t(lang,'mi_qty'))}</th>
              <th style="text-align:right">{html_escape(t(lang,'mi_cum_qty'))}</th>
              <th style="text-align:right">{html_escape(t(lang,'mi_orders'))}</th>
            </tr>
          </thead>
          <tbody id="mi-asks-body"></tbody>
        </table>
      </div>
    </div>

    <div class="book-empty" id="mi-empty" style="display:none;">{no_book}</div>
  </div>

  <script>
  (function(){{
    const symbolEl = document.getElementById("mi-symbol");
    const depthEl = document.getElementById("mi-depth");
    const asofEl = document.getElementById("mi-asof");
    const emptyEl = document.getElementById("mi-empty");
    const bidsBody = document.getElementById("mi-bids-body");
    const asksBody = document.getElementById("mi-asks-body");
    const bidCountEl = document.getElementById("mi-bid-count");
    const askCountEl = document.getElementById("mi-ask-count");
    const asofLabel = {t(lang, "mi_asof")!r};
    let lastBookKey = "";
    let pollInFlight = false;

    function fmtNum(x, digits) {{
      if (x === null || x === undefined) return "—";
      const n = Number(x);
      if (!isFinite(n)) return "—";
      return n.toLocaleString(undefined, {{
        minimumFractionDigits: digits,
        maximumFractionDigits: digits
      }});
    }}

    function fmtInt(x) {{
      if (x === null || x === undefined) return "—";
      const n = Number(x);
      if (!isFinite(n)) return "—";
      return Math.trunc(n).toLocaleString();
    }}

    function fmtAsOf(epoch) {{
      if (!epoch) return "—";
      return new Date(epoch * 1000).toLocaleString(undefined, {{ hour12: false }});
    }}

    function renderSide(bodyEl, rows) {{
      if (!bodyEl) return;
      bodyEl.innerHTML = "";
      let cumulativeQty = 0;
      for (const row of rows) {{
        const qtyValue = Number(row.quantity);
        if (isFinite(qtyValue)) {{
          cumulativeQty += qtyValue;
        }}
        const tr = document.createElement("tr");
        tr.innerHTML =
          "<td style='text-align:right'>" + fmtNum(row.price, 4) + "</td>" +
          "<td style='text-align:right'>" + fmtNum(row.quantity, 2) + "</td>" +
          "<td style='text-align:right'>" + fmtNum(cumulativeQty, 2) + "</td>" +
          "<td style='text-align:right'>" + fmtInt(row.order_count) + "</td>";
        bodyEl.appendChild(tr);
      }}
    }}

    async function poll() {{
      if (pollInFlight) return;
      const symbol = symbolEl ? symbolEl.value : "";
      const depth = depthEl ? depthEl.value : {default_depth!r};
      if (!symbol) return;

      pollInFlight = true;
      try {{
        const r = await fetch("/api/market-insights?symbol=" + encodeURIComponent(symbol) + "&depth=" + encodeURIComponent(depth), {{ cache: "no-store" }});
        if (!r.ok) return;
        const book = await r.json();
        const bids = Array.isArray(book.bids) ? book.bids : [];
        const asks = Array.isArray(book.asks) ? book.asks : [];
        const bookKey = JSON.stringify([symbol, depth, book.asof_epoch || null, book.error || null, bids, asks]);
        if (bookKey === lastBookKey) return;
        lastBookKey = bookKey;

        const empty = !!book.error || (!bids.length && !asks.length);

        renderSide(bidsBody, bids);
        renderSide(asksBody, asks);
        if (bidCountEl) bidCountEl.textContent = String(bids.length);
        if (askCountEl) askCountEl.textContent = String(asks.length);
        if (asofEl) asofEl.textContent = asofLabel + ": " + fmtAsOf(book.asof_epoch);
        if (emptyEl) emptyEl.style.display = empty ? "block" : "none";
      }} catch (e) {{
        // ignore transient errors
      }} finally {{
        pollInFlight = false;
      }}
    }}

    if (symbolEl) symbolEl.addEventListener("change", poll);
    if (depthEl) depthEl.addEventListener("change", poll);
    poll();
    setInterval(poll, 500);
  }})();
  </script>
"""
