"use strict";

const translations = {
  en: {
    page_title: "Orders · KTrader", brand_label: "KTrader Orders", brand_section: "/ Orders",
    language: "Language", logout: "Sign out", monitor: "ORDER MONITOR",
    login_heading: "Your trading day,", login_heading_end: "in one place.",
    login_description: "Sign in with your KTrader credentials to follow today's orders.",
    username: "Username", password: "Password", login: "Sign in",
    connecting_dashboard: "Connecting to your order dashboard…", today_orders: "Today's orders",
    connecting: "Connecting", all_orders: "All orders", working: "Working", filled: "Filled",
    closed: "Canceled / rejected / expired", orders: "Orders", account: "Account", status: "Status",
    all_accounts: "All accounts", all_statuses: "All statuses", search: "Search",
    search_placeholder: "Symbol, order ID or command ID", search_orders: "Search orders",
    zero_orders: "0 orders", waiting_data: "Waiting for data",
    table_caption: "Today's orders, newest update first", updated: "Updated", order: "Order",
    symbol_side: "Symbol / side", type: "Type", filled_quantity: "Filled / quantity",
    limit_stop: "Limit / stop", average_fill: "Avg. fill", details: "Details",
    no_orders: "No orders today", new_orders: "New orders will appear here automatically.",
    footer: "Read-only order status · Latest updates first",
    created_time: "Created {time}", order_times: "Updated: {updated}\nCreated: {created}",
    client_id: "Client: {id}", notional: "Notional {amount} {currency}", stop_price: "Stop {price}",
    command_id: "Command: {id}", order_count: "{start}–{end} of {count} orders",
    order_count_filtered: "{start}–{end} of {count} matching orders · {total} total",
    pagination: "Order pages", page_size: "Orders per page", previous_page: "Previous", next_page: "Next",
    page_summary: "Page {page} of {pages}",
    no_matches: "No matching orders", try_filters: "Try another account, status or search.",
    waiting_orders: "Waiting for order data", loading_orders: "Orders will appear as the Kafka feed connects and loads.",
    day_summary: "{date} · {timezone} · Orders created today", live_updates: "Live updates",
    incomplete_orders: "{detail}. Displayed orders may be incomplete or out of date.",
    invalid_messages: "{count} invalid Kafka message(s) skipped. See server logs.",
    refreshed: "Refreshed {time}", refresh_note: "Updates every {seconds}s · {timezone}",
    connection_lost: "Connection lost",
    stale_connection: "Cannot reach the order server. Displayed orders may be out of date. Retrying automatically…",
    retry_connection: "Cannot reach the order server. Retrying automatically…",
    login_incorrect: "Incorrect username or password.", login_failed: "Unable to sign in. Please try again.",
    logout_failed: "Unable to sign out. Please try again when the server reconnects.",
    feed_connecting: "Connecting to Kafka", feed_loading: "Loading retained order snapshots",
    feed_waiting: "Waiting for Kafka partitions", feed_unavailable: "Kafka unavailable; reconnecting",
    feed_connected: "Kafka connected", feed_interrupted: "Kafka connection interrupted; reconnecting",
    feed_stopped: "Order consumer stopped",
    value_PENDING_NEW: "Pending new", value_NEW: "New", value_PARTIALLY_FILLED: "Partially filled",
    value_PENDING_CANCEL: "Pending cancel", value_FILLED: "Filled", value_CANCELED: "Canceled",
    value_REJECTED: "Rejected", value_EXPIRED: "Expired", value_UNKNOWN: "Unknown",
    value_MARKET: "Market", value_LIMIT: "Limit", value_STOP: "Stop", value_STOP_LIMIT: "Stop limit",
    value_TRAILING_STOP: "Trailing stop", value_OTHER: "Other", value_BUY: "BUY", value_SELL: "SELL",
    value_starting: "Starting", value_replaying: "Replaying", value_disconnected: "Disconnected",
    value_stopped: "Stopped", value_live: "Live updates",
    tif_DAY: "Day", tif_GTC: "Good till canceled", tif_IOC: "Immediate or cancel",
    tif_FOK: "Fill or kill", tif_GTD: "Good till date", tif_OPG: "At the open", tif_CLS: "At the close"
  },
  zh: {
    page_title: "订单 · KTrader", brand_label: "KTrader 订单", brand_section: "/ 订单",
    language: "界面语言", logout: "退出登录", monitor: "订单监控",
    login_heading: "今日交易，", login_heading_end: "一目了然。",
    login_description: "使用您的 KTrader 账户登录，查看今日订单的最新状态。",
    username: "用户名", password: "密码", login: "登录",
    connecting_dashboard: "正在连接订单看板…", today_orders: "今日订单",
    connecting: "连接中", all_orders: "全部订单", working: "进行中", filled: "已成交",
    closed: "已撤销 / 已拒绝 / 已过期", orders: "订单", account: "账户", status: "状态",
    all_accounts: "全部账户", all_statuses: "全部状态", search: "搜索",
    search_placeholder: "证券代码、订单编号或指令编号", search_orders: "搜索订单",
    zero_orders: "0 笔订单", waiting_data: "等待数据",
    table_caption: "今日订单，按最新更新时间排序", updated: "更新时间", order: "订单",
    symbol_side: "证券 / 方向", type: "类型", filled_quantity: "已成交 / 委托数量",
    limit_stop: "限价 / 止损价", average_fill: "成交均价", details: "详情",
    no_orders: "今日暂无订单", new_orders: "新订单将自动显示在此处。",
    footer: "订单状态只读展示 · 最新更新优先",
    created_time: "创建于 {time}", order_times: "更新时间：{updated}\n创建时间：{created}",
    client_id: "客户端编号：{id}", notional: "委托金额 {amount} {currency}", stop_price: "止损价 {price}",
    command_id: "指令编号：{id}", order_count: "第 {start}–{end} 笔，共 {count} 笔订单",
    order_count_filtered: "第 {start}–{end} 笔，匹配 {count} 笔 · 全部 {total} 笔",
    pagination: "订单分页", page_size: "每页订单数", previous_page: "上一页", next_page: "下一页",
    page_summary: "第 {page} 页，共 {pages} 页",
    no_matches: "没有匹配的订单", try_filters: "请尝试其他账户、状态或搜索条件。",
    waiting_orders: "等待订单数据", loading_orders: "连接并加载 Kafka 数据后，订单将显示在此处。",
    day_summary: "{date} · {timezone} · 今日创建的订单", live_updates: "实时更新",
    incomplete_orders: "{detail}。当前显示的订单可能不完整或已过时。",
    invalid_messages: "已跳过 {count} 条无效的 Kafka 消息，详情请查看服务器日志。",
    refreshed: "刷新于 {time}", refresh_note: "每 {seconds} 秒更新 · {timezone}",
    connection_lost: "连接已断开",
    stale_connection: "无法连接订单服务器，当前显示的订单可能已过时。正在自动重试…",
    retry_connection: "无法连接订单服务器，正在自动重试…",
    login_incorrect: "用户名或密码错误。", login_failed: "登录失败，请重试。",
    logout_failed: "退出登录失败，请在服务器恢复连接后重试。",
    feed_connecting: "正在连接 Kafka", feed_loading: "正在加载历史订单快照",
    feed_waiting: "正在等待 Kafka 分区分配", feed_unavailable: "Kafka 暂不可用，正在重新连接",
    feed_connected: "Kafka 已连接", feed_interrupted: "Kafka 连接中断，正在重新连接",
    feed_stopped: "订单数据接收已停止",
    value_PENDING_NEW: "待确认", value_NEW: "已接受", value_PARTIALLY_FILLED: "部分成交",
    value_PENDING_CANCEL: "待撤销", value_FILLED: "已成交", value_CANCELED: "已撤销",
    value_REJECTED: "已拒绝", value_EXPIRED: "已过期", value_UNKNOWN: "未知",
    value_MARKET: "市价单", value_LIMIT: "限价单", value_STOP: "止损单", value_STOP_LIMIT: "止损限价单",
    value_TRAILING_STOP: "追踪止损单", value_OTHER: "其他", value_BUY: "买入", value_SELL: "卖出",
    value_starting: "连接中", value_replaying: "加载历史订单中", value_disconnected: "连接已断开",
    value_stopped: "已停止", value_live: "实时更新",
    tif_DAY: "当日有效", tif_GTC: "撤销前有效", tif_IOC: "立即成交，否则撤销",
    tif_FOK: "全部成交，否则撤销", tif_GTD: "指定日期前有效", tif_OPG: "开盘时执行", tif_CLS: "收盘时执行"
  }
};

const feedDetailKeys = {
  "Connecting to Kafka": "feed_connecting",
  "Loading retained order snapshots": "feed_loading",
  "Waiting for Kafka partitions": "feed_waiting",
  "Kafka unavailable; reconnecting": "feed_unavailable",
  "Kafka connected": "feed_connected",
  "Kafka connection interrupted; reconnecting": "feed_interrupted",
  "Order consumer stopped": "feed_stopped"
};

function resolveLanguage() {
  const query = new URLSearchParams(window.location.search).get("lang");
  if (query === "en" || query === "zh") return query;
  // Use the same preference cookie and precedence as trading_ui.i18n.
  const cookie = document.cookie.split(";").map((part) => part.trim()).find((part) => /^lang=(en|zh)$/.test(part));
  return cookie ? cookie.slice(5) : "en";
}

let language = resolveLanguage();
const locale = () => language === "zh" ? "zh-CN" : "en-US";
const t = (key, values = {}) => (translations[language][key] ?? translations.en[key] ?? key)
  .replace(/\{(\w+)\}/g, (match, name) => values[name] ?? match);

function translatePage() {
  document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  document.title = t("page_title");
  for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
  for (const node of document.querySelectorAll("[data-i18n-placeholder]")) node.placeholder = t(node.dataset.i18nPlaceholder);
  for (const node of document.querySelectorAll("[data-i18n-label]")) node.setAttribute("aria-label", t(node.dataset.i18nLabel));
  for (const button of document.querySelectorAll("[data-language]")) button.setAttribute("aria-pressed", String(button.dataset.language === language));
}

function chooseLanguage(value) {
  if (value !== "en" && value !== "zh") return;
  language = value;
  document.cookie = `lang=${value}; Path=/; Max-Age=${180 * 24 * 3600}; SameSite=Lax`;
  const url = new URL(window.location.href);
  url.searchParams.set("lang", value);
  window.history.replaceState(null, "", url);
  translatePage();
}
