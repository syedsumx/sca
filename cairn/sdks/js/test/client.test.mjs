// Offline checks of the client's own behaviour (a fake fetch); test/live.test.mjs runs it against Cairn.
import assert from "node:assert/strict";
import { test } from "node:test";
import { CairnClient, CairnError } from "../src/index.js";

function fake(responses) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url: String(url), ...init });
    const r = responses.shift();
    return new Response(r.body === undefined ? null : JSON.stringify(r.body), { status: r.status ?? 200, headers: r.headers ?? {} });
  };
  return { fetch, calls };
}

test("sends the token and JSON, and parses the answer", async () => {
  const { fetch, calls } = fake([{ body: { urn: "urn:li:corpuser:ana" } }]);
  const c = new CairnClient({ baseUrl: "https://cairn.example.com/cairn/api/", token: "ca_x", fetch });
  assert.deepEqual(await c.runSql("wh", "SELECT 1", { limit: 5 }), { urn: "urn:li:corpuser:ana" });
  assert.equal(calls[0].url, "https://cairn.example.com/cairn/api/sql/run");
  assert.equal(calls[0].headers.Authorization, "Bearer ca_x");
  assert.deepEqual(JSON.parse(calls[0].body), { connection: "wh", sql: "SELECT 1", limit: 5 });
});

test("refuses plain http except to localhost", () => {
  assert.throws(() => new CairnClient({ baseUrl: "http://cairn.example.com" }), /https/);
  new CairnClient({ baseUrl: "http://127.0.0.1:8080" });
});

test("errors carry Cairn's status and detail", async () => {
  const { fetch } = fake([{ status: 403, body: { detail: "you may not see this asset" } }]);
  const c = new CairnClient({ baseUrl: "https://c.example", fetch });
  await assert.rejects(c.me(), (e) => e instanceof CairnError && e.status === 403 && e.detail === "you may not see this asset");
});

test("retries 429 and 503 only when told when, a bounded number of times", async () => {
  const { fetch, calls } = fake([{ status: 429, headers: { "retry-after": "0" } }, { status: 503, headers: { "retry-after": "0" } },
    { body: [] }]);
  const c = new CairnClient({ baseUrl: "https://c.example", fetch });
  assert.deepEqual(await c.savedQueries(), []);
  assert.equal(calls.length, 3);
  const once = fake([{ status: 503, body: { detail: "not configured" } }]);
  await assert.rejects(new CairnClient({ baseUrl: "https://c.example", fetch: once.fetch }).me(), (e) => e.status === 503);
  assert.equal(once.calls.length, 1);                    // no Retry-After: not retried
  const many = fake([1, 2, 3].map(() => ({ status: 429, headers: { "retry-after": "0" } })));
  await assert.rejects(new CairnClient({ baseUrl: "https://c.example", fetch: many.fetch, retries: 2 }).me(), (e) => e.status === 429);
  assert.equal(many.calls.length, 3);
});

test("a deprecated route is reported once, with its sunset and successor", async () => {
  const headers = { deprecation: "@1767225600", sunset: "Fri, 01 Jan 2027 00:00:00 GMT", link: '</v2/things>; rel="successor-version"' };
  const { fetch } = fake([{ body: [], headers }, { body: [], headers }]);
  const seen = [];
  const c = new CairnClient({ baseUrl: "https://c.example", fetch, onDeprecation: (d) => seen.push(d) });
  await c.savedQueries();
  await c.savedQueries();
  assert.deepEqual(seen, [{ method: "GET", path: "/sql/saved", deprecation: "@1767225600", sunset: "2027-01-01", successor: "/v2/things" }]);
});
