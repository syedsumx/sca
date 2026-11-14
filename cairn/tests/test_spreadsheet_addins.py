"""Cairn in Excel and Google Sheets (L118), end to end.

Cairn runs in this process on a real port (uvicorn) with a SQLite warehouse, the metrics layer and the decision
point with a PII mask, a row filter and a denied table. The add-ins are built (``addins/build.mjs``) for that Cairn
and their own JavaScript runs in Node: the Office add-in's client and custom functions (with a shim for Office's
``CustomFunctions`` and ``OfficeRuntime.storage``) and the Apps Script project (``Code.gs``, with a shim for
``SpreadsheetApp``, ``UrlFetchApp``, ``PropertiesService`` and the other services it uses). Each signs in as ana
through Cairn's real OAuth authorization code flow with PKCE (the consent page as a signed-in person), then runs
=QUERY, =METRIC and =ASK: a PII column comes back masked, the row filter holds, a denied table is refused, the token
reaches nothing outside its scopes, refresh rotates (one refresh for concurrent calls), and sign-out revokes.
The model behind ASK is a scripted stand-in (no model runs offline), as is the search index that picks its table."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
ADDINS = ROOT / "addins"
ANA, ADMIN = "urn:li:corpuser:ana", "urn:li:corpuser:admin"
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
USERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.users,PROD)"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
SECRETS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.secrets,PROD)"
ADDIN_ORIGIN = "http://localhost:8443"
SCRIPT_ID = "1Ab2Cd3Ef4Gh5Ij6Kl7Mn8Op9Qr0St-Uv_WxYz12345"
OFFICE = "{http://schemas.microsoft.com/office/appforoffice/1.1}"
BT = "{http://schemas.microsoft.com/office/officeappbasictypes/1.0}"
OV = "{http://schemas.microsoft.com/office/taskpaneappversionoverrides}"
XSI = "{http://www.w3.org/2001/XMLSchema-instance}"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ScriptedSQL:
    """Stands in for the model behind ask-your-data: always the same read-only statement."""

    def __init__(self):
        self.prompts = []

    def generate_json(self, system, prompt, schema, model=None):
        self.prompts.append(prompt)
        return {"sql": "SELECT region, count(*) AS orders FROM main.orders GROUP BY region ORDER BY region",
                "explanation": "orders per region"}, None


def _build_stack(graph, tmp_path, monkeypatch):
    from cairn.common.config import Settings
    from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
    from cairn.quality.collectors import ConnectionRegistry
    from cairn.services import build_services

    wh = f"sqlite:///{tmp_path}/wh.db"
    with create_engine(wh).begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER, email TEXT)"))
        c.execute(text("CREATE TABLE orders (id INTEGER, user_id INTEGER, amount REAL, region TEXT)"))
        c.execute(text("CREATE TABLE secrets (k TEXT)"))
        c.execute(text("INSERT INTO users VALUES (1, 'kim@acme.com'), (2, 'lee@acme.com')"))
        c.execute(text("INSERT INTO orders VALUES (1, 1, 10.0, 'EU'), (2, 1, 5.5, 'EU'), (3, 2, 7.0, 'US'), "
                       "(4, 2, 1.0, 'APAC')"))
        c.execute(text("INSERT INTO secrets VALUES ('s3cr3t')"))
    graph.aspects[(USERS, "schemaMetadata")] = {"fields": [
        {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}]}
    graph.aspects[(ORDERS, "schemaMetadata")] = {"fields": [{"fieldPath": f} for f in ("id", "user_id", "amount", "region")]}
    graph.aspects[(SECRETS, "schemaMetadata")] = {"fields": [{"fieldPath": "k"}]}
    monkeypatch.setenv("CAIRN_EXCEL_ADDIN_ORIGINS", ADDIN_ORIGIN)
    monkeypatch.setenv("CAIRN_SHEETS_ADDON_SCRIPT_IDS", SCRIPT_ID)
    settings = Settings(database_url=f"sqlite:///{tmp_path}/cairn.db", service_token="svc-secret",
                        dev_trust_actor_header=True, gateway={"key_dir": str(tmp_path / "keys")})
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.data_access.registry = ConnectionRegistry({"wh": {"url": wh, "catalog": {"platform": "sqlite"}}})
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub(ADMIN)),
        Policy(id="read", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               condition={"attr": "subject.urn", "op": "exists"}),
        Policy(id="secrets", effect="deny", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=[SECRETS])),
        Policy(id="mask-pii", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})]),
        Policy(id="ana-no-apac", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=[ORDERS]), condition=sub(ANA),
               obligations=[Obligation(type="row_filter", params={"expression": "region <> 'APAC'"})]),
    ]))
    s.metrics.save(ANA, {"name": "revenue", "label": "Revenue", "description": "Booked revenue", "connection": "wh",
                         "table": "main.orders", "measure": "Sum([amount])", "dimensions": ["region"]})
    s.ask.sql.llm = ScriptedSQL()
    s.ask.search = lambda question, can_view, k: [ORDERS]
    return s


@pytest.fixture
def cairn(graph, tmp_path, monkeypatch):
    import uvicorn

    from cairn.app import create_api_app

    s = _build_stack(graph, tmp_path, monkeypatch)
    app = create_api_app(s)
    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    yield s, app, f"http://127.0.0.1:{port}"
    server.should_exit = True


def _manifest_checks(path: Path, url: str) -> None:
    """The Office add-in manifest, structurally: the elements the schema requires, in its order, every resid
    defined, every URL on the add-in's origin (https for a real deployment, the test's loopback here)."""
    root = ET.parse(path).getroot()
    assert root.tag == f"{OFFICE}OfficeApp" and root.get(f"{XSI}type") == "TaskPaneApp"
    order = ["Id", "Version", "ProviderName", "DefaultLocale", "DisplayName", "Description", "IconUrl",
             "HighResolutionIconUrl", "SupportUrl", "AppDomains", "Hosts", "Requirements", "DefaultSettings",
             "Permissions", "VersionOverrides"]
    tags = [c.tag.split("}")[1] for c in root]
    assert tags == order, tags
    assert len(root.find(f"{OFFICE}Id").text) == 36 and root.find(f"{OFFICE}Permissions").text == "ReadWriteDocument"
    assert [d.text for d in root.find(f"{OFFICE}AppDomains")] == [url]
    vo = root.find(f"{OV}VersionOverrides")
    assert vo.get(f"{XSI}type") == "VersionOverridesV1_0"
    host = vo.find(f"{OV}Hosts/{OV}Host")
    assert host.get(f"{XSI}type") == "Workbook"
    cf = host.find(f"{OV}AllFormFactors/{OV}ExtensionPoint")
    assert cf.get(f"{XSI}type") == "CustomFunctions"
    assert host.find(f"{OV}Runtimes/{OV}Runtime").get("lifetime") == "long"
    defined = {e.get("id") for e in vo.iter() if e.tag in (f"{BT}Image", f"{BT}Url", f"{BT}String") and e.get("id")}
    used = {e.get("resid") for e in vo.iter() if e.get("resid")}
    assert used and used <= defined, used - defined
    urls = [e.get("DefaultValue") for e in root.iter() if (e.get("DefaultValue") or "").startswith("http")]
    assert urls and all(u.startswith(ADDIN_ORIGIN + "/") for u in urls), urls
    assert "example.com" not in path.read_text()
    for name in ("taskpane.html", "functions.js", "functions.json", "assets/icon-80.png", "callback.html", "dialog.html"):
        assert (path.parent / name).exists(), name


def _run_node(env: dict) -> str:
    r = subprocess.run(["node", "--test", "--test-concurrency=1", "test/*.test.mjs"], cwd=ADDINS, env=env, capture_output=True,
                       text=True, timeout=600)
    assert r.returncode == 0, f"node --test failed:\n{r.stdout[-6000:]}\n{r.stderr[-4000:]}"
    return r.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_excel_and_sheets_add_ins_against_cairn(cairn, tmp_path):
    s, app, url = cairn
    # The clients exist from the configuration: public, PKCE, the person's scopes; the Sheets token lives two hours.
    excel, sheets = s.agents.get("agt_excel_addin"), s.agents.get("agt_sheets_addon")
    assert excel["kind"] == sheets["kind"] == "oauth" and excel["enabled"] and sheets["enabled"]
    assert excel["redirect_uris"] == [f"{ADDIN_ORIGIN}/callback.html"]
    assert sheets["redirect_uris"] == [f"https://script.google.com/macros/d/{SCRIPT_ID}/usercallback"]
    assert excel["scopes"] == sheets["scopes"] == ["read", "query_data", "model"]

    dist = tmp_path / "dist"
    out = subprocess.run(["node", "build.mjs", "--cairn-url", url, "--api-base", url, "--addin-url", ADDIN_ORIGIN,
                          "--out", str(dist)], cwd=ADDINS, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    _manifest_checks(dist / "excel" / "manifest.xml", url)
    fns = json.loads((dist / "excel" / "functions.json").read_text())
    assert [f["id"] for f in fns["functions"]] == ["QUERY", "METRIC", "ASK"]
    project = json.loads((dist / "sheets" / "appsscript.json").read_text())
    assert project["urlFetchWhitelist"] == [f"{url}/"] and project["runtimeVersion"] == "V8"

    env = {**os.environ, "CAIRN_TEST_URL": url, "CAIRN_TEST_DIST": str(dist), "CAIRN_TEST_PERSON": ANA,
           "CAIRN_TEST_SCRIPT_ID": SCRIPT_ID, "CAIRN_TEST_ADDIN_ORIGIN": ADDIN_ORIGIN}
    stdout = _run_node(env)
    assert "# fail 0" in stdout and "# skipped 0" in stdout, stdout

    # Audited as ana, through each add-in.
    agents = s.audit.export(category="agents")
    authorized = {(e["subject"], e["actor"]) for e in agents if e["action"] == "agent.authorized"}
    assert {("agt_excel_addin", ANA), ("agt_sheets_addon", ANA)} <= authorized
    queried = [e for e in s.audit.export(category="data") if e["action"] == "data.queried" and e["actor"] == ANA]
    assert any("email" in (e["details"].get("masked") or []) for e in queried)
    denied = [e for e in s.audit.export(category="data") if e["action"] == "data.query_denied"]
    assert any(e["details"].get("table") == SECRETS for e in denied)


def test_cors_for_the_office_origin_only(graph, tmp_path, monkeypatch):
    from cairn.app import create_api_app

    s = _build_stack(graph, tmp_path, monkeypatch)
    c = TestClient(create_api_app(s))
    pre = {"Origin": ADDIN_ORIGIN, "Access-Control-Request-Method": "POST",
           "Access-Control-Request-Headers": "authorization, content-type"}
    ok = c.options("/data/sheets/query", headers=pre)
    assert ok.status_code == 204 and ok.headers["access-control-allow-origin"] == ADDIN_ORIGIN
    assert "Authorization" in ok.headers["access-control-allow-headers"]
    assert "access-control-allow-credentials" not in ok.headers                     # bearer tokens, never cookies
    assert c.options("/oauth/token", headers=pre).headers["access-control-allow-origin"] == ADDIN_ORIGIN
    # Another origin, or a route the add-in does not call: no CORS, so a browser refuses.
    assert "access-control-allow-origin" not in c.options("/data/sheets/query",
                                                           headers={**pre, "Origin": "https://evil.example"}).headers
    assert "access-control-allow-origin" not in c.options("/admin/agents", headers=pre).headers
    assert "access-control-allow-origin" not in c.get("/admin/agents", headers={"Origin": ADDIN_ORIGIN}).headers
    # A simple request from the add-in gets the header (and still needs a token).
    r = c.post("/data/sheets/query", json={"sql": "SELECT 1"}, headers={"Origin": ADDIN_ORIGIN})
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == ADDIN_ORIGIN


def test_sheet_grids_and_argument_parsing(graph, tmp_path, monkeypatch):
    from cairn.app import create_api_app
    from cairn.dataaccess.sheets import SheetsError, parse_dimensions, parse_filters

    assert parse_dimensions([["region"], ["country, city"], [""]]) == ["region", "country", "city"]
    assert parse_filters("region = EU; amount >= 10.5") == [
        {"column": "region", "op": "=", "value": "EU"}, {"column": "amount", "op": ">=", "value": 10.5}]
    assert parse_filters([["region in EU, 'US'"], ["n <> 3"], ["x not in (1, 2)"]]) == [
        {"column": "region", "op": "in", "values": ["EU", "US"]}, {"column": "n", "op": "!=", "value": 3},
        {"column": "x", "op": "not_in", "values": [1, 2]}]
    for bad in ("region", "1x = 2", "region = "):
        with pytest.raises(SheetsError):
            parse_filters(bad)
    with pytest.raises(SheetsError):
        parse_dimensions("region; DROP TABLE x")

    s = _build_stack(graph, tmp_path, monkeypatch)
    c = TestClient(create_api_app(s), headers={"X-Cairn-Actor": ANA})
    q = c.post("/data/sheets/query", json={"sql": "SELECT id, email FROM main.users ORDER BY id"}).json()
    assert q["values"][0] == ["id", "email"] and q["values"][1][0] == 1 and "kim@acme.com" not in json.dumps(q)
    assert q["masked_columns"] == ["email"] and q["connection"] == "wh"         # the only connection is the default
    m = c.post("/data/sheets/metric", json={"name": "revenue", "dimensions": "region"}).json()
    assert m["values"] == [["region", "revenue"], ["EU", 15.5], ["US", 7.0]], m          # the row filter holds
    bare = c.post("/data/sheets/metric", json={"name": "revenue", "headers": False, "filters": ["region = US"]}).json()
    assert bare["values"] == [[7.0]]
    assert c.post("/data/sheets/metric", json={"name": "nope"}).status_code == 404
    assert c.post("/data/sheets/metric", json={"name": "revenue", "dimensions": "user_id"}).status_code == 400
    assert c.post("/data/sheets/query", json={"sql": "SELECT k FROM main.secrets"}).status_code == 403
    assert c.post("/data/sheets/query", json={"sql": "DELETE FROM main.orders"}).status_code == 400
    listed = c.get("/data/sheets/metrics", params={"q": "booked"}).json()
    assert [(x["name"], x["dimensions"], x["source"]) for x in listed] == [("revenue", ["region"], "metrics")]
    a = c.post("/data/sheets/ask", json={"question": "orders per region"}).json()
    assert a["values"] == [["region", "orders"], ["EU", 2], ["US", 1]] and a["sql"].startswith("SELECT region")
    # Services are not people: the add-ins' endpoints refuse them.
    assert TestClient(create_api_app(s)).get("/data/sheets/metrics", headers={
        "Authorization": "Bearer svc-secret"}).status_code == 403


def test_configured_clients_follow_the_configuration(graph, tmp_path, monkeypatch):
    from cairn.identity.addins import SpreadsheetAddins, excel_origins, sheets_script_ids

    s = _build_stack(graph, tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        excel_origins("http://addins.example.com")              # https only (loopback aside)
    with pytest.raises(ValueError):
        excel_origins("https://addins.example.com/path")
    with pytest.raises(ValueError):
        sheets_script_ids("not a script id")
    code = s.agents.issue_code("agt_excel_addin", ANA, ["read"], f"{ADDIN_ORIGIN}/callback.html", "x" * 43, None)
    assert code
    tokens = s.agents._tokens  # noqa: SLF001 - make a grant to see it revoked
    with s.engine.begin() as conn:
        grant = tokens(conn, "agt_excel_addin", ANA, ["read"], None)
    assert s.agents.resolve(grant["access_token"]).actor == ANA
    # Removed from the configuration: the client is off and its tokens revoked; configured again: back on.
    SpreadsheetAddins(s.agents, [], [SCRIPT_ID]).sync()
    assert not s.agents.get("agt_excel_addin")["enabled"] and s.agents.resolve(grant["access_token"]) is None
    SpreadsheetAddins(s.agents, ["https://addins.example.com"], [SCRIPT_ID]).sync()
    again = s.agents.get("agt_excel_addin")
    assert again["enabled"] and again["redirect_uris"] == ["https://addins.example.com/callback.html"]
    # An administrator's own disable is kept across restarts.
    from cairn.identity.agents import AgentPatch

    s.agents.update("agt_excel_addin", AgentPatch(enabled=False), ADMIN)
    SpreadsheetAddins(s.agents, ["https://addins.example.com"], [SCRIPT_ID]).sync()
    assert not s.agents.get("agt_excel_addin")["enabled"]
