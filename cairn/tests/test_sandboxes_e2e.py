"""Code sandboxes (L116), end to end with real containers: each run is a new docker container (no network interface,
a read-only root, CPU, memory and time limits, an in-memory working directory) started by the real Cairn API, which
also runs the run's egress proxy. Through the real services, decision point, database and HTTP API, and a real SQLite
warehouse queried from inside the sandbox through Cairn. Checked:
- a computation over an input file returns its result and a generated file;
- the network is refused: no interface for direct connections, and the proxy lets through only Cairn (when asked
  for) and hosts an operator allows; asking for another host is refused before anything runs;
- the time limit and the memory limit stop a run, and say which;
- code querying Cairn sees what the person who asked may see: a masked column comes back masked (and unmasked for
  an administrator), with a token that is revoked when the run ends;
- RUN_CODE is needed, a person's concurrent runs are capped, runs are recorded, metered and audited, and the
  run_python tool (custom agents, the assistant, MCP) runs as its caller within the agent's scopes.

Needs docker and the python:3.12-slim image (CAIRN_SANDBOX_TEST_IMAGE)."""

import base64
import os
import shutil
import subprocess
import threading
import time

import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine, select, text

from cairn.app import create_api_app
from cairn.common.security import Principal
from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
from cairn.services import build_services
from test_trino_e2e import port, wait

IMAGE = os.environ.get("CAIRN_SANDBOX_TEST_IMAGE", "python:3.12-slim")
ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
USERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.users,PROD)"
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"


def _ready():
    if not shutil.which("docker"):
        return False
    if subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode != 0:
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not _ready(), reason=f"needs docker and the {IMAGE} image")


def sub(who):
    return {"attr": "subject.urn", "op": "eq", "value": who}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph

    tmp = tmp_path_factory.mktemp("sandboxes")
    wh = tmp / "wh.db"
    e = create_engine(f"sqlite:///{wh}")
    with e.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER, email TEXT, region TEXT)"))
        c.execute(text("INSERT INTO users VALUES (1, 'jane@acme.com', 'EU'), (2, 'bo@example.org', 'US')"))
    conns = tmp / "connections.yaml"
    conns.write_text(f"connections:\n  wh:\n    url: sqlite:///{wh}\n    catalog:\n      platform: sqlite\n")
    p = port()
    env = {"CAIRN_QUALITY_CONNECTIONS": str(conns), "CAIRN_SANDBOX_RUNTIME": "docker", "CAIRN_SANDBOX_IMAGE": IMAGE,
           "CAIRN_SANDBOX_CAIRN_URL": f"http://127.0.0.1:{p}", "CAIRN_SANDBOX_ALLOWED_HOSTS": "pypi.org",
           "CAIRN_SANDBOX_MAX_TIMEOUT_S": "120"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        graph = InMemoryGraph()
        graph.aspects[(USERS, "datasetProperties")] = {"name": "users"}
        graph.aspects[(USERS, "schemaMetadata")] = {"fields": [
            {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}},
            {"fieldPath": "region"}]}
        settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                            dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")})
        s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub(ADMIN)),
        Policy(id="coders", effect="allow", actions=["RUN_CODE"], condition=sub(ANA)),
        Policy(id="readers", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*main.users*"])),
        Policy(id="mask-pii", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})],
               condition={"attr": "subject.urn", "op": "neq", "value": ADMIN})]))
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    yield s, f"http://127.0.0.1:{p}"
    server.should_exit = True
    subprocess.run("docker ps -aq --filter name=cairn-sbx- | xargs -r docker rm -f", shell=True, capture_output=True)


def run(url, who, body, timeout=240):
    return httpx.post(f"{url}/v1/sandboxes/runs", json=body, headers={"X-Cairn-Actor": who}, timeout=timeout)


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def test_a_computation_returns_its_result_and_a_generated_file(stack):
    s, url = stack
    code = """
import csv, os, statistics
rows = list(csv.DictReader(open("data/sales.csv")))
total = sum(float(r["amount"]) for r in rows)
os.makedirs("out", exist_ok=True)
with open("out/report.csv", "w") as fh:
    fh.write("region,total\\n")
    for region in sorted({r["region"] for r in rows}):
        fh.write(f"{region},{sum(float(r['amount']) for r in rows if r['region'] == region)}\\n")
print("rows:", len(rows))
result = {"total": total, "median": statistics.median(float(r["amount"]) for r in rows),
          "user": os.getuid(), "root_writable": os.access("/usr", os.W_OK), "token": "CAIRN_TOKEN" in os.environ}
"""
    r = run(url, ANA, {"code": code, "files": [{"name": "data/sales.csv",
                                                 "content": b64("region,amount\nEU,10\nUS,5\nEU,2.5\n")}],
                       "limits": {"timeout_s": 60, "memory_mb": 256, "cpus": 0.5}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "succeeded", out
    assert out["result"] == {"total": 17.5, "median": 5.0, "user": 1000, "root_writable": False, "token": False}
    assert out["stdout"].strip() == "rows: 3"
    [f] = out["files"]
    assert f["name"] == "report.csv" and base64.b64decode(f["content"]).decode() == "region,total\nEU,12.5\nUS,5.0\n"
    # Recorded (never the code), audited, metered.
    rec = httpx.get(f"{url}/v1/sandboxes/runs/{out['id']}", headers={"X-Cairn-Actor": ANA}).json()
    assert rec["status"] == "succeeded" and rec["files"] == [{"name": "report.csv", "bytes": f["bytes"]}]
    assert rec["memory_mb"] == 256 and rec["cpus"] == 0.5 and "code" not in rec
    assert httpx.get(f"{url}/v1/sandboxes/runs/{out['id']}", headers={"X-Cairn-Actor": BOB}).status_code == 404
    usage = httpx.get(f"{url}/v1/sandboxes/usage", headers={"X-Cairn-Actor": ANA}).json()["usage"]
    assert usage[0]["actor"] == ANA and usage[0]["runs"] >= 1 and usage[0]["cpu_seconds"] > 0
    audit = [e for e in s.audit.entries("ai", limit=50) if e["action"] == "sandbox.run_succeeded"]
    assert audit and audit[-1]["actor"] == ANA and "statistics" not in str(audit[-1])
    # The container is gone.
    left = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=cairn-sbx-{out['id']}"], capture_output=True,
                          text=True).stdout.strip()
    assert left == ""


def test_the_network_is_refused(stack):
    s, url = stack
    code = """
import socket, urllib.request, os
out = {}
try:
    socket.create_connection(("1.1.1.1", 443), 3)
    out["direct"] = "connected"
except OSError as e:
    out["direct"] = type(e).__name__ + ": " + str(e)
try:
    urllib.request.urlopen("http://example.org/", timeout=5)
    out["proxy"] = "reached"
except Exception as e:
    out["proxy"] = str(getattr(e, "code", "")) + " " + (e.read().decode() if hasattr(e, "read") else str(e))
out["proxy_env"] = os.environ.get("HTTP_PROXY", "")
result = out
"""
    # No network asked for: no interface, and no proxy either.
    r = run(url, ANA, {"code": code}).json()
    assert r["status"] == "succeeded", r
    assert "unreachable" in r["result"]["direct"].lower() and r["result"]["proxy_env"] == ""
    assert "reached" not in r["result"]["proxy"]
    # With Cairn allowed: direct connections still fail; the proxy refuses any other host.
    r = run(url, ANA, {"code": code, "network": {"cairn": True}}).json()
    assert r["status"] == "succeeded", r
    assert "unreachable" in r["result"]["direct"].lower()
    assert r["result"]["proxy"].startswith("403") and "not allowed" in r["result"]["proxy"], r["result"]
    # Hosts an operator has not allowed are refused before anything runs.
    bad = run(url, ANA, {"code": "print(1)", "network": {"allow": ["example.org"]}})
    assert bad.status_code == 403 and "CAIRN_SANDBOX_ALLOWED_HOSTS" in bad.text
    assert run(url, ANA, {"code": "print(1)", "network": {"allow": ["10.0.0.1"]}}).status_code == 400


def test_time_and_memory_limits_stop_a_run(stack):
    s, url = stack
    started = time.monotonic()
    r = run(url, ANA, {"code": "while True:\n    pass\n", "limits": {"timeout_s": 3}}).json()
    assert r["status"] == "timeout" and "time limit (3 s)" in r["error"], r
    assert time.monotonic() - started < 60
    r = run(url, ANA, {"code": "blocks = [bytearray(16 << 20) for _ in range(40)]\nresult = len(blocks)\n",
                       "limits": {"memory_mb": 96, "timeout_s": 60}}).json()
    assert r["status"] == "memory" and "memory limit (96 MB)" in r["error"], r
    # Limits over the operator's caps are refused.
    assert run(url, ANA, {"code": "1", "limits": {"timeout_s": 999}}).status_code == 400
    assert run(url, ANA, {"code": "1", "limits": {"memory_mb": 32}}).status_code == 400


QUERY = """
import json, os, urllib.request
req = urllib.request.Request(os.environ["CAIRN_URL"] + "/data/query", method="POST",
    data=json.dumps({"sql": "SELECT id, email, region FROM main.users ORDER BY id", "connection": "wh"}).encode(),
    headers={"Authorization": "Bearer " + os.environ["CAIRN_TOKEN"], "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=30) as r:
    body = json.load(r)
result = {"rows": body["rows"], "masked": body["masked_columns"], "url": os.environ["CAIRN_URL"]}
"""


def test_a_governed_query_from_the_sandbox_sees_the_persons_masks(stack):
    from cairn.identity.agents import agent_tokens

    s, url = stack
    r = run(url, ANA, {"code": QUERY, "network": {"cairn": True}}).json()
    assert r["status"] == "succeeded", r
    assert r["result"]["url"] == "http://cairn"                     # Cairn's real address stays outside
    assert r["result"]["masked"] == ["email"]
    assert [row[1] for row in r["result"]["rows"]] == ["j•••@•••.com", "b•••@•••.org"]
    assert r["egress_bytes"] > 0
    a = run(url, ADMIN, {"code": QUERY, "network": {"cairn": True}}).json()
    assert [row[1] for row in a["result"]["rows"]] == ["jane@acme.com", "bo@example.org"], a
    # Each run's token was the person's, narrowed to reading, and is revoked now.
    with s.engine.connect() as c:
        toks = c.execute(select(agent_tokens).where(agent_tokens.c.client_id == "agt_cairn_sandbox")).all()
    assert {t.actor for t in toks} == {ANA, ADMIN} and all(t.revoked for t in toks)
    assert all(sorted(t.scopes) == ["query_data", "read"] for t in toks)
    # The read is attributed to the sandbox's agent identity.
    q = [e for e in s.audit.export(category="data") if e["action"] == "data.queried" and e["actor"] == ANA]
    assert q and q[-1]["details"].get("agent") == "agt_cairn_sandbox"


def test_who_may_run_concurrency_and_the_agent_tool(stack):
    s, url = stack
    bob = run(url, BOB, {"code": "print(1)"})
    assert bob.status_code == 403 and "RUN_CODE" in bob.text
    assert httpx.post(f"{url}/v1/sandboxes/runs", json={"code": "1"}, headers={"Authorization": "Bearer svc-secret"},
                      timeout=30).status_code == 403
    # One run at a time for Ana: a second while the first sleeps is refused.
    s.sandboxes.per_person = 1
    try:
        first = {}
        t = threading.Thread(target=lambda: first.update(r=run(url, ANA, {"code": "import time\ntime.sleep(6)\n"})))
        t.start()
        end = time.time() + 30
        while not s.sandboxes.list(ANA, 1) or s.sandboxes.list(ANA, 1)[0]["status"] != "running":
            assert time.time() < end
            time.sleep(0.1)
        second = run(url, ANA, {"code": "print(2)"})
        assert second.status_code == 429 and "at most 1" in second.text
        t.join(120)
        assert first["r"].json()["status"] == "succeeded"
    finally:
        s.sandboxes.per_person = 2
    # The run_python tool (custom agents, the assistant, MCP) runs as its caller, within the agent's scopes.
    tools = s.tools_for(Principal(ANA, "session"))
    out = tools.run_python("result = sum(range(10))", {"note.txt": "hi"})
    assert out["status"] == "succeeded" and out["result"] == 45
    reader = s.tools_for(Principal(ANA, "agent", agent="agt_x", scopes=("read",)))
    with pytest.raises(ValueError, match="query_data"):
        reader.run_python(QUERY, cairn_data=True)
    from cairn.ai.assistant import TOOL_SPECS
    from cairn.identity.agents import tool_scope

    assert "run_python" in {t["name"] for t in TOOL_SPECS} and tool_scope("run_python") == "query_data"


def test_sql_runs_over_duckdb_on_the_input_files(stack):
    has_duckdb = subprocess.run(["docker", "run", "--rm", "--network", "none", IMAGE, "python", "-c", "import duckdb"],
                                capture_output=True, timeout=120).returncode == 0
    if not has_duckdb:
        pytest.skip(f"{IMAGE} has no DuckDB (the cairn/sandbox image has)")
    s, url = stack
    r = run(url, ANA, {"language": "sql", "files": [{"name": "sales.csv",
                                                      "content": b64("region,amount\nEU,10\nUS,5\nEU,2.5\n")}],
                       "code": "SELECT region, sum(amount) AS total FROM read_csv_auto('sales.csv') "
                               "GROUP BY region ORDER BY region"}).json()
    assert r["status"] == "succeeded", r
    assert r["result"] == {"columns": ["region", "total"], "rows": [["EU", 12.5], ["US", 5.0]]}
