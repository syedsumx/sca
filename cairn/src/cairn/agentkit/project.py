"""``cairn agents init | dev | deploy | invoke``: an agent project from an empty directory to a deployed agent app.

* ``init <dir> [--template basic|openai-agents|langgraph] [--name n]`` writes a project: ``agent.py`` (the agent),
  ``agent.toml`` (its name, model and scopes), ``requirements.txt``, ``tests/test_agent.py`` (runs offline with
  :class:`cairn.agentkit.testing.FakeContext`) and ``ui/index.html`` (a chat page).
* ``dev [--dir .] [--port 8765]`` runs it on your machine (``cairn.agentkit.runtime``, dev mode): every model and data
  call goes through Cairn as you (``CAIRN_URL`` and ``CAIRN_TOKEN``, never on the command line), sessions are kept in
  ``.cairn/``, the chat page is at ``http://127.0.0.1:8765/``.
* ``deploy [--dir .]`` sends it to Cairn (``POST /v1/agent-apps``), which runs it as an agent app.
* ``invoke <name> <message> [--session s]`` asks a deployed agent, as you.
"""

from __future__ import annotations

import base64
import io
import os
import re
import tarfile
from pathlib import Path
from typing import Any, Optional

NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
SKIP = {".cairn", "__pycache__", ".venv", "venv", ".git", ".pytest_cache", "node_modules"}
TEMPLATES = ("basic", "openai-agents", "langgraph")

TOML = '''name = "{name}"
description = "{description}"
template = "{template}"
model = "{model}"
# What the agent may do for the person it works for (their own rights, narrowed to these): "model" calls models
# through Cairn's AI gateway, "query_data" runs SQL as them, "read" reads the catalog.
scopes = ["model", "query_data"]
'''

AGENT = {
    "basic": '''"""{name}: answers questions about the data team's tables, with SQL run through Cairn as the person asking."""

import json

MODEL = "{model}"
SYSTEM = ("You help people with their data. When a question needs data, call run_sql with one read-only Trino "
          "SELECT; then answer briefly from the rows.")
TOOLS = [{{"name": "run_sql", "description": "Run one read-only SQL query through Cairn, as the person asking.",
          "input_schema": {{"type": "object", "properties": {{"sql": {{"type": "string"}}}}, "required": ["sql"]}}}}]


def respond(ctx, messages):
    """The answer to the conversation so far (messages: role/content dicts, newest last)."""
    client = ctx.anthropic()                      # Claude, through Cairn's AI gateway, as the person
    convo = [dict(m) for m in messages]
    for _ in range(5):
        msg = client.messages.create(model=MODEL, max_tokens=1024, system=SYSTEM, tools=TOOLS, messages=convo)
        if msg.stop_reason != "tool_use":
            return "".join(b.text for b in msg.content if b.type == "text")
        results = []
        for block in msg.content:
            if block.type == "tool_use":
                try:
                    out = json.dumps(ctx.sql(block.input["sql"], limit=50).get("rows", []), default=str)
                except Exception as e:  # noqa: BLE001 - the model is told and may try again
                    out = f"error: {{e}}"
                results.append({{"type": "tool_result", "tool_use_id": block.id, "content": out[:20000]}})
        said = [{{"type": "text", "text": b.text}} if b.type == "text" else
                {{"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}} for b in msg.content]
        convo += [{{"role": "assistant", "content": said}}, {{"role": "user", "content": results}}]
    return "I could not finish that in five steps."
''',
    "openai-agents": '''"""{name}: an OpenAI Agents SDK agent; its model calls go through Cairn's AI gateway as the person asking."""

import json

from agents import Agent, OpenAIChatCompletionsModel, Runner, function_tool, set_tracing_disabled
from openai import AsyncOpenAI

MODEL = "{model}"
INSTRUCTIONS = "You help people with their data. Use run_sql for questions about data; answer briefly."
set_tracing_disabled(True)                        # traces stay in Cairn, not with a model vendor


def respond(ctx, messages):
    gw = ctx.gateway
    client = AsyncOpenAI(base_url=gw["openai_base_url"], api_key=gw["api_key"])

    @function_tool
    def run_sql(sql: str) -> str:
        """Run one read-only SQL query through Cairn, as the person asking."""
        return json.dumps(ctx.sql(sql, limit=50).get("rows", []), default=str)

    agent = Agent(name="{name}", instructions=INSTRUCTIONS, tools=[run_sql],
                  model=OpenAIChatCompletionsModel(model=MODEL, openai_client=client))
    return Runner.run_sync(agent, [dict(m) for m in messages]).final_output
''',
    "langgraph": '''"""{name}: a LangGraph agent; its model calls go through Cairn's AI gateway as the person asking."""

import json

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

MODEL = "{model}"
PROMPT = "You help people with their data. Use run_sql for questions about data; answer briefly."


def respond(ctx, messages):
    gw = ctx.gateway
    llm = ChatOpenAI(model=MODEL, base_url=gw["openai_base_url"], api_key=gw["api_key"])

    @tool
    def run_sql(sql: str) -> str:
        """Run one read-only SQL query through Cairn, as the person asking."""
        return json.dumps(ctx.sql(sql, limit=50).get("rows", []), default=str)

    graph = create_react_agent(llm, [run_sql], prompt=PROMPT)
    out = graph.invoke({{"messages": [(m["role"], m["content"]) for m in messages]}})
    return out["messages"][-1].content
''',
}
REQUIREMENTS = {"basic": "anthropic>=0.40\n", "openai-agents": "openai-agents>=0.2\nopenai>=1.40\n",
                "langgraph": "langgraph>=0.2\nlangchain-openai>=0.2\n"}
MODELS = {"basic": "claude-sonnet-5-5", "openai-agents": "gpt-4.1-mini", "langgraph": "gpt-4.1-mini"}
TEST = '''"""{name}: runs offline; the model is a local stand-in with scripted replies (cairn.agentkit.testing)."""

import agent
from cairn.agentkit.testing import FakeContext


def test_it_answers():
    with FakeContext(replies=["Hello from the agent"]) as ctx:
        assert agent.respond(ctx, [{{"role": "user", "content": "Hi"}}]) == "Hello from the agent"
        assert ctx.requests and ctx.requests[0]["model"] == agent.MODEL
'''


class ProjectError(Exception):
    pass


def init(directory: str, name: Optional[str] = None, template: str = "basic", description: str = "") -> Path:
    if template not in TEMPLATES:
        raise ProjectError(f"template: one of {', '.join(TEMPLATES)}")
    root = Path(directory)
    name = name or root.resolve().name.lower().replace("_", "-")
    if not NAME.match(name):
        raise ProjectError("name: lowercase letters, digits and -, starting with a letter (2 to 41 characters)")
    if root.exists() and any(root.iterdir()):
        raise ProjectError(f"{root} is not empty")
    from cairn.agentkit.runtime import DEFAULT_UI

    model = MODELS[template]
    files = {"agent.toml": TOML.format(name=name, description=description.replace('"', "'"), template=template,
                                       model=model),
             "agent.py": AGENT[template].format(name=name, model=model),
             "requirements.txt": REQUIREMENTS[template],
             "tests/test_agent.py": TEST.format(name=name),
             "tests/conftest.py": "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).parent.parent))\n",
             "ui/index.html": DEFAULT_UI,
             ".gitignore": ".cairn/\n__pycache__/\n.venv/\n"}
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def manifest(directory: str) -> dict:
    import tomllib

    path = Path(directory) / "agent.toml"
    if not path.is_file():
        raise ProjectError(f"{directory} has no agent.toml (cairn agents init)")
    return tomllib.loads(path.read_text())


def bundle(directory: str) -> bytes:
    """The project as a gzipped tar, without local state (.cairn, caches, virtualenvs, .git)."""
    root = Path(directory).resolve()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if any(part in SKIP for part in rel.parts) or p.suffix == ".pyc" or not p.is_file():
                continue
            tar.add(str(p), arcname=str(rel))
    return buf.getvalue()


def _client(cairn_url: Optional[str], headers: Optional[dict]):
    import httpx

    url = (cairn_url or os.environ.get("CAIRN_URL", "")).rstrip("/")
    if not url:
        raise ProjectError("set CAIRN_URL to your Cairn")
    if headers is None:
        token = os.environ.get("CAIRN_TOKEN", "")
        if not token:
            raise ProjectError("set CAIRN_TOKEN to your Cairn token (it is read from the environment, never an argument)")
        headers = {"Authorization": f"Bearer {token}"}
    return httpx.Client(base_url=url, headers=headers, timeout=300)


def deploy(directory: str = ".", cairn_url: Optional[str] = None, headers: Optional[dict] = None) -> dict:
    m = manifest(directory)
    with _client(cairn_url, headers) as c:
        r = c.post("/v1/agent-apps", json={"manifest": m, "bundle": base64.b64encode(bundle(directory)).decode()})
    if r.status_code not in (200, 201):
        raise ProjectError(f"Cairn refused the deploy ({r.status_code}): {r.text[:500]}")
    return r.json()


def invoke(name: str, message: str, session: str = "cli", cairn_url: Optional[str] = None,
           headers: Optional[dict] = None) -> dict:
    with _client(cairn_url, headers) as c:
        r = c.post(f"/v1/agent-apps/{name}/invocations", json={"input": message, "session_id": session})
    if r.status_code != 200:
        raise ProjectError(f"{r.status_code}: {r.text[:500]}")
    return r.json()


def dev(directory: str = ".", port: int = 8765) -> None:
    from cairn.agentkit import runtime

    manifest(directory)
    for var in ("CAIRN_URL", "CAIRN_TOKEN"):
        if not os.environ.get(var):
            raise ProjectError(f"set {var} (your Cairn and your token; read from the environment)")
    print(f"agent running at http://127.0.0.1:{port}/ (its model and data calls go through Cairn as you)")
    runtime.main(["--dir", directory, "--mode", "dev", "--port", str(port)])


def cli(a: Any) -> None:
    """The ``cairn agents`` command."""
    import json
    import sys

    try:
        if a.agents_cmd == "init":
            root = init(a.dir, a.name, a.template)
            print(f"made {root}: edit agent.py, run its tests (pytest {root}/tests), then cairn agents dev")
        elif a.agents_cmd == "dev":
            dev(a.dir, a.port)
        elif a.agents_cmd == "deploy":
            print(json.dumps(deploy(a.dir), indent=2))
        elif a.agents_cmd == "invoke":
            print(invoke(a.name, a.message, a.session)["output"])
    except ProjectError as e:
        print(f"cairn agents: {e}", file=sys.stderr)
        sys.exit(2)
