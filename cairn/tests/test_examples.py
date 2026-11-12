"""The example agents, skills and plugins (L157).

* Every example agent (``examples/agents/*/agent.yaml``) is created, deployed and run through Cairn's API on a real
  stack, its cases (``evals.yaml``) replayed with the model's tool calls scripted: the definition is valid, its tools
  and scopes agree, and each call runs as the caller. With ``CAIRN_EXAMPLES_LIVE=1`` and ``ANTHROPIC_API_KEY`` a
  real model chooses instead, and the cases' expected tools are checked.
* The plugins are exactly what ``examples/plugins/build.py`` builds from the skills and ``cairn ide init``.
* Every Cairn tool the skills and agents name exists on Cairn's MCP server, and every file's front matter is valid."""

from __future__ import annotations

import glob
import json
import os
import re

import pytest
import yaml
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle

ROOT = os.path.join(os.path.dirname(__file__), "..", "examples")
ADMIN = "urn:li:corpuser:admin"
DS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
AGENTS = sorted(glob.glob(os.path.join(ROOT, "agents", "*", "agent.yaml")))


def front_matter(path: str) -> dict:
    text = open(path).read()
    assert text.startswith("---\n"), path
    return yaml.safe_load(text.split("---\n")[1])


class Scripted:
    """Shaped like the Anthropic provider; answers with the case's tool calls, then its answer."""
    name = "anthropic"

    def __init__(self):
        self.script: list[list[dict]] = []
        me = self

        class _Messages:
            def create(self, **kw):
                content = me.script.pop(0) if me.script else [{"type": "text", "text": "done"}]
                stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
                return type("R", (), {"content": content, "stop_reason": stop, "model": kw["model"], "usage": None})()
        self.client = type("C", (), {"messages": _Messages()})()


@pytest.fixture
def cairn(settings, graph):
    from cairn.services import build_services

    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}, {"fieldPath": "email"},
                                                        {"fieldPath": "amount"}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"])]))
    live = os.environ.get("CAIRN_EXAMPLES_LIVE") == "1" and os.environ.get("ANTHROPIC_API_KEY")
    if live:
        from cairn.ai.llm import AnthropicProvider

        s.assistant.llm = AnthropicProvider(**({"model": m} if (m := os.environ.get("CAIRN_EXAMPLES_MODEL")) else {}))
    else:
        s.assistant.llm = Scripted()
    return s, TestClient(create_api_app(s)), bool(live)


@pytest.mark.parametrize("path", AGENTS, ids=[os.path.basename(os.path.dirname(p)) for p in AGENTS])
def test_each_example_agent_is_built_deployed_and_runs_its_cases(cairn, path):
    s, c, live = cairn
    h = {"X-Cairn-Actor": ADMIN}
    defn = yaml.safe_load(open(path))
    assert defn["name"] == os.path.basename(os.path.dirname(path))
    r = c.post("/v1/custom-agents", json=defn, headers=h)
    assert r.status_code == 201, r.text
    assert c.post(f"/v1/custom-agents/{defn['name']}/deploy", headers=h).status_code == 200
    cases = yaml.safe_load(open(os.path.join(os.path.dirname(path), "evals.yaml")))["cases"]
    assert cases
    for i, case in enumerate(cases):
        assert set(case["expect_tools"]) <= set(defn["tools"]), case
        if not live:
            s.assistant.llm.script = [[{"type": "tool_use", "id": f"tu_{i}_{j}", "name": step["tool"],
                                        "input": step["input"]}] for j, step in enumerate(case["script"])]
            s.assistant.llm.script.append([{"type": "text", "text": case["answer"]}])
        r = c.post(f"/v1/custom-agents/{defn['name']}/run", json={"input": case["input"]}, headers=h)
        assert r.status_code == 200, r.text
        run = r.json()
        called = [x["name"] for x in run["calls"]]
        if live:
            assert set(case["expect_tools"]) <= set(called), (case["input"], called)
        else:
            assert called == case["expect_tools"] and run["answer"] == case["answer"], run
        assert run["status"] == "ok" and all(x.get("status") != "error" for x in run["calls"]), run


def test_the_plugins_are_what_the_build_makes():
    import importlib.util

    here = os.path.join(ROOT, "plugins")
    spec = importlib.util.spec_from_file_location("cairn_examples_plugins_build", os.path.join(here, "build.py"))
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)                       # by path: never another module called build
    built = build.build()
    on_disk = {}
    for base, dirs, names in os.walk(here):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in names:
            rel = os.path.relpath(os.path.join(base, n), here)
            if rel in ("build.py", "README.md") or n in (".DS_Store",) or n.endswith("~"):
                continue
            on_disk[rel] = open(os.path.join(base, n)).read()
    assert on_disk == built, "run: python examples/plugins/build.py"
    # And none of it is ignored by git: a file only on this disk passes here and is missing from every checkout.
    import shutil
    import subprocess

    if shutil.which("git") and os.path.isdir(os.path.join(ROOT, "..", "..", ".git")):
        ignored = subprocess.run(["git", "check-ignore", "--no-index", *(os.path.join(here, f) for f in built)],
                                 capture_output=True, text=True, cwd=here).stdout.split()
        assert not ignored, f"git ignores generated files (add a !rule to .gitignore): {ignored}"
    plugin = json.loads(built["claude-code/cairn/.claude-plugin/plugin.json"])
    assert plugin["name"] == "cairn" and re.match(r"^\d+\.\d+\.\d+$", plugin["version"])
    market = json.loads(built[".claude-plugin/marketplace.json"])
    assert all(os.path.isdir(os.path.join(here, p["source"])) for p in market["plugins"])


def _tools_named(text: str) -> set[str]:
    return {m for m in re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text)}


def test_every_skill_and_agent_names_real_tools_and_has_valid_front_matter(cairn):
    s, _, _ = cairn
    import asyncio

    from cairn.ai.mcp_server import build_mcp_server

    server, _ = build_mcp_server(s)
    tools = {t.name for t in asyncio.run(server.list_tools())} | set(server._tool_manager._tools)
    files = glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md"))
    files += glob.glob(os.path.join(ROOT, "plugins", "claude-code", "cairn", "agents", "*.md"))
    assert len(files) >= 7
    for path in files:
        fm = front_matter(path)
        assert fm["name"] and len(fm["description"]) > 30, path
        if path.endswith("SKILL.md"):
            assert fm["name"] == os.path.basename(os.path.dirname(path))
        unknown = _tools_named(open(path).read()) - tools
        assert not unknown, (path, unknown)
    for path in AGENTS:
        defn = yaml.safe_load(open(path))
        named = _tools_named(defn["instructions"])
        assert named, (path, "its instructions name its tools in backticks")
        assert named <= set(defn["tools"]), (path, "it names tools it is not given", named - set(defn["tools"]))
        assert not named - set(s.custom_agents.specs), (path, named - set(s.custom_agents.specs))   # built agents' tools
