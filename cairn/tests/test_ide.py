"""The Cairn agent in GitHub Copilot and other AI coding tools (L156): ``cairn ide init`` writes each tool's MCP
configuration (merged, never a secret), Copilot's custom agents, prompt files and repository instructions; and the
configuration it writes connects a real MCP client to Cairn's MCP server, as the Copilot coding agent does (an agent
key from its secret), with the toolsets and servers it names. VS Code's own sign-in (OAuth 2.1) is the flow
tests/test_agents_iam.py covers; here, that an unauthenticated client is pointed at it."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest
import uvicorn
import yaml

from cairn.ai import ide
from cairn.identity.agents import AgentIn

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def run_cli(*args, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "cairn.cli", "ide", "init", *args], capture_output=True, text=True,
                          cwd=cwd, env={**os.environ, "PYTHONPATH": SRC}, timeout=120)


def front_matter(text: str) -> dict:
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n")[1])


def test_init_writes_every_tools_configuration_and_copilots_files(tmp_path):
    r = run_cli("--url", "https://cairn.example.com", "--client", "all", "--toolsets", "pipelines,iac",
                "--servers", "github", "--dir", str(tmp_path))
    assert r.returncode == 0, r.stderr
    url = "https://cairn.example.com/cairn/mcp/mcp?toolsets=pipelines,iac&servers=github"
    assert url in r.stdout
    load = lambda rel: json.loads((tmp_path / rel).read_text())  # noqa: E731
    assert load(".vscode/mcp.json") == {"servers": {"cairn": {"type": "http", "url": url}}}
    agent = load(".github/copilot-mcp.json")["mcpServers"]["cairn"]
    assert agent["headers"] == {"Authorization": "Bearer $COPILOT_MCP_CAIRN_TOKEN"} and agent["tools"] == ["*"]
    assert load(".mcp.json")["mcpServers"]["cairn"]["url"] == url
    assert load(".cursor/mcp.json")["mcpServers"]["cairn"] == {"url": url}
    assert load(".junie/mcp/mcp.json")["mcpServers"]["cairn"]["type"] == "http"
    for name in ("cairn-data-engineer", "cairn-steward", "cairn-platform-admin"):
        fm = front_matter((tmp_path / f".github/agents/{name}.agent.md").read_text())
        assert fm["name"] == name and fm["tools"] == ["cairn/*"] and fm["description"]
    for name in ("cairn-new-pipeline", "cairn-quality-check", "cairn-impact"):
        fm = front_matter((tmp_path / f".github/prompts/{name}.prompt.md").read_text())
        assert fm["agent"] == "agent" and fm["tools"] == ["cairn/*"]
    text = "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    assert "iac_propose" in text and "Bearer ey" not in text and "ca_" not in text          # no credential anywhere


def test_existing_files_are_merged_not_replaced(tmp_path):
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode/mcp.json").write_text(json.dumps({"servers": {"github": {"type": "http", "url": "https://g"}},
                                                           "inputs": [{"id": "x"}]}))
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/copilot-instructions.md").write_text("# Our rules\n\nUse tabs.\n")
    assert run_cli("--url", "https://cairn.example.com", "--dir", str(tmp_path)).returncode == 0
    doc = json.loads((tmp_path / ".vscode/mcp.json").read_text())
    assert set(doc["servers"]) == {"github", "cairn"} and doc["inputs"] == [{"id": "x"}]
    first = (tmp_path / ".github/copilot-instructions.md").read_text()
    assert first.startswith("# Our rules") and first.count(ide.BEGIN) == 1
    # Again: the same, not a second copy; a different cairn entry needs --force.
    assert run_cli("--url", "https://cairn.example.com", "--dir", str(tmp_path)).returncode == 0
    assert (tmp_path / ".github/copilot-instructions.md").read_text() == first
    r = run_cli("--url", "https://other.example.com", "--dir", str(tmp_path))
    assert r.returncode != 0 and "--force" in r.stderr
    assert run_cli("--url", "https://other.example.com", "--dir", str(tmp_path), "--force").returncode == 0
    assert "other.example.com" in (tmp_path / ".vscode/mcp.json").read_text()


def test_bad_input_is_refused():
    for url, sets, why in (("http://cairn.example.com", None, "https"), ("https://c", ["nope"], "unknown toolsets"),
                           ("cairn.example.com", None, "address")):
        with pytest.raises(ide.IdeError, match=why):
            ide.mcp_url(url, sets)
    assert ide.mcp_url("http://localhost:9000", ["all"]).endswith("/cairn/mcp/mcp?toolsets=all")
    with pytest.raises(ide.IdeError, match="client"):
        ide.entry("emacs", "u")


# ---- the written configuration, used by a real MCP client against Cairn's MCP server ----------------------------
def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def cairn(tmp_path_factory):
    from mcp.server.transport_security import TransportSecuritySettings

    from cairn.ai.mcp_server import _BearerAuthMiddleware, build_mcp_server
    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("ide")
    s = build_services(Settings(database_url=f"sqlite:///{tmp}/c.db", service_token="svc", dev_trust_actor_header=True,
                                gateway={"key_dir": str(tmp / "k")}), graph=InMemoryGraph(), with_ai=False,
                       with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"])]))
    create_api_app(s)
    server, _ = build_mcp_server(s)
    inner = _BearerAuthMiddleware(server.streamable_http_app(
        host="127.0.0.1", stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)), s)

    async def ingress(scope, receive, send):                  # as the chart's ingress: /cairn/mcp -> the MCP server
        if scope["type"] == "http" and scope["path"].startswith("/cairn/mcp/"):
            scope = {**scope, "path": scope["path"][len("/cairn/mcp"):], "raw_path": None}
        await inner(scope, receive, send)
    p = _port()
    srv = uvicorn.Server(uvicorn.Config(ingress, host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    end = time.time() + 30
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", p), timeout=1).close()
            break
        except OSError:
            time.sleep(0.1)
    key = s.agents.create_key_agent(AgentIn(name="Copilot coding agent", scopes=["read"], owner=ANA), ADMIN)["key"]
    yield {"base": f"http://127.0.0.1:{p}", "key": key}
    srv.should_exit = True


def test_the_copilot_agents_configuration_connects_with_its_secret(cairn, tmp_path):
    assert run_cli("--url", cairn["base"], "--client", "copilot-agent", "--toolsets", "pipelines",
                   "--dir", str(tmp_path)).returncode == 0
    conf = json.loads((tmp_path / ".github/copilot-mcp.json").read_text())["mcpServers"]["cairn"]
    # What the coding agent does: $COPILOT_MCP_* replaced by the repository secret's value.
    headers = {k: v.replace("$COPILOT_MCP_CAIRN_TOKEN", cairn["key"]) for k, v in conf["headers"].items()}

    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async def go():
        client = create_mcp_http_client(headers=headers)
        async with client:
            async with streamable_http_client(conf["url"], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    names = {t.name for t in (await s.list_tools()).tools}
                    found = await s.call_tool("api_search", {"query": "flow runs"})
                    flows = await s.call_tool("get_flows", {})
                    return names, found.structured_content, flows.structured_content
    names, found, flows = asyncio.run(go())
    assert {"api_search", "api_call", "get_flows", "search_assets"} <= names
    assert not any(n.startswith("get_governance") for n in names)       # only the toolsets the config names
    assert any(o["name"] == "post_flows_by_ref_runs" for o in found["operations"])
    assert flows["status"] == 200 and flows["result"] == []


def test_a_tool_without_a_token_is_sent_to_cairns_sign_in(cairn, tmp_path):
    assert run_cli("--url", cairn["base"], "--client", "vscode", "--dir", str(tmp_path)).returncode == 0
    url = json.loads((tmp_path / ".vscode/mcp.json").read_text())["servers"]["cairn"]["url"]
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                   headers={"accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert 'resource_metadata="' in r.headers["www-authenticate"]
    assert r.headers["www-authenticate"].rstrip('"').endswith("/.well-known/oauth-protected-resource/cairn/mcp/mcp")


def test_a_refusal_changes_nothing_and_comments_are_not_dropped_silently(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"cairn": {"url": "https://elsewhere"}}}))
    (tmp_path / ".vscode").mkdir()
    jsonc = '{\n  // our servers\n  "servers": {"github": {"type": "http", "url": "https://g",}},\n}\n'
    (tmp_path / ".vscode/mcp.json").write_text(jsonc)
    r = run_cli("--url", "https://cairn.example.com", "--client", "vscode,claude-code", "--dir", str(tmp_path))
    assert r.returncode != 0 and ".vscode/mcp.json" in r.stderr and "comments" in r.stderr
    assert (tmp_path / ".vscode/mcp.json").read_text() == jsonc                  # nothing written by a refusal
    assert not (tmp_path / ".github").exists()
    r = run_cli("--url", "https://cairn.example.com", "--client", "vscode", "--force", "--dir", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert set(json.loads((tmp_path / ".vscode/mcp.json").read_text())["servers"]) == {"github", "cairn"}


def test_links_markers_empty_clients_and_other_paths(tmp_path):
    (tmp_path / ".github").mkdir()
    shared = tmp_path / "shared.md"
    shared.write_text("shared rules\n")
    (tmp_path / ".github/copilot-instructions.md").symlink_to(shared)
    r = run_cli("--url", "https://cairn.example.com", "--dir", str(tmp_path))
    assert r.returncode != 0 and "link" in r.stderr and shared.read_text() == "shared rules\n"
    (tmp_path / ".github/copilot-instructions.md").unlink()
    (tmp_path / ".github/copilot-instructions.md").write_text(f"x\n{ide.BEGIN}\nhalf\n")
    r = run_cli("--url", "https://cairn.example.com", "--dir", str(tmp_path))
    assert r.returncode != 0 and "markers" in r.stderr
    r = run_cli("--url", "https://cairn.example.com", "--client", " , ", "--dir", str(tmp_path))
    assert r.returncode != 0 and "--client" in r.stderr
    assert ide.mcp_url("https://mcp.cairn.example.com", [], path="/mcp") == "https://mcp.cairn.example.com/mcp"
    with pytest.raises(ide.IdeError, match="mcp-path"):
        ide.mcp_url("https://c.example.com", [], path="mcp?x")
