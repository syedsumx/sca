"""MCL-driven invalidation of the PDP's attribute and decision caches."""

from __future__ import annotations

import json

from cairn.common.core import InMemoryGraph
from cairn.governance.abac import Policy, PolicyBundle
from cairn.governance.attributes import AttributeResolver
from cairn.governance.invalidation import MclInvalidator
from cairn.governance.pdp import AuthorizeRequest, PolicyDecisionPoint, ResourceRef

DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.customers,PROD)"
BOB = "urn:li:corpuser:bob"
PII = "urn:li:tag:pii"


def _pdp(graph):
    resolver = AttributeResolver(graph, ttl_seconds=3600)
    pdp = PolicyDecisionPoint(resolver, mode="authoritative", cache_ttl_seconds=3600)
    pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="view-all", effect="allow", actions=["VIEW_ENTITY_PAGE"]),
        Policy(id="deny-pii", effect="deny", actions=["VIEW_ENTITY_PAGE"],
               condition={"attr": "resource.tags", "op": "contains", "value": PII}),
    ]))
    return pdp, resolver


def _view(pdp):
    return pdp.decide(AuthorizeRequest(actor=BOB, privilege="VIEW_ENTITY_PAGE",
                                       resource=ResourceRef(type="dataset", urn=DS))).decision


def test_tag_change_takes_effect_on_the_next_decision():
    graph = InMemoryGraph()
    pdp, resolver = _pdp(graph)
    assert _view(pdp) == "ALLOW"
    graph.aspects[(DS, "globalTags")] = {"tags": [{"tag": PII}]}
    assert _view(pdp) == "ALLOW"                                    # cached for an hour without an event
    inv = MclInvalidator(resolver, on_policy_change=lambda: None)
    assert inv.handle({"entityUrn": DS, "aspectName": "globalTags", "changeType": "UPSERT"})
    assert _view(pdp) == "DENY"


def test_policy_change_triggers_reimport_and_irrelevant_aspects_are_ignored():
    calls = []
    inv = MclInvalidator(AttributeResolver(None), on_policy_change=lambda: calls.append(1))
    assert inv.handle({"entityUrn": "urn:li:dataHubPolicy:7", "aspectName": "dataHubPolicyInfo"})
    assert calls == [1]
    assert not inv.handle({"entityUrn": DS, "aspectName": "datasetProfile"})
    assert not inv.handle({"aspectName": "globalTags"})
    assert inv.handled == 1


def test_group_membership_change_invalidates_the_user():
    resolver = AttributeResolver(None)
    inv = MclInvalidator(resolver, on_policy_change=lambda: None)
    before = resolver.version(BOB)
    inv.handle({"entityUrn": BOB, "aspectName": "groupMembership"})
    assert resolver.version(BOB) == before + 1


def test_data_product_membership_invalidates_added_and_removed_assets():
    resolver = AttributeResolver(None)
    inv = MclInvalidator(resolver, on_policy_change=lambda: None)
    other = DS.replace("customers", "orders")
    event = {"entityUrn": "urn:li:dataProduct:c360", "aspectName": "dataProductProperties",
             "aspect": {"value": json.dumps({"assets": [{"destinationUrn": DS}]}), "contentType": "application/json"},
             "previousAspectValue": {"value": json.dumps({"assets": [{"destinationUrn": other}]}).encode()}}
    assert inv.handle(event)
    assert resolver.version(DS) == 1 and resolver.version(other) == 1
