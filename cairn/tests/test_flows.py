"""Cairn Flows (L122): the engine on a real SQLite warehouse through the SQL workspace (each step as the flow's
owner, authorized and masked per table), its definition format, versions, permissions, approvals, concurrency,
the scheduler's single start with two engines on one database, the API, bundles and the MCP tools."""

from __future__ import annotations

import json
import textwrap
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, update

from cairn.common.core import InMemoryGraph
from cairn.common.db import get_engine, init_db
from cairn.coreproxy.masking import DatasetPlan
from cairn.dataaccess.service import DataAccessService
from cairn.flows import expr, model
from cairn.flows.engine import Flows, flow_runs, flow_step_runs, flows as flows_table
from cairn.flows.model import FlowError
from cairn.flows.steps import Backends
from cairn.pipelines.changes import DatasetChanges
from cairn.quality.collectors import ConnectionRegistry
from cairn.sqlworkspace import SqlWorkspace

USERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.users,PROD)"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
ANA, BOB, EVE, ADM = ("urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:eve", "urn:li:corpuser:adm")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_FLOWS_MIN_INTERVAL_MINUTES", "1")
    url = f"sqlite:///{tmp_path}/wh.db"
    e = create_engine(url)
    with e.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER, email TEXT, region TEXT)"))
        c.execute(text("CREATE TABLE orders (id INTEGER, user_id INTEGER, amount REAL)"))
        c.execute(text("INSERT INTO users VALUES (1, 'a@x.com', 'EU'), (2, 'b@y.com', 'US')"))
        c.execute(text("INSERT INTO orders VALUES (1, 1, 9.5), (2, 1, 3.0), (3, 2, 7.0)"))
    reg = ConnectionRegistry({"wh": {"url": url, "catalog": {"platform": "sqlite"}}})
    plans = {USERS: DatasetPlan(fields={"email"}, strategy="partial")}
    denied = {(EVE, ORDERS)}
    data = DataAccessService(reg, lambda actor, urn: plans.get(urn, DatasetPlan()),
                             lambda actor, urn: (actor, urn) not in denied, None, known=lambda urn: urn in (USERS, ORDERS))
    meta = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(meta)
    graph = InMemoryGraph()
    ws = SqlWorkspace(meta, data, graph)
    told: list[tuple] = []
    groups = {BOB: ["finance"]}
    lineage: list[dict] = []
    changes = DatasetChanges(meta)

    def make(instance=None):
        b = Backends(workspace=ws, notify=lambda *a: told.append(a), is_operator=lambda a: a == ADM)
        return Flows(meta, b, None, lambda *a: told.append(a), lambda a: a == ADM, lambda a: groups.get(a, []),
                     changes, None, lineage.append, None, instance)
    f = make("one")
    f.told, f.lineage_events, f.make, f.wh, f.changes = told, lineage, make, reg, changes
    return f


def wait_until(fn, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("timed out")


def steps_of(run):
    return {s["key"]: s for s in run["steps"]}


FLOW = textwrap.dedent("""
    kind: flow
    name: nightly
    params:
      - {name: region, type: string, default: EU}
      - {name: limit, type: int, default: 2}
    notify: {on: [failure, success], to: [urn:li:corpuser:bob]}
    steps:
      - {name: count, type: sql, sql: "SELECT count(*) AS n FROM main.orders", connection: wh}
      - name: enough
        type: check
        after: [count]
        sql: "SELECT count(*) AS n FROM main.orders"
        connection: wh
        measure: n
        op: ">="
        value: 3
      - {name: big, type: branch, after: [enough], condition: "steps.count.preview[0][0] >= params.limit", then: [report], else: [small]}
      - {name: report, type: sql, after: [big], sql: "SELECT id, email FROM main.users ORDER BY id", connection: wh}
      - {name: small, type: notify, after: [big], message: "only {{ steps.count.preview[0][0] }} orders"}
      - name: each
        type: for_each
        after: [report]
        items: "{{ steps.report.preview }}"
        concurrency: 2
        step: {type: notify, message: "user {{ item[0] }} in {{ params.region }}"}
      - {name: done, type: notify, after: [each, small], when: none_failed, message: "done: {{ steps.count.rows }}"}
    layout: {count: {x: 0, y: 0}, enough: {x: 260, y: 0}}
""")


def test_a_flow_runs_in_order_as_its_owner_with_branch_for_each_and_templates(env):
    f = env.create(ANA, {"yaml": FLOW}, publish=True)
    assert f["published_version"] == 1 and f["errors"] == [] and f["draft"]["layout"]["enough"] == {"x": 260.0, "y": 0.0}
    run = env.run(ANA, "nightly", {"region": "US"}, wait=True)
    assert run["status"] == "succeeded", run
    by = steps_of(run)
    assert by["count"]["output"]["preview"] == [[3]] and by["enough"]["output"]["value"] == 3.0
    assert by["big"]["output"] == {"result": True, "chosen": "then"}
    assert by["small"]["status"] == "skipped" and "not chosen by big" in by["small"]["error"]
    # As its owner: her mask applies to what the report keeps.
    assert by["report"]["output"]["preview"][0][1] != "a@x.com" and by["report"]["output"]["rows"] == 2
    assert [i["status"] for i in by["each"]["items"]] == ["ok", "ok"] and by["each"]["output"]["items"] == 2
    assert by["done"]["status"] == "ok"
    order = sorted(run["steps"], key=lambda s: s["started_ms"] or 0)
    names = [s["key"] for s in order if s["started_ms"]]
    assert names.index("count") < names.index("enough") < names.index("big") < names.index("report") < names.index("each")
    notices = [t for t in env.told if t[0] == "flow.notice"]
    assert {t[2] for t in notices} >= {"user 1 in US", "user 2 in US", "done: 1"}
    assert env.told[-1][0] == "flow.succeeded" and env.told[-1][3] == [ANA, BOB]
    # The run's steps, as the API shows them, and a step's log.
    detail = env.run_detail(ANA, run["id"])
    assert detail["definition"]["name"] == "nightly" and detail["params"] == {"region": "US", "limit": 2}
    assert "items succeeded" in env.step_log(ANA, run["id"], "each")["log"]


def test_a_failing_check_stops_what_depends_on_it_retries_and_tells_the_owner(env):
    env.create(ANA, {"yaml": FLOW}, publish=True)
    with env.wh.engine("wh").begin() as c:
        c.execute(text("DELETE FROM orders WHERE id = 3"))
    d = env.get(ANA, "nightly")["draft"]
    d["steps"][1].update(retries=1, retry_delay_s=0)
    env.save(ANA, "nightly", d, publish=True)
    run = env.run(ANA, "nightly", wait=True)
    by = steps_of(run)
    assert run["status"] == "failed" and by["enough"]["status"] == "failed" and by["enough"]["attempt"] == 2
    assert "check failed: n is 2, not >= 3" in by["enough"]["error"]
    assert by["big"]["status"] == "skipped" and by["report"]["status"] == "skipped"
    assert by["done"]["status"] == "ok"                 # none_failed: what it waits on was skipped, none failed
    failed = [t for t in env.told if t[0] == "flow.failed"]
    assert failed and failed[-1][3] == [ANA, BOB] and "enough: failed" in failed[-1][2]
    # Rerun from the check once the data is right: count keeps its output, the rest runs again.
    with env.wh.engine("wh").begin() as c:
        c.execute(text("INSERT INTO orders VALUES (3, 2, 7.0)"))
    again = env.rerun(ANA, run["id"], "enough")
    again = env.wait_for(again["id"])
    by = steps_of(again)
    assert again["status"] == "succeeded" and by["count"]["reused"] and not by["enough"]["reused"]
    assert again["detail"]["rerun_of"] == run["id"] and again["version"] == run["version"]
    with pytest.raises(FlowError, match="no step"):
        env.rerun(ANA, run["id"], "nope")


def test_each_step_runs_as_the_owner_and_people_see_only_what_they_may(env):
    env.create(EVE, {"name": "eves", "steps": [
        {"name": "read", "type": "sql", "sql": "SELECT count(*) AS n FROM main.orders", "connection": "wh"}]},
        publish=True)
    run = env.run(EVE, "eves", wait=True)
    assert run["status"] == "failed" and "may not query main.orders" in steps_of(run)["read"]["error"]
    # An editor runs it, but as its owner: still refused. Viewers see it; others do not even see it.
    env.share(EVE, "eves", [ANA], [BOB])
    run = env.run(ANA, "eves", wait=True)
    assert run["status"] == "failed" and run["run_as"] == EVE and run["triggered_by"] == ANA
    with pytest.raises(FlowError) as e:
        env.publish(ANA, "eves")                           # only the owner (or an admin) publishes
    assert e.value.status == 403
    assert env.get(BOB, "eves")["role"] == "viewer"
    with pytest.raises(FlowError) as e:
        env.run(BOB, "eves")
    assert e.value.status == 403
    with pytest.raises(FlowError) as e:
        env.get("urn:li:corpuser:zed", "eves")
    assert e.value.status == 404
    assert [x["name"] for x in env.list("urn:li:corpuser:zed")] == []
    assert env.get(ADM, "eves")["role"] == "admin"
    # Groups count: bob's group may edit.
    env.share(EVE, "eves", ["urn:li:corpGroup:finance"], [])
    assert env.get(BOB, "eves")["role"] == "editor"


def test_a_suspended_owner_runs_nothing(env):
    f = env.make("two")
    f.may_run = lambda owner: "suspended" if owner == ANA else None
    f.create(ANA, {"name": "x", "steps": [{"name": "n", "type": "notify", "message": "hi"}]}, publish=True)
    run = f.run(ANA, "x", wait=True)
    assert run["status"] == "failed" and "not run: suspended" in run["error"] and steps_of(run)["n"]["status"] == "pending"
    f.may_run = lambda owner: 1 / 0                      # cannot tell: refused
    assert f.run(ANA, "x", wait=True)["status"] == "failed"


def test_approvals_wait_for_the_named_people_or_their_group(env):
    env.create(ANA, {"name": "gate", "steps": [
        {"name": "ask", "type": "approval", "approvers": [EVE], "groups": ["finance"], "message": "ship it?"},
        {"name": "after", "type": "notify", "after": ["ask"], "message": "approved by {{ steps.ask.by }}"}]}, publish=True)
    run = env.run(ANA, "gate")
    wait_until(lambda: env._run_public(run["id"])["status"] == "waiting")
    assert any(t[0] == "flow.approval_requested" and t[3] == [EVE] for t in env.told)
    assert [a["step"] for a in env.pending_approvals(BOB)] == ["ask"]   # through his group
    assert env.pending_approvals(ANA) == []
    with pytest.raises(FlowError) as e:
        env.decide(ANA, run["id"], "ask", "approve")
    assert e.value.status == 403
    env.decide(BOB, run["id"], "ask", "approve", "looks right")
    done = env.wait_for(run["id"])
    assert done["status"] == "succeeded" and steps_of(done)["ask"]["output"]["by"] == BOB
    assert any(t[0] == "flow.notice" and t[2] == f"approved by {BOB}" for t in env.told)
    with pytest.raises(FlowError) as e:
        env.decide(EVE, run["id"], "ask", "approve")       # decided once
    assert e.value.status == 409
    run = env.run(ANA, "gate")
    wait_until(lambda: env._run_public(run["id"])["status"] == "waiting")
    env.decide(EVE, run["id"], "ask", "reject", "not now")
    done = env.wait_for(run["id"])
    assert done["status"] == "failed" and "rejected by urn:li:corpuser:eve: not now" in steps_of(done)["ask"]["error"]
    assert steps_of(done)["after"]["status"] == "skipped"


def test_cancel_and_timeouts(env):
    env.create(ANA, {"name": "slow", "steps": [{"name": "nap", "type": "wait", "seconds": 60},
                                                {"name": "next", "type": "notify", "after": ["nap"], "message": "x"}]},
               publish=True)
    run = env.run(ANA, "slow")
    wait_until(lambda: env._run_public(run["id"])["status"] == "waiting")
    env.cancel(ANA, run["id"])
    done = env.wait_for(run["id"], 20)
    assert done["status"] == "cancelled" and {s["status"] for s in done["steps"]} == {"cancelled"}
    with pytest.raises(FlowError):
        env.cancel(ANA, run["id"])
    # An approval nobody decides in time fails.
    env.create(ANA, {"name": "late", "steps": [{"name": "ask", "type": "approval", "approvers": [BOB], "timeout_s": 1}]},
               publish=True)
    run = env.run(ANA, "late")
    done = env.wait_for(run["id"], 20)
    assert done["status"] == "failed" and "no decision within" in steps_of(done)["ask"]["error"]


def test_concurrency_skip_queue_and_allow(env):
    body = {"name": "busy", "concurrency": {"policy": "skip"}, "steps": [{"name": "nap", "type": "wait", "seconds": 60}]}
    env.create(ANA, body, publish=True)
    first = env.run(ANA, "busy")
    second = env.run(ANA, "busy")
    assert second["status"] == "skipped" and "in progress" in second["error"]
    env.cancel(ANA, first["id"])
    env.wait_for(first["id"])
    env.save(ANA, "busy", {**body, "concurrency": {"policy": "queue"}}, publish=True)
    first, second = env.run(ANA, "busy"), env.run(ANA, "busy")
    assert second["status"] == "queued"
    env.cancel(ANA, first["id"])
    wait_until(lambda: env._run_public(second["id"])["status"] in ("running", "waiting"))
    env.cancel(ANA, second["id"])
    env.wait_for(second["id"])
    env.save(ANA, "busy", {**body, "concurrency": {"policy": "allow", "max_active": 2}}, publish=True)
    runs = [env.run(ANA, "busy") for _ in range(3)]
    wait_until(lambda: [env._run_public(r["id"])["status"] for r in runs][:2] == ["waiting", "waiting"])
    assert env._run_public(runs[2]["id"])["status"] == "queued"
    for r in runs:
        env.cancel(ANA, r["id"])


def test_the_scheduler_starts_each_run_once_with_two_engines_on_one_database(env):
    other = env.make("two")
    env.create(ANA, {"name": "tick", "triggers": [{"type": "schedule", "cron": "*/5 * * * *", "timezone": "Europe/Berlin"},
                                                    {"type": "dataset", "tables": ["sales.orders"]}],
                     "steps": [{"name": "n", "type": "notify", "message": "{{ run.trigger }} {{ run.logical_date }}"}]},
               publish=True)
    row = env._row("tick")
    assert row["next_run_ms"] and row["watch"] == ["urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"]
    with env.engine.begin() as c:
        c.execute(update(flows_table).values(next_run_ms=int(time.time() * 1000) - 1000))
    barrier = threading.Barrier(2)
    counts = []

    def tick(f):
        barrier.wait()
        counts.append(f.run_due())
    ts = [threading.Thread(target=tick, args=(f,)) for f in (env, other)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(counts) == [0, 1]
    runs = env.runs(ANA, "tick")
    assert len(runs) == 1 and runs[0]["trigger"] == "schedule"
    assert env._row("tick")["next_run_ms"] > time.time() * 1000
    # A table it watches changes: one run, whichever engine ticks; its own lineage does not start it again.
    env.changes.record(["urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"], "openlineage", "trino:job")
    counts.clear()
    barrier = threading.Barrier(2)
    ts = [threading.Thread(target=tick, args=(f,)) for f in (env, other)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(counts) == [0, 1]
    runs = env.runs(ANA, "tick")
    assert [r["trigger"] for r in runs].count("dataset") == 1
    env.changes.record(["urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"], "openlineage",
                       "urn:li:dataJob:(urn:li:dataFlow:(cairn-flows,tick,PROD),n)")
    assert env.run_due() == 0 and other.run_due() == 0
    for r in env.runs(ANA, "tick"):
        env.wait_for(r["id"])
    assert all(r["status"] == "succeeded" for r in env.runs(ANA, "tick"))


def test_the_scheduler_and_leases_on_postgres():
    """The same single start, admission and approval on PostgreSQL (row locks, not SQLite's single writer)."""
    import os

    url = os.environ.get("CAIRN_TEST_PG_URL")
    if not url:
        pytest.skip("CAIRN_TEST_PG_URL (a PostgreSQL to use) not set")
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    init_db(engine)
    told: list = []
    make = lambda i: Flows(engine, Backends(notify=lambda *a: told.append(a)), None, lambda *a: told.append(a),  # noqa: E731
                           instance=i)
    one, two = make("one"), make("two")
    one.create(ANA, {"name": "pg", "concurrency": {"policy": "allow", "max_active": 2},
                     "triggers": [{"type": "schedule", "cron": "*/5 * * * *", "timezone": "UTC"}],
                     "steps": [{"name": "ask", "type": "approval", "approvers": [BOB]},
                               {"name": "n", "type": "notify", "after": ["ask"], "message": "{{ steps.ask.by }}"}]},
               publish=True)
    with engine.begin() as c:
        c.execute(update(flows_table).values(next_run_ms=int(time.time() * 1000) - 1000))
    barrier, counts = threading.Barrier(2), []

    def tick(f):
        barrier.wait()
        counts.append(f.run_due())
    ts = [threading.Thread(target=tick, args=(f,)) for f in (one, two)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(counts) == [0, 1]
    runs = [one.run(ANA, "pg") for _ in range(2)]
    rid = one.runs(ANA, "pg")[-1]["id"]
    wait_until(lambda: one._run_public(rid)["status"] == "waiting")
    assert [one._run_public(r["id"])["status"] for r in runs].count("queued") == 1    # max_active 2
    two.decide(BOB, rid, "ask", "approve")
    assert two.wait_for(rid, 30)["status"] == "succeeded"
    for r in one.runs(ANA, "pg"):
        if r["status"] in ("queued", "running", "waiting"):
            one.cancel(ANA, r["id"])
    engine.dispose()


def test_a_run_whose_driver_died_is_taken_over(env):
    env.create(ANA, {"name": "lost", "steps": [{"name": "a", "type": "notify", "message": "a", "retries": 1, "retry_delay_s": 0},
                                                {"name": "b", "type": "notify", "after": ["a"], "message": "b"}]}, publish=True)
    row = env._row("lost")
    rid = env._new_run(row, 1, env._version(row["id"], 1), "manual", ANA, None)
    with env.engine.begin() as c:            # as if another replica was running step a when it died
        c.execute(update(flow_runs).where(flow_runs.c.id == rid).values(
            status="running", started_ms=1, lease_owner="dead", lease_until_ms=1))
        c.execute(update(flow_step_runs).where(flow_step_runs.c.run_id == rid, flow_step_runs.c.key == "a")
                  .values(status="running", attempt=1, started_ms=1))
    other = env.make("two")
    other.run_due()
    done = other.wait_for(rid)
    by = steps_of(done)
    assert done["status"] == "succeeded" and by["a"]["attempt"] == 2 and by["b"]["status"] == "ok"


def test_versions_drafts_and_diff(env):
    f = env.create(ANA, {"yaml": FLOW})
    assert f["published_version"] is None
    with pytest.raises(FlowError) as e:
        env.run(ANA, "nightly")
    assert e.value.status == 409
    assert env.run(ANA, "nightly", version="draft", wait=True)["version"] == 0
    env.publish(ANA, "nightly", "first")
    d = env.get(ANA, "nightly")["draft"]
    d["steps"] = [s for s in d["steps"] if s["name"] != "small"]
    d["steps"][-1]["after"] = ["each"]
    d["steps"][0]["limit"] = 10
    d["steps"].append({"name": "extra", "type": "notify", "after": ["done"], "message": "x"})
    d["steps"][2]["else"] = []
    saved = env.save(ANA, "nightly", d)
    assert saved["published_version"] == 1 and saved["errors"] == []
    ch = env.diff(ANA, "nightly")
    assert ch["added"] == ["extra"] and ch["removed"] == ["small"]
    assert {c["name"]: c["fields"] for c in ch["changed"]} == {"count": ["limit"], "big": ["else"], "done": ["after"]}
    env.publish(ANA, "nightly")
    assert [v["version"] for v in env.versions(ANA, "nightly")] == [2, 1]
    assert env.version(ANA, "nightly", 1)["definition"]["steps"][4]["name"] == "small"
    assert env.diff(ANA, "nightly", 1, "2")["removed"] == ["small"]
    run = env.run(ANA, "nightly", version=1, wait=True)
    assert run["version"] == 1 and "small" in steps_of(run)
    # A draft may be saved invalid; it is not published until it is valid.
    d["steps"].append({"name": "bad", "type": "teleport"})
    saved = env.save(ANA, "nightly", d)
    assert saved["draft_errors"] == 1 and saved["errors"][0]["path"] == "steps[7].type"
    with pytest.raises(FlowError, match="unknown step type"):
        env.publish(ANA, "nightly")
    with pytest.raises(FlowError, match="name does not change"):
        env.save(ANA, "nightly", {**d, "name": "other"})


def test_definitions_are_checked_with_precise_errors():
    def errors(raw):
        return [(e["path"], e["message"]) for e in model.validate(raw)["errors"]]
    good = {"name": "a", "type": "notify", "message": "x"}
    assert errors({"name": "f", "steps": [good, {**good, "name": "b", "after": ["a", "zz"]}]}) == \
        [("steps[1].after", "no step 'zz' to come after")]
    assert errors({"name": "f", "steps": [{**good, "after": ["b"]}, {**good, "name": "b", "after": ["a"]}]}) == \
        [("steps", "steps depend on each other in a cycle: a -> b -> a")]
    assert errors({"name": "f", "steps": [{"name": "a", "type": "teleport"}]})[0][0] == "steps[0].type"
    assert errors({"name": "f", "steps": [good, {**good, "name": "b", "message": "{{ steps.c.rows }}"},
                                           {**good, "name": "c"}]}) == \
        [("steps[1]", "reads steps.c, which does not come before it (add c to its after)")]
    assert errors({"name": "f", "steps": [{"name": "a", "type": "branch", "condition": "1 > 0", "then": ["b"]},
                                           {**good, "name": "b"}]}) == \
        [("steps[0].then", "b is on the then side, so it comes after a (add a to its after)")]
    assert "is not allowed" in errors({"name": "f", "steps": [{"name": "a", "type": "branch", "condition": "lambda: 1",
                                                                "then": []}]})[0][1]
    assert errors({"name": "f", "steps": [{**good, "colour": "red"}]})[0][0] == "steps[0].colour"
    assert errors({"name": "f", "steps": [{"name": "q", "type": "sql", "sql": "SELECT 1"}]})[0][1].startswith("needs a saved query")
    assert errors({"name": "Bad Name", "steps": [good]})[0][0] == "name"
    assert errors({"name": "f", "triggers": [{"type": "schedule", "cron": "nope"}], "steps": [good]})[0][0] == "triggers[0].cron"
    assert errors({"name": "f", "triggers": [{"type": "flow", "flow": "f"}], "steps": [good]})[0][1] == \
        "a flow cannot start after itself"
    assert errors({"name": "f", "steps": [{"name": "w", "type": "wait"}]})[0][1].startswith("needs either seconds")
    with pytest.raises(FlowError) as e:
        model.normalize("name: f\nsteps: [\n")
    assert "line" in str(e.value)


def test_yaml_round_trips_with_the_layout(env):
    d = model.normalize(FLOW)
    text_ = model.to_yaml(d)
    assert "retries" not in text_ and "after: [count]" in text_ and "layout:" in text_
    assert model.normalize(text_) == d
    assert model.normalize(model.to_yaml(d, full=True)) == d
    assert model.normalize(json.dumps(model.compact(d))) == d
    assert expr.render("{{ params.n * 2 }}", {"params": {"n": 21}}) == 42
    steps = {"steps": {"full-report": {"rows": 2}, "a": {"rows": 5}}}
    assert expr.evaluate("steps.full-report.rows + 1", steps) == 3 and expr.evaluate("steps.a.rows - 1", steps) == 4
    assert expr.references("steps.full-report.rows > 'steps.x-y'") == {"full-report"}
    with pytest.raises(expr.ExprError):
        expr.evaluate("params.__class__", {"params": {}})


def test_backfill_runs_one_per_day_in_order(env):
    env.create(ANA, {"name": "daily", "params": [{"name": "day", "type": "date"}],
                     "steps": [{"name": "n", "type": "notify", "message": "{{ params.day }}/{{ run.logical_date }}"}]},
               publish=True)
    ids = env.backfill(ANA, "daily", "2026-01-01", "2026-01-03", "day")
    assert len(ids) == 3
    for rid in ids:
        assert env.wait_for(rid)["status"] == "succeeded"
    got = sorted(t[2] for t in env.told if t[0] == "flow.notice")
    assert got == ["2026-01-01/2026-01-01", "2026-01-02/2026-01-02", "2026-01-03/2026-01-03"]
    with pytest.raises(FlowError):
        env.backfill(ANA, "daily", "2026-01-03", "2026-01-01")


def test_a_flow_starts_after_another_and_late_runs_are_told(env):
    env.create(ANA, {"name": "first", "sla_minutes": 1, "steps": [{"name": "n", "type": "notify", "message": "1"}]},
               publish=True)
    env.create(ANA, {"name": "second", "triggers": [{"type": "flow", "flow": "first"}],
                     "steps": [{"name": "n", "type": "notify", "message": "2"}]}, publish=True)
    env.run(ANA, "first", wait=True)
    run = wait_until(lambda: env.runs(ANA, "second"))[0]
    assert run["trigger"] == "flow" and env.wait_for(run["id"])["status"] == "succeeded"
    # SLA: a run still going after sla_minutes is told once.
    env.save(ANA, "first", {"name": "first", "sla_minutes": 1, "steps": [{"name": "w", "type": "wait", "seconds": 600}]},
             publish=True)
    r = env.run(ANA, "first")
    later = int(time.time() * 1000) + 120_000
    env._slas(later)
    env._slas(later)
    assert [t[0] for t in env.told].count("flow.sla_missed") == 1
    env.cancel(ANA, r["id"])


def test_lineage_for_steps_that_read_and_write(env):
    env.create(ANA, {"name": "lin", "steps": [{"name": "n", "type": "notify", "message": "x", "reads": ["sales.orders"],
                                               "writes": ["marts.revenue"]}]}, publish=True)
    env.run(ANA, "lin", wait=True)
    start, end = env.lineage_events[-2:]
    assert start["eventType"] == "START" and end["eventType"] == "COMPLETE"
    assert end["job"] == {"namespace": "cairn-flows://lin", "name": "n"}
    assert end["inputs"] == [{"namespace": "iceberg://", "name": "sales.orders"}]
    assert end["outputs"] == [{"namespace": "iceberg://", "name": "marts.revenue"}]
    from cairn.sources.openlineage import dataset_urn, job_urns

    assert dataset_urn("iceberg://", "marts.revenue") == "urn:li:dataset:(urn:li:dataPlatform:iceberg,marts.revenue,PROD)"
    assert job_urns("cairn-flows://lin", "n")[2] == "urn:li:dataJob:(urn:li:dataFlow:(cairn-flows,lin,PROD),n)"
    # A dataset trigger on a table the flow writes would start itself: refused when publishing.
    with pytest.raises(FlowError, match="would start itself"):
        env.create(ANA, {"name": "loop", "triggers": [{"type": "dataset", "tables": ["marts.revenue"]}],
                         "steps": [{"name": "n", "type": "notify", "message": "x", "writes": ["marts.revenue"]}]},
                   publish=True)


def test_from_a_sql_workflow(env):
    wf = {"name": "Nightly Report", "cron": "0 2 * * *", "notify": "always", "recipients": [BOB], "steps": [
        {"name": "refresh", "type": "derived", "table": "rev", "full": False, "after": [], "retries": 0},
        {"name": "ok", "type": "check", "query": "q1", "measure": "n", "op": ">", "value": 0.0, "after": ["refresh"],
         "retries": 1},
        {"name": "report", "type": "query", "sql": "SELECT 1", "connection": "wh", "after": ["ok"], "retries": 0}]}
    d = model.normalize(Flows.from_sql_workflow(wf))
    assert d["name"] == "nightly-report" and [s["type"] for s in d["steps"]] == ["derived", "check", "sql"]
    assert d["triggers"][0]["cron"] == "0 2 * * *" and d["notify"] == {"on": ["failure", "success"], "to": [BOB]}


# ---- the API, bundles and MCP, on the real app ------------------------------------------------------------
@pytest.fixture
def api(settings, graph, tmp_path, monkeypatch):
    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    wh = tmp_path / "wh.db"
    with create_engine(f"sqlite:///{wh}").begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, region TEXT, amount REAL)"))
        c.execute(text("INSERT INTO orders VALUES (1, 'EU', 9.5), (2, 'US', 3.0), (3, 'EU', 7.0)"))
    conns = tmp_path / "connections.yaml"
    conns.write_text(f"connections:\n  wh:\n    url: sqlite:///{wh}\n    catalog:\n      platform: sqlite\n")
    monkeypatch.setenv("CAIRN_QUALITY_CONNECTIONS", str(conns))
    ds = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
    graph.aspects[(ds, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in ("id", "region", "amount")]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADM}),
        Policy(id="read", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"])]))
    app = create_api_app(s)
    s.adm_client = TestClient(app, headers={"X-Cairn-Actor": ADM})
    return s, TestClient(app, headers={"X-Cairn-Actor": ANA}), TestClient(app, headers={"X-Cairn-Actor": BOB})


def test_the_api(api):
    s, ana, bob = api
    assert s.flows is not None
    yaml_ = "name: report\nsteps:\n  - {name: q, type: sql, sql: 'SELECT count(*) AS n FROM main.orders', connection: wh}\n" \
            "  - {name: gate, type: approval, after: [q], approvers: [urn:li:corpuser:bob]}\n" \
            "  - {name: tell, type: notify, after: [gate], message: 'n={{ steps.q.preview[0][0] }}'}\n"
    r = ana.post("/v1/flows/validate", json={"yaml": yaml_.replace("type: sql", "type: sequel")})
    assert r.status_code == 200 and not r.json()["valid"] and r.json()["errors"][0]["path"] == "steps[0].type"
    assert ana.post("/v1/flows/validate", json={"yaml": yaml_}).json()["yaml"].startswith("kind: flow")
    r = ana.post("/v1/flows?publish=true", json={"yaml": yaml_})
    assert r.status_code == 201, r.text
    assert ana.post("/v1/flows", json={"yaml": yaml_}).status_code == 409
    assert [t["type"] for t in ana.get("/v1/flows/step-types").json()][:3] == ["sql", "derived", "check"]
    r = ana.post("/v1/flows/report/runs", json={})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    wait_until(lambda: ana.get(f"/v1/flows/runs/{rid}").json()["status"] == "waiting")
    assert [a["step"] for a in bob.get("/v1/flows/approvals").json()] == ["gate"]
    assert ana.post(f"/v1/flows/runs/{rid}/steps/gate/approve", json={}).status_code == 403
    assert bob.get(f"/v1/flows/runs/{rid}").status_code == 404           # an approver, not a viewer
    r = bob.post(f"/v1/flows/runs/{rid}/steps/gate/approve", json={"comment": "ok"})
    assert r.status_code == 200, r.text
    run = wait_until(lambda: (lambda x: x if x["status"] == "succeeded" else None)(ana.get(f"/v1/flows/runs/{rid}").json()))
    assert steps_of(run)["q"]["output"]["preview"] == [[3]]
    assert ana.get(f"/v1/flows/runs/{rid}/steps/gate/log").json()["output"]["comment"] == "ok"
    assert ana.get("/v1/flows/report/yaml").text.startswith("kind: flow")
    assert ana.get("/v1/flows/report/runs").json()[0]["status"] == "succeeded"
    assert ana.get("/v1/flows/report/diff").json() == {"added": [], "removed": [], "changed": [], "settings": []}
    assert bob.put("/v1/flows/report", json={"yaml": yaml_}).status_code == 404
    assert ana.put("/v1/flows/report/sharing", json={"editors": [BOB], "viewers": []}).status_code == 200
    assert bob.post("/v1/flows/report/publish", json={}).status_code == 403
    assert [x["name"] for x in bob.get("/v1/flows").json()] == ["report"]
    r = bob.post("/v1/flows/report/backfill", json={"start": "2026-01-01", "end": "2026-01-02"})
    assert r.status_code == 201 and len(r.json()["runs"]) == 2
    assert bob.delete("/v1/flows/report").status_code == 403
    actions = {e["action"] for e in s.audit.entries("flows", 500)}
    assert {"flow.created", "flow.published", "flow.run_started", "flow.approval_approved", "flow.run_succeeded",
            "flow.shared", "flow.backfill"} <= actions, actions


def test_bundles_plan_apply_and_drift(api, tmp_path):
    from cairn.iac import bundle as b

    s, _, _ = api
    c = b.Client("", "unused", http=s.adm_client)
    path = tmp_path / "cairn.yaml"
    path.write_text(textwrap.dedent("""
        bundle: ops
        flows:
          - name: nightly
            spec:
              triggers: [{type: schedule, cron: "15 2 * * *", timezone: UTC}]
              steps:
                - {name: q, type: sql, sql: "SELECT count(*) AS n FROM main.orders", connection: wh}
                - {name: tell, type: notify, after: [q], message: "{{ steps.q.rows }} rows"}
    """))
    spec = b.load(str(path))
    changes, state = b.plan(c, spec)
    assert [(x.action, x.kind, x.key) for x in changes] == [("create", "flows", "nightly")]
    b.apply(c, spec, changes, state, log=lambda line: None)
    f = s.flows.get(ADM, "nightly")
    assert f["published_version"] == 1 and f["next_run_ms"]
    changes, _ = b.plan(c, spec)
    assert [x.action for x in changes] == ["keep"]
    # Someone changes it by hand: drift, and apply puts the declared version back as version 3.
    d = f["draft"]
    d["steps"][1]["message"] = "changed"
    s.flows.save(ADM, "nightly", d, publish=True)
    changes, state = b.plan(c, spec)
    assert changes[0].action == "update" and "spec" in changes[0].diff
    b.apply(c, spec, changes, state, log=lambda line: None)
    assert s.flows.get(ADM, "nightly")["published_version"] == 3
    assert s.flows.get(ADM, "nightly")["published"]["steps"][1]["message"] == "{{ steps.q.rows }} rows"
    # Turned off by the bundle: kept as declared.
    spec["flows"]["nightly"]["enabled"] = False
    changes, state = b.plan(c, spec)
    assert changes[0].action == "update" and "enabled" in changes[0].diff
    b.apply(c, spec, changes, state, log=lambda line: None)
    assert s.flows.get(ADM, "nightly")["enabled"] is False and [x.action for x in b.plan(c, spec)[0]] == ["keep"]
    bad = tmp_path / "bad.yaml"
    bad.write_text("bundle: ops\nflows:\n  - {name: x, spec: {steps: [{name: a, type: nope}]}}\n")
    with pytest.raises(b.BundleError, match="steps\\[0\\].type"):
        b.load(str(bad))
    path.write_text("bundle: ops\n")
    changes, state = b.plan(c, b.load(str(path)), prune=True)
    assert [(x.action, x.key) for x in changes] == [("delete", "nightly")]
    b.apply(c, b.load(str(path)), changes, state, log=lambda line: None)
    assert s.flows.list(ADM) == []


def test_the_mcp_tools_run_as_the_caller(api):
    from cairn.ai.mcp_server import CairnTools, _principal
    from cairn.common.security import Principal
    from cairn.services import Forbidden

    s, _, _ = api
    s.flows.create(ANA, {"name": "mine", "steps": [{"name": "n", "type": "notify", "message": "hi"}]}, publish=True)
    tools = CairnTools(s)
    token = _principal.set(Principal(urn=ANA, via="token"))
    try:
        assert [f["name"] for f in tools.list_flows()["flows"]] == ["mine"]
        run = tools.run_flow("mine")
        done = s.flows.wait_for(run["run"])
        assert done["status"] == "succeeded" and done["trigger"] == "api"
        assert tools.get_flow_run(run["run"])["steps"][0]["status"] == "ok"
    finally:
        _principal.reset(token)
    token = _principal.set(Principal(urn=BOB, via="agent", agent="a1", scopes=("read", "write")))
    try:
        assert tools.list_flows()["flows"] == []
        with pytest.raises(Forbidden):
            tools.run_flow("mine")
        with pytest.raises(Forbidden):
            tools.get_flow_run(run["run"])
    finally:
        _principal.reset(token)
    from cairn.identity.agents import tool_scope

    assert tool_scope("run_flow") == "write" and tool_scope("list_flows") == "read"


def test_the_cli_validates_here_and_needs_cairn_for_the_rest(tmp_path, monkeypatch, capsys):
    import argparse

    from cairn.flows import cli

    good, bad = tmp_path / "good.yaml", tmp_path / "bad.yaml"
    good.write_text(FLOW)
    bad.write_text("name: x\nsteps:\n  - {name: a, type: notify, message: hi, after: [zz]}\n")
    assert cli.main(argparse.Namespace(flows_cmd="validate", files=[str(good)])) == 0
    assert "flow nightly, 7 steps, valid" in capsys.readouterr().out
    assert cli.main(argparse.Namespace(flows_cmd="validate", files=[str(bad)])) == 1
    assert "steps[0].after: no step 'zz' to come after" in capsys.readouterr().out
    for v in ("CAIRN_URL", "CAIRN_TOKEN", "CAIRN_TOKEN_FILE"):
        monkeypatch.delenv(v, raising=False)
    assert cli.main(argparse.Namespace(flows_cmd="list")) == 1
    assert "set CAIRN_URL" in capsys.readouterr().err
