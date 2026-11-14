"""Compliance forms: assignment by condition, completion writes catalog metadata, verification, progress."""

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.governance.forms import FormError, FormService, FormSpec, answer_mcps, validate_answers
from cairn.services import build_services

PII = "urn:li:glossaryTerm:Classification.PII.Email"
FIN = "urn:li:domain:finance"
ANA, BOB, OPS = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:ops"
RETENTION = "urn:li:structuredProperty:io.cairn.retention"


def ds(name):
    return f"urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.{name},PROD)"


def seed(graph):
    graph.aspects[(ds("customers"), "domains")] = {"domains": [FIN]}
    graph.aspects[(ds("customers"), "glossaryTerms")] = {"terms": [{"urn": PII}]}
    graph.aspects[(ds("customers"), "ownership")] = {"owners": [{"owner": ANA, "type": "DATAOWNER"},
                                                                {"owner": BOB, "type": "TECHNICAL_OWNER"}]}
    graph.aspects[(ds("customers"), "datasetProperties")] = {"description": "Customers"}
    graph.aspects[(ds("payments"), "domains")] = {"domains": [FIN]}
    graph.aspects[(ds("payments"), "glossaryTerms")] = {"terms": [{"urn": PII}]}     # no owner
    graph.aspects[(ds("weather"), "domains")] = {"domains": ["urn:li:domain:ops"]}
    rows = {}
    for (urn, aspect), value in graph.aspects.items():
        rows.setdefault(urn, {"urn": urn})[aspect] = value
    graph.scroll_entities = lambda etype, aspects, page_size=500: [r for r in rows.values()
                                                                   if r["urn"].startswith("urn:li:dataset:")]


SPEC = {
    "name": "PII ownership and retention", "kind": "verification",
    "select": {"all": [{"attr": "resource.classification", "op": "contains", "value": "PII"},
                       {"attr": "resource.domains", "op": "contains", "value": FIN}]},
    "assignees": {"ownership_types": ["DATAOWNER"], "users": [OPS]},
    "questions": [
        {"id": "desc", "type": "description", "title": "What is in this table?"},
        {"id": "owner", "type": "owners", "title": "Accountable owner", "ownership_type": "BUSINESS_OWNER"},
        {"id": "retention", "type": "structured_property", "title": "Retention", "property_urn": RETENTION,
         "allowed": ["1y", "7y"], "multiple": False},
        {"id": "dpia", "type": "yes_no", "title": "DPIA done?", "required": False},
    ],
    "due_days": 14,
}


@pytest.fixture
def svc(engine, graph):
    seed(graph)
    from cairn.quality.auto import Planner

    sent = []
    return FormService(engine, graph, None, lambda row, who, urns: sent.append((who, urns)),
                       lambda: Planner(graph).datasets()), graph, sent


def test_spec_validation():
    with pytest.raises(ValueError):
        FormSpec.model_validate({**SPEC, "select": {"attr": "resource.x", "op": "nope", "value": 1}})
    with pytest.raises(ValueError):
        FormSpec.model_validate({**SPEC, "questions": [SPEC["questions"][0], SPEC["questions"][0]]})
    with pytest.raises(ValueError):
        FormSpec.model_validate({**SPEC, "questions": [{"id": "p", "type": "structured_property", "title": "x"}]})


def test_assignment_follows_the_selector_and_owners(svc):
    forms, graph, sent = svc
    f = forms.create(FormSpec.model_validate(SPEC), OPS)
    preview = forms.assign(f["id"], OPS, dry_run=True)
    assert sorted(preview["assigned"]) == [ds("customers"), ds("payments")] and not forms.tasks_for(ANA)
    out = forms.assign(f["id"], OPS)
    assert sorted(out["assigned"]) == [ds("customers"), ds("payments")]
    # Data owners get their assets; an asset with no data owner falls back to the named users.
    assert [t["urn"] for t in forms.tasks_for(ANA)] == [ds("customers")]
    assert [t["urn"] for t in forms.tasks_for(OPS)] == [ds("payments")]
    assert forms.tasks_for(BOB) == []                         # technical owner, not asked
    assert sorted(sent) == [([ANA], [ds("customers")]), ([OPS], [ds("payments")])]
    assert forms.tasks_for(ANA)[0]["due_ms"] - forms.tasks_for(ANA)[0]["assigned_ms"] == 14 * 86_400_000
    # Idempotent; an asset that stops matching loses its untouched task.
    assert forms.assign(f["id"], OPS)["assigned"] == []
    graph.aspects[(ds("payments"), "glossaryTerms")] = {"terms": []}
    seed_rows = graph.scroll_entities("dataset", [])
    for r in seed_rows:
        if r["urn"] == ds("payments"):
            r["glossaryTerms"] = {"terms": []}
    assert forms.assign(f["id"], OPS)["withdrawn"] == [ds("payments")]


def test_completion_writes_metadata_and_verification_signs_off(svc):
    forms, graph, _ = svc
    f = forms.create(FormSpec.model_validate(SPEC), OPS)
    forms.assign(f["id"], OPS)
    task = forms.task(f["id"], ds("customers"))
    assert task["answers"]["desc"] == "Customers" and set(task["answers"]["owner"]) == {ANA, BOB}   # pre-filled
    with pytest.raises(FormError, match="assigned to someone else"):
        forms.submit(f["id"], ds("customers"), {}, BOB)
    with pytest.raises(FormError, match="Retention: an answer is required"):
        forms.submit(f["id"], ds("customers"), {"desc": "x", "owner": [ANA]}, ANA)
    with pytest.raises(FormError, match="not among the allowed"):
        forms.submit(f["id"], ds("customers"), {"desc": "x", "owner": [ANA], "retention": "forever"}, ANA)
    answers = {"desc": "One row per customer.", "owner": ["urn:li:corpuser:carol"], "retention": "7y", "dpia": True}
    assert forms.submit(f["id"], ds("customers"), answers, ANA)["state"] == "COMPLETED"
    assert forms.asset_status(ds("customers"))["status"] == "INCOMPLETE"          # needs the sign-off
    assert graph.aspects[(ds("customers"), "editableDatasetProperties")]["description"] == "One row per customer."
    owners = graph.aspects[(ds("customers"), "ownership")]["owners"]
    assert {"owner": "urn:li:corpuser:carol", "type": "BUSINESS_OWNER"} in owners and len(owners) == 3
    props = graph.aspects[(ds("customers"), "structuredProperties")]["properties"]
    assert props == [{"propertyUrn": RETENTION, "values": [{"string": "7y"}]}]
    assert graph.emitted[-1]["systemMetadata"]["properties"]["cairn_form"] == f["id"]
    assert forms.submit(f["id"], ds("customers"), answers, ANA, verify=True)["state"] == "VERIFIED"
    assert forms.asset_status(ds("customers"))["status"] == "COMPLIANT"
    batch = forms.asset_statuses([ds("customers"), ds("payments"), ds("unassigned")])
    assert {u: v["status"] for u, v in batch.items()} == {
        ds("customers"): "COMPLIANT", ds("payments"): "INCOMPLETE", ds("unassigned"): None}
    with pytest.raises(FormError, match="already verified"):
        forms.submit(f["id"], ds("customers"), answers, ANA, verify=True)
    # A steward with edit rights may complete a task assigned to someone else.
    forms.submit(f["id"], ds("payments"), {**answers, "owner": [ANA]}, BOB, may_act=lambda u: True)
    p = forms.progress(f["id"])
    assert p["total"] == {"assets": 2, "PENDING": 0, "COMPLETED": 1, "VERIFIED": 1, "done_pct": 100.0}
    assert p["by_domain"][FIN]["assets"] == 2


def test_answer_validation_and_writes_are_additive():
    spec = FormSpec.model_validate({**SPEC, "questions": [
        {"id": "t", "type": "glossary_terms", "title": "Terms", "allowed": [PII, "urn:li:glossaryTerm:X"]},
        {"id": "g", "type": "tags", "title": "Tags"}, {"id": "d", "type": "domain", "title": "Domain"},
        {"id": "n", "type": "text", "title": "Notes", "required": False}]})
    with pytest.raises(FormError, match="not a valid choice"):
        validate_answers(spec, {"t": [PII], "g": ["nope"], "d": FIN})
    clean = validate_answers(spec, {"t": [PII], "g": ["urn:li:tag:gdpr"], "d": FIN, "n": "  hi "})
    existing = {(ds("a"), "glossaryTerms"): {"terms": [{"urn": PII}]}, (ds("a"), "globalTags"): {"tags": [{"tag": "urn:li:tag:old"}]}}
    mcps = {m["aspectName"]: m for m in answer_mcps(ds("a"), spec, clean, ANA, lambda u, a: existing.get((u, a)), "f1")}
    import json
    assert json.loads(mcps["glossaryTerms"]["aspect"]["value"])["terms"] == [{"urn": PII}]       # no duplicate
    assert [t["tag"] for t in json.loads(mcps["globalTags"]["aspect"]["value"])["tags"]] == ["urn:li:tag:old", "urn:li:tag:gdpr"]
    assert json.loads(mcps["domains"]["aspect"]["value"]) == {"domains": [FIN]}
    assert clean["n"] == "hi" and "editableDatasetProperties" not in mcps     # text stays in Cairn


def test_overdue_reminders(svc):
    forms, _, _ = svc
    f = forms.create(FormSpec.model_validate(SPEC), OPS)
    forms.assign(f["id"], OPS)
    import time
    later = int(time.time() * 1000) + 15 * 86_400_000
    assert {t["urn"] for t in forms.overdue(later)} == {ds("customers"), ds("payments")}
    assert forms.overdue() == []


def test_api_and_trust_badge(settings, graph):
    seed(graph)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"}),
        Policy(id="ops", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": OPS})]))
    c = TestClient(create_api_app(s))
    ana, ops = {"X-Cairn-Actor": ANA}, {"X-Cairn-Actor": OPS}
    assert c.post("/governance/forms", json=SPEC, headers=ana).status_code == 403
    f = c.post("/governance/forms", json=SPEC, headers=ops).json()
    assert c.post(f"/governance/forms/{f['id']}/assign", headers=ops).json()["matched"] == 2
    tasks = c.get("/governance/forms/tasks", headers=ana).json()
    assert [t["urn"] for t in tasks] == [ds("customers")]
    t = c.get(f"/governance/forms/{f['id']}/tasks/{ds('customers')}", headers=ana).json()
    assert [q["id"] for q in t["questions"]] == ["desc", "owner", "retention", "dpia"]
    bad = c.post(f"/governance/forms/{f['id']}/tasks/{ds('customers')}", headers=ana, json={"answers": {}})
    assert bad.status_code == 400
    ok = c.post(f"/governance/forms/{f['id']}/tasks/{ds('customers')}", headers=ana, json={
        "answers": {"desc": "d", "owner": [ANA], "retention": "1y"}, "verify": True})
    assert ok.json()["state"] == "VERIFIED"
    assert c.get("/governance/forms/status", params={"urn": ds("customers")}, headers=ana).json()["status"] == "COMPLIANT"
    assert s.trust([ds("customers"), ds("payments"), ds("weather")]) == {
        ds("customers"): {"certification": None, "quality_status": None, "compliance": "COMPLIANT"},
        ds("payments"): {"certification": None, "quality_status": None, "compliance": "INCOMPLETE"},
        ds("weather"): {"certification": None, "quality_status": None, "compliance": None}}
    prog = c.get(f"/governance/forms/{f['id']}/progress", headers=ops).json()
    assert prog["total"]["VERIFIED"] == 1 and prog["by_assignee"][OPS]["PENDING"] == 1
    assert c.get(f"/governance/forms/{f['id']}/progress", headers=ana).status_code == 403
    assert c.delete(f"/governance/forms/{f['id']}", headers=ops).status_code == 204
    assert c.get("/governance/forms/tasks", headers=ops).json() == []         # archived forms leave the queue


def test_notifier_assigns_new_assets_and_reminds(settings, graph):
    from cairn.cli import _FORMS_STATE, forms_tick

    seed(graph)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    f = s.forms.create(FormSpec.model_validate(SPEC), OPS)
    _FORMS_STATE.update(assigned_ms=0, reminded_ms=0)
    import time
    now = int(time.time() * 1000)
    assert forms_tick(s, now)[f["id"]] == {"assigned": 2, "withdrawn": 0}
    assert {n["type"] for n in s.notify.inbox(ANA)} == {"form.assigned"}
    out = forms_tick(s, now + 15 * 86_400_000)
    assert out["reminded"] == 2 and any(n["type"] == "form.due" for n in s.notify.inbox(OPS))
