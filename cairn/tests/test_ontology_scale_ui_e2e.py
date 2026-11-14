"""The Ontology explorer (L129) with a real ontology of 500+ terms, in Chromium, measured.

A catalog of 150 tables in six schemas (each with keys to the table before it, every fifth one to a table in another
schema) is compiled by the real compiler (cairn-ontology, SQLite as its store), its model calls going through Cairn's
AI gateway to a local stand-in for the provider that answers each step with the structured JSON it asks for (the
pattern of tests/test_ontology_e2e.py), four business terms per table: 600 terms and their relationships. The built UI
bundle (ui/dist/mfe) is mounted on the real Cairn API, and Chromium opens /ontology as a steward.

Measured in the page (performance.now(), from the person's input to the frame that shows the result) and asserted:
first render of the explorer (navigation to the grouped graph on screen) under 3 s; filtering to one domain, search
and zoom to a term, and clustering (every term, then grouped by domain again) each under 500 ms. The numbers are
printed (`-s`) and written to $CAIRN_ONTOLOGY_SCALE_REPORT when set.

Needs Playwright (with Chromium), the built bundle and CAIRN_ONTOLOGY_BIN; no docker."""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
ADMIN, STEW, BOT = "urn:li:corpuser:admin", "urn:li:corpuser:stew", "urn:li:corpuser:svc-ontology"
CHAT, EMBED = "onto-chat", "onto-embed"
TOKEN = "ontology-scale-" + "t" * 24
SCHEMAS = {"sales": "Order", "finance": "Invoice", "people": "Employee", "operations": "Shipment",
           "marketing": "Campaign", "supply": "Supplier"}
PER_SCHEMA = 25                                         # 6 x 25 = 150 tables, four terms each
FACETS = ("Status", "Amount", "Place")                  # the three terms beside each table's main one
LIMITS_MS = {"first_render": 3000, "filter": 500, "search_zoom": 500, "cluster_expand": 500, "cluster_collapse": 500}


def _ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(os.path.join(BUNDLE, "remoteEntry.js")) and bool(os.environ.get("CAIRN_ONTOLOGY_BIN"))


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Playwright, the built UI bundle (npm run build:mfe) and "
                                                     "CAIRN_ONTOLOGY_BIN")

HOST = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Cairn</title></head>
<body><div id="root"></div><script src="/ui/remoteEntry.js"></script><script>
(async () => {
  await cairnConsumer.init({});
  const mount = (await cairnConsumer.get('./mount'))().mount;
  mount(document.getElementById('root'), { apiBaseUrl: window.location.origin, catalogUrl: null });
})();
</script></body></html>"""


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(port, proc=None, secs=60):
    end = time.time() + secs
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return
        except OSError:
            assert time.time() < end and (proc is None or proc.poll() is None), f"nothing listens on {port}"
            time.sleep(0.2)


def table_name(schema: str, i: int) -> str:
    return f"{schema}_e{i:02d}"


def ds(schema: str, i: int) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:iceberg,lake.{schema}.{table_name(schema, i)},PROD)"


def main_term(table: str) -> str:
    schema, n = table.rsplit("_e", 1)
    return f"{SCHEMAS[schema]} V{int(n)}"


def parents(schema: str, i: int) -> list[str]:
    """The tables this one keys to: the one before it, and every fifth one to the same place in the next schema."""
    out = [table_name(schema, i - 1)] if i else []
    if i % 5 == 0:
        names = list(SCHEMAS)
        out.append(table_name(names[(names.index(schema) + 1) % len(names)], i))
    return out


def catalog(graph) -> int:
    n = 0
    for schema in SCHEMAS:
        for i in range(PER_SCHEMA):
            name = table_name(schema, i)
            cols = [("id", "bigint"), *[(f"{p}_id", "bigint") for p in parents(schema, i)], ("code", "varchar"),
                    ("status", "varchar"), ("note", "varchar"), ("amount", "double"), ("region", "varchar"),
                    ("created_on", "date")]
            graph.aspects[(ds(schema, i), "schemaMetadata")] = {
                "schemaName": name, "primaryKeys": ["id"],
                "fields": [{"fieldPath": c, "nativeDataType": t, "nullable": c != "id"} for c, t in cols]}
            graph.aspects[(ds(schema, i), "datasetProperties")] = {
                "name": name, "description": f"{SCHEMAS[schema]} records of the {schema} team, set {i}."}
            n += 1
    return n


def answer(task: str, user: str) -> dict:
    """Each compilation step's structured answer, from what the step's prompt shows."""
    if task == "column_descriptions":
        cols = re.findall(r"^  - (\S+) \(", user, re.M)
        return {"descriptions": [{"column_name": c, "description": f"The {c.replace('_', ' ')} of the record."}
                                 for c in cols]}
    if task == "fk_suggestions":
        cand = user.split("Candidate columns:\n", 1)[1]
        cols = re.findall(r"^  - (\S+) \(", cand, re.M)
        return {"fk_suggestions": [{"column_name": c, "rationale": f"{c} names another table"}
                                   for c in cols if c.endswith("_id")],
                "pk_column_names": [c for c in cols if c == "id"], "is_junction_table": False,
                "junction_table_rationale": ""}
    if task == "terms":
        table = re.search(r"^Table: (\S+)", user, re.M).group(1)
        cols = re.findall(r"source_column=(\S+) \(", user)
        base = main_term(table)
        groups = {base: [c for c in cols if c == "id" or c.endswith("_id") or c == "code"],
                  f"{base} Status": [c for c in cols if c in ("status", "note")],
                  f"{base} Amount": [c for c in cols if c == "amount"],
                  f"{base} Place": [c for c in cols if c in ("region", "created_on")]}
        return {"terms": [{"name": name, "description": f"The {name.lower()} in the business.",
                           "attributes": [{"source_column": c, "display_name": c.replace("_", " ").title().replace(" ", "")}
                                          for c in group]}
                          for name, group in groups.items() if group]}
    if task == "synonyms":
        return {"synonyms": []}
    if task == "fk_hit":
        col = re.search(r"Foreign-key column: (\S+)", user).group(1)
        want = main_term(col.removesuffix("_id"))
        for cid, term in re.findall(r"column_id=(\S+) \| ColumnAttribute: [^\n]*? of Term ([^.(\n]+?)\s*[.(]", user):
            if term == want:
                return {"selected_id": cid, "rationale": f"{col} identifies a {want}"}
        return {"selected_id": None, "rationale": "not sure"}
    if task == "join_default":
        return {"selected_column_id": re.search(r"source_column_id: (\S+)", user).group(1), "rationale": "first"}
    if task == "sql_attributes":
        return {"suggestions": []}
    raise AssertionError(f"unexpected task {task}")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    import uvicorn
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle, Target
    from cairn.identity.agents import AgentPatch
    from cairn.services import build_services
    from test_ontology_e2e import OntologyProvider

    class Scale(OntologyProvider):
        def reply(self, body) -> str:
            task = ((body.get("response_format") or {}).get("json_schema") or {}).get("name")
            return json.dumps(answer(task, body["messages"][-1]["content"])) if task else "Hello from the model"

    tmp = tmp_path_factory.mktemp("onto-scale")
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Scale)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    d = tempfile.mkdtemp(prefix="onto-scale-")
    with open(f"{d}/token", "w") as fh:
        fh.write(TOKEN)
    cport = _port()
    env = {"CAIRN_AI_GATEWAY_OPENAI_URL": f"http://127.0.0.1:{up.server_address[1]}",
           "CAIRN_AI_GATEWAY_OPENAI_KEY": "sk-oai-upstream", "CAIRN_AI_GATEWAY_MODELS": f"{CHAT},{EMBED}",
           "CAIRN_ONTOLOGY_URL": f"http://127.0.0.1:{cport}", "CAIRN_ONTOLOGY_TOKEN_FILE": f"{d}/token",
           "CAIRN_ONTOLOGY_INTERVAL_SECONDS": "0"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    procs: list = []
    server = None
    try:
        graph = InMemoryGraph()
        assert catalog(graph) == 6 * PER_SCHEMA
        settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                            dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")})
        s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
        sub = lambda who: {"attr": "subject.urn", "op": "eq", "value": who}  # noqa: E731
        models = Target(resource_urns=[f"urn:li:aiModel:{CHAT}", f"urn:li:aiModel:{EMBED}"])
        s.pdp.set_bundle(PolicyBundle(policies=[
            Policy(id="admin", effect="allow", actions=["*"], condition=sub(ADMIN)),
            Policy(id="steward", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "MANAGE_GLOSSARIES",
                                                          "EDIT_ENTITY_GLOSSARY_TERMS"], condition=sub(STEW)),
            Policy(id="compiler-models", effect="allow", actions=["USE_AI_GATEWAY"], target=models, condition=sub(BOT)),
        ]))
        app = create_api_app(s)
        app.mount("/ui", StaticFiles(directory=BUNDLE), name="ui")
        app.add_api_route("/host", lambda: HTMLResponse(HOST), methods=["GET"])
        p = _port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=p, timeout_keep_alive=75, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        _wait(p)
        url = f"http://127.0.0.1:{p}"
        # The compiler, with its own agent identity for model calls (a key in a file, never on a command line).
        agent = s.ontology.create_agent(BOT, ADMIN)
        s.agents.update(agent["id"], AgentPatch(per_minute=10000), ADMIN)
        with open(f"{d}/model-key", "w") as fh:
            fh.write(agent["key"])
        with open(f"{d}/db-url", "w") as fh:
            fh.write(f"sqlite:{tmp}/ontology.db")
        proc = subprocess.Popen([os.environ["CAIRN_ONTOLOGY_BIN"]], stdout=open(f"{d}/compiler.log", "a"),
                                stderr=subprocess.STDOUT, env={
            **{k: v for k, v in os.environ.items() if not k.startswith(("CAIRN_", "ANTHROPIC_"))},
            "CAIRN_ONTOLOGY_ADDR": f"127.0.0.1:{cport}", "CAIRN_ONTOLOGY_DATABASE_URL_FILE": f"{d}/db-url",
            "CAIRN_ONTOLOGY_TOKEN_FILE": f"{d}/token", "CAIRN_ONTOLOGY_MODEL_URL": f"{url}/v1/ai/gateway/openai/v1",
            "CAIRN_ONTOLOGY_MODEL": CHAT, "CAIRN_ONTOLOGY_EMBED_MODEL": EMBED,
            "CAIRN_ONTOLOGY_MODEL_API_KEY_FILE": f"{d}/model-key", "CAIRN_ONTOLOGY_MODEL_RETRIES": "1",
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        procs.append(proc)
        _wait(cport, proc)
        started = time.time()
        r = httpx.post(f"{url}/v1/ontology/runs", json={"mode": "full", "wait": True},
                       headers={"X-Cairn-Actor": STEW}, timeout=1800)
        assert r.status_code == 200, r.text
        assert r.json()["run"]["state"] == "completed", (r.text[:2000], open(f"{d}/compiler.log").read()[-3000:])
        yield {"url": url, "s": s, "compile_seconds": round(time.time() - started, 1), "dir": d}
    finally:
        if server is not None:
            server.should_exit = True
        for proc in procs:
            proc.terminate()
            proc.wait(15)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        up.shutdown()
        shutil.rmtree(d, ignore_errors=True)


# In the page: the time from the person's last pointer-down to the frame where `cond` first holds.
SINCE_INPUT = """() => { window.__t0 = null; document.addEventListener('pointerdown',
    () => { window.__t0 = performance.now(); }, { capture: true }); }"""


def _since_input(page, cond: str) -> float:
    return page.wait_for_function(f"() => (window.__t0 !== null && ({cond})) ? performance.now() - window.__t0 : null",
                                  timeout=30_000, polling="raf").json_value()


def test_a_real_ontology_of_600_terms_is_explored_fast(stack):
    from playwright.sync_api import expect, sync_playwright

    url = stack["url"]
    data = httpx.get(f"{url}/v1/ontology/explorer", headers={"X-Cairn-Actor": STEW}, timeout=120).json()
    terms, rels = len(data["terms"]), len(data["relationships"])
    assert terms >= 500, (terms, rels)
    assert rels >= 100, (terms, rels)               # each table keys to the one before it (and across schemas)
    timings: dict[str, float] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(extra_http_headers={"X-Cairn-Actor": STEW},
                                   viewport={"width": 1600, "height": 1000}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            # Warm the bundle's cache (the host's assets), as a person who has used Cairn before.
            page.goto(f"{url}/host#/discover", wait_until="networkidle")
            # First render: from navigating to /ontology to the grouped graph (one node per domain) on screen.
            # (Another URL than the warm-up's, so a new document: performance.now() counts from this navigation.)
            page.goto(f"{url}/host?open=ontology#/ontology", wait_until="commit")
            assert page.evaluate("performance.getEntriesByType('navigation')[0].name").endswith("#/ontology")
            canvas = page.get_by_test_id("ontology-canvas")
            expect(canvas.locator('[data-node^="cluster:"]').first).to_be_visible(timeout=60_000)
            timings["first_render"] = page.evaluate("performance.now()")
            clusters = canvas.locator('[data-node^="cluster:"]').count()
            assert clusters == len(SCHEMAS), clusters
            expect(page.get_by_text(re.compile(rf"{terms} terms grouped into {clusters} domains"))).to_be_visible()
            page.evaluate(SINCE_INPUT)

            # Filter: one domain (the Domains select), its ~100 terms drawn.
            page.get_by_role("combobox", name="Domains").click()
            page.locator(".ant-select-item-option", has_text="schema finance").first.click()
            timings["filter"] = _since_input(
                page, "document.querySelectorAll('[data-testid=ontology-canvas] [data-node^=\"cluster:\"]').length === 0 && "
                      "document.querySelectorAll('[data-testid=ontology-canvas] [data-node]').length >= 90")
            page.keyboard.press("Escape")
            page.get_by_role("button", name="Clear filters").click()
            expect(canvas.locator('[data-node^="cluster:"]').first).to_be_visible()

            # Search and zoom: a term in another domain, chosen from the suggestions; its group opens, it is selected.
            find = page.get_by_role("combobox", name="Find a term")
            find.fill("Supplier V17 Amount")
            option = page.locator(".ant-select-item-option", has_text="Supplier V17 Amount").first
            expect(option).to_be_visible()
            option.click()
            timings["search_zoom"] = _since_input(
                page, "(() => { const n = document.querySelector('[data-testid=ontology-canvas] [aria-pressed=\"true\"]');"
                      " return !!n && (n.getAttribute('aria-label') || '').startsWith('Supplier V17 Amount'); })()")
            expect(page.get_by_test_id("term-detail")).to_contain_text("Supplier V17 Amount", timeout=20_000)
            page.get_by_role("button", name="Close the details").click()
            page.get_by_role("button", name="Clear filters").click()
            expect(canvas.locator('[data-node^="cluster:"]').first).to_be_visible()

            # Clustering: every term at once, then grouped by domain again.
            page.get_by_role("button", name="Show every term").click()
            timings["cluster_expand"] = _since_input(
                page, f"document.querySelectorAll('[data-testid=ontology-canvas] [data-node]').length >= {terms}")
            page.get_by_role("button", name="Group by domain").click()
            timings["cluster_collapse"] = _since_input(
                page, f"document.querySelectorAll('[data-testid=ontology-canvas] [data-node]').length === {clusters}")
        finally:
            browser.close()
    report = {"terms": terms, "relationships": rels, "tables": 6 * PER_SCHEMA, "compile_seconds": stack["compile_seconds"],
              "timings_ms": {k: round(v) for k, v in timings.items()}, "limits_ms": LIMITS_MS}
    print("\nontology explorer at scale:", json.dumps(report))
    if os.environ.get("CAIRN_ONTOLOGY_SCALE_REPORT"):
        with open(os.environ["CAIRN_ONTOLOGY_SCALE_REPORT"], "w") as fh:
            json.dump(report, fh, indent=1)
    assert not errors, errors
    slow = {k: round(v) for k, v in timings.items() if v >= LIMITS_MS[k]}
    assert not slow, f"over the limits {LIMITS_MS}: {slow} (all: {report['timings_ms']})"
