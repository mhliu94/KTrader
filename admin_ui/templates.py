import json
from typing import Dict, List

from .i18n import t
from .models import StrategySnapshot, StrategySummary


def html_escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def render_layout(lang: str, active_tab: str, inner_html: str, current_user: str = "") -> str:
    next_path = f"/{active_tab}"
    tab_cls = "tab active" if active_tab == "strategies" else "tab"
    lang_toggle = (
        f"<a class='lang' href='/set-lang?lang=en&next={html_escape(next_path)}'>{html_escape(t(lang,'lang_en'))}</a>"
        f"<span class='lang-sep'>|</span>"
        f"<a class='lang' href='/set-lang?lang=zh&next={html_escape(next_path)}'>{html_escape(t(lang,'lang_zh'))}</a>"
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
  <meta http-equiv="refresh" content="30" />
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
      box-shadow: 0 1px 2px rgba(0,0,0,0.04); font-weight: 600;
    }}
    .tab.active {{ outline: 2px solid rgba(0,0,0,0.12); }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 16px; }}
    .table-wrap {{ overflow-x:auto; }}
    .card {{ background: white; border: 1px solid #e5e5e5; border-radius: 12px; padding: 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }}
    .hdr {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom: 6px; gap: 12px; }}
    .acct {{ font-size: 18px; font-weight: 700; }}
    .ts {{ font-size: 12px; color: #888; }}
    .meta {{ font-size: 12px; color: #666; margin-bottom: 10px; }}
    .summary-num {{ font-size: 28px; font-weight: 800; margin: 8px 0; }}
    .summary-label {{ color:#666; font-size: 13px; }}
    table.pos {{ width: 100%; border-collapse: collapse; }}
    table.pos th, table.pos td {{ border-top: 1px solid #eee; padding: 8px; font-size: 13px; text-align:left; vertical-align: top; }}
    table.pos thead th {{ border-top: none; color: #444; }}
    .pill {{ display:inline-block; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
    .pill-running {{ background:#dcfce7; color:#166534; }}
    .pill-stopped {{ background:#e5e7eb; color:#374151; }}
    .pill-error {{ background:#fee2e2; color:#991b1b; }}
    .pill-other {{ background:#dbeafe; color:#1d4ed8; }}
    .lang {{ text-decoration:none; color:#222; font-weight:700; }}
    .lang-sep {{ color:#888; }}
    .user {{ color:#333; font-size: 13px; }}
    .logout {{ text-decoration:none; color:#2563eb; font-weight:700; }}
    .warn {{ color:#a33; font-size: 12px; }}
    pre {{ margin:0; white-space:pre-wrap; word-break:break-word; background:#f8fafc; border:1px solid #e5e7eb; border-radius:10px; padding:12px; font-size:12px; }}
    form {{ display:flex; flex-direction:column; gap: 10px; }}
    label {{ font-size: 13px; color:#333; font-weight:600; }}
    select, input {{
      padding: 10px 12px; border-radius: 10px; border: 1px solid #ddd; background: white; font-size: 14px;
    }}
    .btn {{
      display:inline-block; padding: 10px 14px; border-radius: 12px; border: 1px solid #e5e5e5;
      font-weight: 700; cursor: pointer; width: fit-content; color: white; background: #2563eb;
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>{html_escape(t(lang, "title"))}</h1>
      <div class="sub">
        {html_escape(t(lang, "auto_refresh"))}
        · {html_escape(t(lang, "refresh_in"))}: <b><span id="refreshCountdown">30</span>s</b>
      </div>
    </div>
    <div class="right">
      <div>{user_html}</div>
      <div>{lang_toggle}</div>
    </div>
  </div>

  <div class="tabs">
    <a class="{tab_cls}" href="/strategies">{html_escape(t(lang, "tab_overview"))}</a>
  </div>

  {inner_html}

  <script>
    (function(){{
      var left = 30;
      var el = document.getElementById("refreshCountdown");
      if (el) el.textContent = left;
      setInterval(function(){{
        left = left - 1;
        if (left < 0) left = 0;
        if (el) el.textContent = left;
      }}, 1000);
    }})();
  </script>
</body>
</html>
"""


def render_login_page(lang: str, next_path: str, error: str = "") -> str:
    msg = f"<div class='warn'>{html_escape(error)}</div>" if error else ""
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
        <select id="username" name="username" required>
          <option value="admin">admin</option>
        </select>
      </div>
      <div>
        <label for="password">{html_escape(t(lang,'password'))}</label>
        <input id="password" name="password" type="password" required />
      </div>
      <button class="btn" type="submit">{html_escape(t(lang,'login'))}</button>
    </form>
  </div>
"""


def _status_class(status: str) -> str:
    if status == "RUNNING":
        return "pill pill-running"
    if status in {"STOPPED", "COMPLETED"}:
        return "pill pill-stopped"
    if status in {"ERROR", "FAILED"}:
        return "pill pill-error"
    return "pill pill-other"


def _render_summary_card(label: str, value: str, helper: str = "") -> str:
    helper_html = f"<div class='meta'>{html_escape(helper)}</div>" if helper else "<div class='meta'>&nbsp;</div>"
    return (
        "<div class='card'>"
        f"<div class='summary-label'>{html_escape(label)}</div>"
        f"<div class='summary-num'>{html_escape(value)}</div>"
        f"{helper_html}"
        "</div>"
    )


def render_strategy_page(
    lang: str,
    strategies: Dict[str, StrategySnapshot],
    summary: StrategySummary,
    topic_name: str,
) -> str:
    items: List[StrategySnapshot] = sorted(
        strategies.values(),
        key=lambda item: ((item.status != "RUNNING"), item.trading_mode, item.symbol, item.strategy_key),
    )

    running_items = [item for item in items if item.status == "RUNNING"]
    running_rows = []
    for item in running_items:
        running_rows.append(
            "<tr>"
            f"<td><span class='{_status_class(item.status)}'>{html_escape(item.status)}</span></td>"
            f"<td>{html_escape(item.trading_mode or '-')}</td>"
            f"<td>{html_escape(item.symbol or '-')}</td>"
            f"<td>{html_escape(', '.join(item.account_ids) or '-')}</td>"
            f"<td>{html_escape(item.updated_at or '-')}</td>"
            "</tr>"
        )

    all_rows = []
    for item in items:
        raw = html_escape(json.dumps(item.raw, ensure_ascii=False, indent=2, sort_keys=True))
        all_rows.append(
            "<tr>"
            f"<td>{html_escape(item.strategy_key)}</td>"
            f"<td><span class='{_status_class(item.status)}'>{html_escape(item.status)}</span></td>"
            f"<td>{html_escape(item.trading_mode or '-')}</td>"
            f"<td>{html_escape(item.symbol or '-')}</td>"
            f"<td>{html_escape(', '.join(item.account_ids) or '-')}</td>"
            f"<td>{html_escape(item.command_id or '-')}</td>"
            f"<td>{html_escape(item.started_at or '-')}</td>"
            f"<td>{html_escape(item.updated_at or '-')}</td>"
            f"<td>{html_escape(item.detail or '-')}</td>"
            f"<td><pre>{raw}</pre></td>"
            "</tr>"
        )

    return f"""
  <div class="grid">
    {_render_summary_card(t(lang,'summary_total'), str(summary.total), f"{t(lang,'topic_live_updates')}: {topic_name}")}
    {_render_summary_card(t(lang,'summary_running'), str(summary.running), t(lang,'running_only'))}
    {_render_summary_card(t(lang,'summary_stopped'), str(summary.stopped), t(lang,'all_strategies'))}
    {_render_summary_card(t(lang,'summary_errored'), str(summary.errored), f"{t(lang,'last_update')}: {summary.last_update or '-'}")}
  </div>

  <div class="card" style="margin-bottom:16px;">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'running_only'))}</div>
      <div class="ts">{html_escape(t(lang,'last_update'))}: {html_escape(summary.last_update or '-')}</div>
    </div>
    <div class="table-wrap">
      <table class="pos">
        <thead>
          <tr>
            <th>{html_escape(t(lang,'status'))}</th>
            <th>{html_escape(t(lang,'mode'))}</th>
            <th>{html_escape(t(lang,'symbol'))}</th>
            <th>{html_escape(t(lang,'accounts'))}</th>
            <th>{html_escape(t(lang,'updated_at'))}</th>
          </tr>
        </thead>
        <tbody>
          {''.join(running_rows) if running_rows else f"<tr><td colspan='5'><em>{html_escape(t(lang,'no_data'))}</em></td></tr>"}
        </tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <div class="hdr">
      <div class="acct">{html_escape(t(lang,'all_strategies'))}</div>
      <div class="ts">{html_escape(t(lang,'topic_live_updates'))}: {html_escape(topic_name)}</div>
    </div>
    <div class="table-wrap">
      <table class="pos">
        <thead>
          <tr>
            <th>Key</th>
            <th>{html_escape(t(lang,'status'))}</th>
            <th>{html_escape(t(lang,'mode'))}</th>
            <th>{html_escape(t(lang,'symbol'))}</th>
            <th>{html_escape(t(lang,'accounts'))}</th>
            <th>{html_escape(t(lang,'command_id'))}</th>
            <th>{html_escape(t(lang,'started_at'))}</th>
            <th>{html_escape(t(lang,'updated_at'))}</th>
            <th>{html_escape(t(lang,'details'))}</th>
            <th>{html_escape(t(lang,'raw_payload'))}</th>
          </tr>
        </thead>
        <tbody>
          {''.join(all_rows) if all_rows else f"<tr><td colspan='10'><em>{html_escape(t(lang,'no_data'))}</em></td></tr>"}
        </tbody>
      </table>
    </div>
  </div>
"""
