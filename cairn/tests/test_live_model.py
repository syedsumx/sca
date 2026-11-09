"""Live-model checks: Cairn's AI features against the real model provider, through Cairn's real AI gateway, with real
Trino, Cairn Store and Cairn Catalog where a feature needs data (the fixtures of ``tests/test_trino_e2e.py``).

Every model call goes the way agent code's do: an Anthropic client pointed at Cairn's gateway
(``/v1/ai/gateway/anthropic``), called as the person the feature works for, admitted on their ``USE_AI_GATEWAY``,
metered under them, and sent on with the provider key the gateway reads from its file
(``CAIRN_AI_GATEWAY_ANTHROPIC_KEY_FILE``). The suite checks outcomes, never exact text:

* 4.1, the LLM layer: a structured-output call validates against its schema, and adaptive thinking is accepted;
* L35, the BI AI builder: a question becomes a saved dashboard whose tiles run on Trino as the person (Bob, row
  filtered to EU, sees only EU);
* L64, custom agents: an agent with one tool and the read scope answers with its tool, stays within it, and is refused
  to someone not given it and for a scope it lacks;
* L94, the pipeline agent: a goal becomes a derived table that ships and checks green; a broken one is fixed and runs;
* L95, CoWork: a scheduled task runs as its owner and waits in the outbox until approved (Slack is a local stand-in:
  there is no Slack workspace in CI);
* L126, Ask an agent: plain words become a proposed change (a saved query and a flow) that plans cleanly.

A model's answers vary: each check runs once more on failure, then fails with the transcript of its model calls.
Tokens and estimated cost per check go to a JSON report (``CAIRN_LIVE_USAGE_FILE``).

Runs only with ``CAIRN_LIVE_MODEL=1`` and a provider key in ``ANTHROPIC_API_KEY_FILE`` (preferred) or
``ANTHROPIC_API_KEY``. Settings: ``CAIRN_LIVE_MODEL_NAME`` (default: Cairn's default model; one Cairn prices),
``CAIRN_LIVE_EFFORT``, ``CAIRN_LIVE_MAX_TOKENS`` (a ceiling on every call, default 8000), ``CAIRN_LIVE_MAX_TURNS``
(tool-loop turns, default 8), ``CAIRN_LIVE_PROVIDER_URL`` (default the provider's API; ``tests/live_model_standin.py``
for a plumbing check without a model). Needs docker and the store and catalog binaries for L35 and L94."""

from __future__ import annotations

import http.server
import json
import os
import tempfile
import threading
import time
from typing import Any, Callable, Optional

import httpx
import pytest

from cairn.ai.llm import AnthropicProvider, LLMError, validate_against_schema
from cairn.ai.usage import DEFAULT_PRICES, current_actor, set_actor, usage_table
from cairn.common.config import AiSettings
from cairn.governance.abac import Policy, PolicyBundle, Target
from test_trino_e2e import ORDERS, _ready, cairn, lake, trino  # noqa: F401 - the fixtures Trino's checks share

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
KEY_FILE = os.environ.get("ANTHROPIC_API_KEY_FILE", "")
HAVE_KEY = bool(os.environ.get("ANTHROPIC_API_KEY") or (KEY_FILE and os.path.isfile(KEY_FILE)))
MODEL = os.environ.get("CAIRN_LIVE_MODEL_NAME") or AiSettings().model
EFFORT = os.environ.get("CAIRN_LIVE_EFFORT") or None
MAX_TOKENS = int(os.environ.get("CAIRN_LIVE_MAX_TOKENS", "8000"))
MAX_TURNS = int(os.environ.get("CAIRN_LIVE_MAX_TURNS", "8"))
PROVIDER_URL = os.environ.get("CAIRN_LIVE_PROVIDER_URL", "https://api.anthropic.com")
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1")    # where Cairn asks for the fallback

pytestmark = pytest.mark.skipif(
    os.environ.get("CAIRN_LIVE_MODEL") != "1" or not HAVE_KEY,
    reason="live-model checks: set CAIRN_LIVE_MODEL=1 and ANTHROPIC_API_KEY_FILE (or ANTHROPIC_API_KEY)")
needs_trino = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")


def _clip(v: Any, n: int = 4000) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"... ({len(v) - n} more characters)"
    if isinstance(v, list):
        return [_clip(x, n) for x in v]
    if isinstance(v, dict):
        return {k: _clip(x, n) for k, x in v.items()}
    return v


class Recorder(httpx.AsyncBaseTransport):
    """The gateway's way out to the provider: every call recorded (bodies, never headers: the key stays out) and its
    ``max_tokens`` held to a ceiling. Proxies from the environment still apply (the inner client reads them)."""

    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens
        self.calls: list[dict] = []
        self.label: Optional[str] = None
        self._inner: Optional[httpx.AsyncClient] = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._inner is None:
            self._inner = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
        body = json.loads(await request.aread() or b"{}")
        if isinstance(body.get("max_tokens"), int) and body["max_tokens"] > self.max_tokens:
            body["max_tokens"] = self.max_tokens
        headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
        entry: dict = {"check": self.label, "request": body}
        self.calls.append(entry)
        stream = bool(body.get("stream"))
        resp = await self._inner.send(httpx.Request(request.method, request.url, headers=headers, json=body,
                                                    extensions=request.extensions), stream=stream)
        entry["status"] = resp.status_code
        if stream:
            entry["response"] = "(streamed)"
            return resp
        data = resp.content
        try:
            entry["response"] = json.loads(data)
        except ValueError:
            entry["response"] = data[:2000].decode(errors="replace")
        drop = ("content-encoding", "content-length", "transfer-encoding")
        return httpx.Response(resp.status_code, headers=[(k, v) for k, v in resp.headers.items() if k.lower() not in drop],
                              content=data)

    def of(self, label: str) -> list[dict]:
        return [c for c in self.calls if c["check"] == label]


class GatewayProvider:
    """Cairn's own model calls (its Anthropic provider, the assistant's tool loop) sent through Cairn's AI gateway as
    the person they are for: the request's actor, or ``default_actor`` where a feature does not set one. Nobody to
    act as: refused (fail closed). The client carries a Cairn key of that person's (an agent key with the ``model``
    scope, as code a person writes uses), never the provider's key."""

    name = "anthropic"

    def __init__(self, url: str, model: str, effort: Optional[str], key_for: Callable[[str], str]):
        self.url, self.model, self.key_for = url, model, key_for
        self.effort = effort or AiSettings().effort
        self.server_side_fallback = AiSettings().server_side_fallback
        self.default_actor: Optional[str] = None
        self._by: dict[str, AnthropicProvider] = {}

    def _for(self) -> AnthropicProvider:
        import anthropic

        actor = current_actor()
        actor = actor if actor and actor != "unknown" else self.default_actor
        if not actor:
            raise LLMError("no person to call the model as")
        if actor not in self._by:
            client = anthropic.Anthropic(base_url=f"{self.url}/v1/ai/gateway/anthropic", api_key=self.key_for(actor),
                                         max_retries=2, timeout=600)
            self._by[actor] = AnthropicProvider(self.model, self.effort, self.server_side_fallback, client=client)
        return self._by[actor]

    @property
    def client(self):
        return self._for().client

    def generate_json(self, *args, **kwargs):
        return self._for().generate_json(*args, **kwargs)


class Live:
    def __init__(self, url: str, s, rec: Recorder, provider: GatewayProvider, report: str):
        self.url, self.s, self.rec, self.provider, self.report = url, s, rec, provider, report
        self.checks: dict[str, dict] = {}
        self._mark = 0

    def _last_id(self) -> int:
        from sqlalchemy import func, select

        with self.s.engine.connect() as c:
            return int(c.execute(select(func.max(usage_table.c.id))).scalar() or 0)

    def begin(self, check: str, attempt: int) -> None:
        self._mark = self._last_id()
        self.rec.label = f"{check}#{attempt}"
        self.provider.default_actor = None
        set_actor(None)

    def end(self, check: str, attempt: int, outcome: str, error: Optional[str] = None) -> None:
        from sqlalchemy import select

        with self.s.engine.connect() as c:
            rows = c.execute(select(usage_table).where(usage_table.c.id > self._mark)).all()
        c = self.checks.setdefault(check, {"outcome": None, "attempts": [], "input_tokens": 0, "output_tokens": 0,
                                           "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0,
                                           "model_calls": 0})
        a = {"attempt": attempt, "outcome": outcome, "model_calls": len(rows), "error": error,
             "provider_calls": len(self.rec.of(f"{check}#{attempt}"))}
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            a[k] = sum(getattr(r, k) or 0 for r in rows)
            c[k] += a[k]
        a["cost_usd"] = round(sum(r.cost_usd or 0 for r in rows), 6)
        c["cost_usd"] = round(c["cost_usd"] + a["cost_usd"], 6)
        c["model_calls"] += a["model_calls"]
        c["attempts"].append(a)
        c["outcome"] = outcome
        self.rec.label = None

    def transcript(self, check: str) -> str:
        path = os.path.join(os.path.dirname(self.report) or ".", "live-model-transcripts", f"{check}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        calls = [c for c in self.rec.calls if str(c["check"]).startswith(f"{check}#")]
        with open(path, "w") as f:
            json.dump(_clip(calls, 20000), f, indent=1, default=str)
        return path

    def write(self) -> None:
        keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "model_calls")
        total = {k: sum(c[k] for c in self.checks.values()) for k in keys}
        total["cost_usd"] = round(sum(c["cost_usd"] for c in self.checks.values()), 6)
        out = {"model": MODEL, "provider": httpx.URL(PROVIDER_URL).host, "effort": self.provider.effort,
               "max_tokens": MAX_TOKENS, "max_turns": MAX_TURNS,
               "price_usd_per_mtok": dict(zip(("input", "output"), _price(MODEL))),
               "checks": self.checks, "total": total, "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        os.makedirs(os.path.dirname(self.report) or ".", exist_ok=True)
        with open(self.report, "w") as f:
            json.dump(out, f, indent=1)


def _price(model: str) -> tuple[float, float]:
    best = max((p for p in DEFAULT_PRICES if model.startswith(p)), key=len, default=None)
    return DEFAULT_PRICES[best] if best else (0.0, 0.0)


def twice(live: Live, check: str, body: Callable[[int], None]) -> None:
    """``body(attempt)``, once more if it fails (a model's answers vary), then a failure with the transcript."""
    errors = []
    for attempt in (1, 2):
        live.begin(check, attempt)
        try:
            body(attempt)
        except Exception as e:  # noqa: BLE001 - every failure counts; pytest's own outcomes are BaseExceptions
            msg = f"{type(e).__name__}: {e}"
            errors.append(f"attempt {attempt}: {msg[:3000]}")
            live.end(check, attempt, "failed", msg[:2000])
            continue
        finally:
            set_actor(None)
        live.end(check, attempt, "passed")
        return
    path = live.transcript(check)
    tail = json.dumps(_clip(live.rec.calls[-4:], 1500), indent=1, default=str)
    pytest.fail(f"{check} failed twice with {MODEL}\n" + "\n".join(errors) + f"\ntranscript: {path}\nlast calls:\n{tail}",
                pytrace=False)


@pytest.fixture(scope="module", autouse=True)
def gateway_env(tmp_path_factory):
    """Cairn's AI gateway, set up the way an operator does: the provider's URL, the key as a file, the model served."""
    assert MODEL in DEFAULT_PRICES, f"CAIRN_LIVE_MODEL_NAME={MODEL}: use a model Cairn lists ({', '.join(DEFAULT_PRICES)})"
    key_file = KEY_FILE
    if not key_file:                       # the key from the environment, into a file only this process reads
        key_file = str(tmp_path_factory.mktemp("live-key") / "provider.key")
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(os.environ["ANTHROPIC_API_KEY"])
    env = {"CAIRN_AI_GATEWAY_ANTHROPIC_URL": PROVIDER_URL, "CAIRN_AI_GATEWAY_ANTHROPIC_KEY_FILE": key_file,
           "CAIRN_AI_GATEWAY_MODELS": MODEL, "CAIRN_IAC_AGENT_MODEL": MODEL}
    gone = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CAIRN_AI_GATEWAY_ANTHROPIC_KEY",
            "CAIRN_AI_GATEWAY_UPSTREAMS", "CAIRN_AI_GATEWAY_RPM")
    old = {k: os.environ.get(k) for k in (*env, *gone)}
    for k in gone:                         # only the gateway holds the key; the features' clients never see it
        os.environ.pop(k, None)
    os.environ.update(env)
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="module")
def live(cairn):  # noqa: F811 - test_trino_e2e's fixture, built with the gateway's settings above
    import cairn.iac.assistant as iac_assistant

    url, s = cairn
    assert s.ai_gateway is not None, "the AI gateway did not start"
    rec = Recorder(MAX_TOKENS)
    s.ai_gateway.http = httpx.AsyncClient(transport=rec, timeout=httpx.Timeout(600, connect=10))
    me = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=list(s.pdp.bundle.policies) + [
        Policy(id="live-model", effect="allow", actions=["USE_AI_GATEWAY"],
               target=Target(resource_urns=[f"urn:li:aiModel:{MODEL}"]), condition={"attr": "subject.urn", "op": "exists"}),
        Policy(id="live-agents", effect="allow", actions=["USE_AGENT"],
               target=Target(resource_urns=["urn:li:cairnAgent:live-*"]), condition=me(ANA))]))
    from cairn.identity.agents import AgentIn

    def key_for(actor: str) -> str:
        made = s.agents.create_key_agent(AgentIn(name=f"live model {actor.rsplit(':', 1)[-1]}", scopes=["model"],
                                                 owner=actor), ADMIN)
        return made["key"]
    provider = GatewayProvider(url, MODEL, EFFORT, key_for)
    s.assistant.llm, s.assistant.model, s.assistant.max_iterations = provider, MODEL, MAX_TURNS
    if EFFORT:
        s.assistant.effort = EFFORT
    s.iac_assistant.model = MODEL
    steps = iac_assistant.MAX_STEPS
    iac_assistant.MAX_STEPS = MAX_TURNS
    report = os.environ.get("CAIRN_LIVE_USAGE_FILE") or os.path.join(tempfile.gettempdir(), "live-model-usage.json")
    run = Live(url, s, rec, provider, report)
    yield run
    iac_assistant.MAX_STEPS = steps
    run.write()


def test_4_1_structured_output_validates_and_adaptive_thinking_is_accepted(live):
    schema = {"type": "object", "additionalProperties": False, "required": ["answer", "capital"],
              "properties": {"answer": {"type": "integer"}, "capital": {"type": "string"}}}

    def check(attempt):
        live.provider.default_actor = ADMIN
        data, usage = live.provider.generate_json(
            "You answer short arithmetic and geography questions.",
            "Simple arithmetic: what is 17 times 3? And what is the capital city of France?", schema, max_tokens=4000)
        validate_against_schema(data, schema)
        assert data["answer"] == 51 and "paris" in data["capital"].lower(), data
        assert usage.input_tokens > 0 and usage.output_tokens > 0
        [call] = live.rec.of(f"4.1#{attempt}")
        req = call["request"]
        assert call["status"] == 200, call["response"]             # adaptive thinking and the format were accepted
        assert req["thinking"] == {"type": "adaptive"} and req["output_config"]["effort"] == live.provider.effort
        assert req["output_config"]["format"] == {"type": "json_schema", "schema": schema}
        assert ("fallbacks" in req) == (MODEL in FALLBACK_MODELS)   # the refusal fallback where Cairn asks for it
    twice(live, "4.1", check)


@needs_trino
def test_l35_the_ai_builder_makes_a_dashboard_that_runs_as_the_person(live, trino):  # noqa: F811
    from cairn.quality.collectors import ConnectionRegistry

    s, url = live.s, live.url
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.lm_orders,PROD)"
    s.graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in ("id", "region", "status", "amount")]}
    trino("CREATE TABLE IF NOT EXISTS lake.sales.lm_orders AS SELECT * FROM (VALUES (1, 'EU', 'paid', 100e0), "
          "(2, 'EU', 'refunded', 40e0), (3, 'US', 'paid', 70e0)) AS t(id, region, status, amount)")
    s.metrics.save(ADMIN, {"name": "lm_revenue", "label": "Revenue", "connection": "lakehouse", "table": "sales.lm_orders",
                           "measure": "Sum([amount])", "filters": [{"column": "status", "op": "=", "value": "paid"}],
                           "dimensions": ["region", "status"]})
    s.builder.llm, s.builder.model = live.provider, MODEL

    def regions(tiles):
        seen = set()
        for t in tiles:
            res = t.get("result") or {}
            cols = [c if isinstance(c, str) else c.get("name") for c in res.get("columns") or []]
            idx = [i for i, c in enumerate(cols) if str(c).lower() == "region"]
            seen |= {row[idx[0]] for row in res.get("rows") or [] if idx}
        return seen

    def check(attempt):
        live.provider.default_actor = ADMIN
        r = httpx.post(f"{url}/sql/builder", json={
            "question": "Revenue by region this quarter from paid orders, and the number of orders per status",
            "datasets": [urn], "save": True, "name": f"Live revenue {attempt}"},
            headers={"X-Cairn-Actor": ADMIN}, timeout=900)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["tiles"] and not [t for t in out["tiles"] if t.get("error")], out["tiles"]
        assert any((t.get("result") or {}).get("rows") for t in out["tiles"]), out["tiles"]   # it ran on Trino
        saved = s.sql_boards.dashboard(ADMIN, out["dashboard"]["id"])
        assert saved["name"] == f"Live revenue {attempt}" and not saved["shared"] and saved["tiles"]
        assert regions(out["tiles"]) >= {"EU", "US"}, out["tiles"]
        # Bob asks: the tiles run as him, and his row filter keeps him to EU.
        live.provider.default_actor = BOB
        b = httpx.post(f"{url}/sql/builder", json={"question": "Paid revenue by region", "datasets": [urn]},
                       headers={"X-Cairn-Actor": BOB}, timeout=900)
        assert b.status_code == 200, b.text
        assert regions(b.json()["tiles"]) == {"EU"}, b.json()["tiles"]
        assert b.json()["dashboard"] is None
    twice(live, "L35", check)


def test_l64_a_custom_agent_uses_its_tool_and_stays_in_its_scope(live):
    from cairn.ai.custom_agents import AgentBuildError
    from cairn.common.security import Principal

    s = live.s
    ca = s.custom_agents

    def check(attempt):
        name = f"live-steward-{attempt}"
        a = ca.create(ADMIN, {"name": name, "description": "Reports data quality",
                              "instructions": "Report the data quality of the dataset you are asked about. Always use "
                                              "your tool first. You cannot change anything.",
                              "tools": ["get_quality_status"], "scopes": ["read"], "max_steps": 4})
        ca.deploy(ADMIN, name)
        r = ca.run(ANA, name, f"How is the data quality of the dataset {ORDERS}? If you can, also tag it Gold.")
        assert r["status"] == "ok" and r["answer"], r
        names = [c["name"] for c in r["calls"]]
        assert "get_quality_status" in names and set(names) == {"get_quality_status"}, r["calls"]
        acts = s.traces.activity(agent=a["agent_id"])
        assert acts and {(x["tool"], x["actor"]) for x in acts} == {("get_quality_status", ANA)}, acts
        tags = (s.graph.get_aspect(ORDERS, "globalTags") or {}).get("tags") or []
        assert not [t for t in tags if "Gold" in str(t)]                 # nothing outside its tool happened
        # Outside its scope: someone not given it, and a tool needing a scope it lacks.
        with pytest.raises(AgentBuildError) as e:
            ca.run(BOB, name, "How is the data quality of the orders?")
        assert e.value.status == 404
        with pytest.raises(PermissionError, match="propose"):
            s.assistant._run_tool(Principal(ANA, "agent", agent=a["agent_id"], scopes=("read",)), "suggest_tags",
                                  {"urn": ORDERS, "tags": ["Gold"]})
    twice(live, "L64", check)


@needs_trino
def test_l94_the_pipeline_agent_writes_tries_fixes_and_ships(live, trino):  # noqa: F811
    from cairn.ai.pipeline_agent import PipelineAgent
    from cairn.common.security import Principal
    from cairn.pipelines.derived import DerivedTables
    from cairn.quality.collectors import ConnectionRegistry

    s = live.s
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    definition = s.data_access.registry.definitions()["lakehouse"]

    def run(actor, sql, limit=100_000):
        return s.data_access.run_on_trino(definition, actor, sql, limit)
    run(ADMIN, "CREATE TABLE IF NOT EXISTS lake.sales.lm_pa_orders AS SELECT * FROM (VALUES (1, 'EU', 'paid', 100e0), "
               "(2, 'EU', 'refunded', -40e0), (3, 'US', 'paid', 70e0)) AS t(id, region, status, amount)")
    dt = DerivedTables(s.engine, lambda owner, sql: run(owner, sql)[1], "lake", s.audit)

    def context(actor, goal, urns, can_view):
        return {"sales.lm_pa_orders": ["id", "region", "status", "amount"]}
    agent = PipelineAgent(s.engine, live.provider, MODEL, dt, run, context, s.audit)
    can_view = s.can_view(Principal(urn=ADMIN, via="token"))

    def check(attempt):
        set_actor(ADMIN)
        target = f"sales.lm_revenue_{attempt}"
        t = agent.create(ADMIN, {"goal": "Paid revenue per region, as columns region and revenue", "target": target,
                                 "checks": [{"name": "no_negative",
                                             "sql": "SELECT count(*) FROM candidate WHERE revenue < 0"}]}, can_view)
        assert t["status"] == "ready", t["steps"]
        shipped = agent.approve(ADMIN, t["id"])
        assert shipped["status"] == "shipped", shipped["outcome"]
        assert all(c["passed"] for c in shipped["outcome"]["checks"]) and shipped["outcome"]["run"]["status"] == "ok"
        got = {r["region"]: float(r["revenue"]) for r in trino(f"SELECT region, revenue FROM {target}")}
        assert got == {"EU": 100.0, "US": 70.0}, got
        # A broken pipeline (a column that does not exist) is fixed from its failed run, and runs.
        broken = dt.create({"name": f"lm_usd_{attempt}", "target": f"sales.lm_usd_{attempt}", "every_minutes": 60,
                            "mode": "full", "sql": "SELECT id, amount * fx AS amount_usd FROM lake.sales.lm_pa_orders"},
                           ADMIN, ADMIN)
        assert dt.refresh(broken["id"])["status"] == "error"
        fix = agent.create(ADMIN, {"fix": broken["id"]}, can_view)
        assert fix["kind"] == "fix" and fix["status"] == "ready", fix["steps"]
        done = agent.approve(ADMIN, fix["id"])
        assert done["status"] == "shipped" and done["outcome"]["derived_id"] == broken["id"], done["outcome"]
        assert done["outcome"]["run"]["status"] == "ok"
        assert sorted(r["id"] for r in trino(f"SELECT id FROM sales.lm_usd_{attempt}")) == [1, 2, 3]
    twice(live, "L94", check)


def test_l95_a_scheduled_task_runs_and_waits_for_approval(live, monkeypatch):
    from cairn.notify.channels import SlackSender

    s = live.s
    c = s.cowork
    posted: list[dict] = []

    class Slack(http.server.BaseHTTPRequestHandler):     # Slack's Web API (chat.postMessage): no workspace in CI
        def do_POST(self):
            posted.append({"path": self.path, **json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            out = json.dumps({"ok": True, "ts": "1.2"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slack)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("CAIRN_COWORK_SLACK_CHANNELS", "C0LIVE")
    s.notify.channels["slack"] = SlackSender("xoxb-standin", api_url=f"http://127.0.0.1:{srv.server_address[1]}/api")

    made: list[str] = []

    def check(attempt):
        posted.clear()
        for tid in made:                                  # a failed attempt's task never runs again
            c.delete_task(ADMIN, tid)
        made.clear()
        t = c.save_task(ADMIN, {"name": f"Weekly orders {attempt}", "cron": "0 8 * * 1", "max_steps": 1,
                                "prompt": "Summarise what the catalog knows about the orders datasets, citing them",
                                "send": {"slack": "C0LIVE"}})
        made.append(t["id"])
        due = t["next_run_ms"] + 1
        c._ms = lambda: due
        try:
            assert c.run_due(due) == 1                    # due on its schedule, run once, as its owner
        finally:
            del c._ms
        [run] = c.task(ADMIN, t["id"])["runs"]
        assert run["status"] == "done", run["error"]
        report = s.ai_workspace.artifact(ADMIN, run["artifact_id"])
        assert report["kind"] == "report" and len(str(report.get("content") or "")) > 20, report
        assert posted == []                                              # nothing leaves before approval
        [item] = [i for i in c.outbox(ADMIN, "pending") if i["id"] == run["outbox_id"]]
        assert item["target"] == "C0LIVE"
        sent = c.approve(ADMIN, item["id"])
        assert sent["status"] == "sent", sent
        assert [p["channel"] for p in posted] == ["C0LIVE"]
    try:
        twice(live, "L95", check)
    finally:
        srv.shutdown()


def test_l126_ask_an_agent_drafts_a_query_and_a_flow_that_plan_cleanly(live):
    url = live.url
    admin = {"X-Cairn-Actor": ADMIN}

    def check(attempt):
        prompt = (f"Add a saved query named live-revenue-{attempt} on the connection lakehouse with the SQL "
                  "SELECT region, sum(amount) AS revenue FROM sales.orders GROUP BY region, and a flow named "
                  f"live-nightly-{attempt} that runs that SQL on lakehouse every night at 02:00 UTC and then sends a "
                  "notification that it is done.")
        r = httpx.post(f"{url}/v1/iac/changes/draft", json={"prompt": prompt}, headers=admin, timeout=900)
        assert r.status_code == 200, r.text
        out = r.json()
        change = out["change"]
        assert change and change["status"] == "proposed", out
        assert change["agent"] == "cairn-iac-assistant" and change["sponsor"] == ADMIN
        assert any(c["name"] == "iac_plan" and c["ok"] for c in out["tool_calls"]), out["tool_calls"]
        assert out["flows"] and all((f["spec"] or {}).get("steps") for f in out["flows"]), out["flows"]
        # The proposed change plans cleanly again, as the person: a saved query and a flow to create.
        p = httpx.post(f"{url}/v1/iac/changes/plan", json={"bundle_yaml": change["bundle"]}, headers=admin, timeout=120)
        assert p.status_code == 200, p.text
        kinds = {(c["action"], c["kind"]) for c in p.json()["changes"]}
        assert {("create", "queries"), ("create", "flows")} <= kinds, kinds
    twice(live, "L126", check)
