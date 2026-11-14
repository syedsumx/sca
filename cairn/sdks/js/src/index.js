// Cairn client for JavaScript and TypeScript (Node 18+, browsers, Deno, Bun): the main Cairn APIs over fetch.
// No dependencies. Every call is made as the holder of the token, so it sees and does only what they may.

const RETRY_STATUSES = new Set([429, 503]);
const MAX_WAIT_MS = 30_000;

export class CairnError extends Error {
  /** @param {number} status @param {string} detail @param {unknown} body */
  constructor(status, detail, body) {
    super(`Cairn answered ${status}: ${detail}`);
    this.name = "CairnError";
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

function detailOf(body, fallback) {
  if (body && typeof body === "object" && "detail" in body) {
    const d = body.detail;
    return typeof d === "string" ? d : JSON.stringify(d);
  }
  return typeof body === "string" && body ? body.slice(0, 500) : fallback;
}

function retryAfterMs(value) {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
  const when = Date.parse(value);
  return Number.isNaN(when) ? null : Math.max(0, when - Date.now());
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class CairnClient {
  /**
   * @param {object} options
   * @param {string} options.baseUrl  Cairn's API, e.g. https://cairn.example.com/cairn/api
   * @param {string} [options.token]  a Cairn access token or agent key, sent as a bearer token
   * @param {typeof fetch} [options.fetch]
   * @param {number} [options.retries]  how many times a 429/503 with Retry-After is retried (default 2)
   * @param {number} [options.timeoutMs]  per request (default 60 s)
   * @param {(d: {method: string, path: string, deprecation: string, sunset: string|null, successor: string|null}) => void} [options.onDeprecation]
   * @param {Record<string, string>} [options.headers]  extra headers on every request
   */
  constructor({ baseUrl, token, fetch: fetchImpl, retries = 2, timeoutMs = 60_000, onDeprecation, headers } = {}) {
    if (!baseUrl) throw new Error("baseUrl is required");
    const url = new URL(baseUrl);
    if (url.protocol !== "https:" && !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) {
      throw new Error("baseUrl must use https (plain http only to localhost)");
    }
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.token = token;
    this.fetch = fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.retries = retries;
    this.timeoutMs = timeoutMs;
    this.headers = headers ?? {};
    this.onDeprecation = onDeprecation ?? ((d) => console.warn(
      `Cairn: ${d.method} ${d.path} is deprecated${d.sunset ? ` and stops answering on ${d.sunset}` : ""}` +
      (d.successor ? `; use ${d.successor}` : "")));
    this._warned = new Set();
  }

  /**
   * Any Cairn API call. Returns the parsed JSON (or null for 204).
   * @param {string} method @param {string} path
   * @param {{query?: Record<string, string|number|boolean|undefined|null>, body?: unknown, headers?: Record<string,string>}} [opts]
   */
  async request(method, path, { query, body, headers } = {}) {
    const url = new URL(this.baseUrl + (path.startsWith("/") ? path : `/${path}`));
    for (const [k, v] of Object.entries(query ?? {})) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }
    const h = { Accept: "application/json", ...this.headers, ...(headers ?? {}) };
    if (this.token) h.Authorization = `Bearer ${this.token}`;
    if (body !== undefined) h["Content-Type"] = "application/json";
    for (let attempt = 0; ; attempt++) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      let res;
      try {
        res = await this.fetch(url, {
          method, headers: h, body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal,
        });
      } finally {
        clearTimeout(timer);
      }
      this._noticeDeprecation(method, path, res.headers);
      const wait = retryAfterMs(res.headers.get("retry-after"));
      if (RETRY_STATUSES.has(res.status) && wait !== null && attempt < this.retries) {
        await res.body?.cancel().catch(() => {});      // free the connection before trying again
        await sleep(Math.min(wait, MAX_WAIT_MS));
        continue;
      }
      const text = await res.text();
      let parsed = null;
      if (text) {
        try { parsed = JSON.parse(text); } catch { parsed = text; }
      }
      if (!res.ok) throw new CairnError(res.status, detailOf(parsed, res.statusText), parsed);
      return parsed;
    }
  }

  _noticeDeprecation(method, path, headers) {
    const deprecation = headers.get("deprecation");
    if (!deprecation) return;
    const key = `${method} ${path.split("?")[0]}`;
    if (this._warned.has(key)) return;
    this._warned.add(key);
    const link = headers.get("link") ?? "";
    const successor = /<([^>]+)>\s*;\s*rel="?successor-version"?/.exec(link)?.[1] ?? null;
    const sunset = headers.get("sunset");
    this.onDeprecation({ method, path, deprecation, sunset: sunset ? new Date(sunset).toISOString().slice(0, 10) : null, successor });
  }

  // ---- the main APIs ------------------------------------------------------------------------------
  /** Who the token belongs to. */
  me() { return this.request("GET", "/me"); }

  /** Routes being retired, with their sunset dates. */
  deprecations() { return this.request("GET", "/deprecations"); }

  /** A dataset's quality health. */
  datasetHealth(datasetUrn) { return this.request("GET", `/quality/datasets/${encodeURIComponent(datasetUrn)}/health`); }

  /** A dataset's urn from its name (catalog.schema.table). */
  resolveDataset(name) { return this.request("GET", "/v1/datasets/resolve", { query: { name } }); }

  /** Search the catalog by meaning. */
  semanticSearch(query, { limit = 10, entityTypes } = {}) {
    return this.request("POST", "/ai/search/semantic", { body: { query, limit, entity_types: entityTypes ?? null } });
  }

  /** Ask a question about the catalog. */
  ask(question) { return this.request("POST", "/ai/ask", { body: { question } }); }

  /** A turn with the assistant; pass the conversation id it returned to continue. */
  chat(message, conversationId) {
    return this.request("POST", "/ai/chat", { body: { message, conversation_id: conversationId ?? null } });
  }

  /** Run SQL on a connection, governed: row filters and masks apply as for the token's holder. */
  runSql(connection, sql, { limit = 200 } = {}) {
    return this.request("POST", "/sql/run", { body: { connection, sql, limit } });
  }

  savedQueries() { return this.request("GET", "/sql/saved"); }

  /** Data products in the marketplace. */
  products({ q, domain, certified } = {}) {
    return this.request("GET", "/marketplace/products", { query: { q, domain, certified } });
  }

  product(urn) { return this.request("GET", `/marketplace/products/${encodeURIComponent(urn)}`); }

  /** Ask for access to a data product, for a purpose. */
  requestAccess({ productUrn, purpose, justification = "", durationDays }) {
    const body = { product_urn: productUrn, purpose, justification };
    if (durationDays !== undefined) body.duration_days = durationDays;
    return this.request("POST", "/marketplace/access-requests", { body });
  }

  accessRequests() { return this.request("GET", "/marketplace/access-requests"); }

  accessRequest(id) { return this.request("GET", `/marketplace/access-requests/${encodeURIComponent(id)}`); }

  /** Custom agents the token's holder may run. */
  customAgents() { return this.request("GET", "/v1/custom-agents"); }

  /** Run a deployed custom agent as the token's holder. */
  runAgent(name, input) { return this.request("POST", `/v1/custom-agents/${encodeURIComponent(name)}/run`, { body: { input } }); }
}

export default CairnClient;
