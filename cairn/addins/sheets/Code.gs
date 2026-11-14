/**
 * Cairn for Google Sheets: =CAIRN_QUERY(sql), =CAIRN_METRIC(name, dimensions, filters) and =CAIRN_ASK(question),
 * and a sidebar to sign in, browse governed metrics and insert them.
 *
 * Every call runs in Cairn as the person signed in to the add-on: their masks, row filters and table permissions
 * apply. Sign-in is OAuth 2.0 authorization code with PKCE (S256) against Cairn; the add-on is the public client
 * `agt_sheets_addon`, its redirect URI https://script.google.com/macros/d/<script id>/usercallback. Tokens are the
 * person's, scoped to read, query_data and model, kept in the person's user properties: an access token (two hours)
 * and a refresh token that rotates on each use.
 *
 * Sheets' custom functions may read properties but not write them, so they never refresh (a rotated refresh token
 * they could not store would end the grant). An hourly time-driven trigger, installed at sign-in, refreshes instead;
 * the sidebar and the menu refresh too.
 *
 * Set up (script properties, by the administrator who deploys the add-on): CAIRN_API_URL, e.g.
 * https://cairn.example.com/cairn/api; optionally CAIRN_CLIENT_ID (default agt_sheets_addon).
 */

var CAIRN_SCOPES = 'read query_data model';
var CAIRN_DEFAULT_CLIENT = 'agt_sheets_addon';
var CAIRN_SKEW_MS = 60 * 1000;
var CAIRN_TOKENS = 'cairn.tokens';
var CAIRN_PENDING = 'cairn.pending';

// ---- menu ----------------------------------------------------------------------------------------------------
function onInstall(e) {
  onOpen(e);
}

function onOpen(e) {
  SpreadsheetApp.getUi().createAddonMenu()
    .addItem('Sign in', 'showSignIn')
    .addItem('Browse metrics', 'showSidebar')
    .addSeparator()
    .addItem('Sign out', 'signOut')
    .addToUi();
}

function showSidebar() {
  var html = HtmlService.createTemplateFromFile('Sidebar').evaluate().setTitle('Cairn');
  SpreadsheetApp.getUi().showSidebar(html);
}

function showSignIn() {
  var url = authorizationUrl();
  var html = HtmlService.createHtmlOutput(
    '<p style="font:14px/1.5 system-ui,sans-serif">Sign in to Cairn in a new tab, then come back here.</p>' +
    '<p><a href="' + escapeHtml_(url) + '" target="_blank" rel="noopener" ' +
    'style="font:14px system-ui,sans-serif">Sign in to Cairn</a></p>').setWidth(360).setHeight(140);
  SpreadsheetApp.getUi().showModalDialog(html, 'Cairn');
}

// ---- configuration -------------------------------------------------------------------------------------------
function config_() {
  var props = PropertiesService.getScriptProperties();
  var api = String(props.getProperty('CAIRN_API_URL') || '').replace(/\/+$/, '');
  var https = /^https:\/\/[A-Za-z0-9.-]+(:\d+)?(\/[^\s?#]*)?$/.test(api);
  var loopback = /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?(\/[^\s?#]*)?$/.test(api);   // development only
  if (!https && !loopback) {
    throw new Error('Cairn is not set up: an administrator sets the script property CAIRN_API_URL ' +
                    '(https://<cairn>/cairn/api)');
  }
  return { api: api, clientId: props.getProperty('CAIRN_CLIENT_ID') || CAIRN_DEFAULT_CLIENT };
}

function redirectUri_() {
  return 'https://script.google.com/macros/d/' + ScriptApp.getScriptId() + '/usercallback';
}

function form_(o) {
  return Object.keys(o).filter(function (k) { return o[k] !== null && o[k] !== undefined; })
    .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(o[k]); }).join('&');
}

function escapeHtml_(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ---- sign-in: authorization code with PKCE ---------------------------------------------------------------------
/** The authorization URL; the PKCE verifier and the state wait in the person's user properties. */
function authorizationUrl() {
  var cfg = config_();
  var verifier = (Utilities.getUuid() + Utilities.getUuid()).replace(/-/g, '');   // 64 characters, 244 random bits
  var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, verifier, Utilities.Charset.US_ASCII);
  var challenge = Utilities.base64EncodeWebSafe(digest).replace(/=+$/, '');
  var state = ScriptApp.newStateToken().withMethod('authCallback').withTimeout(600).createToken();
  PropertiesService.getUserProperties().setProperty(CAIRN_PENDING,
    JSON.stringify({ verifier: verifier, state: state, at: Date.now() }));
  return cfg.api + '/oauth/authorize?' + form_({
    response_type: 'code', client_id: cfg.clientId, redirect_uri: redirectUri_(), scope: CAIRN_SCOPES,
    state: state, code_challenge: challenge, code_challenge_method: 'S256'
  });
}

/** Where Cairn sends the person back (through Apps Script's usercallback, which checks the state token). */
function authCallback(request) {
  var p = (request && request.parameter) || {};
  var props = PropertiesService.getUserProperties();
  var pending = JSON.parse(props.getProperty(CAIRN_PENDING) || 'null');
  props.deleteProperty(CAIRN_PENDING);
  if (p.error) {
    return page_('Not signed in', 'Cairn did not sign you in (' + (p.error_description || p.error) + ').');
  }
  if (!pending || !p.code || p.state !== pending.state || Date.now() - pending.at > 600000) {
    return page_('Sign-in expired', 'This sign-in expired or does not match. Start again from Extensions ▸ Cairn.');
  }
  var cfg = config_();
  try {
    tokenRequest_(cfg, { grant_type: 'authorization_code', code: p.code, client_id: cfg.clientId,
                         redirect_uri: redirectUri_(), code_verifier: pending.verifier });
  } catch (e) {
    return page_('Not signed in', e.message);
  }
  ensureRefreshTrigger_();
  return page_('Signed in to Cairn', 'You can close this tab and use =CAIRN_QUERY, =CAIRN_METRIC and =CAIRN_ASK.');
}

function page_(title, text) {
  return HtmlService.createHtmlOutput('<!doctype html><title>' + escapeHtml_(title) + '</title>' +
    '<main style="font:15px/1.5 system-ui,sans-serif;max-width:480px;margin:10vh auto"><h1>' + escapeHtml_(title) +
    '</h1><p>' + escapeHtml_(text) + '</p></main>');
}

function tokenRequest_(cfg, form) {
  var r = UrlFetchApp.fetch(cfg.api + '/oauth/token', {
    method: 'post', contentType: 'application/x-www-form-urlencoded', payload: form_(form), muteHttpExceptions: true
  });
  var body = {};
  try { body = JSON.parse(r.getContentText() || '{}'); } catch (e) { body = {}; }
  if (r.getResponseCode() !== 200 || !body.access_token) {
    PropertiesService.getUserProperties().deleteProperty(CAIRN_TOKENS);
    throw new Error('Sign in to Cairn again (' + (body.error_description || body.error || r.getResponseCode()) + ')');
  }
  var t = { access_token: body.access_token, refresh_token: body.refresh_token, scope: body.scope,
            issued_at: Date.now(), expires_at: Date.now() + (Number(body.expires_in) || 3600) * 1000 };
  PropertiesService.getUserProperties().setProperty(CAIRN_TOKENS, JSON.stringify(t));
  return t;
}

function tokens_() {
  return JSON.parse(PropertiesService.getUserProperties().getProperty(CAIRN_TOKENS) || 'null');
}

/**
 * A valid access token. Only callers that may write properties (the sidebar, the menu, the trigger) refresh;
 * custom functions use the stored token or ask the person to sign in again.
 */
function accessToken_(mayRefresh) {
  var t = tokens_();
  if (!t) throw new Error('Sign in to Cairn first: Extensions ▸ Cairn ▸ Sign in');
  if (t.expires_at - CAIRN_SKEW_MS > Date.now()) return t.access_token;
  if (!mayRefresh) throw new Error('Your Cairn session expired: Extensions ▸ Cairn ▸ Sign in');
  return refresh_().access_token;
}

/** One refresh at a time per person: a spent refresh token used twice ends the whole grant. */
function refresh_() {
  var lock = LockService.getUserLock();
  lock.waitLock(20000);
  try {
    var t = tokens_();
    if (!t) throw new Error('Sign in to Cairn first: Extensions ▸ Cairn ▸ Sign in');
    if (Date.now() - (t.issued_at || 0) < 60 * 1000) return t;          // someone else just refreshed
    var cfg = config_();
    return tokenRequest_(cfg, { grant_type: 'refresh_token', refresh_token: t.refresh_token, client_id: cfg.clientId });
  } finally {
    lock.releaseLock();
  }
}

/** The hourly trigger: keeps the access token valid for the custom functions. */
function refreshSession() {
  if (!tokens_()) return;
  try {
    refresh_();
  } catch (e) {
    console.warn('Cairn session refresh failed: ' + e.message);
  }
}

function ensureRefreshTrigger_() {
  var has = ScriptApp.getProjectTriggers().some(function (t) { return t.getHandlerFunction() === 'refreshSession'; });
  if (!has) ScriptApp.newTrigger('refreshSession').timeBased().everyHours(1).create();
}

function signOut() {
  var t = tokens_();
  if (t) {
    var cfg = config_();
    [t.refresh_token, t.access_token].forEach(function (tok) {
      if (!tok) return;
      UrlFetchApp.fetch(cfg.api + '/oauth/revoke', { method: 'post', contentType: 'application/x-www-form-urlencoded',
        payload: form_({ token: tok, client_id: cfg.clientId }), muteHttpExceptions: true });
    });
  }
  PropertiesService.getUserProperties().deleteProperty(CAIRN_TOKENS);
  ScriptApp.getProjectTriggers().forEach(function (tr) {
    if (tr.getHandlerFunction() === 'refreshSession') ScriptApp.deleteTrigger(tr);
  });
  return { signedIn: false };
}

// ---- calling Cairn -----------------------------------------------------------------------------------------------
function call_(method, path, body, mayRefresh) {
  var cfg = config_();
  var params = { method: method, headers: { Authorization: 'Bearer ' + accessToken_(mayRefresh), Accept: 'application/json' },
                 muteHttpExceptions: true };
  if (body !== undefined) {
    params.contentType = 'application/json';
    params.payload = JSON.stringify(body);
  }
  var r = UrlFetchApp.fetch(cfg.api + path, params);
  var code = r.getResponseCode(), out = {};
  try { out = JSON.parse(r.getContentText() || '{}'); } catch (e) { out = {}; }
  if (code === 401) throw new Error('Sign in to Cairn again: Extensions ▸ Cairn ▸ Sign in');
  if (code < 200 || code >= 300) {
    var detail = typeof out.detail === 'string' ? out.detail : JSON.stringify(out.detail || code);
    throw new Error('Cairn: ' + detail);
  }
  return out;
}

function cellText_(v) {
  if (v === null || v === undefined) return null;
  if (Array.isArray(v)) return v;           // a range: the server reads its cells
  var s = String(v);
  return s === '' ? null : s;
}

// ---- custom functions ---------------------------------------------------------------------------------------------
/**
 * Runs read-only SQL in Cairn as you; the rows spill, headers first.
 *
 * @param {string} sql A SELECT over catalogued tables (schema.table).
 * @param {string} connection Optional: the connection (empty for the default).
 * @return The rows, headers first.
 * @customfunction
 */
function CAIRN_QUERY(sql, connection) {
  return call_('post', '/data/sheets/query', { sql: String(sql), connection: cellText_(connection) }, false).values;
}

/**
 * A governed metric from Cairn, broken down by dimensions and filtered.
 *
 * @param {string} name The metric's name.
 * @param {string} dimensions Optional: "region, country", or a range of names.
 * @param {string} filters Optional: "region = EU; amount >= 10", or a range.
 * @return The metric per dimension, headers first.
 * @customfunction
 */
function CAIRN_METRIC(name, dimensions, filters) {
  return call_('post', '/data/sheets/metric',
               { name: String(name), dimensions: cellText_(dimensions), filters: cellText_(filters) }, false).values;
}

/**
 * Asks Cairn a question of your data; the answer's rows spill, headers first.
 *
 * @param {string} question The question, in your words.
 * @return The answer, headers first.
 * @customfunction
 */
function CAIRN_ASK(question) {
  return call_('post', '/data/sheets/ask', { question: String(question) }, false).values;
}

// ---- the sidebar's server side --------------------------------------------------------------------------------------
function sidebarState() {
  if (!tokens_()) return { signedIn: false, signInUrl: authorizationUrl() };
  try {
    var me = call_('get', '/me', undefined, true);
    return { signedIn: true, person: String(me.urn || '').replace(/^urn:li:corpuser:/, '') };
  } catch (e) {
    return { signedIn: false, signInUrl: authorizationUrl(), error: e.message };
  }
}

function listMetrics(q) {
  return call_('get', '/data/sheets/metrics' + (q ? '?q=' + encodeURIComponent(String(q).slice(0, 200)) : ''),
               undefined, true);
}

function formula_(fn, args) {
  while (args.length && (args[args.length - 1] === null || args[args.length - 1] === undefined || args[args.length - 1] === '')) {
    args.pop();
  }
  return '=' + fn + '(' + args.map(function (a) {
    return '"' + String(a === null || a === undefined ? '' : a).replace(/"/g, '""') + '"';
  }).join(',') + ')';
}

/** Puts a live formula into the selected cell: kind is "metric" ([name, dimensions, filters]) or "query" ([sql]). */
function insertFormula(kind, args) {
  var fn = { metric: 'CAIRN_METRIC', query: 'CAIRN_QUERY', ask: 'CAIRN_ASK' }[kind];
  if (!fn || !Array.isArray(args)) throw new Error('unknown formula');
  var f = formula_(fn, args.slice(0, 3));
  SpreadsheetApp.getActiveRange().getCell(1, 1).setFormula(f);
  return f;
}

/** Puts a metric's current values (not a formula) at the selected cell. */
function insertMetricValues(name, dimensions, filters) {
  var values = call_('post', '/data/sheets/metric',
                     { name: String(name), dimensions: cellText_(dimensions), filters: cellText_(filters) }, true).values;
  var at = SpreadsheetApp.getActiveRange().getCell(1, 1);
  at.getSheet().getRange(at.getRow(), at.getColumn(), values.length, values[0].length).setValues(values);
  return { rows: values.length - 1 };
}
