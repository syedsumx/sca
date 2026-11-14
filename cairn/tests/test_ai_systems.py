"""AI systems: risk tiers from the assessment, the purpose rule for restricted data, approvals, the inventory."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.governance.ai_systems import load_template
from cairn.governance.personas import PurposeSpec
from cairn.services import build_services

GOV, ANA = "urn:li:corpuser:gov", "urn:li:corpuser:ana"
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
CVS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,hr.public.cvs,PROD)"
JOBS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,hr.public.jobs,PROD)"
NONE = {"prohibited": [], "high_risk_use": [], "safety_component": False, "transparency": []}


def test_tier_is_the_worst_answer():
    t = load_template()
    assert t.tier(NONE) == "minimal" and t.missing(NONE, "minimal") == []
    assert t.tier({**NONE, "transparency": ["interacts_with_people"]}) == "limited"
    assert t.tier({**NONE, "transparency": ["generates_content"], "high_risk_use": ["employment"]}) == "high"
    assert t.tier({**NONE, "safety_component": True}) == "high"
    assert t.tier({**NONE, "prohibited": ["social_scoring"], "high_risk_use": ["employment"]}) == "unacceptable"
    assert t.missing({**NONE, "high_risk_use": ["employment"]}, "high") == ["human_oversight"]
    assert t.missing({}, "minimal") == ["prohibited", "high_risk_use", "safety_component", "transparency"]


@pytest.fixture
def api(settings, graph):
    graph.aspects[(CVS, "glossaryTerms")] = {"terms": [{"urn": EMAIL}]}
    graph.aspects[(JOBS, "datasetProperties")] = {"name": "jobs"}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="gov", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": GOV})]))
    return s, TestClient(create_api_app(s)), {"X-Cairn-Actor": GOV}, {"X-Cairn-Actor": ANA}


def screening(**extra):
    return {"name": "CV screening", "purpose": "Ranks applicants for recruiters.", "models": ["urn:li:mlModel:x"],
            "datasets": [CVS, JOBS], "answers": {**NONE, "high_risk_use": ["employment"],
                                                  "human_oversight": "Recruiters review every ranking."}, **extra}


def test_high_risk_needs_a_purpose_for_restricted_data(api):
    s, c, gov, ana = api
    sys_ = c.post("/governance/ai-systems", json=screening(), headers=ana).json()
    assert sys_["tier"] == "high" and sys_["state"] == "DRAFT" and sys_["owner"] == ANA
    assert [v["dataset"] for v in sys_["violations"]] == [CVS] and sys_["violations"][0]["labels"] == ["PII.Email"]
    sid = sys_["id"]
    assert c.post(f"/governance/ai-systems/{sid}/submit", headers=ana).json()["state"] == "IN_REVIEW"
    assert c.post(f"/governance/ai-systems/{sid}/decision", json={"approve": True}, headers=ana).status_code == 403
    refused = c.post(f"/governance/ai-systems/{sid}/decision", json={"approve": True}, headers=gov)
    assert refused.status_code == 422 and "no approved purpose" in refused.json()["detail"]

    wrong = s.personas.upsert_purpose(PurposeSpec(name="Payroll", tags=["Financial"]), GOV)
    right = s.personas.upsert_purpose(PurposeSpec(name="Recruiting", tags=["PII.*"]), GOV)
    assert c.put(f"/governance/ai-systems/{sid}", json=screening(data_purpose_id=wrong.id),
                 headers=ana).json()["violations"][0]["reason"] == "purpose 'Payroll' does not cover PII.Email"
    fixed = c.put(f"/governance/ai-systems/{sid}", json=screening(data_purpose_id=right.id), headers=ana).json()
    assert fixed["violations"] == [] and fixed["state"] == "DRAFT"                 # a material change: review again
    c.post(f"/governance/ai-systems/{sid}/submit", headers=ana)
    ok = c.post(f"/governance/ai-systems/{sid}/decision", json={"approve": True, "note": "DPIA on file"}, headers=gov)
    assert ok.json()["state"] == "APPROVED" and ok.json()["decided_by"] == GOV, ok.text

    # Classified as restricted after approval: the inventory shows it at once.
    s.graph.aspects[(JOBS, "glossaryTerms")] = {"terms": [{"urn": "urn:li:glossaryTerm:Classification.Confidential"}]}
    s.attributes.invalidate(JOBS)
    rep = c.get("/governance/ai-systems/report", headers=gov).json()
    assert rep["by_tier"]["high"] == 1 and rep["by_state"]["APPROVED"] == 1
    assert [(v["dataset"], v["labels"]) for v in rep["violations"]] == [(JOBS, ["Confidential"])]
    assert c.get("/governance/ai-systems/report", headers=ana).status_code == 403
    csv_ = c.get("/governance/ai-systems/report?format=csv", headers=gov).text
    assert csv_.splitlines()[0].startswith("id,name,owner,tier,state") and "CV screening" in csv_
    audit = [e["action"] for e in s.audit.export(category="ai_governance")]
    assert audit.count("ai_system.submitted") == 2 and audit[-1] == "ai_system.approved"


def test_prohibited_practices_cannot_be_approved(api):
    s, c, gov, ana = api
    body = screening(datasets=[JOBS], answers={**NONE, "prohibited": ["social_scoring"],
                                               "human_oversight": "n/a"})
    sid = c.post("/governance/ai-systems", json=body, headers=ana).json()["id"]
    c.post(f"/governance/ai-systems/{sid}/submit", headers=ana)
    r = c.post(f"/governance/ai-systems/{sid}/decision", json={"approve": True}, headers=gov)
    assert r.status_code == 422 and "prohibited practice" in r.json()["detail"]
    assert c.post(f"/governance/ai-systems/{sid}/decision", json={"approve": False, "note": "Not allowed"},
                  headers=gov).json()["state"] == "REJECTED"


def test_assessment_must_be_complete_and_valid(api):
    s, c, gov, ana = api
    assert c.post("/governance/ai-systems", json=screening(answers={"prohibited": ["teleport"]}),
                  headers=ana).status_code == 422
    assert c.post("/governance/ai-systems", json=screening(datasets=["not-a-urn"]), headers=ana).status_code == 422
    sid = c.post("/governance/ai-systems", json=screening(answers={**NONE, "high_risk_use": ["employment"]}),
                 headers=ana).json()["id"]
    r = c.post(f"/governance/ai-systems/{sid}/submit", headers=ana)
    assert r.status_code == 422 and "human_oversight" in r.json()["detail"]
    other = {"X-Cairn-Actor": "urn:li:corpuser:mallory"}
    assert c.put(f"/governance/ai-systems/{sid}", json=screening(), headers=other).status_code == 403
    assert c.post(f"/governance/ai-systems/{sid}/retire", headers=ana).json()["state"] == "RETIRED"
    assert c.put(f"/governance/ai-systems/{sid}", json=screening(), headers=ana).status_code == 409
    assert len(c.get("/governance/ai-systems/assessment", headers=ana).json()["questions"]) == 5
