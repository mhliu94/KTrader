"use strict";
const el = (id) => document.getElementById(id);
const working = new Set(["PENDING_NEW", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"]);
let snapshot = null;
let timer = null;
let generation = 0;
let interval = 2000;
let pageSize = 20;
let currentPage = 1;
let refreshedAt = null;
let connectionLost = false;
let logoutFailed = false;
let loginErrorKey = "";
let number = new Intl.NumberFormat(locale(), { maximumFractionDigits: 8 });
const label = (value) => translations[language][`value_${value}`] ?? value.replaceAll("_", " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());
const fmt = (value) => value == null ? "—" : number.format(value);

async function api(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    return await fetch(path, {method: body === undefined ? "GET" : "POST", credentials: "same-origin",
      cache: "no-store", signal: controller.signal,
      headers: body === undefined ? {} : {"Content-Type": "application/json", "X-Order-UI-Request": "1"},
      body: body === undefined ? undefined : JSON.stringify(body)});
  } finally { clearTimeout(timeout); }
}

function showLogin() {
  clearTimeout(timer);
  snapshot = null;
  currentPage = 1;
  refreshedAt = null;
  connectionLost = false;
  logoutFailed = false;
  loginErrorKey = "";
  el("orders").replaceChildren();
  el("dashboard").hidden = true;
  el("session").hidden = true;
  el("loading").hidden = true;
  el("login").hidden = false;
  renderFeedback();
}

function options(id, items) {
  const select = el(id);
  const signature = JSON.stringify(items);
  if (select.dataset.signature === signature) return;
  const selected = select.value;
  while (select.options.length > 1) select.remove(1);
  for (const [value, name] of items) select.add(new Option(name, value));
  select.value = selected;
  if (select.selectedIndex < 0) select.selectedIndex = 0;
  select.dataset.signature = signature;
}

function cell(row, text, secondary, className) {
  const td = row.insertCell();
  if (className) td.className = className;
  td.textContent = text;
  if (secondary) {
    const sub = document.createElement("span");
    sub.className = "secondary";
    sub.textContent = secondary;
    td.append(sub);
  }
  return td;
}

function render() {
  if (!snapshot) return;
  const account = el("account").value, status = el("status").value;
  const query = el("search").value.trim().toLowerCase();
  const matching = snapshot.orders.filter((o) => (!account || o.account_id === account) && (!status || o.status === status) &&
    (!query || [o.symbol, o.order_id, o.command_id, o.client_order_id, o.account_id].some((v) => v && v.toLowerCase().includes(query))));
  const pageCount = Math.max(1, Math.ceil(matching.length / pageSize));
  currentPage = Math.min(currentPage, pageCount);
  const offset = (currentPage - 1) * pageSize;
  const rows = matching.slice(offset, offset + pageSize);
  const body = document.createDocumentFragment();
  const time = new Intl.DateTimeFormat(locale(), {timeZone: snapshot.timezone, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
  for (const o of rows) {
    const row = document.createElement("tr");
    const updated = cell(row, time.format(new Date(o.updated_at)), t("created_time", {time: time.format(new Date(o.created_at))}), "key-number order-time");
    updated.title = t("order_times", {updated: o.updated_at, created: o.created_at});
    cell(row, `#${o.account_num_id} · ${o.account_id}`, o.broker);
    cell(row, o.order_id, o.client_order_id ? t("client_id", {id: o.client_order_id}) : "", "identifier");
    cell(row, o.symbol, label(o.side), `symbol ${o.side.toLowerCase()}`);
    cell(row, label(o.order_type), translations[language][`tif_${o.time_in_force}`] ?? o.time_in_force ?? "");
    const badge = document.createElement("span");
    badge.className = `badge ${o.status.toLowerCase()}`;
    badge.textContent = label(o.status);
    cell(row, "").append(badge);
    cell(row, `${fmt(o.filled_quantity)} / ${fmt(o.quantity)}`, o.notional ? t("notional", {amount: fmt(o.notional), currency: o.currency || ""}) : "", "number key-number");
    cell(row, fmt(o.limit_price), o.stop_price == null ? "" : t("stop_price", {price: fmt(o.stop_price)}), "number key-number");
    cell(row, fmt(o.average_fill_price), o.currency || "", "number key-number");
    cell(row, o.message || "—", o.command_id ? t("command_id", {id: o.command_id}) : "", "detail");
    body.append(row);
  }
  el("orders").replaceChildren(body);
  el("count").textContent = t(matching.length === snapshot.orders.length ? "order_count" : "order_count_filtered", {
    start: fmt(matching.length ? offset + 1 : 0), end: fmt(offset + rows.length),
    count: fmt(matching.length), total: fmt(snapshot.orders.length)
  });
  const pageSummary = t("page_summary", {page: fmt(currentPage), pages: fmt(pageCount)});
  // Avoid repeating the same screen-reader announcement on every live poll.
  if (el("page-summary").textContent !== pageSummary) el("page-summary").textContent = pageSummary;
  el("previous-page").disabled = currentPage === 1;
  el("next-page").disabled = currentPage === pageCount;
  el("empty").hidden = rows.length > 0;
  const filtered = snapshot.orders.length > 0;
  const live = snapshot.feed.state === "live";
  el("empty-title").textContent = t(filtered ? "no_matches" : live ? "no_orders" : "waiting_orders");
  el("empty-detail").textContent = t(filtered ? "try_filters" : live ? "new_orders" : "loading_orders");
}

function applySnapshot(data) {
  if (snapshot && snapshot.date !== data.date) currentPage = 1;
  snapshot = data;
  interval = data.refresh_interval_seconds * 1000;
  refreshedAt = new Date();
  connectionLost = false;
  logoutFailed = false;
  el("loading").hidden = true;
  el("login").hidden = true;
  el("dashboard").hidden = false;
  el("session").hidden = false;
  renderSnapshot();
}

function renderSnapshot() {
  if (!snapshot) { renderFeedback(); return; }
  const data = snapshot;
  el("username").textContent = data.username;
  el("day").textContent = t("day_summary", {date: data.date, timezone: data.timezone});
  options("account", data.accounts.map((a) => [a.id, `#${a.num_id} · ${a.id}`]));
  options("status", data.statuses.map((s) => [s, label(s)]));
  el("total").textContent = fmt(data.orders.length);
  el("working").textContent = fmt(data.orders.filter((o) => working.has(o.status)).length);
  el("filled").textContent = fmt(data.orders.filter((o) => o.status === "FILLED").length);
  el("closed").textContent = fmt(data.orders.filter((o) => ["CANCELED", "REJECTED", "EXPIRED"].includes(o.status)).length);
  el("refreshed").textContent = t("refreshed", {time: refreshedAt.toLocaleTimeString(locale())});
  el("refresh-note").textContent = t("refresh_note", {seconds: fmt(data.refresh_interval_seconds), timezone: data.timezone});
  renderFeedback();
  render();
}

function renderFeedback() {
  el("login-error").textContent = loginErrorKey ? t(loginErrorKey) : "";
  el("loading").textContent = t(connectionLost ? "retry_connection" : "connecting_dashboard");
  if (!snapshot) return;
  const data = snapshot;
  const live = data.feed.state === "live";
  el("feed-status").textContent = connectionLost ? t("connection_lost") : label(data.feed.state);
  el("feed-status").classList.toggle("warning", connectionLost || !live);
  const messages = [];
  if (logoutFailed) messages.push(t("logout_failed"));
  if (connectionLost) messages.push(t("stale_connection"));
  else if (!live) {
    const detailKey = feedDetailKeys[data.feed.detail];
    messages.push(t("incomplete_orders", {detail: detailKey ? t(detailKey) : label(data.feed.state)}));
  }
  if (data.feed.invalid_messages) messages.push(t("invalid_messages", {count: fmt(data.feed.invalid_messages)}));
  el("notice").hidden = messages.length === 0;
  el("notice").textContent = messages.join(" ");
}

async function refresh() {
  clearTimeout(timer);
  const current = generation;
  try {
    const response = await api("/api/orders");
    if (current !== generation) return;
    if (response.status === 401) { showLogin(); return; }
    if (!response.ok) throw new Error("Unable to load orders");
    const data = await response.json();
    if (current !== generation) return;
    applySnapshot(data);
  } catch (error) {
    if (current !== generation) return;
    connectionLost = true;
    renderFeedback();
  }
  if (current === generation) timer = setTimeout(refresh, interval);
}

el("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  loginErrorKey = "";
  renderFeedback();
  el("login-submit").disabled = true;
  try {
    const response = await api("/api/login", {username: el("login-username").value, password: el("password").value});
    if (!response.ok) {
      loginErrorKey = response.status === 401 ? "login_incorrect" : "login_failed";
      renderFeedback();
      return;
    }
    el("password").value = "";
    generation++;
    await refresh();
  } catch (error) { loginErrorKey = "login_failed"; renderFeedback(); }
  finally { el("login-submit").disabled = false; }
});
el("logout").addEventListener("click", async () => {
  generation++;
  clearTimeout(timer);
  try {
    const response = await api("/api/logout", {});
    if (!response.ok) throw new Error("Sign out failed");
    showLogin();
  } catch (error) {
    logoutFailed = true;
    renderFeedback();
    timer = setTimeout(refresh, interval);
  }
});
for (const id of ["account", "status", "search"]) el(id).addEventListener("input", () => {
  currentPage = 1;
  render();
});
el("page-size").addEventListener("change", () => {
  const selected = Number(el("page-size").value);
  pageSize = [10, 20, 50].includes(selected) ? selected : 20;
  el("page-size").value = String(pageSize);
  currentPage = 1;
  render();
});
el("previous-page").addEventListener("click", () => {
  currentPage = Math.max(1, currentPage - 1);
  render();
});
el("next-page").addEventListener("click", () => {
  currentPage++;
  render();
});
for (const button of document.querySelectorAll("[data-language]")) button.addEventListener("click", () => {
  chooseLanguage(button.dataset.language);
  number = new Intl.NumberFormat(locale(), {maximumFractionDigits: 8});
  renderSnapshot();
});
window.addEventListener("pagehide", () => { generation++; clearTimeout(timer); });
window.addEventListener("pageshow", (event) => { if (event.persisted) refresh(); });
translatePage();
el("page-size").value = String(pageSize);
refresh();
