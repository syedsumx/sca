"""Bundles (cairn.iac): declared resources planned and applied through Cairn's own API, with the bundle's
state kept by Cairn; prune touches only what the bundle created. Runs the real API in-process."""

import json
import textwrap

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.iac import bundle as b
from cairn.services import build_services

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"


@pytest.fixture
def api(settings, graph, monkeypatch):
    monkeypatch.setenv("CAIRN_PIPES_STORE", json.dumps({"endpoint": "http://127.0.0.1:1"}))
    monkeypatch.setenv("CAIRN_PIPES_CATALOG", json.dumps({"uri": "http://127.0.0.1:1"}))
    monkeypatch.setenv("CAIRN_STREAMS_ENABLED", "true")
    monkeypatch.setenv("CAIRN_MAINTENANCE_CONNECTION", "lakehouse")    # also turns derived tables on
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    app = create_api_app(s)
    return s, TestClient(app, headers={"X-Cairn-Actor": ADMIN}), TestClient(app, headers={"X-Cairn-Actor": ANA})


def write(tmp_path, text, name="cairn.yaml"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text))
    return str(p)


BUNDLE = """
    bundle: sales
    maintenance:
      - {scope: "*", expire_days: 7, orphan_days: 7}
    pipes:
      - {name: orders-files, bucket: landing, prefix: orders/, format: csv, target: raw.orders, create_table: true}
    streams:
      - {name: customers-cdc, topic: "${TOPIC}", format: debezium, key: [id], target: cdc.customers, create_table: true}
    derived:
      - name: revenue
        target: marts.revenue
        on_change: true
        sql: SELECT region, sum(amount) AS total FROM raw.orders GROUP BY region;
"""


def test_plan_apply_update_and_prune(api, tmp_path):
    s, admin, _ = api
    c = b.Client("", "unused", http=admin)
    path = write(tmp_path, BUNDLE)
    with pytest.raises(b.BundleError, match=r"\$\{TOPIC\} is not set"):
        b.load(path)
    spec = b.load(path, {"TOPIC": "pg.public.customers"})
    changes, state = b.plan(c, spec)
    assert [(x.action, x.kind, x.key) for x in changes] == [
        ("create", "maintenance", "*"), ("create", "pipes", "orders-files"), ("create", "streams", "customers-cdc"),
        ("create", "derived", "revenue")]
    out = b.apply(c, spec, changes, state, log=lambda _: None)
    assert out["changed"] == 4
    assert {p["name"] for p in s.pipes.list()} == {"orders-files"}
    st = s.streams.list()[0]
    assert (st["mode"], st["key"]) == ("upsert", ["id"])               # debezium is upsert
    d = s.derived.list()[0]
    assert d["sql"].endswith("GROUP BY region") and d["owner"] == ADMIN
    assert s.maintenance.policy_for("raw", "orders")["expire_days"] == 7
    # Applying it again changes nothing.
    changes, state = b.plan(c, spec)
    assert {x.action for x in changes} == {"keep"}
    # A change in git: updated in place (same ids, so the stream keeps its offsets).
    sid, did = st["id"], d["id"]
    spec2 = b.load(write(tmp_path, BUNDLE.replace("batch_rows", "x").replace(
        "create_table: true}\n    derived", "create_table: true, batch_rows: 100}\n    derived").replace(
        "GROUP BY region", "WHERE amount > 0 GROUP BY region"), "v2.yaml"), {"TOPIC": "pg.public.customers"})
    changes, state = b.plan(c, spec2)
    upd = {(x.kind, x.key): x.diff for x in changes if x.action == "update"}
    assert set(upd) == {("streams", "customers-cdc"), ("derived", "revenue")}, upd
    assert upd[("streams", "customers-cdc")] == {"batch_rows": [5000, 100]}
    b.apply(c, spec2, changes, state, log=lambda _: None)
    assert s.streams.list()[0]["id"] == sid and s.streams.list()[0]["batch_rows"] == 100
    assert s.derived.list()[0]["id"] == did
    from cairn.pipelines.derived import derived_tables
    with s.engine.connect() as conn:                                   # the query changed: rebuilt next time
        assert conn.execute(derived_tables.select()).first().full_next is True
    # A pipe made by hand is never pruned; one the bundle dropped is.
    s.pipes.create({"name": "by-hand", "bucket": "landing", "prefix": "x/", "format": "json", "target": "raw.x"}, ADMIN)
    spec3 = b.load(write(tmp_path, BUNDLE.replace(
        "    pipes:\n      - {name: orders-files, bucket: landing, prefix: orders/, format: csv, target: raw.orders, "
        "create_table: true}\n", ""), "v3.yaml"), {"TOPIC": "pg.public.customers"})
    changes, state = b.plan(c, spec3)
    assert not [x for x in changes if x.action == "delete"]            # without --prune nothing goes
    changes, state = b.plan(c, spec3, prune=True)
    assert [(x.action, x.kind, x.key) for x in changes if x.action == "delete"] == [("delete", "pipes", "orders-files")]
    b.apply(c, spec3, changes, state, log=lambda _: None)
    assert {p["name"] for p in s.pipes.list()} == {"by-hand"}
    rec = admin.get("/v1/bundles/sales").json()
    assert rec["resources"]["pipes"] == [] and rec["resources"]["streams"] == ["customers-cdc"] and rec["version"] >= 5
    assert any(e["action"] == "bundle.applied" for e in s.audit.entries("iac", limit=50))


def test_a_stale_plan_is_refused_and_existing_resources_are_adopted(api, tmp_path):
    s, admin, _ = api
    c = b.Client("", "unused", http=admin)
    s.pipes.create({"name": "orders-files", "bucket": "landing", "prefix": "orders/", "format": "csv",
                    "target": "raw.orders", "create_table": True}, ADMIN)
    spec = b.load(write(tmp_path, """
        bundle: adopt
        pipes:
          - {name: orders-files, bucket: landing, prefix: orders/, format: csv, target: raw.orders, create_table: true}
    """))
    changes, state = b.plan(c, spec)
    assert [x.action for x in changes] == ["keep"]
    b.apply(c, spec, changes, state, log=lambda _: None)
    assert admin.get("/v1/bundles/adopt").json()["resources"]["pipes"] == ["orders-files"]   # now this bundle's
    # Two applies from one plan: the second is refused (plan again).
    with pytest.raises(b.BundleError, match="applied meanwhile"):
        b.apply(c, spec, changes, state, log=lambda _: None)


def test_files_are_checked_and_the_state_is_for_operators(api, tmp_path):
    _, admin, ana = api
    for text, msg in [("bundle: X\n", "bundle: a name"), ("bundle: a\nwidgets: []\n", "unknown sections"),
                      ("bundle: a\npipes:\n  - {name: p}\n", "needs bucket, format, target"),
                      ("bundle: a\npipes:\n  - {name: p, bucket: b, format: csv, target: a.b, colour: red}\n", "unknown fields"),
                      ("bundle: a\npipes:\n  - {name: p, bucket: bb, format: csv, target: a.b}\n"
                       "  - {name: p, bucket: bb, format: csv, target: a.b}\n", "declared twice"),
                      ("bundle: [\n", "")]:
        with pytest.raises(b.BundleError, match=msg):
            b.load(write(tmp_path, text, "bad.yaml"))
    assert ana.get("/v1/bundles/sales").status_code == 403
    assert ana.put("/v1/bundles/sales", json={"version": 0, "resources": {}}).status_code == 403
    assert admin.put("/v1/bundles/sales", json={"version": 0, "resources": {"widgets": ["x"]}}).status_code == 400
    # A bad resource stops the apply with Cairn's reason; what was done before it is recorded.
    c = b.Client("", "unused", http=admin)
    spec = b.load(write(tmp_path, """
        bundle: half
        pipes:
          - {name: good, bucket: landing, format: csv, target: raw.good}
        derived:
          - {name: bad, target: marts.bad, every_minutes: 5, sql: "DELETE FROM raw.good"}
    """, "half.yaml"))
    changes, state = b.plan(c, spec)
    with pytest.raises(b.BundleError, match="one SELECT"):
        b.apply(c, spec, changes, state, log=lambda _: None)
    assert admin.get("/v1/bundles/half").json()["resources"]["pipes"] == ["good"]


def test_the_cli(api, tmp_path, monkeypatch, capsys):
    import argparse

    path = write(tmp_path, "bundle: tiny\nmaintenance:\n  - {scope: raw, every_hours: 12}\n")
    args = argparse.Namespace(action="validate", file=path, var=None, url=None, token=None, prune=False,
                              detailed_exitcode=False, verbose=False)
    assert b.main(args) == 0 and "1 resources, valid" in capsys.readouterr().out
    args.action = "plan"
    monkeypatch.delenv("CAIRN_URL", raising=False)
    monkeypatch.delenv("CAIRN_TOKEN", raising=False)
    assert b.main(args) == 1 and "set CAIRN_URL" in capsys.readouterr().out


SQL_BUNDLE = """
    bundle: reports
    derived:
      - {name: revenue, target: marts.revenue, on_change: true, sql: "SELECT region, sum(amount) AS total FROM raw.orders GROUP BY region"}
    vectors:
      - {name: articles, source: support.articles, key: id, text: body, every_minutes: 60}
    queries:
      - {name: revenue-by-region, connection: lakehouse, sql: "SELECT * FROM marts.revenue", chart: {type: bar, x: region, y: [total]}}
      - {name: big-orders, connection: lakehouse, sql: "SELECT * FROM raw.orders WHERE amount > 100"}
    dashboards:
      - {name: sales, shared: true, tiles: [{query: revenue-by-region, width: 2}, {query: big-orders, title: Big}]}
    alerts:
      - {name: revenue-drop, query: revenue-by-region, measure: total, op: "<", value: 1000}
    workflows:
      - name: nightly
        cron: "0 2 * * *"
        steps:
          - {name: refresh, type: derived, table: revenue}
          - {name: not-empty, type: check, query: revenue-by-region, op: ">", value: 0, after: refresh}
"""


def test_saved_queries_dashboards_alerts_workflows_and_vector_indexes(api, tmp_path):
    s, admin, ana = api
    c = b.Client("", "unused", http=admin)
    spec = b.load(write(tmp_path, SQL_BUNDLE))
    changes, state = b.plan(c, spec)
    assert [(x.action, x.kind, x.key) for x in changes] == [
        ("create", "derived", "revenue"), ("create", "vectors", "articles"),
        ("create", "queries", "revenue-by-region"), ("create", "queries", "big-orders"),
        ("create", "dashboards", "sales"), ("create", "alerts", "revenue-drop"), ("create", "workflows", "nightly")]
    assert b.apply(c, spec, changes, state, log=lambda _: None)["changed"] == 7
    q = {x["name"]: x for x in s.sql_workspace.saved(ADMIN)}
    board = s.sql_boards.dashboards(ADMIN)[0]
    assert [t["query"] for t in board["tiles"]] == [q["revenue-by-region"]["id"], q["big-orders"]["id"]]   # names -> ids
    assert [t["title"] for t in board["tiles"]] == ["revenue-by-region", "Big"] and board["shared"]
    assert s.sql_boards.alerts(ADMIN)[0]["query"] == q["revenue-by-region"]["id"]
    wf = s.sql_workflows.list(ADMIN)[0]
    assert wf["steps"][1]["query"] == q["revenue-by-region"]["id"] and wf["steps"][1]["after"] == ["refresh"]
    assert s.vectors.get("articles")["every_minutes"] == 60
    # Cairn's ids, timestamps and states are not drift: applying again changes nothing.
    changes, state = b.plan(c, spec)
    assert {x.action for x in changes} == {"keep"}, [x.line() for x in changes if x.action != "keep"]
    # Someone else's shared dashboard of the same name is not this bundle's.
    ana.post("/sql/saved", json={"name": "revenue-by-region", "connection": "lakehouse", "sql": "SELECT 1", "shared": True})
    assert {x.action for x in b.plan(c, spec)[0]} == {"keep"}
    # Changes in git: updated in place, references followed.
    v2 = (SQL_BUNDLE.replace("{query: big-orders, title: Big}", "{query: big-orders, title: Large}")
          .replace('value: 1000}', 'value: 500}').replace("every_minutes: 60}", "every_minutes: 30}"))
    spec2 = b.load(write(tmp_path, v2, "v2.yaml"))
    changes, state = b.plan(c, spec2)
    upd = {(x.kind, x.key): x.diff for x in changes if x.action == "update"}
    assert set(upd) == {("dashboards", "sales"), ("alerts", "revenue-drop"), ("vectors", "articles")}, upd
    assert upd[("alerts", "revenue-drop")] == {"value": [1000.0, 500.0]}
    b.apply(c, spec2, changes, state, log=lambda _: None)
    assert s.sql_boards.dashboards(ADMIN)[0]["id"] == board["id"]
    assert s.sql_boards.dashboards(ADMIN)[0]["tiles"][1]["title"] == "Large"
    assert s.sql_boards.alerts(ADMIN)[0]["value"] == 500 and s.vectors.get("articles")["every_minutes"] == 30
    # A vector index keeps its source; a reference to a saved query nobody declared is refused at plan.
    with pytest.raises(b.BundleError, match="delete it to change the source"):
        b.plan(c, b.load(write(tmp_path, v2.replace("support.articles", "support.other"), "v3.yaml")))
    with pytest.raises(b.BundleError, match="no saved query of yours named 'nope'"):
        b.plan(c, b.load(write(tmp_path, v2.replace("query: revenue-by-region, measure", "query: nope, measure"), "v4.yaml")))
    # Pruned: the dashboard, alert and workflow go before the query they name.
    v5 = v2.split("    dashboards:")[0].replace(
        "      - {name: big-orders, connection: lakehouse, sql: \"SELECT * FROM raw.orders WHERE amount > 100\"}\n", "")
    changes, state = b.plan(c, b.load(write(tmp_path, v5, "v5.yaml")), prune=True)
    assert [(x.kind, x.key) for x in changes if x.action == "delete"] == [
        ("workflows", "nightly"), ("alerts", "revenue-drop"), ("dashboards", "sales"), ("queries", "big-orders")]
    b.apply(c, b.load(write(tmp_path, v5, "v5.yaml")), changes, state, log=lambda _: None)
    assert [x["name"] for x in s.sql_workspace.saved(ADMIN) if x["owner"] == ADMIN] == ["revenue-by-region"]
    assert not s.sql_boards.dashboards(ADMIN) and not s.sql_boards.alerts(ADMIN) and not s.sql_workflows.list(ADMIN)
    assert [x["name"] for x in s.sql_workspace.saved(ANA) if x["owner"] == ANA] == ["revenue-by-region"]   # ana's stays


ACCESS = """
    bundle: access
    policies:
      - id: analysts-read
        effect: allow
        actions: [VIEW_DATASET_PROFILE, USE_WAREHOUSE]
        priority: 10
        target: {resource_types: [dataset, warehouse]}
        condition: {attr: subject.groups, op: contains, value: analysts}
    connections:
      - name: finance-db
        connector: postgres
        values: {host_port: "finance-db:5432", database: finance, username: reader, password: FINANCE_PG_PASSWORD}
        schedule: {interval: "0 2 * * *", timezone: UTC}
"""


def test_policies_and_connections(api, tmp_path, graph):
    from test_sources import FakeCore

    core = FakeCore()
    graph.graphql_handler = core
    s, admin, _ = api
    s.pdp.set_bundle(PolicyBundle(policies=[*s.pdp.bundle.policies,
                                            Policy(id="persona-x", effect="allow", actions=["*"], source="persona")]))
    c = b.Client("", "unused", http=admin)
    spec = b.load(write(tmp_path, ACCESS))
    changes, state = b.plan(c, spec)
    assert [(x.action, x.kind, x.key) for x in changes] == [("create", "policies", "analysts-read"),
                                                             ("create", "connections", "finance-db")]
    b.apply(c, spec, changes, state, log=lambda _: None)
    pol = next(x for x in s.pdp.bundle.policies if x.id == "analysts-read")
    assert pol.priority == 10 and pol.condition["value"] == "analysts"
    assert [x.id for x in s.pdp.bundle.policies] == ["admin", "persona-x", "analysts-read"]   # the rest stays
    src = next(iter(core.sources.values()))
    recipe = json.loads(src["config"]["recipe"])
    assert recipe["source"]["config"]["password"] == "${FINANCE_PG_PASSWORD}"     # a secret by name, never a value
    assert src["schedule"]["interval"] == "0 2 * * *"
    assert {x.action for x in b.plan(c, spec)[0]} == {"keep"}                      # applied: no drift
    # Changed in git: updated in place. Removing a declared field (the condition) is a change too.
    changed = ACCESS.replace("priority: 10", "priority: 20").replace(
        "        condition: {attr: subject.groups, op: contains, value: analysts}\n", "").replace(
        "database: finance,", "database: finance_v2,")
    spec = b.load(write(tmp_path, changed))
    changes, state = b.plan(c, spec)
    assert {(x.kind, x.action): sorted(x.diff) for x in changes} == {
        ("policies", "update"): ["condition", "priority"], ("connections", "update"): ["values"]}
    owners_before = s.sources.get(src["urn"])["owners"]
    b.apply(c, spec, changes, state, log=lambda _: None)
    pol = next(x for x in s.pdp.bundle.policies if x.id == "analysts-read")
    assert (pol.priority, pol.condition) == (20, None)
    assert json.loads(core.sources[src["urn"]]["config"]["recipe"])["source"]["config"]["database"] == "finance_v2"
    assert s.sources.get(src["urn"])["owners"] == owners_before                    # owners kept when not declared
    # A persona's policy cannot be taken over by a bundle.
    with pytest.raises(b.BundleError, match="persona policy"):
        b.plan(c, b.load(write(tmp_path, "bundle: grab\npolicies:\n  - {id: persona-x, effect: deny, actions: ['*']}\n")))
    # An invalid policy is caught when the file is read, before Cairn is asked.
    with pytest.raises(b.BundleError, match="policies/bad"):
        b.load(write(tmp_path, "bundle: bad\npolicies:\n  - {id: bad, effect: maybe, actions: ['*']}\n"))
    # Pruned: both go, the policy last.
    gone = b.load(write(tmp_path, "bundle: access\n"))
    changes, state = b.plan(c, gone, prune=True)
    assert [(x.action, x.kind) for x in changes] == [("delete", "connections"), ("delete", "policies")]
    b.apply(c, gone, changes, state, log=lambda _: None)
    assert [x.id for x in s.pdp.bundle.policies] == ["admin", "persona-x"] and core.sources == {}


def test_a_literal_dollar_brace_survives(tmp_path):
    escaped = "$" + "${MYSQL_PASSWORD}"
    spec = b.load(write(tmp_path, f"""
        bundle: custom
        connections:
          - name: custom-src
            connector: custom
            recipe: {{source: {{type: mysql, config: {{password: "{escaped}", host_port: "${{HOST}}"}}}}}}
    """), {"HOST": "db:3306"})
    cfg = spec["connections"]["custom-src"]["recipe"]["source"]["config"]
    assert cfg == {"password": "${MYSQL_PASSWORD}", "host_port": "db:3306"}
