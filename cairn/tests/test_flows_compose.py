"""Flows that compose (L173), on the real engine and a real SQLite warehouse through the SQL workspace: a step runs
another flow and reads its steps' outputs; a for-each maps over a sub-flow and collects each run's outputs; a failed
child fails the step and says where; cancelling the parent cancels the child; a flow cannot reach itself through
sub-flows, nor run one its owner may not; and the steps that move data are refused where their service or the owner's
right is missing."""

from __future__ import annotations

import textwrap

import pytest

from cairn.flows.model import FlowError
from test_flows import ADM, ANA, EVE, env, steps_of, wait_until  # noqa: F401  (fixture)

CHILD = textwrap.dedent("""
    kind: flow
    name: orders-over
    params:
      - {name: floor, type: float, default: 0}
    steps:
      - {name: count, type: sql, sql: "SELECT count(*) AS n FROM main.orders WHERE amount > {{ params.floor }}", connection: wh}
      - {name: enough, type: check, after: [count], sql: "SELECT count(*) AS n FROM main.orders WHERE amount > {{ params.floor }}",
         connection: wh, measure: n, op: ">", value: 0}
""")
PARENT = textwrap.dedent("""
    kind: flow
    name: nightly-all
    steps:
      - {name: big, type: flow, flow: orders-over, params: {floor: 5}}
      - {name: say, type: notify, after: [big], message: "{{ steps.big.steps.count.preview[0][0] }} orders over 5 ({{ steps.big.run }})"}
      - name: each
        type: for_each
        after: [say]
        items: [0, 5, 8]
        concurrency: 3
        step: {type: flow, flow: orders-over, params: {floor: "{{ item }}"}}
""")


def test_a_flow_runs_another_and_maps_over_it(env):  # noqa: F811
    f = env
    f.create(ANA, CHILD, publish=True)
    f.create(ANA, PARENT, publish=True)
    run = f.run(ANA, "nightly-all", wait=True, timeout_s=60)
    assert run["status"] == "succeeded", run
    steps = steps_of(run)
    big = steps["big"]["output"]
    assert big["flow"] == "orders-over" and big["status"] == "succeeded" and big["steps"]["count"]["preview"] == [[2]]
    assert big["steps"]["enough"]["passed"] is True
    # The child is a run of its own, started by the parent's owner, and says where it came from.
    child = f.get_run(ANA, big["run"]) if hasattr(f, "get_run") else f._run_public(big["run"])
    assert child["flow"] == "orders-over" and child["params"] == {"floor": 5.0} and child["triggered_by"] == ANA
    assert child["detail"]["parent_run"] == run["id"] and child["detail"]["parent_step"] == "big" and child["detail"]["depth"] == 1
    assert any("2 orders over 5" in str(t) and big["run"] in str(t) for t in f.told)
    # Mapped over: one run a floor, each run's outputs collected in the items' order.
    each = steps["each"]
    assert each["status"] == "ok" and each["output"]["items"] == 3 and each["output"]["succeeded"] == 3
    assert [r["steps"]["count"]["preview"][0][0] for r in each["output"]["results"]] == [3, 2, 1]
    assert len({r["run"] for r in each["output"]["results"]}) == 3


def test_a_failed_child_fails_the_step_and_a_cancelled_parent_cancels_the_child(env):  # noqa: F811
    f = env
    f.create(ANA, CHILD, publish=True)
    f.create(ANA, "kind: flow\nname: strict\nsteps:\n  - {name: none, type: flow, flow: orders-over, params: {floor: 100}}\n"
                  "  - {name: after, type: notify, after: [none], message: reached}\n", publish=True)
    run = f.run(ANA, "strict", wait=True, timeout_s=60)
    steps = steps_of(run)
    assert run["status"] == "failed" and steps["none"]["status"] == "failed" and steps["after"]["status"] == "skipped"
    assert "orders-over failed: enough" in steps["none"]["error"]

    # A child that waits: the parent waits on it without holding a worker; cancelled, both end.
    f.create(ANA, "kind: flow\nname: slow\nsteps:\n  - {name: hold, type: wait, seconds: 600}\n", publish=True)
    f.create(ANA, "kind: flow\nname: outer\nsteps:\n  - {name: inner, type: flow, flow: slow}\n", publish=True)
    outer = f.run(ANA, "outer")
    child = wait_until(lambda: (steps_of(f._run_public(outer["id"]))["inner"].get("status") in ("waiting", "running")
                                and (f._step_rows(outer["id"])[0]["state"] or {}).get("run")))
    assert f._run_public(child)["status"] in ("running", "waiting", "queued")
    f.cancel(ANA, outer["id"])
    wait_until(lambda: f._run_public(outer["id"])["status"] == "cancelled")
    wait_until(lambda: f._run_public(child)["status"] == "cancelled")
    # Not waited for: the step ends once the child has started.
    f.create(ANA, "kind: flow\nname: fire\nsteps:\n  - {name: go, type: flow, flow: slow, wait: false}\n", publish=True)
    fired = f.run(ANA, "fire", wait=True, timeout_s=30)
    assert fired["status"] == "succeeded" and steps_of(fired)["go"]["output"]["status"] in ("queued", "running", "waiting")
    f.cancel(ANA, steps_of(fired)["go"]["output"]["run"])


def test_what_a_flow_may_not_compose_is_refused_when_it_is_published(env):  # noqa: F811
    f = env
    f.create(ANA, CHILD, publish=True)
    sub = lambda name, child: f"kind: flow\nname: {name}\nsteps:\n  - {{name: s, type: flow, flow: {child}}}\n"  # noqa: E731
    # Itself, directly or through another flow.
    with pytest.raises(FlowError, match="may not run itself"):
        f.create(ANA, sub("loop", "loop"), publish=True)
    f.create(ANA, sub("a", "orders-over"), publish=True)
    f.create(ANA, sub("b", "a"), publish=True)
    with pytest.raises(FlowError, match="may not run itself: a → b → a"):
        f.save(ANA, "a", sub("a", "b"), publish=True)
    # One that is not there, not published, or not the owner's to run.
    with pytest.raises(FlowError, match="no flow 'nowhere'"):
        f.create(ANA, sub("c", "nowhere"), publish=True)
    f.create(ANA, sub("draft-only", "orders-over"))
    with pytest.raises(FlowError, match="no published version"):
        f.create(ANA, sub("d", "draft-only"), publish=True)
    with pytest.raises(FlowError, match="the owner may run"):
        f.create(EVE, sub("eves", "orders-over"), publish=True)
    # Steps that move data: their service must be set up, and a mirror or maintenance needs an operator.
    for step, why in (("{name: m, type: mirror, mirror: crm}", "MANAGE_SYSTEM_OPERATIONS"),
                      ("{name: t, type: maintenance, table: sales.orders}", "MANAGE_SYSTEM_OPERATIONS"),
                      ("{name: s, type: saas_sync, sync: hubspot}", "SaaS syncs is not set up"),
                      ("{name: r, type: reverse_sync, sync: to-crm}", "reverse syncs is not set up")):
        with pytest.raises(FlowError, match=why):
            f.create(ANA, f"kind: flow\nname: moves-{step[7]}\nsteps:\n  - {step}\n", publish=True)
    with pytest.raises(FlowError, match="mirrors is not set up"):
        f.create(ADM, "kind: flow\nname: moves-adm\nsteps:\n  - {name: m, type: mirror, mirror: crm}\n", publish=True)
    with pytest.raises(FlowError):
        f.create(ADM, "kind: flow\nname: bad\nsteps:\n  - {name: m, type: mirror}\n", publish=True)   # its field is required


def test_a_sensor_waits_for_data_and_gives_up_at_its_time_limit(env):  # noqa: F811
    from sqlalchemy import text

    f = env
    doc = ("kind: flow\nname: {name}\nsteps:\n  - {{name: arrived, type: sensor, {what}, poll_s: 1, timeout_s: {limit}}}\n"
           "  - {{name: then, type: notify, after: [arrived], message: \"there: {{{{ steps.arrived.detail }}}}\"}}\n")
    f.create(ANA, doc.format(name="wait-big", limit=60, what='sql: "SELECT count(*) AS n FROM main.orders WHERE amount > 50", '
                                                             'connection: wh, measure: n'), publish=True)
    run = f.run(ANA, "wait-big")
    wait_until(lambda: steps_of(f._run_public(run["id"]))["arrived"]["status"] == "waiting")
    assert f._run_public(run["id"])["status"] in ("running", "waiting")              # parked, not holding a worker
    with f.wh.engine("wh").begin() as c:
        c.execute(text("INSERT INTO orders VALUES (9, 1, 99.0)"))
    done = wait_until(lambda: (lambda r: r if r["status"] == "succeeded" else None)(f._run_public(run["id"])))
    arrived = steps_of(done)["arrived"]["output"]
    assert arrived["value"] == 1.0 and arrived["looks"] >= 2 and any("there: n is 1" in str(t) for t in f.told)
    # A partition: a table with rows matching a predicate.
    f.create(ANA, doc.format(name="wait-part", limit=60, what='table: main.orders, where: "amount > 90", connection: wh'), publish=True)
    assert f.run(ANA, "wait-part", wait=True, timeout_s=30)["status"] == "succeeded"
    # Never there: it fails when its time is up, and what waits on it is skipped. A query that errors fails at once.
    f.create(ANA, doc.format(name="wait-never", limit=3, what='table: main.orders, where: "amount > 1000", connection: wh'), publish=True)
    never = f.run(ANA, "wait-never", wait=True, timeout_s=30)
    assert never["status"] == "failed" and "timed out" in steps_of(never)["arrived"]["error"]
    assert steps_of(never)["then"]["status"] == "skipped"
    f.create(ANA, doc.format(name="wait-wrong", limit=60, what='sql: "SELECT nope FROM main.orders", connection: wh'), publish=True)
    wrong = f.run(ANA, "wait-wrong", wait=True, timeout_s=30)
    assert wrong["status"] == "failed" and "timed out" not in steps_of(wrong)["arrived"]["error"]
    for bad in ('poll_s: 1', 'table: main.orders, volume: v', 'table: main.orders'):                  # one thing, with its connection
        with pytest.raises(FlowError):
            f.create(ANA, f"kind: flow\nname: bad-sensor\nsteps:\n  - {{name: s, type: sensor, {bad}}}\n", publish=True)
        try:
            f.delete(ANA, "bad-sensor")
        except FlowError:
            pass


def test_teardown_steps_run_whatever_happened_and_do_not_decide_the_run(env):  # noqa: F811
    f = env
    f.create(ANA, textwrap.dedent("""
        kind: flow
        name: with-cleanup
        steps:
          - {name: setup, type: notify, message: made}
          - {name: work, type: check, after: [setup], sql: "SELECT count(*) AS n FROM main.orders", connection: wh,
             measure: n, op: ">", value: 1000}
          - {name: report, type: notify, after: [work], message: never}
          - {name: cleanup, type: notify, after: [work], teardown: true, message: removed}
    """), publish=True)
    run = f.run(ANA, "with-cleanup", wait=True, timeout_s=30)
    steps = steps_of(run)
    assert run["status"] == "failed" and steps["work"]["status"] == "failed" and steps["report"]["status"] == "skipped"
    assert steps["cleanup"]["status"] == "ok" and any("removed" in str(t) for t in f.told)
    # A teardown step that fails is said on the run, which still succeeded.
    f.create(ANA, textwrap.dedent("""
        kind: flow
        name: bad-cleanup
        steps:
          - {name: work, type: notify, message: did it}
          - {name: cleanup, type: sql, after: [work], teardown: true, sql: "SELECT nope FROM main.orders", connection: wh}
    """), publish=True)
    run = f.run(ANA, "bad-cleanup", wait=True, timeout_s=30)
    assert run["status"] == "succeeded" and steps_of(run)["cleanup"]["status"] == "failed"
    assert run["detail"]["teardown_failed"] == ["cleanup"]
    # A cancelled run still cleans up.
    f.create(ANA, textwrap.dedent("""
        kind: flow
        name: cancel-cleanup
        steps:
          - {name: hold, type: wait, seconds: 600}
          - {name: cleanup, type: notify, after: [hold], teardown: true, message: cleaned after cancel}
    """), publish=True)
    run = f.run(ANA, "cancel-cleanup")
    wait_until(lambda: steps_of(f._run_public(run["id"]))["hold"]["status"] == "waiting")
    f.cancel(ANA, run["id"])
    done = wait_until(lambda: (lambda r: r if r["status"] == "cancelled" else None)(f._run_public(run["id"])))
    assert steps_of(done)["hold"]["status"] == "cancelled" and steps_of(done)["cleanup"]["status"] == "ok"
    assert any("cleaned after cancel" in str(t) for t in f.told)
    with pytest.raises(FlowError):                                   # a branch decides the graph: it is no teardown
        f.create(ANA, "kind: flow\nname: nope\nsteps:\n  - {name: b, type: branch, condition: 'true', teardown: true}\n",
                 publish=True)


def test_missed_schedules_are_made_up_when_asked(env):  # noqa: F811
    from sqlalchemy import select, update

    from cairn.flows.engine import flow_runs, flows as flows_table

    f = env
    doc = ("kind: flow\nname: {name}\ntriggers:\n  - {{type: schedule, cron: \"* * * * *\"{more}}}\n"
           "concurrency: {{policy: skip, max_active: 1}}\nsteps:\n  - {{name: hold, type: wait, seconds: 600}}\n")
    f.create(ANA, doc.format(name="every-minute", more=", catchup: 3"), publish=True)
    f.create(ANA, doc.format(name="no-catchup", more=""), publish=True)
    import time as _t

    now = int(_t.time() * 1000)
    late = (now // 60_000) * 60_000 - 10 * 60_000                    # ten minutes of schedules were missed
    with f.engine.begin() as c:
        c.execute(update(flows_table).values(next_run_ms=late))
    f.run_due()

    def runs_of(name):
        with f.engine.connect() as c:
            fid = c.execute(select(flows_table.c.id).where(flows_table.c.name == name)).scalar()
            return [dict(r._mapping) for r in c.execute(select(flow_runs).where(flow_runs.c.flow_id == fid)
                                                        .order_by(flow_runs.c.created_ms, flow_runs.c.logical_date))]
    made = runs_of("every-minute")
    fired = sorted(r["detail"]["scheduled_ms"] for r in made)
    assert len(made) == 4 and fired == [fired[0] + i * 60_000 for i in range(4)] and fired[-1] <= now < fired[-1] + 60_000
    assert sum(1 for r in made if r["detail"].get("catchup")) == 3
    assert all(r["status"] != "skipped" for r in made)                # queued behind one another, not dropped
    assert len(runs_of("no-catchup")) == 1                            # as before: one run, the rest let go
    with f.engine.connect() as c:
        assert all(n > now for (n,) in c.execute(select(flows_table.c.next_run_ms)))
    for r in made + runs_of("no-catchup"):
        try:
            f.cancel(ANA, r["id"])
        except FlowError:
            pass
    with pytest.raises(FlowError):
        f.create(ANA, doc.format(name="too-many", more=", catchup: 1000"), publish=True)
