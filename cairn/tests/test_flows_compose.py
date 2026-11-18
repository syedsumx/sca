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
