"""Records (cairn.governance.records) without Trino: what an erasure does with a table under a legal hold.

The statements are answered by a stand-in for the Trino connection; the end-to-end test on real Trino is
``test_legal_hold_retention_that_deletes_and_erasure_with_signed_proof`` in ``tests/test_trino_e2e.py``."""

from types import SimpleNamespace

import pytest

pytest.importorskip("sqlglot")

from sqlalchemy import insert  # noqa: E402

OFFICER = "urn:li:corpuser:officer"
EVENTS = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.crm.events,PROD)"
HELD = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.crm.held,PROD)"


class FakeTrino:
    """Each table holds the person's rows until a DELETE; a DELETE on a held table is refused (as Trino's OPA does)."""

    def __init__(self, held):
        self.rows = {"lake.crm.events": 2, "lake.crm.held": 3}
        self.held = held
        self.registry = SimpleNamespace(definitions=lambda: {"lake": {"url": "trino://t:8080/lake", "run_as_user": True}})

    def source_for(self, urn):
        name = urn.split(",")[1]
        return SimpleNamespace(connection="lake", database=None), name.split(".")

    def run_on_trino(self, definition, actor, sql, limit):
        table = next(t for t in self.rows if t in sql.replace('"', ""))
        urn = EVENTS if table.endswith("events") else HELD
        if sql.startswith("SELECT count"):
            return [], [[self.rows[table]]], False
        if sql.startswith("DELETE"):
            if self.held(urn):
                raise RuntimeError("Access Denied: a legal hold is on")
            n, self.rows[table] = self.rows[table], 0
            return [], [[n]], False
        return [], [], False                                          # expire_snapshots


@pytest.fixture
def records(tmp_path):
    from cairn.common.db import get_engine, init_db
    from cairn.dataaccess.trino_auth import TrinoTokens
    from cairn.governance.privacy import PrivacyService, privacy_requests
    from cairn.governance.records import Records

    engine = get_engine(f"sqlite:///{tmp_path}/r.db")
    init_db(engine)
    privacy = PrivacyService(engine, lambda: [], salt="s")
    signer = TrinoTokens(engine, "p" * 32, "cairn", 3600, key_name="privacy-proof")
    rec = Records(engine, privacy, None, signer, actor="urn:li:corpuser:svc-privacy")
    rec.data = FakeTrino(rec.held)
    findings = [{"dataset": d, "columns": ["email"], "rows": n, "searched": True} for d, n in ((EVENTS, 2), (HELD, 3))]
    parts = [{"dataset": d, "owners": [], "state": "PENDING", "note": None, "done_by": None} for d in (EVENTS, HELD)]
    with engine.begin() as conn:
        conn.execute(insert(privacy_requests).values(
            id="psr_1", kind="deletion", identifier_kind="email", identifier_hash=privacy._hash("email", "a@x.org"),
            identifier_hint="a***", state="OPEN", created_by=OFFICER, created_ms=1, due_ms=2,
            doc={"findings": findings, "parts": parts}))
    return rec


def test_a_table_kept_by_a_hold_is_erased_once_the_hold_is_released(records):
    rec = records
    hold = rec.place(OFFICER, {"dataset": HELD, "case": "C-1", "reason": "litigation"})
    out = rec.erase("psr_1", "a@x.org", OFFICER)
    assert {x["dataset"]: x["state"] for x in out["results"]} == {EVENTS: "DONE", HELD: "HELD"}
    assert out["request"]["state"] == "COMPLETED"
    first = rec.proof("psr_1")
    assert {t["dataset"]: t["state"] for t in first["report"]["tables"]} == {EVENTS: "DONE", HELD: "HELD"}
    # While the hold is on, running it again changes nothing.
    again = rec.erase("psr_1", "a@x.org", OFFICER)
    assert {x["dataset"]: x["state"] for x in again["results"]} == {HELD: "HELD"}
    assert rec.data.rows["lake.crm.held"] == 3 and rec.proof("psr_1")["jws"] == first["jws"]

    released = rec.release(OFFICER, hold["id"], "case settled")
    assert released["erasures_waiting"] == ["psr_1"]               # the officer is told what to finish
    out = rec.erase("psr_1", "a@x.org", OFFICER)
    assert {x["dataset"]: x["state"] for x in out["results"]} == {HELD: "DONE"}
    assert rec.data.rows["lake.crm.held"] == 0
    part = next(p for p in out["request"]["parts"] if p["dataset"] == HELD)
    assert part["state"] == "DONE" and part["erasure"]["rows_deleted"] == 3 and "holds" not in part
    # The proof is signed again and now says the table was erased.
    proof = rec.proof("psr_1")
    assert proof["jws"] != first["jws"]
    tables = {t["dataset"]: t for t in proof["report"]["tables"]}
    assert tables[HELD]["state"] == "DONE" and tables[HELD]["rows_deleted"] == 3
    assert rec.verify(proof["jws"])["valid"] is True
    # Nothing is left to do: the request is closed to further runs.
    with pytest.raises(Exception) as e:
        rec.erase("psr_1", "a@x.org", OFFICER)
    assert e.value.status == 409


def test_releasing_one_of_two_holds_leaves_the_erasure_waiting(records):
    rec = records
    h1 = rec.place(OFFICER, {"dataset": HELD, "case": "C-1", "reason": "litigation"})
    rec.place(OFFICER, {"dataset": HELD, "case": "C-2", "reason": "audit"})
    rec.erase("psr_1", "a@x.org", OFFICER)
    assert rec.release(OFFICER, h1["id"], "settled")["erasures_waiting"] == []
    out = rec.erase("psr_1", "a@x.org", OFFICER)
    assert {x["dataset"]: x["state"] for x in out["results"]} == {HELD: "HELD"}
    assert rec.data.rows["lake.crm.held"] == 3
