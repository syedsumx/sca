"""Cairn Ontology end to end: the real compiler binary (cairn-ontology) with a real PostgreSQL as its store, fed by a
real Cairn API from a real lakehouse (Cairn Store, Cairn Catalog, Trino) whose query history comes from queries run
through Trino; the compiler's model calls go through Cairn's AI gateway (OpenAI protocol, chat and embeddings) to a
local stand-in for the provider that answers each compilation step with the structured JSON it asks for. Checked:
- a compile: business terms with display names and synonyms, date notations of string columns, a foreign key the
  model inferred and one visible only from joins in the query log, the bridge table and its join pattern, metrics
  from the query log, a colliding term name qualified, all as proposals in Cairn's context inbox;
- accepting proposals: a glossary term linked to its columns, an approved join;
- Ask-your-data grounded in a compiled relationship, its SQL checked (findings shown; a warning, never a refusal,
  when the compiler is down); /validate-sql flagging a join that contradicts the known keys;
- the MCP tools; reads filtered to the datasets a person may see; compiling needs MANAGE_GLOSSARIES;
- incremental runs reuse unchanged tables; a cancelled run resumes;
- every model call metered under the compiler's agent identity, the provider seeing Cairn's key only;
- the explorer (L129): a term with its evidence (keys, query-log joins with counts, the model's confidence),
  provenance, date formats and open proposals; the terms of a dataset; a term found by its synonym; proposals
  accepted and rejected in batches with a comment and audited, a term edited as it is accepted, refusals without
  the privilege and for agents; nothing about a dataset a person may not see; a run scoped to one table, estimated
  first, watched as it progresses, and diffed against the run before; Ask-your-data naming what grounded it.

Needs docker, CAIRN_STORE_BIN, CAIRN_CATALOG_BIN and CAIRN_ONTOLOGY_BIN (cargo build --release in ontology/)."""

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import httpx
import pytest

import test_trino_e2e
from cairn.governance.abac import Policy, PolicyBundle, Target
from test_ai_gateway import Provider
from test_trino_e2e import lake, port, trino, wait  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(
    not (test_trino_e2e._ready() and os.environ.get("CAIRN_ONTOLOGY_BIN")),
    reason="needs docker, CAIRN_STORE_BIN, CAIRN_CATALOG_BIN and CAIRN_ONTOLOGY_BIN")

ADMIN, STEW, DANA, BOT = ("urn:li:corpuser:admin", "urn:li:corpuser:stew", "urn:li:corpuser:dana",
                          "urn:li:corpuser:svc-ontology")
CHAT, EMBED = "onto-chat", "onto-embed"
TOKEN = "ontology-e2e-" + "t" * 24


def ds(table: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:iceberg,retail.{table},PROD)"


ORDERS, CUSTOMERS, ITEMS, PRODUCTS, CATEGORIES, BRIDGE, SUPPLIERS = (
    ds("orders"), ds("customers"), ds("order_items"), ds("products"), ds("categories"), ds("product_categories"),
    ds("suppliers"))
TERM_NAMES = {"customers": "Customer", "orders": "Purchase Order", "order_items": "Order Line", "products": "Product",
              "categories": "Category", "suppliers": "Supplier"}
STEMS = {"customer": "Customer", "product": "Product", "category": "Category", "order": "Purchase Order"}


def vector(text: str) -> list[float]:
    """A deterministic bag-of-words embedding: texts sharing words are similar."""
    v = [0.0] * 64
    for w in re.findall(r"[a-z]+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
    return v


class OntologyProvider(Provider):
    """The provider's API as the compiler reaches it through Cairn: each step's structured answer, and embeddings."""
    tasks: list = []
    delay = 0.0

    def do_POST(self):
        if self.path != "/v1/embeddings":
            if OntologyProvider.delay:
                time.sleep(OntologyProvider.delay)
            return super().do_POST()
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Provider.seen.append({"path": self.path, "authorization": self.headers.get("authorization"), "body": body})
        if self.headers.get("authorization") != "Bearer sk-oai-upstream":
            return self._json(401, {"error": {"message": "bad key"}})
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        return self._json(200, {"object": "list", "model": body["model"],
                                "data": [{"object": "embedding", "index": i, "embedding": vector(t)}
                                         for i, t in enumerate(texts)],
                                "usage": {"prompt_tokens": 3 * len(texts), "total_tokens": 3 * len(texts)}})

    def reply(self, body) -> str:
        fmt = body.get("response_format") or {}
        task = (fmt.get("json_schema") or {}).get("name")
        if task is None:
            return "Hello from the model"
        OntologyProvider.tasks.append(task)
        user = body["messages"][-1]["content"]
        return json.dumps(answer(task, user))


def answer(task: str, user: str) -> dict:
    if task == "column_descriptions":
        cols = re.findall(r"^  - (\S+) \(", user, re.M)
        return {"descriptions": [{"column_name": c, "description": f"The {c.replace('_', ' ')} of the record."}
                                 for c in cols]}
    if task == "fk_suggestions":
        cand = user.split("Candidate columns:\n", 1)[1]
        cols = re.findall(r"^  - (\S+) \(", cand, re.M)
        return {"fk_suggestions": [{"column_name": c, "rationale": f"{c} names another entity"}
                                   for c in cols if c.endswith("_id")],
                "pk_column_names": [c for c in cols if c == "id"], "is_junction_table": False,
                "junction_table_rationale": ""}
    if task == "terms":
        table = re.search(r"^Table: (\S+)", user, re.M).group(1)
        cols = re.findall(r"source_column=(\S+) \(", user)
        name = TERM_NAMES.get(table, table.replace("_", " ").title())
        return {"terms": [{"name": name, "description": f"A {name.lower()} in the retail business.",
                           "attributes": [{"source_column": c, "display_name": c.replace("_", " ").title()}
                                          for c in cols]}]}
    if task == "synonyms":
        return {"synonyms": re.findall(r"\(([A-Z]{2,})\)", user)}
    if task == "fk_hit":
        col = re.search(r"Foreign-key column: (\S+)", user).group(1)
        want = STEMS.get(col.removesuffix("_id"))
        for cid, term in re.findall(r"column_id=(\S+) \| ColumnAttribute: [^\n]*? of Term ([^.(]+?)[ .(]", user):
            if term == want:
                return {"selected_id": cid, "rationale": f"{col} identifies a {want}"}
        return {"selected_id": None, "rationale": "not sure"}
    if task == "join_default":
        return {"selected_column_id": re.search(r"source_column_id: (\S+)", user).group(1), "rationale": "first"}
    if task == "sql_attributes":
        out = []
        for expr in re.findall(r"^\d+\. \[\d+ uses\] (.+)$", user, re.M):
            if "'paid'" in expr and "status" in expr and "=" in expr and " AND " not in expr.upper():
                out.append({"name": "Paid Orders", "description": "Orders that were paid.", "expression": expr})
            elif expr.upper().startswith("SUM(") and "amount" in expr:
                out.append({"name": "Revenue", "description": "Money taken from orders.", "expression": expr})
        return {"suggestions": out}
    if task == "bridge":
        bridge = re.search(r"Bridge table: (\S+)", user).group(1)
        joins = re.findall(r"^  - (\S+) -> (\S+)\.(\S+)$", user, re.M)
        sql = f"SELECT * FROM {bridge} b" + "".join(
            f" JOIN {t.rsplit('.', 1)[0]} t{i} ON b.{c} = t{i}.{t.rsplit('.', 1)[1]}"
            for i, (c, t) in enumerate((c, f"{tt}.{tc}") for c, tt, tc in joins))
        return {"name": "Product Categories", "description": "Which categories each product is in.", "expression": sql}
    raise AssertionError(f"unexpected task {task}")


def sub(who):
    return {"attr": "subject.urn", "op": "eq", "value": who}


@pytest.fixture(scope="module")
def provider():
    # Provider.seen is shared with every test of this process that uses the gateway's stand-in provider (the
    # trino job runs them all in one pytest), so this module looks only at the requests made while it runs.
    Provider.seen.clear()
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OntologyProvider)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    d = tempfile.mkdtemp(prefix="onto-")
    open(f"{d}/token", "w").write(TOKEN)
    env = {"CAIRN_AI_GATEWAY_OPENAI_URL": f"http://127.0.0.1:{up.server_address[1]}",
           "CAIRN_AI_GATEWAY_OPENAI_KEY": "sk-oai-upstream", "CAIRN_AI_GATEWAY_MODELS": f"{CHAT},{EMBED}",
           "CAIRN_ONTOLOGY_URL": f"http://127.0.0.1:{port()}", "CAIRN_ONTOLOGY_TOKEN_FILE": f"{d}/token",
           "CAIRN_ONTOLOGY_SAMPLE_AS": ADMIN, "CAIRN_ONTOLOGY_INTERVAL_SECONDS": "0"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        os.environ.pop(k, None)
    yield {"dir": d, "url": env["CAIRN_ONTOLOGY_URL"]}
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    up.shutdown()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def cairn(provider, tmp_path_factory):
    """test_trino_e2e's Cairn API, built with the AI gateway and Cairn Ontology configured."""
    yield from test_trino_e2e.cairn.__wrapped__(tmp_path_factory)


@pytest.fixture(scope="module")
def stack(provider, cairn, trino):  # noqa: F811
    url, s = cairn
    models = Target(resource_urns=[f"urn:li:aiModel:{CHAT}", f"urn:li:aiModel:{EMBED}"])
    visible_to_dana = [ORDERS, CUSTOMERS, ITEMS, PRODUCTS, CATEGORIES, BRIDGE]
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub(ADMIN)),
        Policy(id="steward", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "MANAGE_GLOSSARIES",
                                                      "EDIT_ENTITY_GLOSSARY_TERMS"],
               target=Target(resource_urns=["*retail.*"]), condition=sub(STEW)),
        Policy(id="steward-platform", effect="allow", actions=["MANAGE_GLOSSARIES"], condition=sub(STEW)),
        Policy(id="dana", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=visible_to_dana), condition=sub(DANA)),
        Policy(id="compiler-models", effect="allow", actions=["USE_AI_GATEWAY"], target=models, condition=sub(BOT)),
    ]))
    # The lakehouse: a small retail schema, its dates kept as strings.
    for stmt in [
        "CREATE SCHEMA IF NOT EXISTS lake.retail",
        "CREATE TABLE lake.retail.customers AS SELECT * FROM (VALUES (1, 'Ann', 'EU', '2026-01-03'), "
        "(2, 'Raj', 'US', '2026-02-13'), (3, 'Li', 'EU', '2026-03-21'), (4, 'Bo', 'US', '2026-04-30')) "
        "AS t(id, name, region, signup_day)",
        "CREATE TABLE lake.retail.orders (id bigint, customer_id bigint, buyer_ref bigint, status varchar, "
        "amount double, order_day varchar) COMMENT 'Purchase orders (PO) placed by customers'",
        "INSERT INTO lake.retail.orders VALUES (10, 1, 2, 'paid', 120.0, '13/07/2026'), (11, 1, 2, 'refunded', 40.0, "
        "'02/08/2026'), (12, 2, 3, 'paid', 75.5, '21/08/2026'), (13, 3, 3, 'paid', 10.0, '01/09/2026'), "
        "(14, 3, 1, 'open', 99.0, '30/09/2026')",
        "CREATE TABLE lake.retail.products AS SELECT * FROM (VALUES (100, 'Kettle', 30.0), (101, 'Mug', 8.5), "
        "(102, 'Tea', 4.0)) AS t(id, name, price)",
        "CREATE TABLE lake.retail.categories AS SELECT * FROM (VALUES (7, 'Kitchen'), (8, 'Food')) AS t(id, label)",
        "CREATE TABLE lake.retail.product_categories AS SELECT * FROM (VALUES (100, 7), (101, 7), (102, 8), (101, 8)) "
        "AS t(product_id, category_id)",
        "CREATE TABLE lake.retail.order_items AS SELECT * FROM (VALUES (10, 100, 1), (10, 101, 2), (12, 102, 5), "
        "(13, 101, 1), (14, 100, 3)) AS t(order_id, product_id, qty)",
        "CREATE TABLE lake.retail.suppliers AS SELECT * FROM (VALUES (1, 'Acme', 100), (2, 'Brew', 102)) "
        "AS t(id, name, product_id)",
    ]:
        trino(stmt)
    from cairn.quality.collectors import ConnectionRegistry

    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    out = s.trino_catalog.sync("lakehouse", ADMIN)
    assert out["tables"] >= 8, out
    # The query log: what people ran, through Trino (its event listener sends each query to Cairn).
    tag = f"/* onto-{os.getpid()} */"
    log = [
        "SELECT c.region, sum(o.amount) AS revenue FROM retail.orders o JOIN retail.customers c ON o.buyer_ref = c.id "
        "WHERE o.status = 'paid' GROUP BY c.region",
        "SELECT c.name, count(*) AS orders FROM retail.customers c JOIN retail.orders o ON c.id = o.buyer_ref "
        "WHERE o.status = 'paid' GROUP BY c.name",
        "SELECT p.name, sum(i.qty) AS units FROM retail.order_items i JOIN retail.products p ON i.product_id = p.id "
        "GROUP BY p.name",
        "SELECT sum(o.amount) FROM retail.orders o WHERE o.status = 'paid'",
    ]
    for i, q in enumerate(log):
        for _ in range(2 if i == 0 else 1):
            trino(f"{q} {tag}")
    end = time.time() + 60
    while time.time() < end:
        got = s.query_history.list(ADMIN, True, text=tag)
        if len(got) >= 5:
            break
        time.sleep(0.5)
    assert len(got) >= 5, got
    # The compiler: PostgreSQL as its store; its model key is its agent identity's (scope "model").
    pg, pgp = f"cairn-onto-pg-{os.getpid()}", port()
    subprocess.run(["docker", "rm", "-f", pg], capture_output=True, timeout=60)
    subprocess.run(["docker", "run", "-d", "--rm", "--name", pg, "-e", "POSTGRES_PASSWORD=onto-secret", "-e",
                    "POSTGRES_DB=ontology", "-p", f"127.0.0.1:{pgp}:5432", "postgres:16-alpine"],
                   check=True, capture_output=True, timeout=300)
    procs: list = []
    try:
        end = time.time() + 90
        while subprocess.run(["docker", "exec", pg, "pg_isready", "-U", "postgres", "-d", "ontology"],
                             capture_output=True).returncode:
            assert time.time() < end, "PostgreSQL did not start"
            time.sleep(1)
        time.sleep(2)
        agent = s.ontology.create_agent(BOT, ADMIN)
        # A compile, a cancelled run and its resume make more model calls in a minute than an agent's default
        # quota (120): over it the gateway answers 429 and the compiler falls back without the model.
        from cairn.identity.agents import AgentPatch

        s.agents.update(agent["id"], AgentPatch(per_minute=5000), ADMIN)
        d = provider["dir"]
        with open(f"{d}/model-key", "w") as fh:          # a file, never the command line
            fh.write(agent["key"])
        with open(f"{d}/db-url", "w") as fh:
            fh.write(f"postgres://postgres:onto-secret@127.0.0.1:{pgp}/ontology?sslmode=disable")
        cport = int(provider["url"].rsplit(":", 1)[1])

        def start():
            p = subprocess.Popen([os.environ["CAIRN_ONTOLOGY_BIN"]], stdout=open(f"{d}/compiler.log", "a"),
                                 stderr=subprocess.STDOUT, env={
                **{k: v for k, v in os.environ.items() if not k.startswith(("CAIRN_", "ANTHROPIC_"))},
                "CAIRN_ONTOLOGY_ADDR": f"127.0.0.1:{cport}", "CAIRN_ONTOLOGY_DATABASE_URL_FILE": f"{d}/db-url",
                "CAIRN_ONTOLOGY_TOKEN_FILE": f"{d}/token", "CAIRN_ONTOLOGY_MODEL_URL": f"{url}/v1/ai/gateway/openai/v1",
                "CAIRN_ONTOLOGY_MODEL": CHAT, "CAIRN_ONTOLOGY_EMBED_MODEL": EMBED,
                "CAIRN_ONTOLOGY_MODEL_API_KEY_FILE": f"{d}/model-key", "CAIRN_ONTOLOGY_MODEL_RETRIES": "1",
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
            wait(cport)
            return p
        procs.append(start())
        yield {"url": url, "s": s, "agent": agent, "dir": d, "start": start, "proc": procs, "tag": tag}
    finally:
        for p in procs:
            p.terminate()
            p.wait(15)
        subprocess.run(["docker", "rm", "-f", pg], capture_output=True, timeout=60)


def call(st, method, path, who=STEW, **kw):
    return httpx.request(method, f"{st['url']}{path}", headers={"X-Cairn-Actor": who}, timeout=600, **kw)


@pytest.fixture(scope="module")
def compiled(stack):
    r = call(stack, "POST", "/v1/ontology/runs", json={"mode": "full", "wait": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["run"]["state"] == "completed", (out, open(f"{stack['dir']}/compiler.log").read()[-3000:])
    return out


def test_a_compile_builds_the_ontology_and_proposals(stack, compiled):
    out = compiled
    assert out["sync"]["catalog"]["tables"] >= 8 and out["sync"]["queries"]["added"] >= 4, out["sync"]
    stats = out["run"]["stats"]
    assert stats["processed"] == stats["tables"] and stats["bridges"] == 1, stats
    o = call(stack, "GET", "/v1/ontology", who=ADMIN).json()
    terms = {t["table_id"]: t for t in o["terms"]}
    # Three tables are called "orders": the first in canonical order (schema dsales) keeps the Term's name, the
    # others' are qualified, whichever model call finished first.
    assert terms[test_trino_e2e.DELTA_ORDERS]["name"] == "Purchase Order"
    assert terms[ORDERS]["name"] == "Purchase Order (retail.orders)" and terms[ORDERS]["synonyms"] == ["PO"]
    sales = terms["urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"]
    assert sales["name"] == "Purchase Order (sales.orders)", sales["name"]
    attrs = {(a["table_id"], a["column"]): a for a in o["attributes"]}
    assert attrs[(CUSTOMERS, "signup_day")]["format"] == "YYYY-MM-DD"
    assert attrs[(ORDERS, "order_day")]["format"] == "DD/MM/YYYY"
    assert attrs[(ORDERS, "status")]["name"] == "Status"
    assert (ORDERS, "customer_id") not in attrs                 # a foreign key, not an attribute
    rels = {(r["source_table"], r["source_column"], r["target_table"], r["target_column"]): r for r in o["relationships"]}
    inferred = rels[(ORDERS, "customer_id", CUSTOMERS, "id")]
    assert [p["source"] for p in inferred["provenance"]] == ["model"], inferred
    observed = rels[(ORDERS, "buyer_ref", CUSTOMERS, "id")]     # only the query log joins on it
    assert [p["source"] for p in observed["provenance"]] == ["observed_in_queries"], observed
    assert observed["evidence"]["queries"] == 2 and observed["evidence"]["executions"] == 3, observed["evidence"]
    assert observed["cardinality"] == "many_to_one"
    assert (ITEMS, "product_id", PRODUCTS, "id") in rels
    assert {p["source"] for p in rels[(ITEMS, "product_id", PRODUCTS, "id")]["provenance"]} == {"model", "observed_in_queries"}
    [bridge] = o["bridges"]
    assert bridge["table_id"] == BRIDGE and {p["target_table"] for p in bridge["fk_pairs"]} == {PRODUCTS, CATEGORIES}
    metrics = {m["name"]: m for m in o["metrics"]}
    assert metrics["Paid Orders"]["sql"].startswith("SELECT * FROM retail.orders"), metrics["Paid Orders"]
    assert metrics["Revenue"]["source"] == "sql" and metrics["Revenue"]["usage"] >= 1
    assert metrics["Product Categories"]["source"] == "bridgeTable" and BRIDGE in metrics["Product Categories"]["tables"]
    # Back in Cairn's context inbox as proposals.
    props = call(stack, "GET", "/v1/ontology/proposals").json()["proposals"]
    kinds = {(p["kind"], p["title"]) for p in props}
    assert ("term", "Add glossary term “Customer”") in kinds
    assert any(k == "relationship" and "buyer_ref" in t for k, t in kinds)
    assert any(k == "metric" and "Revenue" in t for k, t in kinds)
    assert out["imported"]["new"] == len(props) or out["imported"]["new"] >= len(props)
    inbox = call(stack, "GET", "/context/findings", params={"type": "TERM_SUGGESTION"}).json()
    assert any(f["title"] == "Add glossary term “Customer”" for f in inbox)
    # Graph exports and provenance.
    ttl = call(stack, "GET", "/v1/ontology/graph", who=ADMIN, params={"format": "turtle"}).text
    assert "owl:Class" in ttl and 'rdfs:label "Purchase Order"' in ttl and "cairn:joinOn" in ttl
    ld = call(stack, "GET", "/v1/ontology/graph", who=ADMIN, params={"format": "jsonld"}).json()
    assert ld["@context"]["owl"].startswith("http://www.w3.org/2002/07/owl")
    prov = call(stack, "GET", f"/v1/ontology/provenance/relationship/{observed['id']}", who=ADMIN).json()
    assert prov["evidence"]["queries"] == 2


def test_accepting_proposals_writes_the_glossary_and_approved_joins(stack, compiled):
    s = stack["s"]
    props = call(stack, "GET", "/v1/ontology/proposals").json()["proposals"]
    term = next(p for p in props if p["title"] == "Add glossary term “Customer”")
    # Dana may see customers but not change its glossary terms.
    assert call(stack, "POST", f"/v1/ontology/proposals/{term['id']}/accept", who=DANA).status_code == 403
    r = call(stack, "POST", f"/v1/ontology/proposals/{term['id']}/accept")
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED", r.text
    urn = "urn:li:glossaryTerm:Ontology.Customer"
    info = s.graph.get_aspect(urn, "glossaryTermInfo")
    assert info["name"] == "Customer" and info["parentNode"] == "urn:li:glossaryNode:Ontology", info
    esm = s.graph.get_aspect(CUSTOMERS, "editableSchemaMetadata")
    linked = {f["fieldPath"] for f in esm["editableSchemaFieldInfo"]
              if any(t["urn"] == urn for t in (f.get("glossaryTerms") or {}).get("terms", []))}
    assert linked == {"id", "name", "region", "signup_day"}, linked
    join = next(p for p in props if p["kind"] == "relationship" and "customer_id" in p["title"])
    r = call(stack, "POST", f"/v1/ontology/proposals/{join['id']}/accept")
    assert r.status_code == 200 and r.json()["definition"]["kind"] == "join", r.text
    assert any(d["kind"] == "join" and d["doc"]["source_field"] == "customer_id" for d in s.context_layer.definitions())


def test_ask_your_data_is_grounded_and_checked(stack, compiled):
    from cairn.ai.llm import StaticProvider

    s = stack["s"]
    prompts = []
    sql = {"text": "SELECT c.region, sum(o.amount) AS revenue FROM retail.orders o JOIN retail.customers c "
                   "ON o.buyer_ref = c.id WHERE o.status = 'paid' GROUP BY c.region ORDER BY c.region"}

    def responder(system, user, schema):
        prompts.append(user)
        return {"sql": sql["text"], "explanation": "Revenue by region."}
    s.ask.sql.llm = StaticProvider(responder)
    a = s.ask.ask(ADMIN, "What is paid revenue by region of the buyer?", lambda u: True, [ORDERS, CUSTOMERS])
    assert a["error"] is None and a["result"]["rows"], a
    assert "join retail.orders.buyer_ref = retail.customers.id" in prompts[-1], prompts[-1]
    assert any(d["name"].startswith("ontology:") for d in a["definitions"])
    assert a["ontology"]["valid"] is True and a["ontology"]["joins"][0]["known"], a["ontology"]
    # A join contradicting the known keys is flagged; the governed run still goes ahead.
    sql["text"] = "SELECT count(*) AS n FROM retail.orders o JOIN retail.customers c ON o.id = c.id"
    b = s.ask.ask(ADMIN, "How many orders have customers?", lambda u: True, [ORDERS, CUSTOMERS])
    assert b["result"] is not None and b["ontology"]["valid"] is False, b
    assert [f["kind"] for f in b["ontology"]["findings"]] == ["join_contradicts_keys"], b["ontology"]
    # The same through the API, and a fan-out.
    v = call(stack, "POST", "/v1/ontology/validate-sql",
             json={"sql": "SELECT sum(o.amount) FROM retail.orders o JOIN retail.order_items i ON i.order_id = o.id"}).json()
    assert "fan_out" in [f["kind"] for f in v["findings"]], v
    # The compiler down: a warning, never a refusal.
    real = s.ontology.client.url
    s.ontology.client.url = "http://127.0.0.1:9"
    s.ontology._cache = (0.0, None)
    try:
        c = s.ask.ask(ADMIN, "What is paid revenue by region of the buyer?", lambda u: True, [ORDERS, CUSTOMERS])
    finally:
        s.ontology.client.url = real
    assert c["result"] is not None and "not checked" in c["ontology"]["warning"], c["ontology"]


def test_mcp_tools(stack, compiled):
    import asyncio

    from cairn.ai.mcp_server import _principal, build_mcp_server
    from cairn.common.security import Principal

    server, _ = build_mcp_server(stack["s"])
    token = _principal.set(Principal(STEW, "agent", agent="agt_onto", scopes=("read",)))
    try:
        found = asyncio.run(server.call_tool("ontology_search_terms", {"query": "customer"})).structured_content
        rels = asyncio.run(server.call_tool("ontology_relationships",
                                            {"dataset_urns": [ORDERS, CUSTOMERS], "between": True})).structured_content
        bad = asyncio.run(server.call_tool("ontology_validate_sql", {
            "sql": "SELECT * FROM retail.orders o JOIN retail.customers c ON o.id = c.id"})).structured_content
    finally:
        _principal.reset(token)
    assert found["terms"][0]["name"] == "Customer" and found["hits"][0]["kind"] == "term", found
    assert {r["source_column"] for r in rels["relationships"]} == {"customer_id", "buyer_ref"}, rels
    assert bad["findings"][0]["kind"] == "join_contradicts_keys"


def test_reads_follow_catalog_visibility(stack, compiled):
    def names(who):
        return {t["name"] for t in call(stack, "GET", "/v1/ontology/terms", who=who).json()["terms"]}
    assert "Supplier" in names(STEW) and "Supplier" not in names(DANA) and "Customer" in names(DANA)
    rels = call(stack, "GET", "/v1/ontology/relationships", who=DANA).json()["relationships"]
    assert rels and all(SUPPLIERS not in (r["source"], r["target"]) for r in rels)
    hits = call(stack, "GET", "/v1/ontology/search", who=DANA, params={"q": "supplier", "mode": "name"}).json()["hits"]
    assert not any(h.get("table_id") == SUPPLIERS for h in hits)
    v = call(stack, "POST", "/v1/ontology/validate-sql", who=DANA,
             json={"sql": "SELECT s.name FROM retail.suppliers s JOIN retail.products p ON s.product_id = p.id"}).json()
    assert v["valid"] is False and [f["kind"] for f in v["findings"]] == ["not_visible"] and v["joins"] == [], v
    full = call(stack, "GET", "/v1/ontology", who=ADMIN).json()
    supplier = next(t for t in full["terms"] if t["table_id"] == SUPPLIERS)
    assert call(stack, "GET", f"/v1/ontology/provenance/term/{supplier['id']}", who=DANA).status_code == 404
    assert call(stack, "GET", f"/v1/ontology/provenance/term/{supplier['id']}", who=STEW).status_code == 200
    ttl = call(stack, "GET", "/v1/ontology/graph", who=DANA, params={"format": "turtle"}).text
    assert "Supplier" not in ttl and "Customer" in ttl
    props = call(stack, "GET", "/v1/ontology/proposals", who=DANA).json()["proposals"]
    assert props and all(SUPPLIERS not in p["entities"] for p in props)
    # Compiling needs MANAGE_GLOSSARIES.
    assert call(stack, "POST", "/v1/ontology/runs", who=DANA, json={}).status_code == 403
    assert call(stack, "POST", "/v1/ontology/sync", who=DANA).status_code == 403


def test_incremental_runs_and_resuming(stack, compiled):
    # Nothing changed: an incremental run reuses every table's result.
    r = call(stack, "POST", "/v1/ontology/runs", json={"mode": "incremental", "wait": True, "sync": True}).json()
    assert r["run"]["state"] == "completed" and r["run"]["stats"]["processed"] == 0, r["run"]
    assert r["run"]["stats"]["reused"] == r["run"]["stats"]["tables"]
    # A slow model and a cancel: the run stops after the tables in flight, and resumes from where it stopped.
    OntologyProvider.delay = 0.4
    try:
        started = call(stack, "POST", "/v1/ontology/runs", json={"mode": "full", "sync": False}).json()["run"]
        time.sleep(1.0)
        assert call(stack, "POST", f"/v1/ontology/runs/{started['id']}/cancel").status_code == 200
        run = stack["s"].ontology.wait(started["id"], 300)
    finally:
        OntologyProvider.delay = 0.0
    assert run["state"] == "cancelled" and run["stats"]["skipped"] > 0, run
    # The process stops too: the run is resumed by a new one.
    stack["proc"][0].terminate()
    stack["proc"][0].wait(15)
    stack["proc"][0] = stack["start"]()
    r = call(stack, "POST", f"/v1/ontology/runs/{started['id']}/resume")
    assert r.status_code == 200, r.text
    run = stack["s"].ontology.wait(started["id"], 600)
    assert run["state"] == "completed" and run["stats"]["resumed"] >= 1, run
    assert run["stats"]["resumed"] + run["stats"]["processed"] == run["stats"]["tables"], run["stats"]


def test_model_calls_are_metered_under_the_compilers_agent(stack, compiled):
    from sqlalchemy import select

    from cairn.ai.usage import usage_table

    s = stack["s"]
    with s.engine.connect() as c:
        rows = [dict(r._mapping) for r in c.execute(select(usage_table).where(usage_table.c.actor == BOT))]
    tasks = {r["task"] for r in rows}
    assert tasks == {f"gateway:{stack['agent']['id']}"}, tasks
    assert len(rows) >= len(OntologyProvider.tasks) and sum(r["input_tokens"] for r in rows) > 0
    assert {"fk_suggestions", "terms", "fk_hit", "sql_attributes", "bridge"} <= set(OntologyProvider.tasks)
    # The provider only ever saw Cairn's key, never the compiler's agent key.
    keys = {x.get("authorization") for x in Provider.seen}
    assert keys == {"Bearer sk-oai-upstream"}, keys
    assert stack["agent"]["key"] not in open(f"{stack['dir']}/compiler.log").read()


# ------------------------------------------------------------------------------------------ the explorer (L129)
def _term(st, name, who=STEW):
    ex = call(st, "GET", "/v1/ontology/explorer", who=who).json()
    return next(t for t in ex["terms"] if t["name"] == name), ex


def test_explorer_and_term_detail_with_evidence_and_provenance(stack, compiled):
    orders, ex = _term(stack, "Purchase Order (retail.orders)")
    assert orders["schema"] == "retail" and orders["attributes"] >= 4 and orders["date_formats"] == 1, orders
    customer = next(t for t in ex["terms"] if t["name"] == "Customer")
    assert customer["status"] == "accepted" and orders["status"] == "proposed" and orders["proposal"], (customer, orders)
    # The bridge table and its relationships are marked as such.
    assert [b["table"] for b in ex["bridges"]] == [BRIDGE] and {t["table"] for t in ex["bridges"][0]["targets"]} == {
        PRODUCTS, CATEGORIES}
    assert all(r["bridge"] for r in ex["relationships"] if r["source"] == BRIDGE)
    assert any(t["id"] == BRIDGE and t["bridge"] for t in ex["tables"])
    d = call(stack, "GET", f"/v1/ontology/terms/{orders['id']}").json()
    assert d["synonyms"] == ["PO"] and d["table"] == ORDERS and d["description"]
    assert {(f["column"], f["format"]) for f in d["date_formats"]} == {("order_day", "DD/MM/YYYY")}
    assert "customer_id" not in {a["column"] for a in d["attributes"]}
    rels = {r["source_column"]: r for r in d["relationships"] if r["direction"] == "out"}
    observed, inferred = rels["buyer_ref"], rels["customer_id"]
    # (Ask-your-data's own query, run through Trino above, is in the query log too by now.)
    assert observed["evidence"]["query_log"]["queries"] >= 2 and observed["evidence"]["query_log"]["executions"] >= 3
    assert observed["evidence"]["query_log"]["query_ids"]
    assert "model" not in observed["evidence"] and observed["other_term"]["name"] == "Customer"
    assert inferred["evidence"]["model"]["confidence"] > 0 and inferred["status"] == "accepted", inferred
    assert inferred["join"] == "orders.customer_id = customers.id" and inferred["cardinality"] == "many_to_one"
    assert {"Revenue", "Paid Orders"} <= {m["name"] for m in d["metrics"]}
    prov = d["provenance"]
    assert prov["run"]["id"] and prov["run"]["mode"] in ("full", "incremental") and prov["run"]["requested_by"]
    assert prov["sources"][0]["source"] == "model" and ORDERS in prov["inputs"]["tables"]
    assert "order_day" in prov["inputs"]["columns"] and prov["inputs"]["queries"]
    assert any(p["kind"] == "term" and p["element_id"] == orders["id"] for p in d["proposals"]), d["proposals"]
    assert all(p["status"] == "OPEN" for p in d["proposals"])
    assert "sample_values" not in json.dumps(d)          # sampled as the compiler's identity: never shown
    assert call(stack, "GET", "/v1/ontology/terms/term_nope").status_code == 404
    # The CSV export, over what the caller may see.
    csv_text = call(stack, "GET", "/v1/ontology/graph", params={"format": "csv"}, who=DANA).text
    assert csv_text.startswith("kind,id,name") and "Purchase Order (retail.orders)" in csv_text
    assert "Supplier" not in csv_text and "retail.suppliers" not in csv_text


def test_terms_on_an_asset_and_search_by_synonym(stack, compiled):
    a = call(stack, "GET", "/v1/ontology/asset", params={"urn": ORDERS}).json()
    assert [t["name"] for t in a["terms"]] == ["Purchase Order (retail.orders)"]
    cols = {c["column"]: c for c in a["columns"]}
    assert cols["order_day"]["format"] == "DD/MM/YYYY" and cols["status"]["term_name"] == "Purchase Order (retail.orders)"
    assert {r["column"] for r in a["relationships"]} >= {"customer_id", "buyer_ref"}
    assert call(stack, "GET", "/v1/ontology/asset", params={"urn": BRIDGE}).json()["bridge"] is True
    # Dana may not see suppliers: not found, as for a dataset that does not exist.
    assert call(stack, "GET", "/v1/ontology/asset", params={"urn": SUPPLIERS}, who=DANA).status_code == 404
    hits = call(stack, "GET", "/v1/ontology/search-terms", params={"q": "po"}).json()["terms"]
    assert hits[0]["name"] == "Purchase Order (retail.orders)" and hits[0]["matched"] == "synonym", hits
    assert hits[0]["synonym"] == "PO"
    assert not call(stack, "GET", "/v1/ontology/search-terms", params={"q": "supplier"}, who=DANA).json()["terms"]
    assert call(stack, "GET", "/v1/ontology/search-terms", params={"q": "supplier"}).json()["terms"]


def test_batch_review_with_comments_edits_audit_and_refusals(stack, compiled):
    s = stack["s"]
    props = call(stack, "GET", "/v1/ontology/proposals").json()["proposals"]
    by_title = {p["title"]: p for p in props}
    product, category = by_title["Add glossary term “Product”"], by_title["Add glossary term “Category”"]
    supplier = by_title["Add glossary term “Supplier”"]
    rel = next(p for p in props if p["kind"] == "relationship" and "customer_id" not in p["title"])
    # Without the privilege: every item refused, nothing changes.
    r = call(stack, "POST", "/v1/ontology/proposals/batch", who=DANA,
             json={"ids": [product["id"], category["id"]], "action": "accept", "comment": "lgtm"})
    assert r.status_code == 200 and r.json()["done"] == 0, r.text
    assert {x["status_code"] for x in r.json()["results"]} == {403}
    # A proposal over a dataset Dana may not see is not found for her, whatever she may do.
    r = call(stack, "POST", "/v1/ontology/proposals/batch", who=DANA,
             json={"ids": [supplier["id"]], "action": "reject"})
    assert r.json()["results"][0]["status_code"] == 404
    # Agents never decide, even for a person who could.
    from cairn.common.security import Principal
    from cairn.context.ontology_explorer import OntologyExplorer, ReviewError

    ex = OntologyExplorer(s.ontology, s.graph, s.context, s.audit)
    with pytest.raises(ReviewError) as e:
        ex.decide_many(Principal(STEW, "agent", agent="agt_x", scopes=("read",)), lambda *a: True,
                       lambda u: True, [product["id"]], "accept")
    assert e.value.status == 403
    assert {p["id"] for p in call(stack, "GET", "/v1/ontology/proposals").json()["proposals"]} >= {
        product["id"], category["id"], rel["id"]}
    # The steward: two accepted in one go, one rejected; each with the comment, audited.
    r = call(stack, "POST", "/v1/ontology/proposals/batch",
             json={"ids": [product["id"], category["id"]], "action": "accept", "comment": "reviewed with finance"})
    assert r.status_code == 200 and r.json()["done"] == 2, r.text
    r = call(stack, "POST", "/v1/ontology/proposals/batch",
             json={"ids": [rel["id"], "fnd_missing"], "action": "reject", "comment": "a bridge, not a join"})
    out = r.json()
    assert out["done"] == 1 and out["results"][1]["status_code"] == 404, out
    assert s.graph.get_aspect("urn:li:glossaryTerm:Ontology.Product", "glossaryTermInfo")["name"] == "Product"
    again = call(stack, "POST", "/v1/ontology/proposals/batch", json={"ids": [product["id"]], "action": "reject"})
    assert again.json()["results"][0]["status_code"] == 409             # already decided
    audit = s.audit.entries("ontology", limit=200)
    acc = [e for e in audit if e["action"] == "ontology.proposal_accepted" and e["subject"] == category["id"]]
    rej = [e for e in audit if e["action"] == "ontology.proposal_rejected" and e["subject"] == rel["id"]]
    assert acc and acc[0]["actor"] == STEW and acc[0]["details"]["comment"] == "reviewed with finance", acc
    assert rej and rej[0]["details"]["comment"] == "a bridge, not a join"
    assert any(e["action"] == "ontology.proposals_accept_batch" and e["details"]["done"] == 2 for e in audit)
    assert s.context.get(rel["id"])["status"] == "DISMISSED"
    # The explorer shows the new states.
    product_term, _ = _term(stack, "Product")
    assert product_term["status"] == "accepted"
    # A term edited as it is accepted.
    line = by_title["Add glossary term “Order Line”"]
    r = call(stack, "POST", f"/v1/ontology/proposals/{line['id']}/accept",
             json={"comment": "our word", "edits": {"name": "Order Item", "definition": "One line of an order.",
                                                    "synonyms": ["line item"]}})
    assert r.status_code == 200, r.text
    info = s.graph.get_aspect("urn:li:glossaryTerm:Ontology.OrderLine", "glossaryTermInfo")
    assert info["name"] == "Order Item" and info["definition"] == "One line of an order."
    assert info["customProperties"]["cairn_synonyms"] == "line item"
    # Edits are for terms only.
    metric = next(p for p in call(stack, "GET", "/v1/ontology/proposals").json()["proposals"] if p["kind"] == "metric")
    r = call(stack, "POST", f"/v1/ontology/proposals/{metric['id']}/accept", json={"edits": {"name": "x"}})
    assert r.status_code == 422


def test_explorer_follows_visibility(stack, compiled):
    _, mine = _term(stack, "Customer", who=DANA)
    assert all(t["table"] != SUPPLIERS for t in mine["terms"]) and "Supplier" not in {t["name"] for t in mine["terms"]}
    assert all(SUPPLIERS not in (r["source"], r["target"]) for r in mine["relationships"])
    assert all(t["id"] != SUPPLIERS for t in mine["tables"])
    full = call(stack, "GET", "/v1/ontology/explorer").json()
    supplier = next(t for t in full["terms"] if t["name"] == "Supplier")
    assert call(stack, "GET", f"/v1/ontology/terms/{supplier['id']}", who=DANA).status_code == 404
    products = next(t for t in mine["terms"] if t["name"] == "Product")
    d = call(stack, "GET", f"/v1/ontology/terms/{products['id']}", who=DANA).json()
    # suppliers.product_id → products.id exists, but not for someone who may not see suppliers.
    assert all(SUPPLIERS not in (r["source"], r["target"]) for r in d["relationships"]), d["relationships"]
    assert SUPPLIERS not in d["provenance"]["inputs"]["tables"]
    stew = call(stack, "GET", f"/v1/ontology/terms/{products['id']}").json()
    assert any(r["source"] == SUPPLIERS for r in stew["relationships"])
    # The wizard's catalog: the tables each person may see.
    assert SUPPLIERS not in {t["id"] for t in call(stack, "GET", "/v1/ontology/catalog", who=DANA).json()["tables"]}
    assert "retail" in call(stack, "GET", "/v1/ontology/catalog").json()["schemas"]
    # Estimating, scoping and diffs are for people who compile.
    assert call(stack, "POST", "/v1/ontology/estimate", who=DANA, json={}).status_code == 403
    assert call(stack, "GET", f"/v1/ontology/runs/{compiled['run']['id']}/diff", who=DANA).status_code == 403


def test_a_scoped_run_with_estimate_progress_and_diff(stack, compiled):
    est = call(stack, "POST", "/v1/ontology/estimate", json={"mode": "full", "schemas": ["retail"]}).json()
    assert est["scope"] >= 7 and est["to_process"] == est["scope"] and est["out_of_scope"] >= 1, est
    assert est["model"]["chat_calls_max"] == est["to_process"] * est["model"]["chat_calls_per_table"] > 0
    est = call(stack, "POST", "/v1/ontology/estimate", json={"mode": "incremental"}).json()
    assert est["to_process"] == 0 and est["reused"] == est["tables"] - est["without_columns"], est
    # A table the steward may not see cannot be in his scope.
    r = call(stack, "POST", "/v1/ontology/runs", json={"tables": [test_trino_e2e.DELTA_ORDERS], "sync": False})
    assert r.status_code == 422, r.text
    assert call(stack, "POST", "/v1/ontology/runs", json={"schemas": ["nowhere"], "sync": False}).status_code == 422
    before = call(stack, "GET", "/v1/ontology/runs").json()["published"]
    # A full run of one table, slowed down so its progress can be watched.
    OntologyProvider.delay = 0.3
    try:
        started = call(stack, "POST", "/v1/ontology/runs",
                       json={"mode": "full", "tables": [CUSTOMERS], "sync": False}).json()["run"]
        seen = []
        end = time.time() + 300
        while time.time() < end:
            run = call(stack, "GET", f"/v1/ontology/runs/{started['id']}").json()
            if run["state"] != "running":
                break
            if run.get("progress"):
                seen.append(run["progress"])
            time.sleep(0.2)
    finally:
        OntologyProvider.delay = 0.0
    assert run["state"] == "completed", run
    assert run["stats"]["processed"] == 1 and run["stats"]["scope"] == [CUSTOMERS], run["stats"]
    assert run["stats"]["out_of_scope"] == run["stats"]["tables"] - 1
    assert seen and seen[0]["scope"] == 1 and seen[0]["tables_total"] == run["stats"]["tables"], seen
    assert {p["phase"] for p in seen} & {"tables", "relationships", "metrics", "bridges", "publishing"}
    stack["s"].ontology.import_run(started["id"], STEW)
    # Every other table kept its terms; the diff against the run before says what changed.
    names = {t["name"] for t in call(stack, "GET", "/v1/ontology/terms").json()["terms"]}
    assert {"Customer", "Supplier", "Product", "Purchase Order (retail.orders)"} <= names
    diff = call(stack, "GET", f"/v1/ontology/runs/{started['id']}/diff").json()
    assert diff["against"] == before and diff["run"] == started["id"], diff
    assert diff["terms"]["counts"]["removed"] == 0 and diff["terms"]["counts"]["unchanged"] >= 6, diff["terms"]
    # Against nothing (the first run), everything is added; a run in progress or unknown has no diff.
    first = min((r for r in call(stack, "GET", "/v1/ontology/runs").json()["runs"] if r["state"] == "completed"),
                key=lambda r: r["version"])
    d0 = call(stack, "GET", f"/v1/ontology/runs/{first['id']}/diff").json()
    assert d0["against"] is None and d0["terms"]["counts"]["added"] >= 6        # retail's tables but the bridge
    assert call(stack, "GET", "/v1/ontology/runs/run_nope/diff").status_code == 404
    audit = stack["s"].audit.entries("ontology", limit=200)
    assert any(e["action"] == "ontology.compile_started" and e["details"].get("tables") == 1 for e in audit)


def test_ask_your_data_says_what_it_was_grounded_in(stack, compiled):
    from cairn.ai.llm import StaticProvider

    s = stack["s"]
    s.ask.sql.llm = StaticProvider(lambda system, user, schema: {
        "sql": "SELECT c.region, sum(o.amount) AS revenue FROM retail.orders o JOIN retail.customers c "
               "ON o.customer_id = c.id GROUP BY c.region", "explanation": "Revenue by region."})
    a = s.ask.ask(ADMIN, "What is revenue by region?", lambda u: True, [ORDERS, CUSTOMERS])
    g = a["grounded"]
    assert {"Customer", "Purchase Order (retail.orders)"} <= {t["name"] for t in g["terms"]}, g
    assert any(r["label"] == "orders.buyer_ref → customers.id" for r in g["relationships"]), g
    r = call(stack, "POST", "/data/ask", who=ADMIN, json={"question": "What is revenue by region?",
                                                          "urns": [ORDERS, CUSTOMERS]})
    assert r.status_code == 200 and r.json()["grounded"]["terms"], r.text
