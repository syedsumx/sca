"""ai_search in Trino SQL (L112): a real Trino, with Cairn's functions plugin built from source against it, calls
``ai_search(index, query, k)``; Cairn searches a real vector index (its vectors a table in Cairn Catalog and Cairn
Store) as the query's user, with the refusals of ``GET /v1/vectors/<name>/search`` (no read access; a row filter on
the source), meters each search under the person, and caps searches a day. Needs docker and the store and catalog
binaries."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

import pytest

pa = pytest.importorskip("pyarrow")

from test_trino_e2e import CATALOG_TOKEN, S3_ON, STORE_KEY, TRINO, _ready, lake, port, wait  # noqa: E402, F401

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")

KEY = "ai-search-e2e-" + "k" * 24
PLUGIN_SRC = os.path.join(os.path.dirname(__file__), "..", "plugins", "java", "cairn-trino-functions")
DOCS = [("t1", "Refunds are issued within 30 days of purchase when the item is returned unused."),
        ("t2", "Shipping to Europe takes five to seven business days by standard post."),
        ("t3", "Reset your password from the account settings page using the emailed link."),
        ("t4", "Invoices can be downloaded as PDF from the billing section every month.")]


def build_plugin(out: str) -> str:
    """The plugin jar, compiled inside the Trino image against its own SPI (as docker/trino-functions.Dockerfile)."""
    if os.environ.get("CAIRN_TRINO_FUNCTIONS_PLUGIN"):
        return os.environ["CAIRN_TRINO_FUNCTIONS_PLUGIN"]
    os.makedirs(out, exist_ok=True)
    os.chmod(out, 0o777)
    r = subprocess.run(["docker", "run", "--rm", "-v", f"{os.path.abspath(PLUGIN_SRC)}:/src:ro", "-v", f"{out}:/out",
                        "--entrypoint", "/usr/bin/bash", TRINO, "/src/build.sh", "/out"],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    os.chmod(out, 0o755)
    return out


@pytest.fixture(scope="module")
def cairn(lake, tmp_path_factory):  # noqa: F811
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("cairn")
    env = {"CAIRN_TRINO_ACCESS_KEY": KEY,
           "CAIRN_TRINO_CATALOGS": json.dumps({"lake": {"platform": "iceberg", "database": ""}}),
           "CAIRN_LAKEHOUSE_TRINO_CATALOG": "lake",
           "CAIRN_PIPES_CATALOG": json.dumps({"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                              "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                              "s3.region": "us-east-1", "s3.path-style-access": "true"}),
           "CAIRN_AI_SEARCH_USER_DAILY": "6"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                            dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")})
        s = build_services(settings, graph=InMemoryGraph(), with_ai=False, with_gateway=False)
    finally:
        for k, v in saved.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    assert s.sql_search is not None and s.vectors is not None
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": f"urn:li:corpuser:{u}"}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub("admin")),
        Policy(id="support", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*support.*"]),
               condition={"attr": "subject.urn", "op": "in", "value": ["urn:li:corpuser:ana", "urn:li:corpuser:bob"]}),
        Policy(id="bob-rows", effect="obligation", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=["*support.*"]),
               obligations=[Obligation(type="row_filter", params={"expression": "id <> 't3'"})], condition=sub("bob")),
    ]))
    cat = s.vectors._catalog()
    cat.create_namespace_if_not_exists("support")
    data = pa.table({"id": [d[0] for d in DOCS], "body": [d[1] for d in DOCS]})
    cat.create_table("support.articles", schema=data.schema).append(data)
    s.vectors.create({"name": "help", "source": "support.articles", "key": "id", "text": "body", "chunk_chars": 400},
                     "urn:li:corpuser:admin")
    assert s.vectors.refresh("help")["chunks"] == 4
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="0.0.0.0", port=p, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    wait(p)
    yield f"http://127.0.0.1:{p}", s
    server.should_exit = True
    t.join(30)


@pytest.fixture(scope="module")
def trino(lake, cairn):  # noqa: F811
    cairn_url, _ = cairn
    d = tempfile.mkdtemp(prefix="trino-search-")
    plugin = build_plugin(f"{d}/plugin")
    os.makedirs(f"{d}/catalog")
    tp = port()
    open(f"{d}/catalog/lake.properties", "w").write("\n".join([
        "connector.name=iceberg", "iceberg.catalog.type=rest", f"iceberg.rest-catalog.uri={lake['catalog']}",
        "iceberg.rest-catalog.security=OAUTH2", f"iceberg.rest-catalog.oauth2.token={CATALOG_TOKEN}",
        S3_ON, f"s3.endpoint={lake['store']}", "s3.region=us-east-1", "s3.path-style-access=true",
        f"s3.aws-access-key={STORE_KEY[0]}", f"s3.aws-secret-key={STORE_KEY[1]}", ""]))
    base = f"{cairn_url}/v1/trino/{KEY}"
    open(f"{d}/access-control.properties", "w").write("\n".join([
        "access-control.name=opa", f"opa.policy.uri={base}/allow", f"opa.policy.batched-uri={base}/batch",
        f"opa.policy.row-filters-uri={base}/row-filters", f"opa.policy.column-masking-uri={base}/column-mask", ""]))
    open(f"{d}/config.properties", "w").write("\n".join([
        "coordinator=true", "node-scheduler.include-coordinator=true", f"http-server.http.port={tp}",
        f"discovery.uri=http://127.0.0.1:{tp}", ""]))
    with open(f"{d}/trino.env", "w") as fh:          # as Helm passes them: never on a command line
        fh.write(f"CAIRN_TRINO_ACCESS_KEY={KEY}\nCAIRN_API_URL={cairn_url}\n")
    os.chmod(d, 0o755)
    name = f"cairn-ai-search-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
    try:
        subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host", "--env-file", f"{d}/trino.env",
                        "-v", f"{d}/catalog:/etc/trino/catalog:ro", "-v", f"{d}/config.properties:/etc/trino/config.properties:ro",
                        "-v", f"{d}/access-control.properties:/etc/trino/access-control.properties:ro",
                        "-v", f"{plugin}:/usr/lib/trino/plugin/cairn-functions:ro", TRINO],
                       check=True, capture_output=True, timeout=600)
        end = time.time() + 240
        while "SERVER STARTED" not in subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr:
            assert time.time() < end, "Trino did not start"
            time.sleep(1)

        def sql(statement, user):
            out = subprocess.run(["docker", "exec", "-e", "LANG=C.UTF-8", name, "trino", "--server", f"http://127.0.0.1:{tp}",
                                  "--user", user, "--catalog", "lake", "--output-format", "JSON", "--execute", statement],
                                 capture_output=True, text=True, timeout=300)
            if out.returncode != 0:
                raise RuntimeError(out.stderr.strip()[-800:] or out.stdout)
            return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
        yield sql
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        shutil.rmtree(d, ignore_errors=True)


SEARCH = "SELECT r.key, r.chunk, r.text, r.score FROM UNNEST(ai_search('help', '{q}', {k})) AS r(key, chunk, text, score)"


def test_ai_search_returns_the_closest_chunks_as_rows(trino, cairn):
    _, s = cairn
    rows = trino(SEARCH.format(q="how do I get my money back for a returned item", k=2), "ana")
    assert len(rows) == 2 and rows[0]["key"] == "t1" and rows[0]["chunk"] == 0
    assert rows[0]["text"].startswith("Refunds are issued") and rows[0]["score"] > rows[1]["score"]
    # The same answer as the API's search, as her.
    api = s.vectors.search(type("P", (), {"urn": "urn:li:corpuser:ana"})(), "help",
                           "how do I get my money back for a returned item", 2)["results"]
    assert [r["key"] for r in api] == [r["key"] for r in rows]
    # Joined back to the source table, governed like any other read.
    joined = trino("SELECT a.id, a.body, r.score FROM UNNEST(ai_search('help', 'forgot password login')) AS r(key, chunk, text, score) "
                   "JOIN lake.support.articles a ON a.id = r.key ORDER BY r.score DESC LIMIT 1", "ana")
    assert joined[0]["id"] == "t3" and "password" in joined[0]["body"]
    # The default k is 10 (here: every chunk there is).
    assert len(trino("SELECT * FROM UNNEST(ai_search('help', 'invoice pdf'))", "ana")) == 4
    # Metered under her: each search is in AI usage as sql_ai_search, with an estimate of the query's tokens.
    from sqlalchemy import select

    from cairn.ai.usage import usage_table

    with s.engine.connect() as conn:
        used = conn.execute(select(usage_table).where(usage_table.c.task == "sql_ai_search")).all()
    assert len(used) == 3 and {u.actor for u in used} == {"urn:li:corpuser:ana"}
    assert all(u.input_tokens >= 1 and u.model == "embeddings:hashing" for u in used)


def test_ai_search_refuses_as_the_api_does(trino, cairn):
    # No read access to the source table: refused, with Cairn's reason.
    with pytest.raises(RuntimeError, match="needs read access to support.articles"):
        trino(SEARCH.format(q="refund", k=3), "eve")
    # A row filter applies to bob on the source: the index holds the raw text, so it is not searched for him.
    with pytest.raises(RuntimeError, match="mask or row filter applies to you"):
        trino(SEARCH.format(q="refund", k=3), "bob")
    # No such index; and k out of range is refused before Cairn is asked.
    with pytest.raises(RuntimeError, match="no such"):
        trino("SELECT * FROM UNNEST(ai_search('nope', 'refund', 3))", "ana")
    with pytest.raises(RuntimeError, match="k is 1 to 100"):
        trino(SEARCH.format(q="refund", k=500), "ana")
    # Cairn's own identities are not people: refused.
    with pytest.raises(RuntimeError, match="not one"):
        trino(SEARCH.format(q="refund", k=3), "cairn-catalog")


def test_ai_search_is_capped_a_person_a_day_and_needs_trinos_key(trino, cairn):
    import httpx

    url, s = cairn
    # CAIRN_AI_SEARCH_USER_DAILY=6: ana's later searches today are refused (earlier tests used some of hers).
    with pytest.raises(RuntimeError, match="ai_search calls a day reached"):
        for _ in range(7):
            trino(SEARCH.format(q="shipping to europe", k=1), "ana")
    # Only Trino (its access key) may ask Cairn to search as someone.
    body = {"user": "admin", "index": "help", "query": "refund", "k": 1}
    assert httpx.post(f"{url}/v1/trino/functions/ai-search", json=body).status_code == 404
    assert httpx.post(f"{url}/v1/trino/functions/ai-search", json=body,
                      headers={"x-cairn-trino-key": "not-" + KEY}).status_code == 404
    ok = httpx.post(f"{url}/v1/trino/functions/ai-search", json=body, headers={"x-cairn-trino-key": KEY})
    assert ok.status_code == 200 and ok.json()["results"][0]["key"] == "t1"
