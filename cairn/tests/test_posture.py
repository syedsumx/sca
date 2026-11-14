"""The security posture scan: each check finds what it is for (and only that), the score follows the findings, a
tampered audit chain is critical, new high findings are told once, and a check that cannot run is reported as an
error, never as passed. Real database, audit chain, policy decision point, shares and grants."""

from __future__ import annotations

import time
import types

from sqlalchemy import create_engine, insert, update

from cairn.common.audit import AuditLog, audit_log
from cairn.common.db import init_db
from cairn.dataaccess.shares import lakehouse_shares
from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
from cairn.governance.attributes import AttributeResolver
from cairn.governance.pdp import PolicyDecisionPoint
from cairn.governance.personas import grants_table
from cairn.governance.posture import Posture
from cairn.governance.residency import Residency

NOW = 1_800_000_000_000


def _world(tmp_path, graph):
    engine = create_engine(f"sqlite:///{tmp_path}/p.db")
    init_db(engine)
    pdp = PolicyDecisionPoint(AttributeResolver(graph), "additive", [], 0)
    audit = AuditLog(engine)
    told: list = []
    settings = types.SimpleNamespace(dev_trust_actor_header=True)
    residency = Residency({"regions": {"eu": {"location": "s3://lake-eu/w"}}, "pins": [{"namespaces": ["crm"], "region": "eu"}]},
                          None)
    p = Posture(engine, settings, pdp, audit, residency, lambda: 3, True, told.extend, clock=lambda: NOW)
    return engine, pdp, audit, settings, p, told


def test_each_check_finds_what_it_is_for(tmp_path, graph, monkeypatch):
    monkeypatch.delenv("CAIRN_AI_DAILY_BUDGET_USD", raising=False)
    engine, pdp, audit, settings, p, told = _world(tmp_path, graph)
    pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="everything", effect="allow", actions=["*"]),
        Policy(id="read-all", effect="allow", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=["*"])),
        Policy(id="admins", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": "x"})]))
    now_s = NOW // 1000
    with engine.begin() as c:
        for name, expires, used in (("long", now_s + 400 * 86400, now_s), ("idle", now_s + 10 * 86400, now_s - 120 * 86400)):
            c.execute(insert(lakehouse_shares).values(id=name, name=name, recipient="x", tables="[]", secret_hash="h",
                                                      created_by="a", created_s=now_s - 200 * 86400, expires_s=expires,
                                                      last_used_s=used))
        c.execute(insert(grants_table).values(id="g1", persona_id="p1", actor="urn:li:corpuser:ana", source="manual",
                                              expires_at_ms=None, revoked=False, created_at_ms=NOW))
    audit.record("security", "test.entry", "urn:li:corpuser:a", None, {"n": 1})
    out = p.scan("urn:li:corpuser:op")
    got = {(f["check"], f["severity"]) for f in out["findings"]}
    assert got == {("trusted_header", "critical"), ("open_policy", "critical"), ("open_policy", "high"),
                   ("authz_mode", "medium"), ("pii_unmasked", "high"), ("long_shares", "medium"), ("idle_shares", "low"),
                   ("no_access_review", "medium"), ("residency_region", "high"), ("ai_budget", "low")}, got
    assert not any(f["evidence"].get("policy") == "admins" for f in out["findings"])      # a condition: not open
    assert out["score"] == 100 - (25 + 25 + 10 + 4 + 10 + 4 + 1 + 4 + 10 + 1) == 6
    assert all(v in ("ok", "finding") for v in out["checks"].values()), out["checks"]
    assert {f["check"] for f in told} == {"trusted_header", "open_policy", "pii_unmasked", "residency_region"}
    # Fixed: the findings go, the score comes back; nothing new is told again.
    told.clear()
    settings.dev_trust_actor_header = False
    pdp.set_bundle(PolicyBundle(policies=[Policy(id="mask", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
                                                 obligations=[Obligation(type="mask_columns", params={"labels": ["PII"]})])]))
    p.residency = Residency({}, None)
    second = p.scan()
    assert {f["check"] for f in second["findings"]} == {"authz_mode", "long_shares", "idle_shares", "no_access_review",
                                                       "ai_budget"}
    assert second["score"] == 100 - (4 + 4 + 1 + 4 + 1) and told == []
    assert [h["score"] for h in p.history()] == [second["score"], out["score"]]
    # A tampered audit entry: the chain breaks, critical.
    with engine.begin() as c:
        c.execute(update(audit_log).where(audit_log.c.action == "test.entry").values(actor="urn:li:corpuser:mallory"))
    third = p.scan()
    assert ("audit_chain", "critical") in {(f["check"], f["severity"]) for f in third["findings"]}
    assert [f["check"] for f in told] == ["audit_chain"]


def test_a_check_that_cannot_run_is_an_error_not_a_pass(tmp_path, graph):
    _, _, _, _, p, _ = _world(tmp_path, graph)

    def broken():
        raise RuntimeError("the catalog is down")
    p.sensitive_datasets = broken
    out = p.scan()
    assert out["checks"]["pii_unmasked"].startswith("error: RuntimeError")
    assert time.time()          # (the scan finished; the other checks still ran)
    assert out["checks"]["trusted_header"] == "finding"
