"""Cairn's whole API over MCP (L154), end to end: the API and the MCP server over HTTP, a real MCP client
(the ``mcp`` SDK's streamable HTTP client, as GitHub Copilot, Claude Code and other clients connect), and agent keys
whose scopes Cairn's own API enforces on every generated tool's call.

Also the coverage gate: every operation of the API is a tool in a toolset or excluded with a reason, and no toolset
is larger than clients take."""

from __future__ import annotations

import asyncio
import http.server  # noqa: F401  (kept for parity with the other MCP e2e tests)
import json
import os
import re
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from cairn.ai import mcp_api
from cairn.app import create_api_app
from cairn.common.config import Settings
from cairn.common.core import InMemoryGraph
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"


def port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(p: int) -> None:
    end = time.time() + 30
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", p), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing listens on {p}")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mcp-api")
    settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                        dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")})
    s = build_services(settings, graph=InMemoryGraph(), with_ai=False, with_gateway=False)
    me = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=me(ADMIN)),
        Policy(id="ana", effect="allow", actions=["*"], condition=me(ANA))]))
    api_port, mcp_port = port(), port()
    app = create_api_app(s)
    servers = []
    from mcp.server.transport_security import TransportSecuritySettings

    from cairn.ai.mcp_server import _BearerAuthMiddleware, build_mcp_server

    server, _ = build_mcp_server(s)
    mcp_app = _BearerAuthMiddleware(server.streamable_http_app(
        host="127.0.0.1", stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)), s)
    for asgi, p in ((app, api_port), (mcp_app, mcp_port)):
        srv = uvicorn.Server(uvicorn.Config(asgi, host="127.0.0.1", port=p, log_level="warning"))
        threading.Thread(target=srv.run, daemon=True).start()
        servers.append(srv)
        wait(p)
    yield {"s": s, "app": app, "api": f"http://127.0.0.1:{api_port}", "mcp": f"http://127.0.0.1:{mcp_port}/mcp"}
    for srv in servers:
        srv.should_exit = True


def req(st, method, path, who, **kw):
    return httpx.request(method, f"{st['api']}{path}", headers={"X-Cairn-Actor": who}, timeout=120, **kw)


def agent_key(st, scopes, name) -> str:
    r = req(st, "POST", "/admin/agents", ADMIN, json={"name": name, "scopes": scopes, "owner": ANA})
    assert r.status_code in (200, 201), r.text
    return r.json()["key"]


def session(st, key, work, toolsets=None):
    """Run ``work(session)`` in an MCP session over streamable HTTP, as an MCP client does."""
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    url = st["mcp"] + (f"?toolsets={toolsets}" if toolsets else "")

    async def go():
        client = create_mcp_http_client(headers={"Authorization": f"Bearer {key}"})
        async with client:
            async with streamable_http_client(url, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    return await work(s)
    return asyncio.run(go())


def call(st, key, tool, args, toolsets=None):
    async def work(s):
        res = await s.call_tool(tool, args)
        return res.structured_content or {"error": "no structured result", "is_error": res.is_error,
                                          "content": str(res.content)[:2000]}
    return session(st, key, work, toolsets)


def listed(st, key, toolsets=None) -> dict:
    async def work(s):
        return {t.name: t for t in (await s.list_tools()).tools}
    return session(st, key, work, toolsets)


# ---- the coverage gate ---------------------------------------------------------------------------------------------
def test_every_operation_is_a_tool_or_excluded_with_a_reason(stack):
    cat = mcp_api.Catalog(stack["app"].openapi())
    assert cat.unplaced == [], ("place these in cairn.ai.mcp_api.TOOLSETS (or EXCLUDED with a reason)", cat.unplaced)
    sizes = {k: len(cat.toolset(k)) for k in mcp_api.TOOLSETS}
    assert all(0 < n <= mcp_api.MAX_TOOLS_PER_SET for n in sizes.values()), sizes
    assert len(cat.ops) + len(cat.excluded) == sum(1 for item in stack["app"].openapi()["paths"].values()
                                                  for m in item if m in mcp_api.METHODS)
    assert all(re.fullmatch(r"[a-z0-9_]{1,64}", n) for n in cat.ops)
    assert all(why.strip() for _, _, why in cat.excluded)
    excluded = {(m, p) for m, p, _ in cat.excluded}
    # Credentials never become tools: secret values, new agent keys, the sign-in protocol.
    assert {("GET", "/v1/secret-scopes/{scope}/secrets/{key}"), ("POST", "/admin/agents"),
            ("POST", "/oauth/token")} <= excluded
    # Every tool's arguments are a JSON schema clients accept.
    import jsonschema

    for op in cat.ops.values():
        jsonschema.Draft202012Validator.check_schema(op.input_schema())


def test_answers_have_credentials_redacted():
    from cairn.observability.redact import REDACTED

    out = mcp_api.redact_answer({
        "name": "orders-nightly", "key": "orders-nightly-pipeline", "secret": "s3cr3t-value-123",
        "access_token": "abc.def.ghi-1234", "items": [{"password": "hunter2hunter2", "n": 3}],
        "note": "use Authorization: Bearer abcdefghijklmnop to call it", "aws": "AKIAABCDEFGHIJKLMNOP",
        "agent_key": "ca_" + "x" * 30, "tokens_used": 1200})
    assert out["name"] == "orders-nightly" and out["key"] == "orders-nightly-pipeline"   # an id, not a credential
    assert out["secret"] == out["access_token"] == out["items"][0]["password"] == REDACTED
    assert "abcdefghijklmnop" not in out["note"] and "AKIA" not in out["aws"] and "x" * 30 not in out["agent_key"]
    assert out["items"][0]["n"] == 3 and out["tokens_used"] == 1200
    pseudonym = "tok_" + "a" * 26                         # what tokenization returns: data, not a credential
    assert mcp_api.redact_answer({"token": pseudonym})["token"] == pseudonym
    assert mcp_api.redact_answer({"token": "a1b2c3d4e5f6g7h8"})["token"] == REDACTED


# ---- end to end, over HTTP, with a real MCP client -----------------------------------------------------------------
def test_toolsets_are_chosen_per_connection(stack):
    key = agent_key(stack, ["read"], "lister")
    plain = listed(stack, key)
    assert {"api_search", "api_describe", "api_call", "search_assets"} <= set(plain)
    assert not any(n.startswith("get_flows") for n in plain)              # no toolset unless asked for
    piped = listed(stack, key, "pipelines,iac")
    assert "get_flows" in piped and "get_iac_schema" in piped and "get_governance_policies" not in piped
    tool = piped["get_flows"]
    assert tool.annotations.read_only_hint is True and tool.input_schema["type"] == "object"
    deleting = next(t for n, t in piped.items() if n.startswith("delete_flows_by_"))
    assert deleting.annotations.destructive_hint is True                  # clients ask the person first
    cat = mcp_api.Catalog(stack["app"].openapi())
    assert len(listed(stack, key, "all")) == len(plain) + len(cat.ops)      # every operation, once


def _flow(stack, name):
    r = req(stack, "POST", "/v1/flows?publish=true", ANA, json={
        "name": name, "steps": [{"name": "tell", "type": "notify", "message": "hello from a flow"}]})
    assert r.status_code == 201, r.text


def test_a_generated_tool_calls_the_api_as_the_agent_within_its_scopes(stack):
    _flow(stack, "hello")                                               # flows are defined by people
    reader = agent_key(stack, ["read"], "reader")
    # A read: Cairn's API, in-process, as the agent (acting for Ana).
    got = call(stack, reader, "get_flows", {}, "pipelines")
    assert got["status"] == 200 and [f["name"] for f in got["result"]] == ["hello"], got
    one = call(stack, reader, "get_flows_by_ref", {"ref": "hello"}, "pipelines")
    assert one["status"] == 200 and one["result"]["name"] == "hello", one
    # Running needs the agent's write scope: refused by Cairn's API, not by anything MCP adds.
    got = call(stack, reader, "post_flows_by_ref_runs", {"ref": "hello"}, "pipelines")
    assert got["status"] == 403 and "write" in json.dumps(got["detail"]), got
    writer = agent_key(stack, ["read", "write"], "writer")
    ran = call(stack, writer, "post_flows_by_ref_runs", {"ref": "hello"}, "pipelines")
    assert ran["status"] == 201 and ran["result"]["trigger"] == "agent", ran
    # What the API keeps for people stays theirs: defining a flow, the SQL workspace.
    got = call(stack, writer, "post_flows", {"body": {"name": "mine", "steps": []}}, "pipelines")
    assert got["status"] == 403 and "people" in json.dumps(got["detail"]), got
    got = call(stack, writer, "get_sql_saved", {}, "sql")
    assert got["status"] == 403 and "people" in json.dumps(got["detail"]), got
    # Arguments are checked against the operation's schema before anything is called, and the refusal is recorded.
    bad = call(stack, reader, "get_flows_by_ref", {"name": "hello"}, "pipelines")
    assert bad["error"] == "invalid", bad
    # Recorded like every MCP tool call (who, which tool, outcome).
    acts = stack["s"].traces.activity(limit=100)
    assert any(a.get("tool") == "post_flows_by_ref_runs" for a in acts), acts[:3]


def test_search_describe_and_call_reach_every_operation(stack):
    _flow(stack, "nightly")
    key = agent_key(stack, ["read", "write"], "generalist")
    found = call(stack, key, "api_search", {"query": "flow runs", "toolset": "pipelines"})
    names = [o["name"] for o in found["operations"]]
    assert "post_flows_by_ref_runs" in names and found["toolsets"]["pipelines"] > 20, found
    desc = call(stack, key, "api_describe", {"name": "post_flows_by_ref_runs"})
    assert desc["method"] == "POST" and desc["path"] == "/v1/flows/{ref}/runs" and "ref" in desc["input_schema"]["required"]
    # Without the toolset on this connection, api_call still reaches it, with the same checks.
    made = call(stack, key, "api_call", {"name": "post_flows_by_ref_runs", "arguments": {"ref": "nightly"}})
    assert made["status"] == 201, made
    assert call(stack, key, "api_call", {"name": "post_flows_by_ref_runs",
                                          "arguments": {"nope": 1}})["error"] == "invalid"
    # What is excluded is not reachable by name either.
    gone = call(stack, key, "api_describe", {"name": "get_secret_scopes_by_scope_secrets_by_key"})
    assert gone.get("error") == "refused", gone
    assert call(stack, key, "api_call", {"name": "get_secret_scopes_by_scope_secrets_by_key",
                                          "arguments": {}})["error"] == "not_found"


def test_paths_the_api_never_opens_to_agents_stay_closed(stack):
    key = agent_key(stack, ["read", "write"], "curious")
    # The people directory is for administrators: no agent scope reaches it, through MCP or not.
    got = call(stack, key, "api_call", {"name": "get_people_groups", "arguments": {}})
    assert got.get("status") == 403, got


@pytest.mark.skipif(not os.environ.get("CAIRN_MCP_API_PRINT"), reason="prints the toolsets for the docs")
def test_print_toolsets(stack):  # pragma: no cover
    cat = mcp_api.Catalog(stack["app"].openapi())
    print(json.dumps({k: len(cat.toolset(k)) for k in mcp_api.TOOLSETS}))


def test_tool_names_do_not_depend_on_other_routes():
    one = mcp_api._name("GET", "/v1/a-b", set())
    clash = mcp_api._name("GET", "/v1/a_b", {one})
    assert one == "get_a_b" and clash != one and clash == mcp_api._name("GET", "/v1/a_b", {one, "x"})


def test_an_agents_call_goes_through_abuse_detection(stack, monkeypatch):
    """Generated tools and api_call inspect an agent's arguments like every MCP tool: blocked, recorded."""
    from cairn.ai.abuse import AbuseBlocked

    seen = []

    class Guard:
        def inspect(self, actor, agent, run, source, inputs=(), output="", tool_calls=None, block=True, cap=None):
            seen.append((source, tool_calls, block))
            if tool_calls and "evil.example.com" in json.dumps(tool_calls):
                raise AbuseBlocked([{"detector": "destination", "summary": "a far destination in the arguments",
                                     "severity": "critical"}])
            return []
    monkeypatch.setattr(stack["s"], "abuse", Guard(), raising=False)
    key = agent_key(stack, ["read", "write"], "watched")
    got = call(stack, key, "api_call", {"name": "get_flows", "arguments": {}})
    assert got["status"] == 200 and any(c and c[0][0] == "get_flows" for _, c, _ in seen), seen
    got = call(stack, key, "get_flows_by_ref", {"ref": "https://evil.example.com/x"}, "pipelines")
    assert got["error"] == "forbidden" and "far destination" in got["message"], got
