"""Talk to your app (L120), end to end: a real data app process started by Cairn (a small web app with a context
endpoint), a real SQLite warehouse behind Cairn's query path, the real services, decision point, database and HTTP
API, and the model behind Cairn's AI gateway (the provider stand-in of test_ai_gateway, here repeating back what it
was given). Checked:
- a person with USE_APP asks about what the app shows: the declared query runs as them, so the model gets (and the
  answer repeats) their masked emails, while an administrator's answer has them unmasked;
- the app's own view (its context endpoint) is asked as the viewer, with what the page sent; the app is told who is
  asking and gets none of Cairn's secrets;
- the answer cites the tables used; a query the person may not run is left out and said so;
- people without USE_APP are refused before anything runs; the call is metered to the asker and audited."""

import json
import os
import re
import sys
import threading

import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine, select, text

from cairn.app import create_api_app
from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
from cairn.services import build_services
from test_ai_gateway import SONNET, Provider
from test_trino_e2e import port, wait

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
USERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.users,PROD)"
SALARIES = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.salaries,PROD)"
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
APP = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

SEEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.jsonl")


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlsplit(self.path)
        with open(SEEN, "a") as fh:
            fh.write(json.dumps({"path": u.path, "user": self.headers.get("X-Cairn-User"),
                                 "key_ok": self.headers.get("X-Cairn-App-Key") == os.environ.get("CAIRN_APP_KEY"),
                                 "cairn_secret": bool(os.environ.get("CAIRN_APPS_SECRET")),
                                 "authorization": self.headers.get("Authorization")}) + "\n")
        if u.path.endswith("/cairn-context"):
            view = json.loads(parse_qs(u.query).get("view", ["null"])[0])
            body = {"page": "Customers by region", "filter": (view or {}).get("region"),
                    "viewer": self.headers.get("X-Cairn-User")}
        else:
            body = {"page": "home"}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


class Echo(Provider):
    """The model: says which emails, page and tables it was given (so the test sees exactly what reached it)."""

    def reply(self, body):
        said = body["messages"][-1]["content"]
        emails = re.findall(r"[a-z•]+@[a-z•]+\.[a-z]+", said)
        page = re.findall(r'"page": "([^"]+)"', said)
        tables = sorted(set(re.findall(r"urn:li:dataset:\([^)]*\)", said)))
        return f"Emails: {', '.join(emails)}. Page: {', '.join(page)}. Sources: {' '.join(f'[{t}]' for t in tables)}"


@pytest.fixture
def stack(settings, graph, monkeypatch, tmp_path):
    wh = tmp_path / "wh.db"
    e = create_engine(f"sqlite:///{wh}")
    with e.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER, email TEXT, region TEXT)"))
        c.execute(text("INSERT INTO users VALUES (1, 'jane@acme.com', 'EU'), (2, 'bo@example.org', 'US')"))
        c.execute(text("CREATE TABLE salaries (name TEXT, salary INTEGER)"))
        c.execute(text("INSERT INTO salaries VALUES ('jane', 100)"))
    conns = tmp_path / "connections.yaml"
    conns.write_text(f"connections:\n  wh:\n    url: sqlite:///{wh}\n    catalog:\n      platform: sqlite\n")
    import http.server

    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    for k, v in {"CAIRN_QUALITY_CONNECTIONS": str(conns), "CAIRN_APPS_SECRET": "apps-secret-" + "a" * 24,
                 "CAIRN_AI_GATEWAY_ANTHROPIC_URL": f"http://127.0.0.1:{up.server_address[1]}",
                 "CAIRN_AI_GATEWAY_ANTHROPIC_KEY": "sk-ant-upstream", "CAIRN_AI_GATEWAY_MODELS": SONNET}.items():
        monkeypatch.setenv(k, v)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    graph.aspects[(USERS, "datasetProperties")] = {"name": "users", "description": "Customers and their region"}
    graph.aspects[(USERS, "schemaMetadata")] = {"fields": [
        {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}},
        {"fieldPath": "region"}]}
    graph.aspects[(SALARIES, "datasetProperties")] = {"name": "salaries"}
    graph.aspects[(SALARIES, "schemaMetadata")] = {"fields": [{"fieldPath": "name"}, {"fieldPath": "salary"}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub(ADMIN)),
        Policy(id="app", effect="allow", actions=["USE_APP"], target=Target(resource_urns=["urn:li:dataApp:customers"]),
               condition=sub(ANA)),
        Policy(id="models", effect="allow", actions=["USE_AI_GATEWAY"],
               target=Target(resource_urns=[f"urn:li:aiModel:{SONNET}"]), condition=sub(ANA)),
        Policy(id="readers", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*main.users*"])),
        Policy(id="mask-pii", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})],
               condition={"attr": "subject.urn", "op": "neq", "value": ADMIN})]))
    (tmp_path / "app.py").write_text(APP)
    Provider.seen.clear()
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    yield s, f"http://127.0.0.1:{p}", tmp_path
    for name in list(s.data_apps._running):
        s.data_apps.stop(name)
    server.should_exit = True
    up.shutdown()


def test_a_person_asks_about_what_the_app_shows_with_their_own_access(stack):
    from cairn.ai.usage import usage_table

    s, url, d = stack
    admin, ana, bob = ({"X-Cairn-Actor": u} for u in (ADMIN, ANA, BOB))
    r = httpx.post(f"{url}/v1/apps", headers=admin, json={
        "name": "customers", "title": "Customers", "workdir": str(d),
        "command": [sys.executable, "app.py", "{port}"],
        "chat": {"description": "Customers by region, with their contact emails.", "tables": [USERS, SALARIES],
                 "queries": [{"name": "customers", "connection": "wh",
                              "sql": "SELECT id, email, region FROM main.users ORDER BY id"},
                             {"name": "pay", "connection": "wh", "sql": "SELECT name, salary FROM main.salaries"}],
                 "context_endpoint": "/cairn-context"}})
    assert r.status_code == 201, r.text
    assert httpx.post(f"{url}/v1/apps", headers=admin, json={"name": "bad", "url": "http://x.invalid",
                                                             "chat": {"queries": [{"sql": "x"}]}}).status_code == 400
    listed = httpx.get(f"{url}/v1/apps", headers=ana).json()
    assert listed[0]["chat"] is True and "queries" not in json.dumps(listed)      # the queries stay the operators'

    # Ana (USE_APP; the email column is masked for her) asks; the app's view is asked as her, with the page's state.
    q = {"question": "Which customers are in the EU, and how do I reach them?", "view": {"region": "EU"}}
    got = httpx.post(f"{url}/v1/apps/customers/chat", headers=ana, json=q, timeout=60)
    assert got.status_code == 200, got.text
    out = got.json()
    assert out["answer"].startswith("Emails: j•••@•••.com, b•••@•••.org. Page: Customers by region."), out
    assert "jane@acme.com" not in out["answer"] and "jane@acme.com" not in json.dumps(Provider.seen)
    assert out["citations"] == [{"urn": USERS}] and out["masked_columns"] == ["email"] and out["app_view"]
    assert f"[{USERS}]" in out["answer"]                                     # the model cited what it was given
    assert [x["name"] for x in out["left_out"]] == ["pay"]                   # salaries: not hers to read
    sent = Provider.seen[-1]
    assert sent["x-api-key"] == "sk-ant-upstream" and sent["body"]["model"] == SONNET
    assert '"filter": "EU"' in sent["body"]["messages"][-1]["content"] and ANA in sent["body"]["messages"][-1]["content"]
    seen = [json.loads(line) for line in (d / "seen.jsonl").read_text().splitlines()]
    ctx = [x for x in seen if x["path"] == "/apps/customers/cairn-context"][-1]
    assert ctx["user"] == ANA and ctx["key_ok"] and not ctx["cairn_secret"] and ctx["authorization"] is None

    # The administrator's answer has the emails as they are: the access is the asker's, never the app's.
    adm = httpx.post(f"{url}/v1/apps/customers/chat", headers=admin, json=q, timeout=60).json()
    assert adm["answer"].startswith("Emails: jane@acme.com, bo@example.org."), adm
    assert adm["masked_columns"] == [] and {c["urn"] for c in adm["citations"]} == {USERS, SALARIES}

    # Bob may not use the app: refused before anything runs.
    n = len(Provider.seen)
    bob_r = httpx.post(f"{url}/v1/apps/customers/chat", headers=bob, json=q, timeout=60)
    assert bob_r.status_code == 403 and "USE_APP" in bob_r.text and len(Provider.seen) == n
    assert httpx.post(f"{url}/v1/apps/customers/chat", json=q).status_code == 401
    assert httpx.post(f"{url}/v1/apps/nope/chat", headers=ana, json=q).status_code == 404
    assert httpx.post(f"{url}/v1/apps/customers/chat", headers=ana, json={"question": ""}).status_code == 400

    # Metered to the asker; audited without the question or the answer.
    with s.engine.connect() as c:
        rows = c.execute(select(usage_table).where(usage_table.c.actor == ANA)).all()
    assert rows and {r.task for r in rows} == {"app-chat:customers"}
    audit = [e for e in s.audit.entries("apps", limit=50) if e["action"] == "app.chat"]
    assert audit and audit[0]["details"]["tables"] and "EU" not in json.dumps(audit)
    assert os.environ.get("CAIRN_APPS_SECRET")              # (the app ran without it, above)
