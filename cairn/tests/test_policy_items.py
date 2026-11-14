"""One policy at a time (/governance/policies/{id}): read, set and delete a single Cairn policy, the rest of the
bundle untouched; each change a new audited version; other owners' policies (personas, native) refused; and
concurrent editors never lose each other's changes (serialized in-process, and across processes on PostgreSQL
by an advisory lock)."""

import multiprocessing
import os
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.governance.pdp import PolicyRepository
from cairn.services import build_services

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
ADMIN_POLICY = Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})


@pytest.fixture
def api(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[ADMIN_POLICY, Policy(id="persona-x", effect="allow", actions=["VIEW_ENTITY_PAGE"],
                                                                 source="persona")]))
    app = create_api_app(s)
    return s, TestClient(app, headers={"X-Cairn-Actor": ADMIN}), TestClient(app, headers={"X-Cairn-Actor": ANA})


def test_one_policy_set_read_and_deleted(api):
    s, admin, ana = api
    body = {"id": "analysts-read", "effect": "allow", "actions": ["VIEW_DATASET_PROFILE"], "priority": 10,
            "target": {"resource_types": ["dataset"]}, "condition": {"attr": "subject.groups", "op": "contains", "value": "analysts"}}
    r = admin.put("/governance/policies/analysts-read", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == 10 and r.json()["source"] == "cairn"
    assert [x.id for x in s.pdp.bundle.policies] == ["admin", "persona-x", "analysts-read"]     # the rest untouched
    assert admin.get("/governance/policies/analysts-read").json()["actions"] == ["VIEW_DATASET_PROFILE"]
    # Changed in place, keeping its position; each change a version in the database, audited.
    assert admin.put("/governance/policies/admin", json={**ADMIN_POLICY.model_dump(mode="json"), "description": "ops"}).status_code == 200
    assert [x.id for x in s.pdp.bundle.policies] == ["admin", "persona-x", "analysts-read"]
    assert s.policy_repo.latest() == s.pdp.bundle
    events = [e for e in s.audit.entries("governance", limit=10) if e["action"] == "policies.updated"]
    assert {e["details"].get("changed") for e in events} >= {"analysts-read", "admin"}
    assert admin.delete("/governance/policies/analysts-read").status_code == 204
    assert admin.get("/governance/policies/analysts-read").status_code == 404
    assert admin.delete("/governance/policies/analysts-read").status_code == 404


def test_what_is_refused(api):
    s, admin, ana = api
    one = {"id": "x", "effect": "allow", "actions": ["*"]}
    assert ana.put("/governance/policies/x", json=one).status_code == 403                    # needs MANAGE_POLICIES
    assert admin.put("/governance/policies/y", json=one).status_code == 422                  # id and path differ
    assert admin.put("/governance/policies/x", json={**one, "source": "persona"}).status_code == 422
    assert admin.put("/governance/policies/persona-x", json={**one, "id": "persona-x"}).status_code == 409
    assert admin.delete("/governance/policies/persona-x").status_code == 409                 # the persona layer's own
    assert admin.put("/governance/policies/x", json={**one, "condition": {"attr": "a", "op": "nope"}}).status_code == 422
    assert [x.id for x in s.pdp.bundle.policies] == ["admin", "persona-x"]                    # nothing changed


def test_concurrent_editors_in_one_process_lose_nothing(settings):
    engine = create_engine(settings.database_url)
    from cairn.common.db import metadata

    metadata.create_all(engine)
    repo = PolicyRepository(engine)

    def add(i):
        repo.change(lambda b: PolicyBundle(policies=[*b.policies, Policy(id=f"p{i}", effect="allow", actions=["*"])]), "t")

    threads = [threading.Thread(target=add, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(p.id for p in repo.latest().policies) == sorted(f"p{i}" for i in range(30))


def _add_many(url, who, n):
    repo = PolicyRepository(create_engine(url))
    for i in range(n):
        repo.change(lambda b, i=i: PolicyBundle(policies=[*b.policies, Policy(id=f"{who}-{i}", effect="allow", actions=["*"])]), who)


@pytest.mark.skipif(not os.environ.get("CAIRN_TEST_PG_URL"), reason="CAIRN_TEST_PG_URL not set")
def test_concurrent_replicas_on_postgres_lose_nothing():
    """Separate processes (replicas) share only the database: the advisory lock serializes them."""
    from sqlalchemy import text

    from cairn.common.db import metadata

    url = os.environ["CAIRN_TEST_PG_URL"]
    engine = create_engine(url)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cairn_policy_bundles"))
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_add_many, args=(url, f"r{k}", 15)) for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    ids = sorted(p.id for p in PolicyRepository(engine).latest().policies)
    assert ids == sorted(f"r{k}-{i}" for k in range(4) for i in range(15))
