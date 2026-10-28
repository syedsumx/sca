"""AI stewards: gap detection, owner inference with evidence, PII term proposals, drafts within
budget, batch review with authorization, auto-accept, and no repeats."""

import pytest

from cairn.ai.steward import StewardError, StewardService, gaps, owner_candidates
from cairn.governance.bulk import BulkService

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.orders,PROD)"
RAW = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.raw.orders,PROD)"
DOC = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.done,PROD)"
ANA, BOB, LEE = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:lee"
FIN = "urn:li:domain:finance"


def test_owner_candidates_rank_with_evidence():
    got = owner_candidates([{"user": ANA, "count": 80}, {"user": BOB, "count": 15}, {"user": LEE, "count": 5}],
                           [BOB, "urn:li:corpuser:datahub"], [], [ANA, ANA])
    assert [c["owner"] for c in got] == [ANA, BOB]
    assert got[0]["evidence"] == ["ran 80 of the 100 recent queries", "owns 2 upstream table(s)"]
    assert got[0]["confidence"] > got[1]["confidence"]
    assert owner_candidates([], ["urn:li:corpuser:cairn-automation-x"], [], []) == []      # never system actors


def test_gaps():
    assert gaps({}) == {"description", "owner", "classification"}
    assert gaps({"datasetProperties": {"description": "x"}, "ownership": {"owners": [{"owner": ANA}]},
                 "schemaMetadata": {"fields": [{"fieldPath": "e", "glossaryTerms": {"terms": [{"urn": "t"}]}}]}}) == set()


class Docgen:
    def __init__(self):
        self.calls = 0

    def draft(self, urn):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("daily AI budget exhausted")
        return {"description": "One row per order.", "columns": [], "confidence": 0.7, "unknowns": ["grain"],
                "model": "m"}


@pytest.fixture
def steward(engine, graph):
    graph.aspects[(ORDERS, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}, {"fieldPath": "customer_email"}]}
    graph.aspects[(ORDERS, "domains")] = {"domains": [FIN]}
    graph.aspects[(FIN, "ownership")] = {"owners": [{"owner": LEE, "type": "DATAOWNER"}]}
    graph.aspects[(RAW, "ownership")] = {"owners": [{"owner": ANA, "type": "DATAOWNER"}]}
    graph.aspects[(RAW, "schemaMetadata")] = {"fields": [{"fieldPath": "payload"}]}
    graph.timeseries[(ORDERS, "datasetUsageStatistics")] = [{"userCounts": [{"user": ANA, "count": 40}, {"user": BOB, "count": 2}]}]
    graph.timeseries[(ORDERS, "datasetProfile")] = [{"fieldProfiles": [
        {"fieldPath": "customer_email", "sampleValues": ["a@x.io", "b@y.org", "c@z.com"]}]}]
    graph.aspects[(DOC, "datasetProperties")] = {"description": "Fully documented"}
    graph.aspects[(DOC, "ownership")] = {"owners": [{"owner": ANA}]}
    graph.aspects[(DOC, "schemaMetadata")] = {"fields": [{"fieldPath": "x", "glossaryTerms": {"terms": [{"urn": "t"}]}}]}
    docgen = Docgen()
    s = StewardService(engine, graph, BulkService(engine, graph), popularity=lambda u: {ORDERS: 10, RAW: 1}.get(u, 0),
                       upstreams=lambda u: [RAW] if u == ORDERS else [], docgen=docgen, auto_accept=None)
    return s, graph, docgen


def test_run_proposes_owners_terms_and_drafts_most_used_first(steward):
    s, graph, docgen = steward
    out = s.run()
    assert out["examined"] == 2 and out["datasets_with_gaps"] == 2       # DOC has no gaps
    props = s.proposals()
    assert props[0]["urn"] == ORDERS                                      # most used first
    by = {(p["urn"], p["kind"], p["field"]): p for p in props}
    owner = next(p for p in props if p["kind"] == "owner" and p["value"] == ANA)
    assert "owns 1 upstream table(s)" in owner["evidence"] and "ran 40 of the 42 recent queries" in owner["evidence"]
    assert {p["value"] for p in props if p["urn"] == ORDERS and p["kind"] == "owner"} == {ANA, LEE}
    term = by[(ORDERS, "term", "customer_email")]
    assert term["value"] == "urn:li:glossaryTerm:Classification.PII.Email" and term["confidence"] >= 0.9
    assert by[(ORDERS, "description", "")]["value"]["description"] == "One row per order."
    assert docgen.calls == 2 and (RAW, "description", "") not in by        # the budget ran out: no second draft
    assert s.run()["proposed"] == 0                                       # nothing proposed twice
    assert s.run(urns=[DOC])["examined"] == 0


def test_batch_accept_writes_one_batch_and_reject_sticks(steward):
    s, graph, _ = steward
    s.run()
    props = {(p["kind"], p["field"], p["value"] if isinstance(p["value"], str) else "d"): p for p in s.proposals(urn=ORDERS)}
    email = "urn:li:glossaryTerm:Classification.PII.Email"
    owner, term, desc = props[("owner", "", ANA)], props[("term", "customer_email", email)], props[("description", "", "d")]
    with pytest.raises(StewardError) as e:
        s.decide([owner["id"]], "accept", BOB, authorize=lambda priv, urn: priv != "EDIT_ENTITY_OWNERS")
    assert e.value.status == 403
    out = s.decide([owner["id"], term["id"], desc["id"]], "accept", BOB, authorize=lambda p, u: True,
                   edits={desc["id"]: "Orders, one row each."})
    assert out["decided"] == 3 and out["batch_id"]
    assert [o["owner"] for o in graph.aspects[(ORDERS, "ownership")]["owners"]] == [ANA]
    esm = {f["fieldPath"]: f for f in graph.aspects[(ORDERS, "editableSchemaMetadata")]["editableSchemaFieldInfo"]}
    assert esm["customer_email"]["glossaryTerms"]["terms"] == [{"urn": "urn:li:glossaryTerm:Classification.PII.Email"}]
    assert graph.aspects[(ORDERS, "editableDatasetProperties")]["description"] == "Orders, one row each."
    lee = props[("owner", "", LEE)]
    s.decide([lee["id"]], "reject", BOB, authorize=lambda p, u: True)
    assert s.proposals(state="REJECTED")[0]["value"] == LEE
    s.run()
    assert LEE not in {p["value"] for p in s.proposals()}                 # rejected stays rejected


def test_auto_accept_applies_confident_classifications(engine, graph, steward):
    s, graph, _ = steward
    s.auto_accept = 0.9
    out = s.run()
    assert out["auto_accepted"] == 1
    assert s.proposals(state="AUTO_ACCEPTED")[0]["field"] == "customer_email"
    esm = graph.aspects[(ORDERS, "editableSchemaMetadata")]["editableSchemaFieldInfo"]
    assert esm[0]["glossaryTerms"]["terms"][0]["urn"].endswith("PII.Email")
