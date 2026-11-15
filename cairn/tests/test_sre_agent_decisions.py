"""The SRE agent's (L142) decisions and their edges, against a real Cairn (its API served by uvicorn, its database,
the agent hub and the AI gateway in front of a provider stand-in that answers as this test scripts) and, for the
Postgres helper, a real PostgreSQL 16 container:

* whom it acts for when it joins incidents by itself (``run_as``) is a person who names themselves: someone who
  may change the settings cannot make it read as another person;
* a proposal is decided once: two approvals at the same moment run it once, and it cannot be rejected while it
  runs (the record never says rejected for something that ran);
* a change request it proposes is kept whole (what the approver is shown is what Approve applies);
* an answer from the model it cannot read (a tool call without an object input, a block that is not an object)
  never leaves a run "running" and silent;
* an index suggestion never writes a table name someone chose into the bundle YAML a person approves, unless it is
  a plain identifier (one ``postgres_indexes`` takes).

The rest of the agent's work, on the full observability stack and a managed instance, is tests/test_sre_agent_e2e.py."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid

import httpx
import pytest

OPS, ANA, BOB = "urn:li:corpuser:ops", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
MODEL = "sre-decisions-standin"
KEY = "provider-key-" + uuid.uuid4().hex


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Provider:
    """The model provider's Messages API, answering what ``script`` returns for each request."""

    def __init__(self):
        self.script = lambda body: [{"type": "text", "text": "Nothing to add."}]
        me = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("x-api-key") != KEY:
                    return self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "key"}})
                body = json.loads(raw)
                content = me.script(body)
                stop = "tool_use" if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content) else "end_turn"
                self._json(200, {"id": "msg_x", "type": "message", "role": "assistant", "model": body["model"],
                                 "content": content, "stop_reason": stop, "stop_sequence": None,
                                 "usage": {"input_tokens": 10, "output_tokens": 10}})

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


@pytest.fixture(scope="module")
def cairn(tmp_path_factory):
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("sre-decisions")
    provider = Provider()
    key_file = tmp / "provider.key"
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(KEY)
    env = {"CAIRN_AI_GATEWAY_ANTHROPIC_URL": provider.url, "CAIRN_AI_GATEWAY_ANTHROPIC_KEY_FILE": str(key_file),
           "CAIRN_AI_GATEWAY_MODELS": MODEL, "CAIRN_SRE_AGENT_MODEL": MODEL}
    gone = ("ANTHROPIC_API_KEY", "CAIRN_AI_GATEWAY_ANTHROPIC_KEY", "CAIRN_AI_GATEWAY_UPSTREAMS")
    saved = {k: os.environ.get(k) for k in [*env, *gone]}
    for k in gone:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        s = build_services(Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-" + uuid.uuid4().hex,
                                    dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")}),
                           graph=InMemoryGraph(), with_ai=False, with_gateway=False)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert s.sre is not None and s.ai_gateway is not None
    me = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="ana", effect="allow", actions=["*"], condition=me(ANA)),
        Policy(id="ops", effect="allow", actions=["*"], condition=me(OPS)),
        Policy(id="bob", effect="allow", actions=["MANAGE_SYSTEM_OPERATIONS"], condition=me(BOB))]))
    app = create_api_app(s)
    p = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=p, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    end = time.time() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", p), 0.3).close()
            break
        except OSError:
            if time.time() > end:
                raise
            time.sleep(0.1)
    yield {"url": f"http://127.0.0.1:{p}", "s": s, "provider": provider}
    server.should_exit = True
    t.join(30)
    provider.srv.shutdown()


def api(cairn, who, method, path, **kw):
    return httpx.request(method, cairn["url"] + path, headers={"X-Cairn-Actor": who}, timeout=120, **kw)


def incident(cairn, title="Checkout is slow") -> str:
    r = api(cairn, OPS, "POST", "/v1/observability/incidents", json={"title": title, "severity": "warning"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_run_as_is_a_person_who_names_themselves(cairn):
    # Bob may change the agent's settings, but not make it read (and write into incidents) as Ana.
    r = api(cairn, BOB, "PUT", "/v1/sre/settings", json={"run_as": ANA, "auto_join": ["critical"]})
    assert r.status_code == 403, r.text
    assert cairn["s"].sre.settings()["run_as"] is None
    assert api(cairn, ANA, "PUT", "/v1/sre/settings", json={"run_as": ANA}).json()["run_as"] == ANA
    # Once Ana named herself, others may change the rest and keep her; they may name themselves instead.
    r = api(cairn, BOB, "PUT", "/v1/sre/settings", json={"run_as": ANA, "daily_runs": 50})
    assert r.status_code == 200 and r.json()["daily_runs"] == 50, r.text
    assert api(cairn, OPS, "PUT", "/v1/sre/settings", json={"run_as": ANA, "auto_join": ["warning"]}).status_code == 200
    assert api(cairn, BOB, "PUT", "/v1/sre/settings", json={"run_as": BOB}).json()["run_as"] == BOB
    assert api(cairn, BOB, "PUT", "/v1/sre/settings", json={"run_as": ANA}).status_code == 403
    assert api(cairn, OPS, "PUT", "/v1/sre/settings", json={}).json()["run_as"] is None


def test_a_proposal_is_decided_once(cairn):
    s = cairn["s"]
    iid = incident(cairn, "Disk alert flapping")
    name = "DiskFull-" + uuid.uuid4().hex[:6]
    row = s.sre._action_row(None, iid, kind="runbook_action", action="silence_alert",
                            params={"alertname": name, "minutes": 30}, title=f"silence alert: alertname={name}",
                            rationale="it flaps", evidence=[])
    aid = row["id"]
    # Approving sends the silence through Cairn's API as the approver; held here until both have tried.
    entered, gate, sent = threading.Event(), threading.Event(), []
    real = s.sre._caller

    class Held:
        def __init__(self, inner):
            self.inner = inner

        def call(self, method, path, body=None):
            if path == "/v1/observability/silences":
                sent.append(body)
                entered.set()
                gate.wait(15)
            return self.inner.call(method, path, body)
    s.sre._caller = lambda headers: Held(real(headers))
    out: dict = {}
    try:
        a = threading.Thread(target=lambda: out.update(a=api(cairn, OPS, "POST", f"/v1/sre/actions/{aid}/approve")))
        a.start()
        assert entered.wait(15)
        b = threading.Thread(target=lambda: out.update(b=api(cairn, ANA, "POST", f"/v1/sre/actions/{aid}/approve")))
        b.start()
        b.join(5)
        while_running = api(cairn, ANA, "GET", f"/v1/sre/actions?incident={iid}").json()["items"][0]["status"]
        rejected = api(cairn, ANA, "POST", f"/v1/sre/actions/{aid}/reject", json={"comment": "no"})
        gate.set()
        a.join(30)
        b.join(30)
    finally:
        gate.set()
        s.sre._caller = real
    assert len(sent) == 1, f"the silence was sent {len(sent)} times"
    assert out["a"].status_code == 200 and out["a"].json()["status"] == "executed", out["a"].text
    assert out["b"].status_code == 409, out["b"].text
    assert while_running == "running"
    assert rejected.status_code == 409, rejected.text
    final = s.sre.action(aid)
    assert final["status"] == "executed" and final["decided_by"] == OPS, final
    silences = api(cairn, OPS, "GET", "/v1/observability/silences").json()
    items = silences.get("items", silences) if isinstance(silences, dict) else silences
    assert len([x for x in items if any(m.get("value") == name for m in x.get("matchers") or [])]) == 1, items
    # A refusal gives the proposal back: Bob may not run a backup (MANAGE_DATABASE_INSTANCE), so it stays pending.
    row = s.sre._action_row(None, iid, kind="runbook_action", action="backup_instance", params={"instance": "nope-pg"},
                            title="backup instance: instance=nope-pg", rationale="stale", evidence=[])
    r = api(cairn, BOB, "POST", f"/v1/sre/actions/{row['id']}/approve")
    assert r.status_code == 403, r.text
    assert s.sre.action(row["id"])["status"] == "pending" and s.sre.action(row["id"])["decided_by"] is None


def test_a_proposed_change_is_kept_whole(cairn):
    from cairn.sre.agent import MAX_BUNDLE, SreError, _Run

    s = cairn["s"]
    iid = incident(cairn, "Runbook needed")
    token = s.sre._token(OPS)
    try:
        run = _Run("run_" + uuid.uuid4().hex[:16], "investigate", iid, OPS,
                   s.sre._caller({"authorization": f"Bearer {token}"}), s.sre.settings())
        rb = "rb-" + uuid.uuid4().hex[:8]
        tail = "\n# " + "x" * (MAX_BUNDLE + 100) + "\n"            # past what the proposal would keep
        bundle = (f"sre_runbooks:\n  - id: {rb}\n    name: Disk\n    match:\n      - name: alertname\n"
                  f"        value: DiskFull\n{tail}")
        before = len(api(cairn, ANA, "GET", "/v1/iac/changes").json())
        with pytest.raises(SreError):
            s.sre._propose_change(run, {"title": "A runbook for disk alerts", "intent": "so it is diagnosed",
                                        "bundle_yaml": bundle})
        assert len(api(cairn, ANA, "GET", "/v1/iac/changes").json()) == before
        assert not [a for a in s.sre.actions(iid) if a["kind"] == "change_request"]
        # Within the limit it is proposed, and the proposal holds the bundle as it is.
        small = bundle.replace(tail, "\n")
        got = s.sre._propose_change(run, {"title": "A runbook for disk alerts", "intent": "so it is diagnosed",
                                          "bundle_yaml": small})
        assert s.sre.action(got["proposal"])["params"]["bundle_yaml"] == small
    finally:
        s.agents.revoke_token(token)


def test_an_answer_it_cannot_read_never_leaves_a_run_running(cairn):
    calls: list[dict] = []

    def script(body):
        calls.append(body)
        if len(calls) == 1:          # a block that is not an object, and a tool call whose input is not an object
            return ["oops", {"type": "tool_use", "id": "t1", "name": "health", "input": "not-an-object"}]
        return [{"type": "text", "text": "The health map could not be read."}]
    cairn["provider"].script = script
    try:
        iid = incident(cairn, "Latency up")
        r = api(cairn, OPS, "POST", f"/v1/sre/incidents/{iid}/investigate", params={"wait": "true"})
        assert r.status_code in (200, 202), r.text
        [run] = api(cairn, OPS, "GET", f"/v1/sre/incidents/{iid}").json()["runs"]
        assert run["status"] in ("done", "failed"), run
        assert run["finished_at"], run
        assert [x["tool"] for x in run["steps"]] == ["health"], run["steps"]
        assert len(calls) == 2
    finally:
        cairn["provider"].script = lambda body: [{"type": "text", "text": "Nothing to add."}]


# ---- the Postgres helper on a real PostgreSQL ------------------------------------------------------------------
def _docker() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(["docker", "image", "inspect", "postgres:16-alpine"],
                                                           capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def pg():
    if not _docker():
        pytest.skip("needs docker and postgres:16-alpine")
    import psycopg

    p = _port()
    name = f"cairn-sre-decisions-pg-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:5432",
                    "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "postgres:16-alpine"], check=True, capture_output=True)
    try:
        end = time.time() + 90
        while True:
            try:
                conn = psycopg.connect(host="127.0.0.1", port=p, user="postgres", dbname="postgres", connect_timeout=2,
                                       autocommit=True)
                break
            except psycopg.OperationalError:
                if time.time() > end:
                    raise
                time.sleep(0.5)
        yield conn
        conn.close()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_an_index_suggestion_never_carries_a_chosen_table_name_into_the_bundle(pg):
    from psycopg import sql

    from cairn.sre import pghelper

    weird = "t\nwarehouses:\n- name: grab\n  size: 4x-large\n#"           # a table name anyone could choose
    for table in ("orders", weird):
        pg.execute(sql.SQL("CREATE TABLE public.{} (id bigint, customer_id int, note text)").format(sql.Identifier(table)))
        pg.execute(sql.SQL("INSERT INTO public.{} SELECT g, g % 4000, 'n' FROM generate_series(1, 40000) g").format(
            sql.Identifier(table)))
        pg.execute(sql.SQL("ANALYZE public.{}").format(sql.Identifier(table)))

    def suggest(table):
        q = sql.SQL("SELECT id FROM public.{} WHERE customer_id = $1").format(sql.Identifier(table)).as_string(pg)
        sug, _ = pghelper.index_suggestions(pg, "postgres", {"query": q, "queryid": 1})
        return sug
    [plain] = suggest("orders")
    assert plain["validated"] and plain["bundle_yaml"].startswith("postgres_indexes:\n"), plain
    [chosen] = suggest(weird)
    assert chosen["validated"] is False and "bundle_yaml" not in chosen, chosen
    assert "not a plain" in chosen["why_not"]
