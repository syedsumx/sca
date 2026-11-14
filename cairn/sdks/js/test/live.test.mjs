// Against a running Cairn (tests/test_sdk_clients.py starts one): CAIRN_TEST_URL, CAIRN_TEST_TOKEN (ana's personal
// access token), CAIRN_TEST_AGENT_KEY (an agent key acting for ana) and CAIRN_TEST_PERSON_HEADER (dev mode only).
import assert from "node:assert/strict";
import { test } from "node:test";
import { CairnClient, CairnError } from "../src/index.js";

const url = process.env.CAIRN_TEST_URL;
const skip = !url && "CAIRN_TEST_URL is not set";
const ANA = "urn:li:corpuser:ana";
const DS = process.env.CAIRN_TEST_DATASET;

test("the main APIs, as the token's holder", { skip }, async () => {
  const c = new CairnClient({ baseUrl: url, token: process.env.CAIRN_TEST_TOKEN });
  assert.equal((await c.me()).urn, ANA);
  const health = await c.datasetHealth(DS);
  assert.equal(health.dataset_urn ?? health.urn ?? DS, DS);
  const out = await c.runSql("wh", "SELECT count(*) AS n FROM main.orders");
  assert.deepEqual(out.rows, [[3]]);
  const masked = await c.runSql("wh", "SELECT email FROM main.users ORDER BY id");
  assert.deepEqual(masked.rows.map(([email]) => email.includes("•") && !email.includes("kim") && !email.includes("lee")),
    [true, true], JSON.stringify(masked.rows));          // PII masked for her
  await assert.rejects(c.runSql("wh", "SELECT * FROM main.secrets"), (e) => e instanceof CairnError && e.status === 403);
});

test("an agent key is narrowed to its scopes", { skip }, async () => {
  const agent = new CairnClient({ baseUrl: url, token: process.env.CAIRN_TEST_AGENT_KEY });
  assert.equal((await agent.me()).urn, ANA);
  await assert.rejects(agent.customAgents(), (e) => e.status === 403);  // agents do not start agents
  await assert.rejects(new CairnClient({ baseUrl: url, token: "not-a-token" }).me(), (e) => e.status === 401);
});

test("as a person: agents and access requests", { skip }, async () => {
  const raw = process.env.CAIRN_TEST_PERSON_HEADER;
  const [name, value] = [raw.slice(0, raw.indexOf(":")), raw.slice(raw.indexOf(":") + 1)];
  const c = new CairnClient({ baseUrl: url, headers: { [name]: value } });
  assert.deepEqual((await c.customAgents()).map((a) => a.name), ["steward"]);
  const r = await c.runAgent("steward", "how are orders?");
  assert.equal(r.status, "ok");
  assert.equal(r.answer, "fine");
});

test("deprecated and retired routes", { skip }, async () => {
  const seen = [];
  const c = new CairnClient({ baseUrl: url, token: process.env.CAIRN_TEST_TOKEN, onDeprecation: (d) => seen.push(d) });
  await c.savedQueries();
  await c.savedQueries();
  assert.deepEqual(seen.map((d) => [d.path, d.sunset, d.successor]), [["/sql/saved", "2099-01-01", "/v2/sql/saved"]]);
  await assert.rejects(c.request("GET", "/v1/datasets/changes"), (e) => e.status === 410 && /retired/.test(e.detail));
  const listed = await c.deprecations();
  assert.equal(listed.min_notice_days, 180);
  assert.deepEqual(listed.routes.map((r) => r.path).sort(), ["/sql/saved", "/v1/datasets/changes"]);
});
