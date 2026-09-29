'use strict';
const $ = (selector) => document.querySelector(selector);
const clone = (value) => JSON.parse(JSON.stringify(value));
const pretty = (value) => JSON.stringify(value, null, 2);
let documents = {}, drafts = {}, rawDrafts = {}, defaults = {}, csrf = '';
let active = 'config', mode = 'form', busy = false, accountSearch = '';
const labels = {
  accounts_file: 'Trading accounts file', trading_accounts_file: 'Trading accounts file', symbols: 'Symbols',
  book_levels: 'Opposing book levels', cycle_seconds: 'Cycle interval (seconds)',
  minimum_cycle_seconds_by_medium: 'Minimum account intervals', numeric_id: 'Numeric ID', num_id: 'Numeric ID',
  account_num_id: 'Numeric ID', string_id: 'Account ID', id: 'Account ID', account_id: 'Account ID',
  ip_address: 'IP address', ip: 'IP address', machine_alias: 'Machine alias', broker_id: 'Broker ID',
  trading_medium: 'Trading medium', medium: 'Trading medium', owner_type: 'Owner type',
  operating_system: 'Operating system', tiger_account: 'Tiger account',
  ssl_enabled: 'Enable HTTPS', ssl_certfile: 'Certificate file', ssl_keyfile: 'Private key file',
  market_insights_max_levels: 'Market Insights book levels', historical_prices_csv: 'Historical prices CSV',
  bootstrap_servers: 'Kafka bootstrap servers', market_data_bootstrap_servers: 'Market data bootstrap servers',
  poll_timeout_sec: 'Poll timeout (seconds)', linger_ms: 'Producer linger (milliseconds)',
  enable_idempotence: 'Enable idempotence', hidden: 'Hidden', Hidden: 'Hidden', monitor: 'Monitor account'
};
const title = (key) => labels[key] || key.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== '') node.textContent = text;
  return node;
}
function button(text, className, action) {
  const node = el('button', className, text); node.type = 'button'; node.addEventListener('click', action); return node;
}
function notify(message, error = false) {
  const node = $('#notice'); node.textContent = message; node.className = error ? 'notice error' : 'notice'; node.hidden = !message;
}
async function confirmAction(heading, message, action = 'Continue') {
  const dialog = $('#confirm-dialog');
  $('#confirm-title').textContent = heading; $('#confirm-message').textContent = message;
  $('#confirm-ok').textContent = action;
  return new Promise(resolve => {
    $('#confirm-ok').onclick = () => { dialog.close('yes'); };
    $('#confirm-cancel').onclick = () => { dialog.close('no'); };
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'yes'), {once: true});
    dialog.returnValue = 'no'; dialog.showModal();
  });
}
function showLogin() { $('#workspace').hidden = true; $('#login-screen').hidden = false; }
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options, headers: {'Content-Type': 'application/json', 'X-Configurator-Request': '1', 'X-CSRF-Token': csrf, ...(options.headers || {})}
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login') showLogin();
    throw new Error(result.detail || 'The request could not be completed.');
  }
  return result;
}
function isDirty(name = active) {
  if (!documents[name]) return false;
  try { return JSON.stringify(JSON.parse(rawDrafts[name])) !== JSON.stringify(documents[name].data); }
  catch { return true; }
}
function anyDirty() { return Object.keys(documents).some(name => isDirty(name)); }
function updateStatus() {
  const dirty = isDirty();
  $('#change-status').textContent = dirty ? 'Unsaved changes' : 'Saved';
  $('#change-status').className = dirty ? 'status-pill dirty' : 'status-pill';
  $('#save-summary').textContent = dirty ? 'Changes ready to save' : 'All changes saved';
  $('#save').textContent = busy ? 'Saving…' : active === 'config' ? 'Save settings' : 'Save accounts';
  $('#save').disabled = busy || !dirty; $('#discard').disabled = busy || !dirty;
  $('#reload').disabled = busy;
  $('#raw-json').disabled = busy;
  for (const node of document.querySelectorAll('#editor-form input, #editor-form select, #editor-form textarea, #editor-form button, #form-mode, #json-mode, #logout')) node.disabled = busy;
  document.querySelectorAll('[data-tab]').forEach(node => {
    const selected = node.dataset.tab === active;
    node.classList.toggle('active', selected); node.setAttribute('aria-current', selected ? 'page' : 'false');
  });
}
function changed() { rawDrafts[active] = pretty(drafts[active]); updateStatus(); }
function setAt(path, value) {
  let target = drafts[active];
  for (const key of path.slice(0, -1)) target = target[key];
  target[path.at(-1)] = value; changed();
}
function field(key, value, path, hint = '', options = {}) {
  const label = el('label', 'field'); label.append(el('span', '', options.label || title(key)));
  let input;
  const choices = ['trading_medium', 'medium'].includes(key) ? ['API', 'WEB', 'WINDOWS', 'EMULATOR']
    : key.endsWith('auto_offset_reset') ? ['earliest', 'latest', 'error'] : null;
  if (choices) {
    input = el('select');
    const selected = value == null ? '' : String(value);
    for (const option of new Set([...choices, ...(choices.includes(selected) ? [] : [selected])])) {
      const node = el('option', '', option || 'Select…'); node.value = option; input.append(node);
    }
    input.value = selected; input.addEventListener('change', () => setAt(path, input.value));
  } else if (typeof value === 'boolean' || ['hidden', 'Hidden', 'monitor'].includes(key)) {
    input = el('input'); input.type = 'checkbox'; input.checked = value === true;
    const row = el('span', 'checkbox-field'); row.append(input, el('span', '', 'Enabled'));
    input.addEventListener('change', () => setAt(path, input.checked)); label.append(row);
  } else if (key === 'symbols' && Array.isArray(value)) {
    input = el('textarea', 'array-editor'); input.value = value.join('\n'); input.rows = 4;
    input.addEventListener('input', () => setAt(path, input.value.split(/[\s,]+/).filter(Boolean)));
  } else if (Array.isArray(value) || (value && typeof value === 'object')) {
    input = el('textarea', 'array-editor'); input.value = pretty(value);
    input.addEventListener('input', () => {
      try { setAt(path, JSON.parse(input.value)); input.setCustomValidity(''); }
      catch { input.setCustomValidity('Enter valid JSON for this field.'); }
    });
  } else {
    input = el('input');
    input.type = typeof value === 'number' ? 'number' : /password|secret|token|private_key/i.test(key) ? 'password' : 'text';
    input.value = value == null ? '' : value;
    if (input.type === 'password') input.autocomplete = 'new-password';
    if (input.type === 'number') {
      const integer = ['book_levels', 'market_insights_max_levels', 'numeric_id', 'num_id', 'account_num_id', 'port', 'linger_ms'].includes(key);
      input.step = integer ? '1' : 'any';
      if (key === 'linger_ms') input.min = '0';
      if (key === 'cycle_seconds' || key === 'poll_timeout_sec' || path.includes('minimum_cycle_seconds_by_medium')) input.min = '0.000001';
      if (integer && key !== 'linger_ms') input.min = '1';
      if (['book_levels', 'market_insights_max_levels'].includes(key)) input.max = '20';
      if (key === 'port') input.max = '65535';
      input.required = true;
    }
    input.addEventListener('input', () => setAt(path, input.type === 'number' ? (input.value === '' ? null : Number(input.value)) : input.value));
  }
  input.dataset.path = path.join('.');
  if (input.type !== 'checkbox') label.append(input);
  if (hint) label.append(el('small', '', hint));
  return label;
}
function panel(heading, description, tag = '') {
  const node = el('section', 'panel'); const header = el('div', 'panel-heading'); const text = el('div');
  text.append(el('h2', '', heading), el('p', 'muted', description)); header.append(text);
  if (tag) header.append(el('span', 'tag', tag));
  node.append(header); return node;
}
function fields(object, path, target, excluded = []) {
  const grid = el('div', 'field-grid'); target.append(grid);
  const nested = [];
  for (const [key, value] of Object.entries(object)) {
    if (excluded.includes(key)) continue;
    if (value && typeof value === 'object' && !Array.isArray(value)) nested.push([key, value]);
    else grid.append(field(key, value, [...path, key]));
  }
  for (const [key, value] of nested) {
    const section = el('div', 'nested'); section.append(el('h3', '', title(key)));
    fields(value, [...path, key], section); target.append(section);
  }
}
function renderFastTrading(target) {
  const cfg = drafts.config.fast_trading;
  const node = panel('Fast trading', 'Control how modes E and F scan the opposing book and repeat each cycle.', 'MODES E / F');
  if (!cfg || !cfg.aggression_levels || !cfg.minimum_cycle_seconds_by_medium) {
    node.append(el('p', 'muted', 'Omitted settings use the application defaults: 2 / 3 / 5 levels and 30 / 20 / 10 seconds.'));
    node.append(button('Add default settings', 'secondary', () => {
      drafts.config.fast_trading = {...clone(defaults), ...(cfg || {})}; changed(); renderForm();
    })); target.append(node); return;
  }
  const grid = el('div', 'aggression-grid');
  for (const [level, settings] of Object.entries(cfg.aggression_levels)) {
    const card = el('div', 'aggression-card'); const heading = el('h3');
    heading.append(el('span', 'level-number', level), document.createTextNode('Aggression level ' + level));
    card.append(heading);
    for (const key of ['book_levels', 'cycle_seconds']) card.append(field(key, settings[key], ['fast_trading', 'aggression_levels', level, key]));
    grid.append(card);
  }
  node.append(grid);
  node.append(el('p', 'help', 'E buys from the best asks. F sells into the best bids. Your price limit and account capacity still apply.'));
  const cooldown = el('div', 'subsection'); cooldown.append(el('h3', '', 'Mandatory minimum interval per account'));
  const media = el('div', 'cooldown-grid');
  for (const [medium, seconds] of Object.entries(cfg.minimum_cycle_seconds_by_medium)) {
    media.append(field(medium, seconds, ['fast_trading', 'minimum_cycle_seconds_by_medium', medium], 'Seconds between account actions', {label: medium}));
  }
  cooldown.append(media, el('p', 'help', 'Accounts still in cooldown are skipped. Cooldowns are shared across E/F strategies and symbols.'));
  node.append(cooldown); target.append(node);
}
function renderConfig(target) {
  renderFastTrading(target);
  const cfg = drafts.config;
  const general = panel('Trading universe', 'Choose the account file and symbols available in the trading UI.');
  fields(cfg, [], general, ['fast_trading', 'auth', 'server', 'kafka', 'market_data']); target.append(general);
  const descriptions = {
    server: ['Trading UI server', 'Bind address, port, and HTTPS settings for the trading UI.'],
    kafka: ['Kafka connections', 'Brokers, topics, consumer groups, and producer options.'],
    market_data: ['Market data', 'Historical data location and Market Insights display depth.'],
    auth: ['Users and access', 'Passwords are stored in the configuration file. The configurator accepts the admin account.']
  };
  for (const [key, [heading, description]] of Object.entries(descriptions)) {
    if (!cfg[key]) continue;
    const section = panel(heading, description);
    fields(cfg[key], [key], section); target.append(section);
  }
  target.append(el('p', 'json-tools', 'Use the JSON editor to add or remove optional sections, users, or additional fields. Environment overrides still apply when services start.'));
}
function accountEntries() { return Array.isArray(drafts.accounts) ? drafts.accounts : drafts.accounts.accounts; }
function accountBase(index) { return Array.isArray(drafts.accounts) ? [index] : ['accounts', index]; }
function accountKey(account, ...keys) { return keys.find(key => Object.hasOwn(account, key)) || keys[0]; }
function filterAccounts() {
  const query = accountSearch.trim().toLowerCase(); let count = 0;
  document.querySelectorAll('.account-card').forEach(card => {
    card.hidden = !card.dataset.search.includes(query); if (!card.hidden) count++;
  });
  $('#account-count').textContent = `${count} of ${accountEntries().length} accounts shown`;
}
function renderAccounts(target) {
  const toolbar = el('div', 'account-toolbar');
  const search = el('input'); search.type = 'search'; search.placeholder = 'Find an account, broker, or machine…'; search.setAttribute('aria-label', 'Search accounts'); search.value = accountSearch;
  search.addEventListener('input', () => { accountSearch = search.value; filterAccounts(); });
  toolbar.append(search, button('+ Add account', 'primary', () => {
    const entries = accountEntries();
    const ids = entries.map(a => Number(a.numeric_id ?? a.num_id ?? a.account_num_id) || 0);
    entries.push({numeric_id: Math.max(0, ...ids) + 1, string_id: '', broker: '', trading_medium: 'API', ip_address: '', machine_alias: '', owner_type: '', hidden: false, monitor: false});
    accountSearch = ''; changed(); renderForm();
    const last = target.querySelector('.account-card:last-of-type');
    if (last) { last.scrollIntoView({behavior: 'smooth', block: 'start'}); last.querySelector('input').focus(); }
  })); target.append(toolbar);
  const count = el('p', 'account-count'); count.id = 'account-count'; target.append(count);
  const entries = accountEntries();
  entries.forEach((account, index) => {
    const idKey = accountKey(account, 'string_id', 'id', 'account_id');
    const numKey = accountKey(account, 'numeric_id', 'num_id', 'account_num_id');
    const mediumKey = accountKey(account, 'trading_medium', 'medium');
    const ipKey = accountKey(account, 'ip_address', 'ip');
    const hiddenKey = accountKey(account, 'hidden', 'Hidden');
    const base = accountBase(index); const card = el('section', 'panel account-card');
    card.dataset.search = Object.values(account).join(' ').toLowerCase();
    const header = el('div', 'panel-heading'); const info = el('div');
    info.append(el('h2', 'account-title', account[idKey] || 'New account'), el('p', 'account-subtitle', `#${account[numKey] ?? '—'} · ${account.broker || 'Broker not set'} · ${account[mediumKey] || 'Medium not set'}`));
    const actions = el('div', 'account-actions');
    if (account[hiddenKey] === true) actions.append(el('span', 'tag', 'HIDDEN'));
    actions.append(button('Remove', 'danger', async () => {
      if (await confirmAction('Remove account?', `${account[idKey] || 'This account'} will be removed when you save the accounts file.`, 'Remove account')) {
        entries.splice(index, 1); changed(); renderForm();
      }
    })); header.append(info, actions); card.append(header);
    const grid = el('div', 'field-grid'); const visible = [idKey, numKey, 'broker', mediumKey, ipKey, 'machine_alias', 'broker_id', 'owner_type'];
    for (const key of visible) grid.append(field(key, account[key] ?? (key === numKey ? 1 : ''), [...base, key]));
    card.append(grid);
    const flags = el('div', 'account-flags');
    for (const key of [hiddenKey, 'monitor']) flags.append(field(key, account[key] ?? false, [...base, key]));
    card.append(flags);
    const extras = Object.keys(account).filter(key => ![...visible, hiddenKey, 'monitor'].includes(key));
    if (extras.length) {
      const details = el('details', 'subsection'); details.append(el('summary', '', 'Additional account settings'));
      fields(account, base, details, [...visible, hiddenKey, 'monitor']); card.append(details);
    }
    target.append(card);
  });
  if (!Array.isArray(drafts.accounts) && Object.keys(drafts.accounts).some(key => key !== 'accounts')) {
    const metadata = panel('Additional file settings', 'Extra fields are preserved when accounts are edited.');
    fields(drafts.accounts, [], metadata, ['accounts']); target.append(metadata);
  }
  target.append(el('p', 'json-tools', 'Use the JSON editor to add broker-specific fields. Hidden accounts stay in the file and can be shown again.'));
  filterAccounts();
}
function renderForm() {
  const target = $('#form-editor'); target.replaceChildren();
  try { active === 'config' ? renderConfig(target) : renderAccounts(target); }
  catch { target.replaceChildren(el('p', 'error', 'This document structure cannot be shown as a form. Open the JSON editor to correct it.')); }
}
function render() {
  const name = active === 'config' ? 'Trading settings' : 'Trading accounts';
  $('#page-title').textContent = name; $('#breadcrumb').textContent = name;
  $('#page-description').textContent = active === 'config' ? 'Fine-tune execution, connections, and access in one place.' : 'Manage account identities, broker routing, and visibility.';
  $('#file-path').textContent = documents[active].path;
  $('#raw-json').value = rawDrafts[active]; $('#json-error').textContent = '';
  $('#form-editor').hidden = mode !== 'form'; $('#json-editor').hidden = mode !== 'json';
  $('#form-mode').classList.toggle('selected', mode === 'form'); $('#json-mode').classList.toggle('selected', mode === 'json');
  $('#form-mode').setAttribute('aria-pressed', mode === 'form'); $('#json-mode').setAttribute('aria-pressed', mode === 'json');
  if (mode === 'form') renderForm(); updateStatus();
}
async function loadDocuments() {
  const result = await api('/api/documents');
  documents = result.documents; defaults = result.fast_trading_defaults; csrf = result.csrf_token;
  drafts = {}; rawDrafts = {};
  for (const [name, document] of Object.entries(documents)) { drafts[name] = clone(document.data); rawDrafts[name] = pretty(document.data); }
  $('#login-screen').hidden = true; $('#workspace').hidden = false;
  render();
}
$('#login-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget; const submit = form.querySelector('button'); submit.disabled = true;
  try {
    await api('/api/login', {method: 'POST', body: JSON.stringify(Object.fromEntries(new FormData(form)))});
    form.elements.password.value = ''; $('#login-error').textContent = '';
    if (Object.keys(documents).length && anyDirty()) {
      const result = await api('/api/documents'); csrf = result.csrf_token;
      $('#login-screen').hidden = true; $('#workspace').hidden = false;
      notify('Signed in again. Your unsaved edits have been kept.'); render();
    } else { await loadDocuments(); }
  } catch (error) { $('#login-error').textContent = error.message; }
  finally { submit.disabled = false; }
});
document.querySelectorAll('[data-tab]').forEach(node => node.addEventListener('click', () => {
  if (busy) return;
  if (mode === 'form' && !$('#editor-form').reportValidity()) return;
  active = node.dataset.tab; notify(''); render(); window.scrollTo({top: 0});
}));
$('#json-mode').addEventListener('click', () => {
  if (!$('#editor-form').reportValidity()) return;
  mode = 'json'; render();
});
$('#form-mode').addEventListener('click', () => {
  try { drafts[active] = JSON.parse(rawDrafts[active]); mode = 'form'; render(); }
  catch (error) { $('#json-error').textContent = 'Invalid JSON: ' + error.message; }
});
$('#raw-json').addEventListener('input', event => {
  rawDrafts[active] = event.target.value;
  try { drafts[active] = JSON.parse(rawDrafts[active]); $('#json-error').textContent = ''; }
  catch (error) { $('#json-error').textContent = 'Invalid JSON: ' + error.message; }
  updateStatus();
});
$('#editor-form').addEventListener('submit', event => event.preventDefault());
$('#reload').addEventListener('click', async () => {
  if (anyDirty() && !await confirmAction('Reload configuration files?', 'Unsaved changes in both editors will be discarded.', 'Reload files')) return;
  try { await loadDocuments(); notify('Loaded the latest files.'); } catch (error) { notify(error.message, true); }
});
$('#discard').addEventListener('click', async () => {
  if (!await confirmAction('Discard these changes?', 'This editor will return to the last loaded version. The file on disk will not change.', 'Discard changes')) return;
  drafts[active] = clone(documents[active].data); rawDrafts[active] = pretty(drafts[active]); notify(''); render();
});
$('#logout').addEventListener('click', async () => {
  if (anyDirty() && !await confirmAction('Sign out?', 'Unsaved edits will be discarded.', 'Sign out')) return;
  try { await api('/api/logout', {method: 'POST', body: '{}'}); documents = {}; drafts = {}; rawDrafts = {}; showLogin(); }
  catch (error) { notify(error.message, true); }
});
$('#save').addEventListener('click', async () => {
  if (busy || (mode === 'form' && !$('#editor-form').reportValidity())) return;
  try {
    const data = JSON.parse(rawDrafts[active]);
    const accountsPointer = cfg => cfg.accounts_file || cfg.trading_accounts_file;
    const changedPath = active === 'config' && accountsPointer(data) !== accountsPointer(documents.config.data);
    if (changedPath && isDirty('accounts') && !await confirmAction('Switch the accounts file?', 'Saving this path will discard unsaved edits in the accounts editor and load the newly linked file.', 'Save and switch')) return;
    busy = true; updateStatus(); notify('');
    const name = active;
    const result = await api('/api/documents/' + name, {method: 'PUT', body: JSON.stringify({document: data, revision: documents[name].revision})});
    documents[name] = result.document; drafts[name] = clone(result.document.data); rawDrafts[name] = pretty(result.document.data);
    render(); notify('Saved successfully. Restart the affected services to apply these settings.');
    if (changedPath) {
      const latest = await api('/api/documents');
      documents.accounts = latest.documents.accounts; drafts.accounts = clone(documents.accounts.data); rawDrafts.accounts = pretty(drafts.accounts);
    }
  } catch (error) { notify(error.message, true); $('#notice').scrollIntoView({behavior: 'smooth', block: 'center'}); }
  finally { busy = false; updateStatus(); }
});
window.addEventListener('beforeunload', event => { if (anyDirty()) { event.preventDefault(); event.returnValue = ''; } });
loadDocuments().catch(error => {
  if (!error.message.startsWith('Sign in')) $('#login-error').textContent = error.message;
});
