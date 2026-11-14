"""The SRE agent (L142) end to end, against real components: the bundled observability stack's containers (OpenTelemetry
Collector, Prometheus, Loki, Tempo; tests/test_observability_e2e.py's fixture), a managed PostgreSQL 16 instance run by
Cairn's Docker runtime (L140) with a real Cairn Store binary for its WAL, and Cairn's real AI gateway in front of the
deterministic stand-in model (tests/live_model_standin.py, as the live-model suite's plumbing check uses it).

1. **A missing index.** An application ("shop-api", its spans sent to the Collector) runs a statement that scans a
   400,000-row table on the instance; its latency alert (PromQL on the span metrics, labelled with the instance)
   fires and opens an incident; the agent joins by itself (acting for the on-call person in its settings), runs the
   matching runbook's steps first, diagnoses the instance (the statement planned with EXPLAIN on a short-lived read
   login: a sequential scan no index serves), writes a finding citing that evidence, ranks the causes and proposes a
   change request creating the index (kind postgres_indexes). The agent cannot approve it, apply it or run anything
   with its own credential; someone without the privileges is refused; an approver approves and applies it as
   themselves, the index is built, the statement is fast, the alert resolves; the incident is resolved and the agent
   drafts a postmortem that a person edits and approves.
2. **Connections filled.** Sessions idle in a transaction fill the instance's connection slots; a person opens an
   incident (not one the agent joins by itself) and asks the agent to investigate; it proposes a restart (a runbook
   action); a person without MANAGE_DATABASE_INSTANCE is refused; an approver's approval restarts the instance as
   them (audited as them) and the slots are free.
3. **Fail closed.** No model: it says so in the incident and does nothing. A model the gateway does not serve: the
   same. A stopped component (Loki): the tool that reads it fails, and the agent says so instead of guessing.
4. Nothing it wrote (notes, evidence, runs, the postmortem) carries a credential: no read login's password, the
   instance's superuser password, the provider's key or the agent's own token.

Telemetry is global to a process: CI runs this file in a pytest of its own (job trino). Needs Docker with the stack's
images, postgres:16-alpine and python:3.12-slim, and CAIRN_STORE_BIN."""

from __future__ import annotations

import base64
import http.server
import json
import os
import socket
import subprocess
import threading
import time
import uuid

import httpx
import pytest

pytest.importorskip("opentelemetry.sdk")

from live_model_standin import handler as standin_handler  # noqa: E402
from test_observability_e2e import _ready as stack_ready, docker, port, stack, until  # noqa: E402,F401
from test_postgres_instances import BIN, DOCKER, KEY as STORE_KEY, store  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not (BIN and DOCKER and stack_ready()),
                                reason="needs CAIRN_STORE_BIN, Docker and the observability stack's images")

OPS, ANA, BOB = "urn:li:corpuser:ops", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
MODEL = "sre-standin"
PROVIDER_KEY = "provider-key-held-by-cairn-" + uuid.uuid4().hex
INSTANCE = "sre-shop-pg"
ROWS = 400_000


@pytest.fixture(scope="module", autouse=True)
def pg_images():
    for image in ("postgres:16-alpine", "python:3.12-slim"):
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
            subprocess.run(["docker", "pull", "-q", image], check=True, capture_output=True)


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    """The stand-in for the model provider: the gateway reads its key from a file and sends it there."""
    d = tmp_path_factory.mktemp("provider")
    key_file = d / "provider.key"
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(PROVIDER_KEY)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), standin_handler(PROVIDER_KEY))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{srv.server_address[1]}", "key_file": str(key_file)}
    srv.shutdown()


@pytest.fixture(scope="module")
def cairn(stack, store, model, tmp_path_factory):  # noqa: F811
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.observability import telemetry
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("sre")
    files = {"CAIRN_PG_INSTANCE_KEY_FILE": base64.b64encode(os.urandom(32)).decode(),
             "CAIRN_PG_BACKUP_ACCESS_KEY_FILE": STORE_KEY[0], "CAIRN_PG_BACKUP_SECRET_KEY_FILE": STORE_KEY[1]}
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{stack['otlp_http']}", "OTEL_TRACES_SAMPLER": "always_on",
           "OTEL_BSP_SCHEDULE_DELAY": "300", "OTEL_BLRP_SCHEDULE_DELAY": "300", "OTEL_METRIC_EXPORT_INTERVAL": "2000",
           "CAIRN_OBS_PROMETHEUS_URL": f"http://127.0.0.1:{stack['prometheus']}",
           "CAIRN_OBS_LOKI_URL": f"http://127.0.0.1:{stack['loki']}",
           "CAIRN_OBS_TEMPO_URL": f"http://127.0.0.1:{stack['tempo']}",
           "CAIRN_PG_RUNTIME": "docker", "CAIRN_PG_DOCKER_BIND": "127.0.0.1", "CAIRN_PG_DOCKER_HOST": "127.0.0.1",
           "CAIRN_PG_DOCKER_PORTS": "16600-16640",
           "CAIRN_PG_BACKUP_STORE": json.dumps({"endpoint": store["url"], "bucket": "sre-pg-backups"}),
           "CAIRN_AI_GATEWAY_ANTHROPIC_URL": model["url"], "CAIRN_AI_GATEWAY_ANTHROPIC_KEY_FILE": model["key_file"],
           "CAIRN_AI_GATEWAY_MODELS": f"{MODEL},other-model", "CAIRN_SRE_AGENT_MODEL": MODEL}
    for k, v in files.items():
        (tmp / k).write_text(v)
        env[k] = str(tmp / k)
    gone = ("ANTHROPIC_API_KEY", "CAIRN_AI_GATEWAY_ANTHROPIC_KEY", "CAIRN_AI_GATEWAY_UPSTREAMS")
    for k in gone:
        os.environ.pop(k, None)
    os.environ.update(env)
    s = build_services(Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-" + uuid.uuid4().hex,
                                dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")}),
                       graph=InMemoryGraph(), with_ai=False, with_gateway=False)
    assert s.pg_instances is not None and s.databases is not None and s.sre is not None and s.ai_gateway is not None
    me = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="ana", effect="allow", actions=["*"], condition=me(ANA)),
        Policy(id="ops", effect="allow", actions=["MANAGE_SYSTEM_OPERATIONS", "USE_AI_GATEWAY", "VIEW_DATABASE_INSTANCE",
                                                  "MANAGE_DATABASE_INSTANCE", "VIEW_DATABASE"], condition=me(OPS)),
        Policy(id="bob", effect="allow", actions=["MANAGE_SYSTEM_OPERATIONS", "VIEW_DATABASE_INSTANCE", "VIEW_DATABASE"],
               condition=me(BOB))]))
    # Every read login the agent is handed: its password must never show up in what it writes.
    passwords: list[str] = []
    real = s.databases.credentials

    def credentials(actor, name, access="read", ttl_minutes=60):
        out = real(actor, name, access, ttl_minutes)
        passwords.append(out["password"])
        return out
    s.databases.credentials = credentials
    app = create_api_app(s)
    assert telemetry.enabled()
    p = port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=p, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    until(lambda: socket.create_connection(("127.0.0.1", p), 0.3).close() or True, 30, 0.1, "Cairn's API")
    yield {"url": f"http://127.0.0.1:{p}", "s": s, "passwords": passwords}
    server.should_exit = True
    t.join(30)
    for db in ("shop",):
        try:
            s.databases.drop(ANA, db)
        except Exception:  # noqa: BLE001
            pass
    try:
        s.pg_instances.delete(ANA, INSTANCE)
    except Exception:  # noqa: BLE001
        subprocess.run(["docker", "rm", "-f", f"cairn-pg-{INSTANCE}", f"cairn-pg-{INSTANCE}-agent"], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", f"cairn-pg-{INSTANCE}-data", f"cairn-pg-{INSTANCE}-spool"],
                       capture_output=True)


def api(cairn, who, method, path, **kw):
    return httpx.request(method, cairn["url"] + path, headers={"X-Cairn-Actor": who}, timeout=300, **kw)


def bearer(cairn, token, method, path, **kw):
    return httpx.request(method, cairn["url"] + path, headers={"Authorization": f"Bearer {token}"}, timeout=120, **kw)


def connect(c):
    import psycopg

    return psycopg.connect(host=c["host"], port=c["port"], dbname=c["database"], user=c["user"], password=c["password"],
                           sslmode=c["sslmode"], connect_timeout=10, autocommit=True)


def runs_done(cairn, iid, n=1):
    def done():
        v = api(cairn, OPS, "GET", f"/v1/sre/incidents/{iid}").json()
        finished = [r for r in v["runs"] if r["status"] != "running"]
        return v if len(finished) >= n and not [r for r in v["runs"] if r["status"] == "running"] else None
    return until(done, 300, 2, f"the SRE agent's run on {iid}")


@pytest.fixture(scope="module")
def shop(cairn):
    """The instance, its database "shop" with 400,000 orders and no index on customer_id, and an application login."""
    r = api(cairn, ANA, "POST", "/v1/databases/instances",
            json={"name": INSTANCE, "cpu": 1, "memory_mb": 768, "storage_gb": 5})
    assert r.status_code == 201, r.text
    r = api(cairn, ANA, "PUT", f"/v1/databases/instances/{INSTANCE}/parameters/max_connections", json={"value": 40})
    assert r.status_code == 200, r.text
    r = api(cairn, ANA, "POST", "/v1/databases", json={"name": "shop", "instance": INSTANCE})
    assert r.status_code == 201, r.text
    w = api(cairn, ANA, "POST", "/v1/databases/shop/credentials", json={"access": "write", "ttl_minutes": 120}).json()
    cairn["passwords"].append(w["password"])
    with connect(w) as c:
        c.execute("CREATE TABLE orders (id bigint PRIMARY KEY, customer_id int NOT NULL, total numeric(10,2), "
                  "note text)")
        c.execute(f"INSERT INTO orders SELECT g, g % 20000, (g % 997) / 10.0, md5(g::text) "
                  f"FROM generate_series(1, {ROWS}) g")
        c.execute("ANALYZE orders")
    return {"write": w}


@pytest.fixture(scope="module")
def app_traffic(stack):  # noqa: F811
    """The application "shop-api": each request a server span around a client span to PostgreSQL, sent to the
    Collector like any instrumented service's."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import SpanKind

    tp = TracerProvider(resource=Resource.create({"service.name": "shop-api"}))
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"http://127.0.0.1:{stack['otlp_http']}/v1/traces"),
                                             schedule_delay_millis=300))
    tracer = tp.get_tracer("shop-api")

    def run(creds, n: int) -> float:
        took = []
        with connect(creds) as c:
            for i in range(n):
                t0 = time.time()
                with tracer.start_as_current_span("GET /orders", kind=SpanKind.SERVER,
                                                  attributes={"http.route": "/orders", "http.method": "GET"}):
                    with tracer.start_as_current_span("SELECT shop.orders", kind=SpanKind.CLIENT,
                                                      attributes={"db.system": "postgresql", "peer.service": INSTANCE}):
                        c.execute("SELECT id, total FROM orders WHERE customer_id = %s", (i * 7 % 20000,)).fetchall()
                took.append(time.time() - t0)
        tp.force_flush()
        return sum(took) / len(took) * 1000
    yield run
    tp.shutdown()


def secrets_of(cairn) -> list[str]:
    s = cairn["s"]
    return [*cairn["passwords"], PROVIDER_KEY, s.pg_instances.password(INSTANCE, s.pg_instances._row(INSTANCE).uid)]


def everything_written(cairn, iid) -> str:
    inc = api(cairn, OPS, "GET", f"/v1/observability/incidents/{iid}").json()
    sre = api(cairn, OPS, "GET", f"/v1/sre/incidents/{iid}").json()
    return json.dumps(inc) + json.dumps(sre)


RULE = {"id": "shop-latency", "name": "Shop API is slow", "severity": "critical", "pending_for": "0s",
        "labels": {"pg_instance": INSTANCE, "service": "shop-api"},
        "expr": 'sum(rate(traces_span_metrics_duration_milliseconds_sum{service_name="shop-api",span_kind="SPAN_KIND_SERVER"}[1m]))'
                ' / sum(rate(traces_span_metrics_duration_milliseconds_count{service_name="shop-api",'
                'span_kind="SPAN_KIND_SERVER"}[1m])) > 8',
        "summary": "shop-api answers in {{ $value }} ms on average"}
RUNBOOK = {"id": "postgres-latency", "name": "Postgres latency", "description": "An application on a managed instance is slow",
           "match": [{"name": "pg_instance", "value": ".+", "regex": True}],
           "steps": [{"type": "pg_status", "title": "Instance health", "instance": "{{ labels.pg_instance }}"},
                     {"type": "promql", "title": "Requests a second",
                      "query": 'sum(rate(traces_span_metrics_calls_total{service_name="shop-api"}[1m]))'}],
           "actions": [{"action": "restart_instance", "why": "sessions idle in a transaction fill the slots"}]}


def test_a_missing_index_alert_incident_proposal_approval_and_postmortem(cairn, shop, app_traffic):
    s = cairn["s"]
    # Settings and a runbook, as code would declare them (kinds sre_agent_settings, sre_runbooks).
    r = api(cairn, ANA, "PUT", "/v1/sre/settings", json={"enabled": True, "auto_join": ["critical"], "run_as": OPS,
                                                         "max_steps": 10})
    assert r.status_code == 200 and r.json()["run_as"] == OPS, r.text
    assert api(cairn, ANA, "POST", "/v1/sre/runbooks", json=RUNBOOK).status_code == 201
    assert api(cairn, ANA, "POST", "/v1/sre/runbooks", json={**RUNBOOK, "id": "bad", "actions": [{"action": "drop_db"}]}
               ).status_code == 422
    st = api(cairn, OPS, "GET", "/v1/sre/status").json()
    assert st["ready"] is True and st["model"] == MODEL, st
    assert api(cairn, OPS, "POST", "/v1/observability/alert-rules", json=RULE).status_code == 201

    slow_ms = app_traffic(shop["write"], 150)
    assert slow_ms > 10, f"the statement is not slow enough to alert on ({slow_ms:.1f} ms)"

    def fired():
        app_traffic(shop["write"], 10)
        api(cairn, OPS, "POST", "/v1/observability/evaluate")
        return [a for a in api(cairn, OPS, "GET", "/v1/observability/alerts", params={"state": "firing"}).json()["items"]
                if a["source"] == "rule:shop-latency" and a["incident_id"]]
    alert = until(fired, 240, 3, "the latency alert to fire and open an incident")[0]
    iid = alert["incident_id"]
    view = runs_done(cairn, iid)
    [run] = view["runs"]
    assert run["kind"] == "investigate" and run["status"] == "done", run
    assert run["asked_by"] is None and run["acts_for"] == OPS and run["model"] == MODEL
    tools = [x["tool"] for x in run["steps"]]
    assert tools[:2] == ["pg_status", "promql"], tools            # the runbook's steps first
    assert {"incident_context", "pg_diagnose", "write_note", "rank_causes", "propose_change"} <= set(tools), tools
    assert all(x["ok"] for x in run["steps"]), [x for x in run["steps"] if not x["ok"]]
    diag = json.loads(next(x for x in run["steps"] if x["tool"] == "pg_diagnose")["result"])
    [sug] = [x for x in diag["index_suggestions"] if x["validated"]]
    assert sug["table"] == "public.orders" and sug["columns"] == ["customer_id"] and sug["table_rows"] >= ROWS * 0.9
    assert "Seq Scan" in sug["plan_node"] and "customer_id" in sug["filter"], sug
    # The logins' passwords are not in the slow-statement list (pg_stat_statements keeps utility statements as they ran).
    sq = api(cairn, ANA, "GET", f"/v1/databases/instances/{INSTANCE}/slow-queries", params={"limit": 200}).text
    assert not [p for p in cairn["passwords"] if p in sq]
    # The read login it planned with is gone (dropped as soon as the diagnosis ended).
    logins = api(cairn, ANA, "GET", "/v1/databases/shop/logins").json()["logins"]
    assert [x for x in logins if x["actor"] == OPS] == [], logins

    # In the incident: the runbook's note, a finding citing the diagnosis (its query and result), ranked causes,
    # the proposal; all marked as the agent's.
    inc = api(cairn, OPS, "GET", f"/v1/observability/incidents/{iid}").json()
    mine = [e for e in inc["timeline"] if e.get("by_agent")]
    finding = next(e for e in mine if e["kind"] == "finding")
    assert "customer_id" in finding["text"] and finding["actor"] == "urn:li:cairnAgent:sre-agent"
    [ev] = finding["detail"]["evidence"]
    assert ev["tool"] == "pg_diagnose" and ev["input"] == {"instance": INSTANCE} and "Seq Scan" in ev["result"]
    ranked = next(e for e in mine if e["kind"] == "hypothesis")
    assert ranked["detail"]["ranked_causes"][0]["likelihood"] >= ranked["detail"]["ranked_causes"][-1]["likelihood"]
    assert any(e["kind"] == "note" and e["text"].startswith("Runbook Postgres latency") for e in mine)
    [prop] = view["actions"]
    assert prop["kind"] == "change_request" and prop["status"] == "pending" and prop["evidence"], prop
    cid = prop["change_id"]
    change = api(cairn, ANA, "GET", f"/v1/iac/changes/{cid}").json()
    assert change["status"] == "proposed" and change["sponsor"] == OPS and change["agent"] == "agt_sre_agent", change
    assert {(c["action"], c["kind"]) for c in change["plan"]["changes"]} == {("create", "postgres_indexes")}

    # The agent applies nothing: with its own credential (as it acts, for OPS) every way is closed.
    token = s.agents.issue_run_token("agt_sre_agent", OPS, 600_000, scopes=["read", "model", "iac:propose"])
    for method, path in (("POST", f"/v1/sre/actions/{prop['id']}/approve"), ("POST", f"/v1/iac/changes/{cid}/approve"),
                         ("POST", f"/v1/iac/changes/{cid}/apply"), ("POST", f"/v1/databases/instances/{INSTANCE}/restart"),
                         ("PUT", "/v1/databases/shop/indexes/orders_customer_id_idx")):
        r = bearer(cairn, token, method, path, json={"table": "orders", "columns": ["customer_id"]})
        assert r.status_code == 403, (path, r.status_code, r.text)
    s.agents.revoke_token(token)
    # Someone without APPROVE_IAC_CHANGE and MANAGE_DATABASE is refused; it stays pending.
    r = api(cairn, BOB, "POST", f"/v1/sre/actions/{prop['id']}/approve")
    assert r.status_code == 403, r.text
    assert api(cairn, OPS, "GET", f"/v1/sre/incidents/{iid}").json()["actions"][0]["status"] == "pending"
    # The approver: approved and applied as Ana; the index is built.
    r = api(cairn, ANA, "POST", f"/v1/sre/actions/{prop['id']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "applied", r.text
    change = api(cairn, ANA, "GET", f"/v1/iac/changes/{cid}").json()
    assert change["status"] == "applied" and change["approved_by"] == ANA and change["applied_by"] == ANA
    idx = api(cairn, ANA, "GET", "/v1/databases/shop/indexes").json()["indexes"]
    made = next(i for i in idx if i["name"] == sug["index"])
    assert made["columns"] == ["customer_id"] and made["valid"] and made["managed"]
    entries = [e for e in s.audit.entries(category="data", limit=500) if e["action"] == "database.index_created"]
    assert entries and entries[0]["actor"] == ANA

    # Fast again: the alert resolves; a person resolves the incident; the agent drafts the postmortem.
    fast_ms = app_traffic(shop["write"], 400)
    assert fast_ms < slow_ms / 3, (fast_ms, slow_ms)

    def resolved():
        app_traffic(shop["write"], 100)
        api(cairn, OPS, "POST", "/v1/observability/evaluate")
        a = [x for x in api(cairn, OPS, "GET", "/v1/observability/alerts").json()["items"] if x["id"] == alert["id"]]
        return a and a[0]["state"] == "resolved"
    until(resolved, 240, 5, "the latency alert to resolve")
    r = api(cairn, ANA, "POST", f"/v1/observability/incidents/{iid}/resolve",
            json={"summary": "an index on orders.customer_id, from the SRE agent's change request"})
    assert r.status_code == 200, r.text
    pm = until(lambda: (lambda x: x if x.status_code == 200 else None)(
        api(cairn, OPS, "GET", f"/v1/sre/incidents/{iid}/postmortem")), 120, 2, "the postmortem").json()
    md = pm["markdown"]
    assert pm["status"] == "draft"
    for heading in ("## Summary", "## Impact", "## Timeline", "## Actions taken", "## Cause", "## What fixed it",
                    "## Follow-ups"):
        assert heading in md, (heading, md)
    assert "Shop API is slow" in md and "applied" in md
    r = api(cairn, ANA, "PUT", f"/v1/sre/incidents/{iid}/postmortem", json={"markdown": md + "\nReviewed by Ana.\n"})
    assert r.status_code == 200 and r.json()["edited_by"] == ANA
    r = api(cairn, ANA, "POST", f"/v1/sre/incidents/{iid}/postmortem/approve")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert api(cairn, ANA, "PUT", f"/v1/sre/incidents/{iid}/postmortem", json={"markdown": "x"}).status_code == 409

    written = everything_written(cairn, iid)
    for secret in secrets_of(cairn):
        assert secret not in written
    assert "cat_" not in written and "Bearer " not in written
    acts = {e["action"] for e in s.audit.entries(category="observability", limit=1000)}
    assert {"sre.run_started", "sre.run_done", "sre.action_proposed", "sre.action_refused", "sre.action_executed",
            "sre.postmortem_approved"} <= acts, acts


def test_filled_connections_a_restart_proposed_refused_then_approved(cairn, shop):
    s = cairn["s"]
    admin = s.pg_instances.admin_conninfo(INSTANCE)
    import psycopg

    held = []
    for _ in range(34):                                  # 34 of 40 slots: idle in a transaction
        c = psycopg.connect(admin, autocommit=False)
        c.execute("SELECT 1")
        held.append(c)
    try:
        status = api(cairn, OPS, "GET", f"/v1/databases/instances/{INSTANCE}/status").json()
        assert status["connections"]["percent"] >= 80, status["connections"]
        r = api(cairn, OPS, "POST", "/v1/observability/incidents",
                json={"title": "shop refuses connections", "severity": "warning", "services": [INSTANCE]})
        assert r.status_code == 201
        iid = r.json()["id"]
        time.sleep(1)
        assert api(cairn, OPS, "GET", f"/v1/sre/incidents/{iid}").json()["runs"] == []   # warning: not joined by itself
        r = api(cairn, OPS, "POST", f"/v1/sre/incidents/{iid}/investigate", params={"wait": "true"})
        assert r.status_code == 202 and r.json()["status"] == "done", r.text
        assert r.json()["asked_by"] == OPS
        [prop] = api(cairn, OPS, "GET", f"/v1/sre/incidents/{iid}").json()["actions"]
        assert prop["kind"] == "runbook_action" and prop["action"] == "restart_instance"
        assert prop["params"] == {"instance": INSTANCE} and prop["status"] == "pending"
        # Bob may work on incidents but not operate the instance: refused, audited, still pending.
        r = api(cairn, BOB, "POST", f"/v1/sre/actions/{prop['id']}/approve")
        assert r.status_code == 403 and "MANAGE_DATABASE_INSTANCE" in r.text, r.text
        assert [a["status"] for a in api(cairn, OPS, "GET", "/v1/sre/actions", params={"incident": iid}).json()["items"]] \
            == ["pending"]
        r = api(cairn, ANA, "POST", f"/v1/sre/actions/{prop['id']}/approve")
        assert r.status_code == 200 and r.json()["status"] == "executed" and r.json()["decided_by"] == ANA, r.text
        restarted = [e for e in s.audit.entries(category="data", limit=500) if e["action"] == "pg_instance.restarted"]
        assert restarted and restarted[0]["actor"] == ANA
        with pytest.raises(psycopg.OperationalError):
            held[0].execute("SELECT 1")
        st = until(lambda: (lambda x: x if x.get("health") == "healthy" else None)(
            api(cairn, OPS, "GET", f"/v1/databases/instances/{INSTANCE}/status").json()), 60, 2, "a healthy instance")
        assert st["connections"]["percent"] < 50
        inc = api(cairn, OPS, "GET", f"/v1/observability/incidents/{iid}").json()
        assert any(e["kind"] == "action" and not e["by_agent"] and "approved and ran" in e["text"] for e in inc["timeline"])
        r = api(cairn, OPS, "POST", f"/v1/sre/actions/{prop['id']}/reject")
        assert r.status_code == 409                                       # decided already
        written = everything_written(cairn, iid)
        for secret in secrets_of(cairn):
            assert secret not in written
    finally:
        for c in held:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


def test_diagnose_an_instance_from_its_page(cairn, shop):
    r = api(cairn, OPS, "POST", f"/v1/sre/instances/{INSTANCE}/diagnose", json={"question": "Is it healthy?"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["report"]["instance"] == INSTANCE and out["report"]["health"] in ("healthy", "degraded")
    assert out["answer"] and INSTANCE in out["answer"]
    assert api(cairn, OPS, "POST", "/v1/sre/instances/no-such-pg/diagnose", json={}).status_code == 404
    # Someone who may not see the instance learns nothing about it.
    r = api(cairn, "urn:li:corpuser:nobody", "POST", f"/v1/sre/instances/{INSTANCE}/diagnose", json={})
    assert r.status_code == 404, r.text


def test_fail_closed_no_model_a_model_not_served_and_a_stopped_component(cairn, stack):  # noqa: F811
    s = cairn["s"]
    r = api(cairn, OPS, "POST", "/v1/observability/incidents",
            json={"title": "API errors", "severity": "warning", "services": ["cairn-api"]})
    iid = r.json()["id"]
    # No model: it says so and does nothing.
    env_model = s.sre.env_model
    s.sre.env_model = ""
    try:
        assert api(cairn, OPS, "GET", "/v1/sre/status").json()["ready"] is False
        out = api(cairn, OPS, "POST", f"/v1/sre/incidents/{iid}/investigate", params={"wait": "true"}).json()
        assert out["status"] == "refused" and "no model" in out["error"]
    finally:
        s.sre.env_model = env_model
    # A model the gateway does not serve: the run stops, said in the incident.
    assert api(cairn, ANA, "PUT", "/v1/sre/settings", json={"run_as": OPS, "model": "not-served"}).status_code == 200
    out = api(cairn, OPS, "POST", f"/v1/sre/incidents/{iid}/investigate", params={"wait": "true"}).json()
    assert out["status"] == "failed" and "not-served" in out["error"], out
    assert api(cairn, ANA, "PUT", "/v1/sre/settings", json={"run_as": OPS}).status_code == 200
    # Loki stopped: the logs tool fails; the agent says so instead of guessing.
    loki = f"cairn-obs-{os.getpid()}-loki"
    docker("stop", loki)
    try:
        out = api(cairn, OPS, "POST", f"/v1/sre/incidents/{iid}/investigate", params={"wait": "true"}).json()
        assert out["status"] == "done", out
        failed = [x for x in out["steps"] if not x["ok"]]
        assert [x["tool"] for x in failed] == ["logs"], out["steps"]
    finally:
        docker("start", loki)
    inc = api(cairn, OPS, "GET", f"/v1/observability/incidents/{iid}").json()
    texts = [e["text"] for e in inc["timeline"] if e.get("by_agent")]
    assert any("no model is configured" in t and "did nothing" in t for t in texts), texts
    assert any("not-served" in t and "proposes nothing more" in t for t in texts), texts
    assert any(t.startswith("Some of the SRE agent's tools failed") and "logs" in t for t in texts), texts
    assert any("I could not read everything: logs failed" in t for t in texts), texts
    assert api(cairn, OPS, "GET", f"/v1/sre/incidents/{iid}").json()["actions"] == []   # nothing proposed
    # Asking is for people; an agent's credential may read but not ask.
    token = s.agents.issue_run_token("agt_sre_agent", OPS, 600_000, scopes=["read", "model", "iac:propose"])
    assert bearer(cairn, token, "POST", f"/v1/sre/incidents/{iid}/ask", json={"question": "why?"}).status_code == 403
    s.agents.revoke_token(token)
    r = api(cairn, OPS, "POST", f"/v1/sre/incidents/{iid}/ask", json={"question": "What do the error logs say?"})
    assert r.status_code == 200 and r.json()["kind"] == "ask" and r.json()["answer"], r.text
