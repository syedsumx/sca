"""Agents do infrastructure as code (L126), end to end: a real Cairn API and its MCP server over the streamable HTTP
transport (an agent key, as an MCP client sends it), the bundle engine planning through Cairn's own API, a real git
repository, and "Ask an agent" through Cairn's real AI gateway to a local stand-in for the model provider that answers
with tool calls.

* an agent plans (no side effects) and proposes a change: a saved query and a flow;
* a person without APPROVE_IAC_CHANGE cannot approve it, nor can the person the agent acts for (separation of duties);
* an authorised approver approves, and applying creates the resources as the approver;
* a change that drifted between approval and apply is refused and goes back for approval;
* a delete without allow_delete, a secret's value, and an agent without iac:propose are refused; agents never apply;
* the change is committed to a branch of a git folder for review;
* Ask an agent drafts a flow from plain words, through the gateway, as the person; without a model it says so;
* the audit log has every step."""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import subprocess
import threading

import httpx
import pytest
import uvicorn

from cairn.app import create_api_app
from cairn.common.config import Settings
from cairn.common.core import InMemoryGraph
from cairn.governance.abac import Policy, PolicyBundle, Target
from cairn.services import build_services
from test_trino_e2e import port, wait

ADMIN, ANA, BOB, CARL = ("urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob",
                         "urn:li:corpuser:carl")
MODEL = "iac-standin"
PROVIDER_KEY = "provider-key-held-by-cairn-" + "k" * 16

CHANGE = """
queries:
  - {name: revenue-by-region, connection: lakehouse, sql: "SELECT region, sum(amount) AS total FROM marts.revenue GROUP BY region",
     description: Revenue by region}
flows:
  - name: nightly-revenue
    spec:
      triggers: [{type: schedule, cron: "0 2 * * *", timezone: Europe/Berlin}]
      steps:
        - {name: tell, type: notify, message: "revenue refreshed"}
"""


class Provider(http.server.BaseHTTPRequestHandler):
    """Anthropic's /v1/messages, answering with the tool calls a model would make for "Ask an agent"."""
    seen: list = []
    flow_yaml = """flows:
  - name: nightly-orders
    spec:
      triggers: [{type: schedule, cron: "30 1 * * *", timezone: UTC}]
      steps:
        - {name: refresh, type: sql, sql: "SELECT count(*) AS n FROM raw.orders", connection: lakehouse}
        - {name: check, type: check, after: [refresh], sql: "SELECT count(*) AS n FROM raw.orders",
           connection: lakehouse, measure: n, op: ">", value: 0}
        - {name: email, type: notify, after: [check], message: "orders refreshed: {{ steps.refresh.rows }} rows"}
"""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Provider.seen.append({"path": self.path, "key": self.headers.get("x-api-key"),
                              "authorization": self.headers.get("authorization"), "body": body})
        if self.headers.get("x-api-key") != PROVIDER_KEY:
            return self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})
        results = [b for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        if not results:
            content = [{"type": "text", "text": "I will plan it first."},
                       {"type": "tool_use", "id": "t1", "name": "iac_plan", "input": {"bundle_yaml": self.flow_yaml}}]
        elif len(results) == 1:
            content = [{"type": "tool_use", "id": "t2", "name": "iac_propose", "input": {
                "title": "Nightly orders flow", "intent": "Refresh orders every night, check them, email the report",
                "bundle_yaml": self.flow_yaml}}]
        else:
            content = [{"type": "text", "text": "I proposed the flow nightly-orders; it waits for approval."}]
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return self._json(200, {"id": f"msg_{len(Provider.seen)}", "type": "message", "role": "assistant",
                                "model": body["model"], "content": content, "stop_reason": stop, "stop_sequence": None,
                                "usage": {"input_tokens": 50, "output_tokens": 20}})

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("iac-agent")
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    env = {"CAIRN_AI_GATEWAY_ANTHROPIC_URL": f"http://127.0.0.1:{up.server_address[1]}",
           "CAIRN_AI_GATEWAY_ANTHROPIC_KEY": PROVIDER_KEY, "CAIRN_AI_GATEWAY_MODELS": MODEL,
           "CAIRN_IAC_AGENT_MODEL": MODEL, "CAIRN_GIT_FOLDER_URLS": "file://",
           "CAIRN_IAC_CHANGE_APPROVERS": BOB}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                            dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")})
        s = build_services(settings, graph=InMemoryGraph(), with_ai=False, with_gateway=False)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    me = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=me(ADMIN)),
        Policy(id="bob-approves", effect="allow", actions=["APPROVE_IAC_CHANGE"],
               target=Target(resource_urns=["urn:li:iacKind:*"]), condition=me(BOB)),
        Policy(id="ana-model", effect="allow", actions=["USE_AI_GATEWAY"], target=Target(resource_urns=[f"urn:li:aiModel:{MODEL}"]),
               condition=me(ANA))]))
    api_port, mcp_port = port(), port()
    app = create_api_app(s)
    servers = []
    for asgi, p in ((app, api_port), (None, mcp_port)):
        if asgi is None:
            from mcp.server.transport_security import TransportSecuritySettings

            from cairn.ai.mcp_server import _BearerAuthMiddleware, build_mcp_server

            server, _ = build_mcp_server(s)
            asgi = _BearerAuthMiddleware(server.streamable_http_app(
                host="127.0.0.1", stateless_http=True,
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)), s)
        srv = uvicorn.Server(uvicorn.Config(asgi, host="127.0.0.1", port=p, log_level="warning"))
        threading.Thread(target=srv.run, daemon=True).start()
        servers.append(srv)
        wait(p)
    yield {"s": s, "api": f"http://127.0.0.1:{api_port}", "mcp": f"http://127.0.0.1:{mcp_port}/mcp", "tmp": tmp}
    for srv in servers:
        srv.should_exit = True


def req(st, method, path, who, **kw):
    return httpx.request(method, f"{st['api']}{path}", headers={"X-Cairn-Actor": who}, timeout=120, **kw)


def agent_key(st, scopes) -> str:
    r = req(st, "POST", "/admin/agents", ADMIN, json={"name": f"IaC agent {len(scopes)}", "scopes": scopes, "owner": ANA})
    assert r.status_code in (200, 201), r.text
    return r.json()["key"]


def mcp(st, key, tool, args):
    """One tool call over MCP's streamable HTTP transport, as an MCP client makes it."""
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async def go():
        client = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
        async with client:
            async with streamable_http_client(st["mcp"], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    res = await session.call_tool(tool, args)
                    return res.structured_content or {"error": "no structured result", "content": str(res.content)[:2000]}
    return asyncio.run(go())


def audit(st, action):
    return [e for e in st["s"].audit.entries(category="iac", limit=1000) if e["action"] == action]


def test_an_agent_proposes_and_a_person_approves_and_applies(stack):
    st = stack
    key = agent_key(st, ["read", "iac:propose"])
    # What there is to describe, then a plan: two resources to create, and nothing made by planning.
    schema = mcp(st, key, "iac_schema", {"kind": "flows"})
    assert schema["name"] == "flows" and any(f["name"] == "spec" for f in schema["fields"])
    plan = mcp(st, key, "iac_plan", {"bundle_yaml": CHANGE})
    assert {(c["action"], c["kind"], c["key"]) for c in plan["changes"]} == {
        ("create", "queries", "revenue-by-region"), ("create", "flows", "nightly-revenue")}, plan
    q = next(c for c in plan["changes"] if c["kind"] == "queries")
    assert {"field": "description", "before": None, "after": "Revenue by region"} in q["fields"]
    assert req(st, "GET", "/sql/saved", ANA).json() == [] and req(st, "GET", "/v1/flows", ANA).json() == []
    # Proposed: stored with its plan, by the agent for Ana, with the same change as Terraform for the record.
    out = mcp(st, key, "iac_propose", {"title": "Revenue report", "intent": "A nightly revenue report for sales",
                                       "bundle_yaml": CHANGE})
    assert out["status"] == "proposed" and out["sponsor"] == ANA and out["agent"].startswith("agt_"), out
    cid = out["id"]
    detail = req(st, "GET", f"/v1/iac/changes/{cid}", ANA).json()
    assert 'resource "cairn_saved_query" "revenue_by_region"' in detail["terraform"]
    assert 'resource "cairn_flow" "nightly_revenue"' in detail["terraform"] and detail["may_approve"] is False
    # The agent cannot apply, and cannot approve over HTTP.
    refused = mcp(st, key, "iac_apply", {"change_id": cid})
    assert refused.get("error") == "forbidden", refused
    r = httpx.post(f"{st['api']}/v1/iac/changes/{cid}/approve", headers={"Authorization": f"Bearer {key}"}, json={})
    assert r.status_code in (401, 403), r.text
    # Carl may not approve (no APPROVE_IAC_CHANGE); Ana may not approve her own agent's change.
    r = req(st, "POST", f"/v1/iac/changes/{cid}/approve", CARL, json={})
    assert r.status_code in (403, 404), r.text
    r = req(st, "POST", f"/v1/iac/changes/{cid}/approve", ANA, json={})
    assert r.status_code == 403 and "separation of duties" in r.json()["detail"], r.text
    # Bob may: he could make it himself (Cairn plans it again as him).
    seen = req(st, "GET", f"/v1/iac/changes/{cid}", BOB).json()
    assert seen["may_approve"] is True
    r = req(st, "POST", f"/v1/iac/changes/{cid}/approve", BOB, json={"comment": "looks right"})
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    # Only the approver applies, and as himself.
    assert req(st, "POST", f"/v1/iac/changes/{cid}/apply", ANA, json={}).status_code == 403
    r = req(st, "POST", f"/v1/iac/changes/{cid}/apply", BOB, json={})
    assert r.status_code == 200 and r.json()["status"] == "applied", r.text
    queries = req(st, "GET", "/sql/saved", BOB).json()
    assert [(x["name"], x["owner"]) for x in queries] == [("revenue-by-region", BOB)]
    flow = req(st, "GET", "/v1/flows/nightly-revenue", BOB).json()
    assert flow["owner"] == BOB and flow["published_version"] == 1
    assert req(st, "GET", "/sql/saved", ANA).json() == []
    # Every step is in the audit log.
    for action in ("iac.change_proposed", "iac.change_approval_refused", "iac.change_approved",
                   "iac.change_apply_started", "iac.change_applied"):
        assert any(e["subject"] == f"iac-change:{cid}" for e in audit(st, action)), action
    refusals = [e for e in audit(st, "iac.change_approval_refused") if e["subject"] == f"iac-change:{cid}"]
    assert {e["actor"] for e in refusals} == {CARL, ANA}


def test_a_drift_between_approval_and_apply_is_refused(stack):
    st = stack
    key = agent_key(st, ["read", "iac:propose"])
    change = "queries:\n  - {name: drift-q, connection: lakehouse, sql: SELECT 1 AS n}\n"
    cid = mcp(st, key, "iac_propose", {"title": "Drift", "intent": "a query", "bundle_yaml": change})["id"]
    assert req(st, "POST", f"/v1/iac/changes/{cid}/approve", BOB, json={}).status_code == 200
    # Before Bob applies, someone makes one by that name as Bob (by hand): the plan is not what was approved.
    assert req(st, "POST", "/sql/saved", BOB, json={"name": "drift-q", "connection": "lakehouse",
                                                       "sql": "SELECT 2 AS n"}).status_code == 201
    r = req(st, "POST", f"/v1/iac/changes/{cid}/apply", BOB, json={})
    assert r.status_code == 409 and "drift" in r.json()["detail"], r.text
    assert r.json()["plan"]["changes"][0]["action"] == "update"
    after = req(st, "GET", f"/v1/iac/changes/{cid}", BOB).json()
    assert after["status"] == "proposed" and after["approved_by"] is None
    assert [x["sql"] for x in req(st, "GET", "/sql/saved", BOB).json() if x["name"] == "drift-q"] == ["SELECT 2 AS n"]
    assert any(e["subject"] == f"iac-change:{cid}" for e in audit(st, "iac.change_drift"))


def test_deletes_secrets_and_scopes_are_refused(stack):
    st = stack
    key = agent_key(st, ["read", "iac:propose"])
    assert req(st, "POST", "/sql/saved", ANA, json={"name": "old-q", "connection": "lakehouse",
                                                       "sql": "SELECT 1 AS n"}).status_code == 201
    gone = "delete:\n  - {kind: queries, key: old-q}\n"
    out = mcp(st, key, "iac_propose", {"title": "Clean up", "intent": "remove old-q", "bundle_yaml": gone})
    assert out.get("error") == "refused" and "allow_delete" in out["message"], out
    ok = mcp(st, key, "iac_propose", {"title": "Clean up", "intent": "remove old-q", "bundle_yaml": gone,
                                      "allow_delete": True})
    assert ok["status"] == "proposed" and ok["plan"]["changes"][0]["action"] == "delete"
    # A secret's value is never proposed; a reference is.
    secret = "secrets:\n  - {scope: finance-db, key: password, value: hunter2-but-longer}\n"
    out = mcp(st, key, "iac_plan", {"bundle_yaml": secret})
    assert out.get("error") == "refused" and "reference" in out["message"], out
    leaked = "queries:\n  - {name: k, connection: lakehouse, sql: \"SELECT 'AKIAABCDEFGHIJKLMNOP' AS k\"}\n"
    out = mcp(st, key, "iac_propose", {"title": "x", "intent": "y", "bundle_yaml": leaked})
    assert out.get("error") in ("refused", "blocked") and "aws_access_key" in out["message"], out   # abuse detection first
    r = req(st, "POST", "/v1/iac/changes", ANA, json={"title": "x", "intent": "y", "bundle_yaml": leaked})
    assert r.status_code == 422 and "credential" in r.json()["detail"], r.text                    # and for people too
    tf = {"main.tf": 'resource "cairn_secret" "p" {\n  scope = "finance-db"\n  key = "password"\n  value = "plain"\n}\n'}
    out = mcp(st, key, "iac_plan", {"terraform_files": tf})
    assert out.get("error") == "refused" and "reference" in out["message"], out
    # Without iac:propose an agent plans, but does not propose.
    reader = agent_key(st, ["read"])
    assert mcp(st, reader, "iac_plan", {"bundle_yaml": CHANGE.replace("revenue-by-region", "r2")}).get("changes")
    out = mcp(st, reader, "iac_propose", {"title": "x", "intent": "y", "bundle_yaml": CHANGE})
    assert out.get("error") == "forbidden" and "iac:propose" in out["message"], out
    r = httpx.post(f"{st['api']}/v1/iac/changes", headers={"Authorization": f"Bearer {reader}"},
                   json={"title": "x", "intent": "y", "bundle_yaml": CHANGE})
    assert r.status_code == 403, r.text
    assert any("allow_delete" in (e["details"] or {}).get("why", "") for e in audit(st, "iac.change_refused"))
    assert any("credential" in (e["details"] or {}).get("why", "") for e in audit(st, "iac.change_refused"))
    # Terraform is read like a bundle: a reference for the secret, the saved query by its name.
    tf = {"main.tf": ('variable "pw" {\n  type = string\n  sensitive = true\n}\n'
                      'resource "cairn_saved_query" "q" {\n  name = "tf-q"\n  connection_name = "lakehouse"\n'
                      '  sql = "SELECT 1 AS n"\n}\n'
                      'resource "cairn_sql_alert" "a" {\n  name = "tf-a"\n  query = cairn_saved_query.q.id\n'
                      '  op = ">"\n  value = 0\n}\n')}
    plan = mcp(st, key, "iac_plan", {"terraform_files": tf})
    assert {(c["kind"], c["key"]) for c in plan["changes"]} == {("queries", "tf-q"), ("alerts", "tf-a")}, plan
    alert = next(c for c in plan["changes"] if c["kind"] == "alerts")
    assert {"field": "query", "before": None, "after": "tf-q"} in alert["fields"]


def test_a_change_is_committed_to_a_git_folder_for_review(stack):
    st = stack
    remote = st["tmp"] / "infra.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(remote)], check=True)
    seed = st["tmp"] / "seed"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text("infra\n")
    for args in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@x", "commit", "--quiet", "-m", "init"],
                 ["push", "--quiet", "origin", "HEAD:main"]):
        subprocess.run(["git", *args], cwd=seed, check=True, capture_output=True)
    folder = req(st, "POST", "/v1/git-folders", ANA, json={"name": "infra", "url": f"file://{remote}", "branch": "main"})
    assert folder.status_code == 201, folder.text
    key = agent_key(st, ["read", "iac:propose"])
    change = "queries:\n  - {name: git-q, connection: lakehouse, sql: SELECT 1 AS n}\n"
    cid = mcp(st, key, "iac_propose", {"title": "Git", "intent": "in git", "bundle_yaml": change})["id"]
    out = mcp(st, key, "iac_open_pr", {"change_id": cid, "git_folder": folder.json()["id"]})
    assert out["branch"] == f"cairn/iac-change-{cid[:12]}" and out["commit"], out
    files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", out["branch"]],
                           capture_output=True, text=True).stdout.split()
    assert {f"iac/changes/{cid[:12]}/change.yaml", f"iac/changes/{cid[:12]}/main.tf",
            f"iac/changes/{cid[:12]}/PLAN.md"} <= set(files)
    main = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
                          capture_output=True, text=True).stdout.split()
    assert main == ["README.md"]                                       # never onto the folder's branch
    assert any(e["subject"] == f"iac-change:{cid}" for e in audit(st, "iac.change_committed"))


def test_ask_an_agent_drafts_a_flow_through_the_gateway(stack):
    st, s = stack, stack["s"]
    Provider.seen.clear()
    r = req(st, "POST", "/v1/iac/changes/draft", ANA,
             json={"prompt": "build a nightly flow that refreshes orders, checks them, then emails the report"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["change"]["status"] == "proposed" and out["change"]["agent"] == "cairn-iac-assistant"
    assert out["change"]["sponsor"] == ANA and [f["name"] for f in out["flows"]] == ["nightly-orders"]
    steps = [x["name"] for x in out["flows"][0]["spec"]["steps"]]
    assert steps == ["refresh", "check", "email"] and "waits for approval" in out["reply"]
    assert [c["name"] for c in out["tool_calls"]] == ["iac_plan", "iac_propose"] and all(c["ok"] for c in out["tool_calls"])
    # Through Cairn's gateway: the provider saw Cairn's key, never Ana's credentials, and the IaC tools.
    assert Provider.seen and all(x["key"] == PROVIDER_KEY and not x["authorization"] for x in Provider.seen)
    assert {t["name"] for t in Provider.seen[0]["body"]["tools"]} >= {"iac_plan", "iac_propose", "iac_schema"}
    assert s.ai_usage is not None
    # Someone without the model is told so; and without a model, Ask an agent says so.
    r = req(st, "POST", "/v1/iac/changes/draft", CARL, json={"prompt": "a flow"})
    assert r.status_code == 403 and "may not use the model" in r.json()["detail"], r.text
    s.iac_assistant.model = ""
    try:
        status = req(st, "GET", "/v1/iac/changes/assistant", ANA).json()
        assert status["ready"] is False and "No model is configured" in status["why"]
        r = req(st, "POST", "/v1/iac/changes/draft", ANA, json={"prompt": "a flow"})
        assert r.status_code == 503 and "No model is configured" in r.json()["detail"]
    finally:
        s.iac_assistant.model = MODEL
