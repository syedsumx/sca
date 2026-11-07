"""App packages (Snowflake gap L): a provider releases versions from a real git repository; a consumer installs one
on their own data and opens it through Cairn, where it runs as a real process. Covered:
- binding is consent: you cannot hand the app a table you cannot read;
- the app runs with none of Cairn's environment and gets a Trino token of its own;
- Trino (its access-control protocol, against the real policy engine) lets that identity read the bound table only,
  with the installer's row filter and mask, and nothing once uninstalled;
- versions are immutable; upgrades bind new references; a rollback runs the old commit."""

import base64
import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
from cairn.services import build_services

KEY = "trino-key-" + "k" * 24
ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
REGIONS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.regions,PROD)"
SALARIES = "urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)"
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
SERVER = '''
import http.server, json, os, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"version": "{version}", "app_token": bool(self.headers.get("X-Cairn-App-Trino-Token")),
                "token": self.headers.get("X-Cairn-App-Trino-Token") or "",
                "leaked": sorted(k for k in os.environ if k.startswith("CAIRN_") and k not in ("CAIRN_APP_KEY", "CAIRN_APP_BASE"))}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@x", "GIT_COMMITTER_NAME": "dev",
                               "GIT_COMMITTER_EMAIL": "dev@x"}).stdout.strip()


def commit(dev, version, refs):
    d = dev / "churn"
    d.mkdir(parents=True, exist_ok=True)
    (d / "server.py").write_text(SERVER.replace("{version}", version))
    (d / "cairn-app.json").write_text(json.dumps({
        "title": "Churn explorer", "command": ["python3", "server.py", "{port}"],
        "references": [{"name": r, "privilege": "VIEW_DATASET_PROFILE", "description": f"the {r} table"} for r in refs]}))
    git(dev, "add", "-A")
    git(dev, "commit", "--quiet", "-m", version)
    git(dev, "push", "--quiet", "origin", "main")
    return git(dev, "rev-parse", "HEAD")


@pytest.fixture
def world(settings, graph, tmp_path, monkeypatch):
    bare, dev = tmp_path / "pkg.git", tmp_path / "dev"
    git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    git(tmp_path, "clone", "--quiet", str(bare), str(dev))
    monkeypatch.setenv("CAIRN_APP_GIT_URLS", "file://")
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAIRN_TRINO_ACCESS_KEY", KEY)
    monkeypatch.setenv("CAIRN_TRINO_CATALOGS", '{"lakehouse": {"platform": "iceberg", "database": ""}}')
    monkeypatch.setenv("CAIRN_SECRET_PROBE", "must-not-reach-the-app")
    for u, cols in ((ORDERS, ("id", "email", "region")), (REGIONS, ("code",)), (SALARIES, ("name", "salary"))):
        graph.aspects[(u, "schemaMetadata")] = {"fields": [
            {"fieldPath": c, **({"glossaryTerms": {"terms": [{"urn": EMAIL}]}} if c == "email" else {})} for c in cols]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="provider", effect="allow", actions=["CREATE_APP_PACKAGE", "MANAGE_APP_PACKAGE"], condition=sub(ANA)),
        Policy(id="consumer", effect="allow", actions=["USE_APP_PACKAGE", "USE_APP", "MANAGE_DATA_APP"], condition=sub(BOB)),
        Policy(id="sales", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.*"])),
        Policy(id="bob-eu", effect="obligation", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=["*sales.*"]),
               obligations=[Obligation(type="row_filter", params={"expression": "region = 'EU'"})], condition=sub(BOB)),
        Policy(id="mask", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})])]))
    app = create_api_app(s)
    yield {"s": s, "dev": dev, "url": f"file://{bare}", "ana": TestClient(app, headers={"X-Cairn-Actor": ANA}),
           "bob": TestClient(app, headers={"X-Cairn-Actor": BOB}), "raw": TestClient(app)}
    for name in list(s.data_apps._running):
        s.data_apps.stop(name)


def ask(c, path, user, op, resource=None):
    body = {"input": {"context": {"identity": {"user": user, "groups": []}, "softwareStack": {"trinoVersion": "476"}},
                      "action": {"operation": op, **({"resource": resource} if resource else {})}}}
    r = c.post(f"/v1/trino/{KEY}/{path}", json=body)
    return r.json().get("result") if r.status_code == 200 else f"HTTP {r.status_code}"


def table(t, schema_="sales", **kw):
    return {"table": {"catalogName": "lakehouse", "schemaName": schema_, "tableName": t, **kw}}


def test_a_package_is_released_installed_on_your_own_data_and_upgraded(world):
    ana, bob, raw = world["ana"], world["bob"], world["raw"]
    c1 = commit(world["dev"], "v1", ["orders"])
    git_ = {"url": world["url"], "branch": "main", "path": "churn"}
    assert bob.post("/v1/app-packages", json={"name": "churn", "git": git_}).status_code == 403
    assert ana.post("/v1/app-packages", json={"name": "churn", "git": git_}).status_code == 201
    r = ana.post("/v1/app-packages/churn/versions", json={"version": "1.0.0"})
    assert r.status_code == 201 and r.json()["commit"] == c1, r.text
    assert ana.post("/v1/app-packages/churn/versions", json={"version": "1.0.0"}).status_code == 409   # immutable
    assert [v["version"] for v in bob.get("/v1/app-packages/churn").json()["versions"]] == ["1.0.0"]
    # Binding is consent: Bob may not hand the app a table he cannot read; nor leave a reference unbound.
    inst = {"name": "bob-churn", "package": "churn", "version": "1.0.0"}
    assert bob.post("/v1/app-installs", json={**inst, "references": {"orders": SALARIES}}).status_code == 403
    assert bob.post("/v1/app-installs", json={**inst, "references": {}}).status_code == 400
    r = bob.post("/v1/app-installs", json={**inst, "references": {"orders": ORDERS}})
    assert r.status_code == 201 and r.json()["commit"] == c1, r.text
    assert r.json()["identity"] == "urn:li:corpuser:cairnapp-bob-churn"
    # Opened through Cairn: the app has a token of its own, and none of Cairn's environment.
    page = bob.get("/apps/bob-churn/").json()
    assert page["version"] == "v1" and page["app_token"] and page["leaked"] == []
    claims = json.loads(base64.urlsafe_b64decode(page["token"].split(".")[1] + "=="))
    assert claims["sub"] == "cairnapp-bob-churn" and claims["aud"] == "trino"
    # What that identity may do on Trino: the bound table, read-only, as Bob (his row filter, the mask).
    app = "cairnapp-bob-churn"
    assert ask(raw, "allow", app, "ExecuteQuery") is True
    assert ask(raw, "allow", app, "SelectFromColumns", table("orders", columns=["id", "email"])) is True
    assert ask(raw, "allow", app, "SelectFromColumns", table("regions", columns=["code"])) is False   # Bob's, not bound
    assert ask(raw, "allow", app, "SelectFromColumns", table("salaries", "hr", columns=["salary"])) is False
    assert ask(raw, "allow", app, "InsertIntoTable", table("orders")) is False
    assert ask(raw, "allow", app, "SelectFromColumns", table("orders$files", columns=["file_path"])) is False
    assert ask(raw, "batch", app, "FilterTables") == []
    filters = ask(raw, "row-filters", app, "GetRowFilters", table("orders"))
    assert filters == [{"expression": "region = 'EU'"}]
    mask = ask(raw, "column-mask", app, "GetColumnMask",
               {"column": {"catalogName": "lakehouse", "schemaName": "sales", "tableName": "orders",
                           "columnName": "email", "columnType": "varchar"}})
    assert mask and "email" in mask["expression"]
    # Version 1.1.0 asks for a second table: upgrading needs it bound, then runs the new commit; rolling back
    # runs the old one again.
    c2 = commit(world["dev"], "v2", ["orders", "regions"])
    assert ana.post("/v1/app-packages/churn/versions", json={"version": "1.1.0"}).json()["commit"] == c2
    assert bob.post("/v1/app-installs/bob-churn/upgrade", json={"version": "1.1.0"}).status_code == 400
    r = bob.post("/v1/app-installs/bob-churn/upgrade", json={"version": "1.1.0", "references": {"regions": REGIONS}})
    assert r.status_code == 200 and r.json()["commit"] == c2, r.text
    assert bob.get("/apps/bob-churn/").json()["version"] == "v2"
    assert ask(raw, "allow", app, "SelectFromColumns", table("regions", columns=["code"])) is True
    r = bob.post("/v1/app-installs/bob-churn/upgrade", json={"version": "1.0.0"})
    assert r.status_code == 200 and r.json()["commit"] == c1
    assert bob.get("/apps/bob-churn/").json()["version"] == "v1"
    assert r.json()["references"] == {"orders": ORDERS}                      # regions is unbound again
    assert ask(raw, "allow", app, "SelectFromColumns", table("regions", columns=["code"])) is False
    # Uninstalled: the identity reads nothing.
    assert ana.delete("/v1/app-installs/bob-churn").status_code == 403     # not Ana's install
    assert bob.delete("/v1/app-installs/bob-churn").status_code == 204
    assert ask(raw, "allow", app, "SelectFromColumns", table("orders", columns=["id"])) in (False, "HTTP 403")


def test_manifests_and_names_are_checked(world):
    from cairn.dataaccess.app_packages import PackageError, check_manifest

    for bad in [[], {"command": "x"}, {"command": ["x"], "references": [{"name": "Bad"}]},
                {"command": ["x"], "references": [{"name": "a"}, {"name": "a"}]},
                {"command": ["x"], "references": [{"name": "a", "privilege": "WRITE_DATASET"}]},
                {"command": ["x"], "idle_minutes": 0}]:
        with pytest.raises(PackageError):
            check_manifest(bad)
    ana = world["ana"]
    assert ana.post("/v1/app-packages", json={"name": "Bad", "git": {"url": world["url"]}}).status_code == 400
    assert ana.post("/v1/app-packages", json={"name": "x", "git": {"url": "https://elsewhere.example/x.git"}}).status_code == 403
    # The app identity's name is reserved: nobody signs in as one.
    from cairn.common.security import AuthenticationError

    with pytest.raises(AuthenticationError):
        world["s"].identity.resolve({"X-Cairn-Actor": "urn:li:corpuser:cairnapp-x"}, {})
