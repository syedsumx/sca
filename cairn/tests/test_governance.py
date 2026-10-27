import os
from datetime import datetime, timedelta, timezone

import pytest

from cairn.common.audit import AuditLog
from cairn.common.proposals import aspect_value, schema_field_urn
from cairn.governance.abac import Policy, PolicyBundle, evaluate_leaf, validate_condition
from cairn.governance.attributes import AttributeResolver
from cairn.governance.certification import (
    CERT_STATUS_PROP,
    CERTIFIED_TAG,
    CreateWorkflowRequest,
    EntryChecks,
    WorkflowError,
    WorkflowService,
    bootstrap_mcps,
    load_templates,
)
from cairn.governance.pdp import AuthorizeRequest, PolicyDecisionPoint, ResourceRef, load_bundle_from_dir
from cairn.governance.pii import (
    PROPAGATION_SOURCE,
    FieldEdge,
    apply_terms_mcps,
    classify_field,
    plan_propagation,
    pii_term,
    revert_propagation_mcps,
)
from cairn.governance.propagation_action import PiiPropagator, PropagationConfig
from cairn.governance.rbac import RbacModel, translate_native_policy

from conftest import DS, DS_DOWN, DS_DOWN2

POLICY_DIR = os.path.join(os.path.dirname(__file__), "..", "config", "policies")
ALICE, BOB, CAROL = "urn:li:corpuser:alice", "urn:li:corpuser:bob", "urn:li:corpuser:carol"
STEWARDS = "urn:li:corpGroup:data-stewards"


def _seed(graph):
    graph.aspects[(DS, "ownership")] = {"owners": [{"owner": ALICE, "type": "TECHNICAL_OWNER"}]}
    graph.aspects[(DS, "glossaryTerms")] = {"terms": [{"urn": "urn:li:glossaryTerm:Classification.PII.Email"}]}
    graph.aspects[(DS, "domains")] = {"domains": ["urn:li:domain:finance"]}
    graph.aspects[(BOB, "structuredProperties")] = {"properties": [
        {"propertyUrn": "urn:li:structuredProperty:io.cairn.employment_type", "values": [{"string": "contractor"}]}]}
    graph.aspects[(CAROL, "groupMembership")] = {"groups": [STEWARDS]}
    graph.aspects[(CAROL, "corpUserInfo")] = {"departmentName": "finance"}


def _pdp(graph, mode="authoritative"):
    pdp = PolicyDecisionPoint(AttributeResolver(graph), mode=mode, root_users=["urn:li:corpuser:admin"])
    bundle, rbac = load_bundle_from_dir(POLICY_DIR)
    pdp.set_rbac(rbac)
    pdp.set_bundle(bundle)
    return pdp


def _req(actor, priv, urn=DS, **kw):
    return AuthorizeRequest(actor=actor, privilege=priv, resource=ResourceRef(type="dataset", urn=urn) if urn else None, **kw)


# --------------------------------------------------------------------------- ABAC core
def test_operators():
    ctx = {"subject": {"urn": ALICE, "groups": ["g1"], "attributes": {"clearance": 4, "dept": "fin"}},
           "resource": {"tags": ["urn:li:tag:Gold"], "owners": [ALICE], "structured_properties": {"io.cairn.department": ["fin"]}},
           "environment": {"ip": "10.1.2.3", "time": "2026-10-25T22:30:00+00:00"}}
    L = lambda attr, op, value=None: evaluate_leaf({"attr": attr, "op": op, "value": value}, ctx)  # noqa: E731
    assert L("subject.attributes.clearance", "gte", 3)
    assert not L("subject.attributes.clearance", "lt", 3)
    assert L("resource.tags", "contains", "urn:li:tag:gold")  # case-insensitive
    assert L("resource.owners", "contains", {"ref": "subject.urn"})
    assert L("resource.structured_properties.io.cairn.department", "contains", {"ref": "subject.attributes.dept"})
    assert L("environment.ip", "cidr", ["10.0.0.0/8"])
    assert not L("environment.ip", "cidr", ["192.168.0.0/16"])
    assert L("environment.time", "time_between", ["22:00", "06:00"])  # wraps midnight
    assert L("resource.missing", "not_exists") and not L("resource.missing", "eq", "x")
    assert L("resource.missing", "neq", "x")
    assert L("subject.urn", "matches", r"urn:li:corpuser:a.*")


def test_condition_validation_rejects_unsafe():
    with pytest.raises(ValueError):
        validate_condition({"attr": "x", "op": "matches", "value": "(a+)+$"})
    with pytest.raises(ValueError):
        validate_condition({"attr": "x", "op": "bogus"})
    with pytest.raises(ValueError):
        validate_condition({"all": []})
    with pytest.raises(ValueError):
        PolicyBundle(policies=[Policy(id="a", effect="allow", actions=["*"]), Policy(id="a", effect="deny", actions=["*"])])


# --------------------------------------------------------------------------- PDP
def test_deny_overrides_rbac_and_owner_allow(graph):
    _seed(graph)
    pdp = _pdp(graph)
    # Bob is a contractor and an owner via explicit grant → still denied profile on PII.
    graph.aspects[(DS, "ownership")]["owners"].append({"owner": BOB})
    pdp.resolver.invalidate(DS)
    d = pdp.decide(_req(BOB, "VIEW_DATASET_PROFILE"))
    assert d.decision == "DENY" and "deny-pii-profile-to-contractors" in d.policy_ids
    # Bob may still edit docs as owner.
    assert pdp.decide(_req(BOB, "EDIT_ENTITY_DOCS")).decision == "ALLOW"


def test_owner_allow_and_default_deny(graph):
    _seed(graph)
    pdp = _pdp(graph)
    assert pdp.decide(_req(ALICE, "EDIT_ENTITY_TAGS")).decision == "ALLOW"
    d = pdp.decide(_req("urn:li:corpuser:mallory", "EDIT_ENTITY_TAGS"))
    assert d.decision == "DENY" and "default deny" in d.reason
    additive = _pdp(graph, "additive")
    assert additive.decide(_req("urn:li:corpuser:mallory", "EDIT_ENTITY_TAGS")).decision == "NOT_APPLICABLE"


def test_rbac_binding_and_obligations(graph):
    _seed(graph)
    pdp = _pdp(graph)
    d = pdp.decide(_req(CAROL, "EDIT_ENTITY_GLOSSARY_TERMS"))
    assert d.decision == "ALLOW" and any(p.startswith("rbac:") for p in d.policy_ids)
    # Non-steward viewer gets a mask obligation; steward does not.
    graph.aspects[("urn:li:corpuser:dan", "roleMembership")] = {"roles": ["urn:li:dataHubRole:Reader"]}
    viewer = pdp.decide(_req("urn:li:corpuser:dan", "VIEW_ENTITY_PAGE"))
    assert viewer.decision == "ALLOW" and viewer.obligations[0].type == "mask_columns"
    steward = pdp.decide(_req(CAROL, "VIEW_ENTITY_PAGE"))
    assert steward.decision == "ALLOW" and not steward.obligations


def test_clearance_and_environment(graph):
    _seed(graph)
    graph.aspects[(DS, "domains")] = {"domains": ["urn:li:domain:restricted"]}
    pdp = _pdp(graph)
    assert pdp.decide(_req(ALICE, "EDIT_ENTITY_DOCS")).decision == "DENY"
    ok = pdp.decide(_req(ALICE, "EDIT_ENTITY_DOCS", subject_attributes={"clearance": 4}))
    assert ok.decision == "ALLOW"
    graph.aspects[("urn:li:corpuser:eve", "groupMembership")] = {"groups": ["urn:li:corpGroup:break-glass"]}
    graph.aspects[(DS, "ownership")]["owners"].append({"owner": "urn:li:corpuser:eve"})
    pdp2 = _pdp(graph)
    graph.aspects[(DS, "domains")] = {"domains": []}
    pdp2.resolver.invalidate(DS)
    assert pdp2.decide(_req("urn:li:corpuser:eve", "EDIT_ENTITY_DOCS", environment={"ip": "8.8.8.8"})).decision == "DENY"
    assert pdp2.decide(_req("urn:li:corpuser:eve", "EDIT_ENTITY_DOCS", environment={"ip": "10.2.3.4"})).decision == "ALLOW"


def test_root_and_explain_and_cache_invalidation(graph):
    _seed(graph)
    pdp = _pdp(graph)
    assert pdp.decide(_req("urn:li:corpuser:admin", "MANAGE_POLICIES", None)).decision == "ALLOW"
    explained = pdp.decide(_req(ALICE, "EDIT_ENTITY_TAGS"), explain=True)
    assert explained.trace and any(t.policy_id == "owners-edit-their-assets" and t.matched for t in explained.trace)
    assert pdp.decide(_req("urn:li:corpuser:zed", "EDIT_ENTITY_TAGS")).decision == "DENY"
    graph.aspects[(DS, "ownership")]["owners"].append({"owner": "urn:li:corpuser:zed"})
    assert pdp.decide(_req("urn:li:corpuser:zed", "EDIT_ENTITY_TAGS")).decision == "DENY"  # cached
    pdp.resolver.invalidate(DS)
    assert pdp.decide(_req("urn:li:corpuser:zed", "EDIT_ENTITY_TAGS")).decision == "ALLOW"


def test_sub_resource_deny(graph):
    _seed(graph)
    pdp = _pdp(graph)
    bundle = PolicyBundle(policies=[*pdp.bundle.policies, Policy(
        id="no-certified-tag-by-hand", effect="deny", priority=50, actions=["EDIT_ENTITY_TAGS"],
        condition={"attr": "resource.urn", "op": "eq", "value": CERTIFIED_TAG})])
    pdp.set_bundle(bundle)
    req = _req(ALICE, "EDIT_ENTITY_TAGS", sub_resources=[ResourceRef(type="tag", urn=CERTIFIED_TAG)])
    d = pdp.decide(req)
    assert d.decision == "DENY" and d.policy_ids == ["no-certified-tag-by-hand"]
    assert pdp.decide(_req(ALICE, "EDIT_ENTITY_TAGS", sub_resources=[ResourceRef(type="tag", urn="urn:li:tag:x")])).decision == "ALLOW"


def test_native_policy_translation(graph):
    _seed(graph)
    info = {"displayName": "Finance editors", "type": "METADATA", "state": "ACTIVE",
            "privileges": ["EDIT_ENTITY_DOCS"], "actors": {"groups": ["urn:li:corpGroup:finance"]},
            "resources": {"allResources": False, "filter": {"criteria": [
                {"field": "DOMAIN", "values": ["urn:li:domain:finance"], "condition": "EQUALS"}]}}}
    p = translate_native_policy("urn:li:dataHubPolicy:fin-edit", info)
    assert p and p.source == "native"
    pdp = PolicyDecisionPoint(AttributeResolver(graph), mode="authoritative")
    graph.aspects[("urn:li:dataHubPolicy:fin-edit", "dataHubPolicyInfo")] = info
    assert pdp.set_native_policies(graph.scroll_entities("dataHubPolicy", ["dataHubPolicyInfo"])) == 1
    graph.aspects[("urn:li:corpuser:frank", "groupMembership")] = {"groups": ["urn:li:corpGroup:finance"]}
    assert pdp.decide(_req("urn:li:corpuser:frank", "EDIT_ENTITY_DOCS")).decision == "ALLOW"
    assert pdp.decide(_req("urn:li:corpuser:frank", "EDIT_ENTITY_DOCS", urn=DS_DOWN)).decision == "DENY"
    assert translate_native_policy("x", {**info, "state": "INACTIVE"}) is None
    owners = translate_native_policy("urn:li:dataHubPolicy:own", {
        "type": "METADATA", "privileges": ["EDIT_ENTITY_TAGS"], "actors": {"resourceOwners": True},
        "resources": {"allResources": True}})
    pdp.set_native_policies([{"urn": "urn:li:dataHubPolicy:own", "dataHubPolicyInfo": {
        "type": "METADATA", "privileges": ["EDIT_ENTITY_TAGS"], "actors": {"resourceOwners": True},
        "resources": {"allResources": True}}}])
    assert owners and pdp.decide(_req(ALICE, "EDIT_ENTITY_TAGS")).decision == "ALLOW"


def test_rbac_inheritance():
    model = RbacModel.model_validate({"roles": [
        {"urn": "urn:li:dataHubRole:A", "privileges": ["P1"], "inherits": ["urn:li:dataHubRole:B"]},
        {"urn": "urn:li:dataHubRole:B", "privileges": ["P2"], "inherits": ["urn:li:dataHubRole:A"]}]})
    assert model.effective_privileges("urn:li:dataHubRole:A") == ["P1", "P2"]  # cycle-safe


def test_deny_is_audited(engine, graph):
    _seed(graph)
    audit = AuditLog(engine)
    pdp = _pdp(graph)
    pdp.audit = audit
    pdp.decide(_req(BOB, "VIEW_DATASET_PROFILE"))
    assert audit.entries("authz")[0]["action"] == "deny"
    assert audit.verify_chain()["valid"]


# --------------------------------------------------------------------------- audit chain
def test_audit_chain_detects_tampering(engine):
    from sqlalchemy import update

    from cairn.common.audit import audit_log

    log = AuditLog(engine)
    for i in range(5):
        log.record("t", f"a{i}", ALICE, None, {"i": i})
    assert log.verify_chain() == {"valid": True, "entries_checked": 5, "head": log.verify_chain()["head"]}
    with engine.begin() as c:
        c.execute(update(audit_log).where(audit_log.c.seq == 3).values(actor="urn:li:corpuser:mallory"))
    assert log.verify_chain()["broken_at_seq"] == 3


# --------------------------------------------------------------------------- PII
def test_classifier():
    email = classify_field("customer_email", ["a@b.com", "c@d.org", "x@y.io"])[0]
    assert email.kind == "Email" and email.action == "apply"
    cc = classify_field("payload", ["4111 1111 1111 1111", "5500-0000-0000-0004", "4012888888881881"])[0]
    assert cc.kind == "CreditCard"
    assert not [c for c in classify_field("order_total", ["12.50", "99.00"]) if c.action != "none"]
    assert classify_field("iban", ["DE89370400440532013000"])[0].confidence > 0.9
    assert classify_field("customerFirstName")[0].kind == "Name"
    # Luhn check rejects random 16-digit numbers
    assert all(c.kind != "CreditCard" for c in classify_field("ref", ["1234567812345678", "1111222233334445"]))


def test_propagation_plan_respects_hops_cycles_and_blocking():
    a, b, c, d, e = (schema_field_urn(DS, f) for f in "abcde")
    edges = {a: [FieldEdge(b, "IDENTITY"), FieldEdge(e, "HASH")], b: [FieldEdge(c, None)], c: [FieldEdge(a, None), FieldEdge(d, None)]}
    plan = plan_propagation([(a, pii_term("Email"))], lambda f: edges.get(f, []), max_hops=2)
    targets = [s.target_field for s in plan]
    assert targets == [b, c]  # e blocked by HASH, d beyond 2 hops, a is a cycle back to origin
    assert plan[1].path == [a, b, c]


def _lineage_graph(graph):
    src_email = schema_field_urn(DS, "email")
    down_email = schema_field_urn(DS_DOWN, "customer_email")
    down2 = schema_field_urn(DS_DOWN2, "email")
    graph.aspects[(DS, "editableSchemaMetadata")] = {"editableSchemaFieldInfo": [
        {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": pii_term("Email")}], "auditStamp": {"time": 0, "actor": ALICE}}}]}
    graph.aspects[(DS_DOWN, "upstreamLineage")] = {"upstreams": [{"dataset": DS}], "fineGrainedLineages": [
        {"upstreamType": "FIELD_SET", "upstreams": [src_email], "downstreamType": "FIELD", "downstreams": [down_email]}]}
    graph.aspects[(DS_DOWN2, "upstreamLineage")] = {"upstreams": [{"dataset": DS_DOWN}], "fineGrainedLineages": [
        {"upstreamType": "FIELD_SET", "upstreams": [down_email], "downstreamType": "FIELD", "downstreams": [down2]}]}
    downstream = {DS: [DS_DOWN], DS_DOWN: [DS_DOWN2], DS_DOWN2: []}
    graph.graphql_handler = lambda q, v: {"entity": {"relationships": {"relationships": [
        {"entity": {"urn": u}} for u in downstream.get(v["urn"], [])]}}}
    return src_email, down_email, down2


def test_propagator_end_to_end_and_revert(graph):
    src, down, down2 = _lineage_graph(graph)
    prop = PiiPropagator(graph, PropagationConfig())
    mcl = {"entityUrn": DS, "aspectName": "editableSchemaMetadata", "systemMetadata": {"properties": {}}}
    mcps = prop.handle(mcl)
    assert {m["entityUrn"] for m in mcps} == {DS_DOWN, DS_DOWN2}
    esm = graph.get_aspect(DS_DOWN2, "editableSchemaMetadata")
    term = esm["editableSchemaFieldInfo"][0]["glossaryTerms"]["terms"][0]
    assert term["urn"] == pii_term("Email") and term["attribution"]["source"] == PROPAGATION_SOURCE
    assert term["attribution"]["sourceDetail"]["origin"] == src and term["attribution"]["sourceDetail"]["hops"] == "2"
    # Our own writes are ignored (loop safety) and re-running is idempotent.
    assert prop.handle({**mcl, "systemMetadata": {"properties": {"cairn_propagated": "true"}}}) == []
    assert prop.handle(mcl) == []
    # Removing the origin term reverts every propagated copy.
    prev = graph.get_aspect(DS, "editableSchemaMetadata")
    import json
    graph.aspects[(DS, "editableSchemaMetadata")] = {"editableSchemaFieldInfo": [{"fieldPath": "email"}]}
    prop.handle({**mcl, "previousAspectValue": {"value": json.dumps(prev)},
                 "aspect": {"value": json.dumps(graph.aspects[(DS, "editableSchemaMetadata")])}})
    for ds in (DS_DOWN, DS_DOWN2):
        terms = graph.get_aspect(ds, "editableSchemaMetadata")["editableSchemaFieldInfo"][0]["glossaryTerms"]["terms"]
        assert terms == []


def test_apply_terms_preserves_manual_terms(graph):
    f = schema_field_urn(DS, "phone")
    graph.aspects[(DS, "editableSchemaMetadata")] = {"editableSchemaFieldInfo": [
        {"fieldPath": "phone", "description": "manual", "glossaryTerms": {"terms": [{"urn": "urn:li:glossaryTerm:Contact"}]}}]}
    mcps = apply_terms_mcps(graph, [(f, pii_term("Phone"), PROPAGATION_SOURCE, {"origin": "x"})])
    assert [m["changeType"] for m in mcps] == ["PATCH"]  # atomic in Cairn Core: no lost updates between pipelines
    graph.emit(mcps)
    info = graph.get_aspect(DS, "editableSchemaMetadata")["editableSchemaFieldInfo"][0]
    assert info["description"] == "manual" and [t["urn"] for t in info["glossaryTerms"]["terms"]] == [
        "urn:li:glossaryTerm:Contact", pii_term("Phone")]
    assert revert_propagation_mcps(graph, [DS], "other-origin", pii_term("Phone")) == []


# --------------------------------------------------------------------------- certification workflows
def _workflows(engine, graph, eligible=None, on_doc=None):
    templates = load_templates(os.path.join(os.path.dirname(__file__), "..", "config", "workflows"))
    eligible = eligible or (lambda actor, wf, t: actor in (CAROL, "urn:li:corpuser:dave"))
    return WorkflowService(engine, graph, templates, EntryChecks(graph), eligible, AuditLog(engine), on_doc)


def _documented(graph):
    graph.aspects[(DS, "ownership")] = {"owners": [{"owner": ALICE}]}
    graph.aspects[(DS, "editableDatasetProperties")] = {"description": "All customer orders, one row per order line."}
    graph.aspects[(DS, "schemaMetadata")] = {
        "fields": [{"fieldPath": "id", "description": "pk"}, {"fieldPath": "email", "description": "e"}]}


def test_certification_happy_path_and_projection(engine, graph):
    _documented(graph)
    graph.aspects[(DS, "globalTags")] = {"tags": [{"tag": "urn:li:tag:Gold"}]}
    svc = _workflows(engine, graph)
    wf = svc.create(CreateWorkflowRequest(entity_urn=DS), ALICE)
    assert wf.state == "SUBMITTED" and all(c.passed for c in wf.checks)
    with pytest.raises(WorkflowError) as e:
        svc.act(wf.id, "approve", ALICE)  # separation of duties
    assert e.value.status == 403
    with pytest.raises(WorkflowError):
        svc.act(wf.id, "approve", "urn:li:corpuser:random")
    wf = svc.act(wf.id, "approve", CAROL, "looks good")
    assert wf.state == "CERTIFIED" and wf.valid_until
    props = {p["propertyUrn"]: p["values"][0]["string"] for p in graph.get_aspect(DS, "structuredProperties")["properties"]}
    assert props[CERT_STATUS_PROP] == "CERTIFIED"
    tags = [t["tag"] for t in graph.get_aspect(DS, "globalTags")["tags"]]
    assert tags == ["urn:li:tag:Gold", CERTIFIED_TAG]
    assert [h.to_state for h in wf.history] == ["DRAFT", "SUBMITTED", "IN_REVIEW", "APPROVED", "CERTIFIED"]
    # duplicate open request is prevented only while open; revoke works
    wf = svc.act(wf.id, "revoke", CAROL, "source deprecated")
    assert wf.state == "REVOKED"
    assert [t["tag"] for t in graph.get_aspect(DS, "globalTags")["tags"]] == ["urn:li:tag:Gold"]


def test_quorum_checks_and_veto(engine, graph):
    graph.aspects[(DS, "ownership")] = {"owners": []}
    svc = _workflows(engine, graph)
    wf = svc.create(CreateWorkflowRequest(entity_urn=DS, template="regulated-certification"), ALICE)
    assert not all(c.passed for c in wf.checks)
    wf = svc.act(wf.id, "approve", CAROL)
    assert wf.state == "IN_REVIEW"
    with pytest.raises(WorkflowError):
        svc.act(wf.id, "approve", CAROL)  # same reviewer twice
    wf = svc.act(wf.id, "approve", "urn:li:corpuser:dave")
    assert wf.state == "APPROVED"  # quorum reached but checks fail → not certified
    _documented(graph)
    wf = svc.act(wf.id, "recheck", CAROL)
    assert wf.state == "CERTIFIED"
    wf2 = svc.create(CreateWorkflowRequest(entity_urn=DS_DOWN), ALICE)
    assert svc.act(wf2.id, "reject", CAROL, "no").state == "REJECTED"


def test_expiry_and_auto_revoke(engine, graph):
    _documented(graph)
    svc = _workflows(engine, graph)
    wf = svc.act(svc.create(CreateWorkflowRequest(entity_urn=DS), ALICE).id, "approve", CAROL)
    assert svc.expire_due(datetime.now(timezone.utc)) == 0
    assert svc.expire_due(datetime.now(timezone.utc) + timedelta(days=181)) == 1
    assert svc.get(wf.id).state == "EXPIRED"
    wf = svc.act(wf.id, "submit", ALICE)
    wf = svc.act(wf.id, "approve", CAROL)
    assert wf.state == "CERTIFIED"
    assert svc.on_quality_failure(DS, "HIGH", "x") == 0
    assert svc.on_quality_failure(DS, "CRITICAL", "orders volume dropped 95%") == 1
    assert svc.get(wf.id).state == "REVOKED"


def test_optimistic_concurrency(engine, graph):
    _documented(graph)
    svc = _workflows(engine, graph)
    wf = svc.create(CreateWorkflowRequest(entity_urn=DS), ALICE)
    stale = svc.get(wf.id)
    svc.act(wf.id, "approve", CAROL)
    with pytest.raises(WorkflowError):
        svc._save(stale, 1)


def test_bootstrap_mcps_shapes():
    mcps = bootstrap_mcps()
    names = {m["aspectName"] for m in mcps}
    assert names == {"propertyDefinition", "tagProperties", "glossaryTermInfo"}
    status = next(aspect_value(m) for m in mcps if m["entityUrn"] == CERT_STATUS_PROP)
    assert status["valueType"] == "urn:li:dataType:datahub.string"


def test_rule_propagation_tags_and_terms_with_platform_filter_and_revert(graph):
    import json

    from cairn.governance.propagation_rules import PropagationRule, RulePropagator, RulesConfig

    src, down, down2 = _lineage_graph(graph)
    esm = graph.aspects[(DS, "editableSchemaMetadata")]
    esm["editableSchemaFieldInfo"][0]["globalTags"] = {"tags": [{"tag": "urn:li:tag:gdpr-subject"}]}
    prop = RulePropagator(graph, RulesConfig(rules=[
        PropagationRule(id="gdpr", labels=["urn:li:tag:gdpr*"]),
        PropagationRule(id="pii-snowflake", labels=["urn:li:glossaryTerm:Classification.PII.*"],
                        target_platforms=["snowflake"]),
    ]))
    mcl = {"entityUrn": DS, "aspectName": "editableSchemaMetadata", "systemMetadata": {"properties": {}}}
    prop.handle(mcl)
    down_info = graph.get_aspect(DS_DOWN, "editableSchemaMetadata")["editableSchemaFieldInfo"][0]
    down2_info = graph.get_aspect(DS_DOWN2, "editableSchemaMetadata")["editableSchemaFieldInfo"][0]
    assert down_info["globalTags"]["tags"][0]["tag"] == "urn:li:tag:gdpr-subject"
    assert down_info["globalTags"]["tags"][0]["attribution"]["sourceDetail"]["rule"] == "gdpr"
    assert down_info["glossaryTerms"]["terms"][0]["urn"] == pii_term("Email")       # snowflake target
    assert "glossaryTerms" not in down2_info                                        # looker filtered out
    assert down2_info["globalTags"]["tags"][0]["attribution"]["sourceDetail"]["hops"] == "2"
    assert prop.handle(mcl) == []  # idempotent

    with pytest.raises(ValueError):
        PropagationRule(id="bad", labels=["PII"])

    prev = json.loads(json.dumps(graph.get_aspect(DS, "editableSchemaMetadata")))
    graph.aspects[(DS, "editableSchemaMetadata")]["editableSchemaFieldInfo"][0]["globalTags"]["tags"] = []
    cur = graph.get_aspect(DS, "editableSchemaMetadata")
    prop.handle({**mcl, "previousAspectValue": {"value": json.dumps(prev)}, "aspect": {"value": json.dumps(cur)}})
    for ds in (DS_DOWN, DS_DOWN2):
        assert graph.get_aspect(ds, "editableSchemaMetadata")["editableSchemaFieldInfo"][0]["globalTags"]["tags"] == []
    assert graph.get_aspect(DS_DOWN, "editableSchemaMetadata")["editableSchemaFieldInfo"][0]["glossaryTerms"]["terms"]


def test_propagation_survives_stale_lineage_index(graph):
    """Labels written right after new lineage: the graph index does not know the edge yet."""
    import json
    import time

    src, down, down2 = _lineage_graph(graph)
    downstream = {DS: [], DS_DOWN: [], DS_DOWN2: []}            # index not caught up
    graph.graphql_handler = lambda q, v: {"entity": {"relationships": {"relationships": [
        {"entity": {"urn": u}} for u in downstream.get(v["urn"], [])]}}}
    prop = PiiPropagator(graph, PropagationConfig(recheck_seconds=0.2))
    esm_mcl = {"entityUrn": DS, "aspectName": "editableSchemaMetadata", "systemMetadata": {"properties": {}}}
    assert prop.handle(esm_mcl) == []                            # nothing found yet → re-check scheduled
    downstream.update({DS: [DS_DOWN], DS_DOWN: [DS_DOWN2]})      # index catches up
    time.sleep(0.6)
    assert graph.get_aspect(DS_DOWN2, "editableSchemaMetadata"), "re-check should have propagated"

    # The lineage change itself carries the new edge, so it propagates without the index.
    graph.aspects.pop((DS_DOWN, "editableSchemaMetadata"))
    downstream.update({DS: [], DS_DOWN: []})
    lineage = graph.get_aspect(DS_DOWN, "upstreamLineage")
    mcl = {"entityUrn": DS_DOWN, "aspectName": "upstreamLineage", "systemMetadata": {"properties": {}},
           "aspect": {"value": json.dumps(lineage)}}
    assert {m["entityUrn"] for m in PiiPropagator(graph, PropagationConfig(recheck_seconds=0)).handle(mcl)} == {DS_DOWN}


def test_failed_attribute_reads_are_not_cached_for_the_full_ttl(monkeypatch):
    class Flaky:
        def __init__(self):
            self.fail = True
            self.calls = 0

        def get_aspect(self, urn, name):
            self.calls += 1
            if self.fail:
                raise RuntimeError("HTTP 403")
            return {"groups": ["urn:li:corpGroup:data-stewards"]} if name == "groupMembership" else None

    graph = Flaky()
    resolver = AttributeResolver(graph, ttl_seconds=60)
    clock = [1000.0]
    monkeypatch.setattr("cairn.governance.attributes.time.monotonic", lambda: clock[0])
    assert resolver.subject("urn:li:corpuser:bob")["groups"] == []
    graph.fail = False
    clock[0] += 1
    assert resolver.subject("urn:li:corpuser:bob")["groups"] == []        # degraded entry, briefly
    clock[0] += AttributeResolver.DEGRADED_TTL_SECONDS
    assert resolver.subject("urn:li:corpuser:bob")["groups"] == ["urn:li:corpGroup:data-stewards"]
    calls = graph.calls
    clock[0] += 30
    resolver.subject("urn:li:corpuser:bob")
    assert graph.calls == calls                                            # a clean load keeps the full TTL


def test_catalog_definitions_are_created_once(settings, graph):
    import threading

    from cairn.governance.certification import CERT_STATUS_PROP
    from cairn.services import build_services

    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.bootstrap_catalog(threading.Event())
    assert graph.aspects[(CERT_STATUS_PROP, "propertyDefinition")]["allowedValues"]
    emitted = len(graph.emitted)
    s.bootstrap_catalog(threading.Event())                      # already there: nothing written
    assert len(graph.emitted) == emitted
