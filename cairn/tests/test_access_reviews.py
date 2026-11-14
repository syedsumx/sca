"""Access reviews (recertification): owners confirm or take away the access people hold, and access nobody
vouches for does not survive the due date. On SQLite and, with CAIRN_TEST_PG_URL, PostgreSQL."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from cairn.common.audit import AuditLog
from cairn.common.db import init_db
from cairn.governance.access_reviews import DAY_MS, AccessReviews, DecisionIn, ReviewError, ReviewIn
from cairn.governance.attributes import AttributeResolver
from cairn.governance.pdp import AuthorizeRequest, PolicyDecisionPoint, ResourceRef
from cairn.governance.personas import PersonaEngine, PersonaSpec

DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,finance.ledger,PROD)"
ANA, BOB, CAROL, DAN, OWNER = (f"urn:li:corpuser:{n}" for n in ("ana", "bob", "carol", "dan", "owner"))


def _allowed(pdp, actor) -> bool:
    return pdp.decide(AuthorizeRequest(actor=actor, privilege="VIEW_ENTITY_PAGE",
                                       resource=ResourceRef(type="dataset", urn=DS))).decision == "ALLOW"


def _check(engine, graph):
    init_db(engine)
    attrs = AttributeResolver(graph)
    pdp = PolicyDecisionPoint(attrs, "authoritative", [], 0)
    audit = AuditLog(engine)
    personas = PersonaEngine(engine, pdp, audit, attrs)
    p = personas.upsert_persona(PersonaSpec(name="Ledger readers", scope={"asset_urns": [DS]}), CAROL)
    clock = {"now": 1_800_000_000_000}
    told = []
    ar = AccessReviews(engine, personas, lambda rid: [OWNER] if rid == "req-1" else [], audit,
                       lambda *a: told.append(a), clock=lambda: clock["now"])
    far = clock["now"] + 90 * DAY_MS
    g_ana = personas.grant(p.id, ANA, "access_request:req-1", far, OWNER)      # reviewed by the product's owner
    personas.grant(p.id, BOB, "manual", far, CAROL)                             # reviewed by the campaign's reviewers
    personas.grant(p.id, CAROL, "manual", far, CAROL)                           # Carol's own: not hers to review
    personas.grant(p.id, DAN, "manual", clock["now"] - 1, CAROL)                # already lapsed: not reviewed
    assert all(_allowed(pdp, u) for u in (ANA, BOB, CAROL))

    with pytest.raises(ReviewError, match="no reviewer"):                      # Carol's grant has nobody else
        ar.start(CAROL, ReviewIn(name="Q4", reviewers=[CAROL]))
    rev = ar.start(CAROL, ReviewIn(name="Q4 ledger access", personas=[p.id], reviewers=[CAROL, DAN], due_days=7))
    items = {i["actor"]: i for i in rev["items"]}
    assert set(items) == {ANA, BOB, CAROL}                                      # the lapsed grant is not there
    assert items[ANA]["reviewers"] == [OWNER] and items[BOB]["reviewers"] == [CAROL, DAN]
    assert items[CAROL]["reviewers"] == [DAN]                                   # separation of duties
    assert {r for t in told for r in t[3]} == {OWNER, CAROL, DAN}               # every reviewer told

    # Reviewers see their items only; nobody else sees the campaign.
    assert {i["actor"] for i in ar.get(OWNER, rev["id"])["items"]} == {ANA}
    with pytest.raises(ReviewError):
        ar.get(BOB, rev["id"])
    with pytest.raises(ReviewError, match="no such item"):
        ar.decide(BOB, rev["id"], items[BOB]["id"], DecisionIn(decision="keep"))       # deciding on his own
    with pytest.raises(ReviewError, match="no such item"):
        ar.decide(CAROL, rev["id"], items[CAROL]["id"], DecisionIn(decision="keep"))   # nor Carol on hers

    kept = ar.decide(OWNER, rev["id"], items[ANA]["id"], DecisionIn(decision="keep", comment="still on the audit"))
    assert kept["decision"] == "keep" and kept["decided_by"] == OWNER and _allowed(pdp, ANA)
    gone = ar.decide(DAN, rev["id"], items[CAROL]["id"], DecisionIn(decision="revoke", comment="moved team"))
    assert gone["decision"] == "revoke" and not _allowed(pdp, CAROL)            # taken away at once
    with pytest.raises(ReviewError, match="already decided"):
        ar.decide(DAN, rev["id"], items[CAROL]["id"], DecisionIn(decision="keep"))

    # Due: Bob's access, which nobody vouched for, is revoked; the review closes.
    clock["now"] += 8 * DAY_MS
    assert ar.run_due() == 1
    done = ar.get(CAROL, rev["id"], admin=True)
    assert done["state"] == "closed" and done["progress"] == {"items": 3, "decided": 3, "kept": 1, "revoked": 2}
    assert not _allowed(pdp, BOB) and _allowed(pdp, ANA)
    with pytest.raises(ReviewError, match="closed"):
        ar.decide(OWNER, rev["id"], items[ANA]["id"], DecisionIn(decision="revoke"))

    # A campaign can keep what nobody answered for instead.
    personas.grant(p.id, BOB, "manual", clock["now"] + 90 * DAY_MS, CAROL)
    soft = ar.start(CAROL, ReviewIn(name="Soft", personas=[p.id], reviewers=[DAN], on_no_response="keep"))
    ar.close(CAROL, soft["id"])
    assert _allowed(pdp, BOB)
    actions = {e["action"] for e in audit.entries("governance", limit=200)}
    assert {"access_review.started", "access_review.keep", "access_review.revoke", "access_review.closed"} <= actions
    assert audit.verify_chain()["valid"]
    assert grant_still(personas, g_ana)


def grant_still(personas, gid) -> bool:
    from sqlalchemy import select

    from cairn.governance.personas import grants_table

    with personas.engine.connect() as c:
        return c.execute(select(grants_table.c.revoked).where(grants_table.c.id == gid)).scalar() is False


def test_access_reviews_on_sqlite(graph, tmp_path):
    _check(create_engine(f"sqlite:///{tmp_path}/ar.db"), graph)


def test_access_reviews_on_postgres(graph):
    url = os.environ.get("CAIRN_TEST_PG_URL")
    if not url:
        pytest.skip("CAIRN_TEST_PG_URL (a PostgreSQL to use) not set")
    from sqlalchemy import text

    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    _check(engine, graph)
