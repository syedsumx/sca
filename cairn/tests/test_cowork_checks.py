"""CoWork's outbox, without the services around it: review findings on L95 (the full flow, with Mailpit and a Slack
stand-in, is tests/test_cowork.py)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select

from cairn.ai.cowork import CoWork, CoWorkError, cowork_outbox
from cairn.common.db import init_db

ANA = "urn:li:corpuser:ana"


class Workspace:
    def artifact(self, actor, aid, version=None):
        return {"id": aid, "title": "Weekly orders", "content": "All good.", "version": 1, "sources": []}


@pytest.fixture
def cw(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_COWORK_EMAIL_DOMAINS", "corp.example")
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    return CoWork(engine, Workspace(), None, lambda actor, priv, urn: True)


def test_an_address_cannot_name_a_recipient_outside_the_allowed_domains(cw):
    """Review finding: the local part allowed any character but '@', so ``evil,me@corp.example`` (a comma) or a
    quoted or '%' relay form passed the domain check and put another recipient in the To header."""
    for bad in ("evil,me@corp.example", '"x y"@corp.example', "x%evil.com@corp.example", "a<b>@corp.example",
                "x;y@corp.example"):
        with pytest.raises(CoWorkError) as e:
            cw.propose(ANA, {"artifact_id": "a1", "send": {"email": [bad]}})
        assert e.value.status == 403, bad
    assert cw.propose(ANA, {"artifact_id": "a1", "send": {"email": ["sales-team+weekly@corp.example"]}})["status"] \
        == "pending"


def test_a_message_being_sent_cannot_be_recorded_as_rejected(cw):
    """Review finding: reject checked the status, then overwrote it unconditionally: a reject racing an approval
    marked a message that was sent (or being sent) as rejected."""
    item = cw.propose(ANA, {"artifact_id": "a1", "send": {"email": ["me@corp.example"]}})
    real = cw._item

    def stale(actor, oid):                     # read before the approval claimed it
        d = real(actor, oid)
        with cw.engine.begin() as c:
            c.execute(cowork_outbox.update().where(cowork_outbox.c.id == oid).values(status="sending"))
        return d
    cw._item = stale
    with pytest.raises(CoWorkError) as e:
        cw.reject(ANA, item["id"])
    assert e.value.status == 409
    with cw.engine.connect() as c:
        assert c.execute(select(cowork_outbox.c.status)).scalar_one() == "sending"
