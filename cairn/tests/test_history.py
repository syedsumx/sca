"""Metadata change history: changes recorded from the core's change log, shown as field diffs, and
reverted in one click by people who may make that edit, unless the aspect changed again since."""

import json

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.governance.history import HistoryError, diff
from cairn.services import build_services

ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"
DS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.ledger,PROD)"
PII, GOLD = "urn:li:tag:PII", "urn:li:tag:Gold"


def mcl(aspect, after, before=None, actor=ANA, ts=1000, urn=DS):
    blob = lambda v: {"value": json.dumps(v), "contentType": "application/json"} if v is not None else None  # noqa: E731
    return {"entityUrn": urn, "aspectName": aspect, "changeType": "UPSERT", "aspect": blob(after),
            "previousAspectValue": blob(before), "created": {"time": ts, "actor": actor}}


def test_diff_reads_like_the_change():
    before = {"editableSchemaFieldInfo": [{"fieldPath": "email", "description": "Email",
                                           "globalTags": {"tags": [{"tag": GOLD}]}}],
              "created": {"time": 1, "actor": ANA}}
    after = {"editableSchemaFieldInfo": [{"fieldPath": "email", "description": "Email at checkout",
                                          "globalTags": {"tags": [{"tag": GOLD}, {"tag": PII}]}},
                                         {"fieldPath": "id", "description": "Key"}],
             "created": {"time": 2, "actor": BOB}}
    got = diff(before, after)
    assert {"field": "editableSchemaFieldInfo[email].description", "op": "changed", "before": "Email",
            "after": "Email at checkout"} in got
    assert {"field": f"editableSchemaFieldInfo[email].globalTags.tags[{PII}]", "op": "added", "before": None,
            "after": {"tag": PII}} in got
    assert any(c["field"] == "editableSchemaFieldInfo[id]" and c["op"] == "added" for c in got)
    assert len(got) == 3                                                    # audit stamps are not changes
    assert diff({"domains": ["urn:li:domain:a"]}, {"domains": ["urn:li:domain:b"]}) == [
        {"field": "domains", "op": "added", "before": None, "after": "urn:li:domain:b"},
        {"field": "domains", "op": "removed", "before": "urn:li:domain:a", "after": None}]


@pytest.fixture
def s(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"}),
        Policy(id="ana-tags", effect="allow", actions=["EDIT_ENTITY_TAGS"],
               condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    return s


def test_record_list_and_revert(s, graph):
    h = s.history
    v1, v2 = {"tags": [{"tag": GOLD}]}, {"tags": [{"tag": GOLD}, {"tag": PII}]}
    assert h.handle(mcl("globalTags", v1, None, ts=1000))
    assert h.handle(mcl("globalTags", v2, v1, actor=BOB, ts=2000))
    assert not h.handle(mcl("globalTags", v2, v1, actor=BOB, ts=2000))       # the same event again
    assert not h.handle(mcl("datasetProfile", {"rowCount": 1}))               # not a governed aspect
    assert not h.handle(mcl("globalTags", v2, v2, ts=3000))                   # nothing changed
    graph.aspects[(DS, "globalTags")] = v2
    rows = h.list(DS)
    assert [r["actor"] for r in rows] == [BOB, ANA]
    assert rows[0]["summary"] == f"added PII (tags[{PII}])" and rows[0]["revertible"]
    with pytest.raises(HistoryError) as e:
        h.revert(rows[1]["id"], ANA)                                          # changed again since
    assert e.value.status == 409
    back = h.revert(rows[0]["id"], ANA)
    assert graph.aspects[(DS, "globalTags")] == v1 and back["actor"] == ANA and back["revert_of"] == rows[0]["id"]
    assert not h.handle(mcl("globalTags", v1, v2, actor="urn:li:corpuser:cairn", ts=9000))   # Cairn's own write
    rows = h.list(DS)
    assert len(rows) == 3 and rows[0]["actor"] == ANA and rows[1]["reverted_by"] == back["id"]
    with pytest.raises(HistoryError):
        h.revert(rows[1]["id"], ANA)                                          # already reverted
    h.revert(rows[2]["id"], ADMIN, force=True)                                # the first change: back to nothing
    assert graph.aspects[(DS, "globalTags")] == {"tags": []}
    assert h.prune(days=1, now_ms=10 * 86_400_000) == 2                     # the reverts are recent


def test_api(s, graph):
    c = TestClient(create_api_app(s))
    hd = lambda a: {"X-Cairn-Actor": a}  # noqa: E731
    v1, v2 = {"tags": [{"tag": GOLD}]}, {"tags": [{"tag": GOLD}, {"tag": PII}]}
    s.history.handle(mcl("globalTags", v2, v1, actor=BOB, ts=2000))
    s.history.handle(mcl("ownership", {"owners": [{"owner": BOB, "type": "DATAOWNER"}]}, {"owners": []}, ts=2500))
    graph.aspects[(DS, "globalTags")] = v2
    rows = c.get("/history", params={"urn": DS}, headers=hd(BOB)).json()
    assert [r["aspect"] for r in rows] == ["ownership", "globalTags"]
    tags = rows[1]["id"]
    one = c.get(f"/history/{tags}", headers=hd(BOB)).json()
    assert one["before"] == v1 and one["changes"][0]["op"] == "added"
    assert c.post(f"/history/{rows[0]['id']}/revert", headers=hd(ANA)).status_code == 403   # owners: not ana's to edit
    assert c.post(f"/history/{tags}/revert", headers=hd(BOB)).status_code == 403
    r = c.post(f"/history/{tags}/revert", headers=hd(ANA))
    assert r.status_code == 200, r.text
    assert graph.aspects[(DS, "globalTags")] == v1
    assert c.get("/history/recent", headers=hd(BOB)).status_code == 403
    assert len(c.get("/history/recent", headers=hd(ADMIN)).json()) == 3
