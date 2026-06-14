from datetime import datetime
from typing import Dict, List

from .services.market_data import QuoteRow
from .i18n import t
from .models import AccountMeta, AccountSnapshot
from .store import AccountStore

from datetime import datetime, timezone


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


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
    refresh_enabled = active_tab not in ("control-panel", "currency-conversion", "trading-status", "market-insights")
    refresh_meta = '<meta http-equiv="refresh" content="30" />' if refresh_enabled else ""
    refresh_status = ""
    if refresh_enabled:
        refresh_status = (
            f"{html_escape(t(lang, 'auto_refresh'))}"
            f" · {html_escape(t(lang, 'refresh_in'))}: <b><span id=\"refreshCountdown\">30</span>s</b>"
        )
    refresh_script = ""
    if refresh_enabled:
        refresh_script = """
  <script>
    (function(){
      // Page refresh countdown (starts at 30s and resets on reload)
      var left = 30;
      var el = document.getElementById("refreshCountdown");
      if (el) el.textContent = left;
      setInterval(function(){
        left = left - 1;
        if (left < 0) left = 0;
        if (el) el.textContent = left;
      }, 1000);
    })();
  </script>
"""

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
  {refresh_meta}
  <title>{html_escape(t(lang, "title"))}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; background: #fafafa; }}
    .topbar {{ display:flex; align-items:center; justify-content:space-between; margin-bottom: 12px; }}
    .right {{ display:flex; align-items:center; gap: 16px; }}
    h1 {{ margin: 0; font-size: 22px; }}
    .sub {{ color: #666; margin-top: 8px; }}
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
    .status-row {{ display:flex; align-items:center; gap:8px; margin: 8px 0 12px 0; font-size: 13px; }}
    .status-pill {{ display:inline-block; padding: 3px 8px; border-radius: 999px; font-weight: 700; font-size: 12px; }}
    .status-on {{ color:#166534; background:#dcfce7; border:1px solid #86efac; }}
    .status-off {{ color:#991b1b; background:#fee2e2; border:1px solid #fecaca; }}
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
    <div>
      <h1>{html_escape(t(lang, "title"))}</h1>
      <div class="sub">{refresh_status}</div>
    </div>
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
  {refresh_script}

</body>
</html>
"""


def render_account_details_page(
    lang: str,
    store: AccountStore,
    accounts: Dict[str, AccountSnapshot],
    source_label: str,
    account_metas: Dict[str, AccountMeta],
    account_details_topic: str,
) -> str:
    def account_num(acct: AccountSnapshot) -> int:
        meta = account_metas.get(acct.account_id)
        if meta:
            return meta.num_id
        if acct.account_num_id is not None:
            return acct.account_num_id
        return 999

    items = sorted(
        accounts.values(),
        key=lambda a: (account_num(a), a.account_id),
    )

    cards: List[str] = []
    for display_idx, acct in enumerate(items, start=1):
        meta = account_metas.get(acct.account_id)
        display_num = account_num(acct)
        if display_num == 999:
            display_num = display_idx
        acct_title = f"#{display_num} - {html_escape(acct.account_id)}"
        meta_line = ""
        if meta:
            meta_line = (
                f"{html_escape(t(lang,'broker'))}: {html_escape(meta.broker)} · "
                f"{html_escape(t(lang,'broker_id'))}={html_escape(meta.broker_id)}"
            )

        pos_rows = []
        for p in sorted(acct.positions, key=lambda x: x.symbol):
            pos_rows.append(
                "<tr>"
                f"<td>{html_escape(p.symbol)}</td>"
                f"<td style='text-align:right'>{p.qty:,.2f}</td>"
                f"<td style='text-align:right'>{'' if p.avg_price is None else f'{p.avg_price:,.4f}'}</td>"
                "</tr>"
            )

        trading_cls = "status-on" if acct.trading_enabled else "status-off"
        trading_text = t(lang, "trading_on" if acct.trading_enabled else "trading_off")

        pos_table = (
            "<table class='pos'>"
            f"<thead><tr><th>{html_escape(t(lang,'symbol'))}</th>"
            f"<th style='text-align:right'>{html_escape(t(lang,'qty'))}</th>"
            f"<th style='text-align:right'>{html_escape(t(lang,'avg_px'))}</th></tr></thead>"
            f"<tbody>{''.join(pos_rows) if pos_rows else '<tr><td colspan=3><em>—</em></td></tr>'}</tbody>"
            "</table>"
        )

        cards.append(
            "<div class='card'>"
            f"<div class='hdr'><div class='acct'>{acct_title}</div>"
            f"<div class='ts'>{html_escape(acct.ts or '')}</div></div>"
            f"<div class='meta'>{meta_line}</div>"
            f"<div class='status-row'>{html_escape(t(lang,'trading'))}: "
            f"<span class='status-pill {trading_cls}'>{html_escape(trading_text)}</span></div>"
            f"<div class='cash'>{html_escape(t(lang,'cash'))} (USD): <b>${acct.cash:,.2f}</b></div>"
            f"{pos_table}"
            "</div>"
        )

    # NOTE REMOVED: no source/topic line, users don't care
    inner = f"""
  <div class="grid">
    {''.join(cards) if cards else f"<div><em>{html_escape(t(lang,'no_data'))}</em></div>"}
  </div>
"""
    return inner


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
        status_cls = "status-on" if enabled else "status-off"
        on_disabled = " disabled" if enabled else ""
        off_disabled = " disabled" if has_snapshot and not enabled else ""
        rows.append(
            f"""
        <div class="status-item trading-status-row" data-account-id="{html_escape(account_id)}">
          <div>
            <div class="status-title">#{meta.num_id} {html_escape(account_id)}</div>
            <div class="status-meta">{html_escape(meta.broker)} · {html_escape(meta.broker_id)}</div>
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
    cancel_account_buttons = "\n".join(
        (
            f"<input class='choice-input' type='checkbox' id='cancel-acct-{idx}' name='account_ids' value='{html_escape(aid)}'>"
            f"<label class='choice-btn' for='cancel-acct-{idx}'>#{account_metas[aid].num_id}</label>"
        )
        for idx, aid in enumerate(sorted_account_ids, start=1)
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

    <form method="post" action="/submit-algo">
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
          </div>
        </div>
        <div>
          <label for="algo_symbol">{html_escape(t(lang,'symbol'))}</label>
          <select id="algo_symbol" name="symbol" required>
            {symbol_opts}
          </select>
        </div>
      </div>

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

      <button class="btn btn-green" type="submit">{html_escape(t(lang,'algo_submit'))}</button>
    </form>
      </section>

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

    <form method="post" action="/submit-cancel-open-orders">
      <div>
        <label>{html_escape(t(lang,'cancel_accounts'))}</label>
        <div class="choice-grid">
          {cancel_account_buttons}
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
        const accountInputs = limitForm ? Array.from(limitForm.querySelectorAll("input[name='account_ids']")) : [];
        let accountsCache = null;
        let marketCache = null;

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

        accountInputs.forEach((el) => el.addEventListener("change", renderLimitSummary));
        if (symbolEl) symbolEl.addEventListener("change", renderLimitSummary);
        refreshLimitSummary();
        setInterval(refreshLimitSummary, 5000);
      }})();
    </script>

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
    <div class="sub" style="margin-bottom: 16px;">{html_escape(t(lang,'mi_note_live'))}</div>

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
      const symbol = symbolEl ? symbolEl.value : "";
      const depth = depthEl ? depthEl.value : {default_depth!r};
      if (!symbol) return;

      try {{
        const r = await fetch("/api/market-insights?symbol=" + encodeURIComponent(symbol) + "&depth=" + encodeURIComponent(depth), {{ cache: "no-store" }});
        if (!r.ok) return;
        const book = await r.json();
        const bids = Array.isArray(book.bids) ? book.bids : [];
        const asks = Array.isArray(book.asks) ? book.asks : [];
        const empty = !!book.error || (!bids.length && !asks.length);

        renderSide(bidsBody, bids);
        renderSide(asksBody, asks);
        if (bidCountEl) bidCountEl.textContent = String(bids.length);
        if (askCountEl) askCountEl.textContent = String(asks.length);
        if (asofEl) asofEl.textContent = asofLabel + ": " + fmtAsOf(book.asof_epoch);
        if (emptyEl) emptyEl.style.display = empty ? "block" : "none";
      }} catch (e) {{
        // ignore transient errors
      }}
    }}

    if (symbolEl) symbolEl.addEventListener("change", poll);
    if (depthEl) depthEl.addEventListener("change", poll);
    poll();
    setInterval(poll, 5000);
  }})();
  </script>
"""
