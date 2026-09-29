"use strict";
const el = (id) => document.getElementById(id);
const working = new Set(["PENDING_NEW", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"]);
let snapshot = null;
let timer = null;
let generation = 0;
let interval = 2000;
const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 8 });
const label = (value) => value.replaceAll("_", " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());
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
  el("orders").replaceChildren();
  el("dashboard").hidden = true;
  el("session").hidden = true;
  el("loading").hidden = true;
  el("login").hidden = false;
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
  const rows = snapshot.orders.filter((o) => (!account || o.account_id === account) && (!status || o.status === status) &&
    (!query || [o.symbol, o.order_id, o.command_id, o.client_order_id, o.account_id].some((v) => v && v.toLowerCase().includes(query))));
  const body = document.createDocumentFragment();
  const time = new Intl.DateTimeFormat(undefined, {timeZone: snapshot.timezone, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
  for (const o of rows) {
    const row = document.createElement("tr");
    const updated = cell(row, time.format(new Date(o.updated_at)), `Created ${time.format(new Date(o.created_at))}`);
    updated.title = `Updated: ${o.updated_at}\nCreated: ${o.created_at}`;
    cell(row, `#${o.account_num_id} · ${o.account_id}`, o.broker);
    cell(row, o.order_id, o.client_order_id ? `Client: ${o.client_order_id}` : "", "identifier");
    cell(row, o.symbol, o.side, `symbol ${o.side.toLowerCase()}`);
    cell(row, label(o.order_type), o.time_in_force || "");
    const badge = document.createElement("span");
    badge.className = `badge ${o.status.toLowerCase()}`;
    badge.textContent = label(o.status);
    cell(row, "").append(badge);
    cell(row, `${fmt(o.filled_quantity)} / ${fmt(o.quantity)}`, o.notional ? `Notional ${fmt(o.notional)} ${o.currency || ""}` : "", "number");
    cell(row, fmt(o.limit_price), o.stop_price == null ? "" : `Stop ${fmt(o.stop_price)}`, "number");
    cell(row, fmt(o.average_fill_price), o.currency || "", "number");
    cell(row, o.message || "—", o.command_id ? `Command: ${o.command_id}` : "", "detail");
    body.append(row);
  }
  el("orders").replaceChildren(body);
  el("count").textContent = `${rows.length} of ${snapshot.orders.length} orders`;
  el("empty").hidden = rows.length > 0;
  const filtered = snapshot.orders.length > 0;
  const live = snapshot.feed.state === "live";
  el("empty-title").textContent = filtered ? "No matching orders" : live ? "No orders today" : "Waiting for order data";
  el("empty-detail").textContent = filtered ? "Try another account, status or search." : live ? "New orders will appear here automatically." : "Orders will appear as the Kafka feed connects and loads.";
}

function applySnapshot(data) {
  snapshot = data;
  interval = data.refresh_interval_seconds * 1000;
  el("loading").hidden = true;
  el("login").hidden = true;
  el("dashboard").hidden = false;
  el("session").hidden = false;
  el("username").textContent = data.username;
  el("day").textContent = `${data.date} · ${data.timezone} · Orders created today`;
  options("account", data.accounts.map((a) => [a.id, `#${a.num_id} · ${a.id}`]));
  options("status", data.statuses.map((s) => [s, label(s)]));
  el("total").textContent = data.orders.length;
  el("working").textContent = data.orders.filter((o) => working.has(o.status)).length;
  el("filled").textContent = data.orders.filter((o) => o.status === "FILLED").length;
  el("closed").textContent = data.orders.filter((o) => ["CANCELED", "REJECTED", "EXPIRED"].includes(o.status)).length;
  const live = data.feed.state === "live";
  el("feed-status").textContent = live ? "Live updates" : label(data.feed.state);
  el("feed-status").classList.toggle("warning", !live);
  const messages = [];
  if (!live) messages.push(`${data.feed.detail}. Displayed orders may be incomplete or out of date.`);
  if (data.feed.invalid_messages) messages.push(`${data.feed.invalid_messages} invalid Kafka message(s) skipped. See server logs.`);
  el("notice").hidden = messages.length === 0;
  el("notice").textContent = messages.join(" ");
  el("refreshed").textContent = `Refreshed ${new Date().toLocaleTimeString()}`;
  el("refresh-note").textContent = `Updates every ${data.refresh_interval_seconds}s · ${data.timezone}`;
  render();
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
    if (snapshot) {
      el("feed-status").textContent = "Connection lost";
      el("feed-status").classList.add("warning");
      el("notice").hidden = false;
      el("notice").textContent = "Cannot reach the order server. Displayed orders may be out of date. Retrying automatically…";
    } else {
      el("loading").textContent = "Cannot reach the order server. Retrying automatically…";
    }
  }
  if (current === generation) timer = setTimeout(refresh, interval);
}

el("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  el("login-error").textContent = "";
  el("login-submit").disabled = true;
  try {
    const response = await api("/api/login", {username: el("login-username").value, password: el("password").value});
    if (!response.ok) throw new Error(response.status === 401 ? "Incorrect username or password." : "Unable to sign in. Please try again.");
    el("password").value = "";
    generation++;
    await refresh();
  } catch (error) { el("login-error").textContent = error.message; }
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
    el("notice").hidden = false;
    el("notice").textContent = "Unable to sign out. Please try again when the server reconnects.";
    timer = setTimeout(refresh, interval);
  }
});
for (const id of ["account", "status", "search"]) el(id).addEventListener("input", render);
window.addEventListener("pagehide", () => { generation++; clearTimeout(timer); });
window.addEventListener("pageshow", (event) => { if (event.persisted) refresh(); });
refresh();
