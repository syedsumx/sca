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
- every model call metered under the compiler's agent identity, the provider seeing Cairn's key only.

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
