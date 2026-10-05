"""HTTP API tests (FastAPI TestClient against the real database)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tether.api.app import create_app
from tether.db.models import Customer
from tether.deletion import worker
from tether.deletion.certificate import verify_certificate
from tether.pipelines.run import PipelineResult
from tether.policy.enforcement import is_restricted

SUBJECT = "cust-007"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def _query(session: Session) -> str:
    c = session.get(Customer, SUBJECT)
    assert c is not None
    return f"{c.email} {c.phone}"


def test_health_and_openapi(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    paths = client.get("/openapi.json").json()["paths"]
    for p in [
        "/subjects/{subject_id}/lineage",
        "/subjects/{subject_id}/artifacts",
        "/subjects/{subject_id}/consent",
        "/subjects/{subject_id}/deletion-requests",
        "/deletion-requests/{request_id}",
        "/deletion-requests/{request_id}/certificate",
        "/models",
        "/audit-log",
        "/rag/query",
    ]:
        assert p in paths


def test_subjects_lineage_and_artifacts(client: TestClient, demo_world: PipelineResult) -> None:
    found = client.get("/subjects", params={"q": SUBJECT}).json()
    assert [s["subject_id"] for s in found] == [SUBJECT]
    assert found[0]["artifact_count"] > 0

    graph = client.get(f"/subjects/{SUBJECT}/lineage").json()
    types = {n["type"] for n in graph["nodes"]}
    assert {
        "subject",
        "source_record",
        "chunk",
        "vector",
        "collection",
        "snapshot",
        "model",
        "memory",
    } <= types
    ids = {n["id"] for n in graph["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in graph["edges"])

    arts = client.get(f"/subjects/{SUBJECT}/artifacts").json()
    assert arts["counts"]["vector"] == len(arts["artifacts"]["vector"]) > 0
    assert client.get("/subjects/nobody/lineage").status_code == 404


def test_consent_revocation_via_api(
    client: TestClient, session: Session, demo_world: PipelineResult
) -> None:
    q = {"query": _query(session), "top_k": 10}
    assert any(
        SUBJECT in r["subject_ids"] for r in client.post("/rag/query", json=q).json()["results"]
    )
    resp = client.post(f"/subjects/{SUBJECT}/consent", json={"consent_status": "revoked"})
    assert resp.status_code == 200
    assert resp.json()["restrictions"] == ["consent_revoked"]
    assert all(
        SUBJECT not in r["subject_ids"] for r in client.post("/rag/query", json=q).json()["results"]
    )
    assert client.get("/memory", params={"subject_id": SUBJECT}).json() == []
    # Partial update keeps other fields.
    resp = client.post(f"/subjects/{SUBJECT}/consent", json={"license": "confidential"})
    assert resp.json()["consent_status"] == "revoked"
    assert resp.json()["license"] == "confidential"


def test_deletion_via_api(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    demo_world: PipelineResult,
) -> None:
    q = {"query": _query(session), "top_k": 20}
    session.commit()
    resp = client.post(f"/subjects/{SUBJECT}/deletion-requests", json={"reason": "GDPR"})
    assert resp.status_code == 202
    body = resp.json()
    req_id = body["id"]
    assert body["created"] is True
    assert body["status"] == "retrieval_blocked"
    # Committed before the response returned: a fresh session sees the block.
    with session_factory() as fresh:
        assert is_restricted(fresh, SUBJECT)
    assert all(
        SUBJECT not in r["subject_ids"] for r in client.post("/rag/query", json=q).json()["results"]
    )
    assert client.get(f"/deletion-requests/{req_id}/certificate").status_code == 409

    worker.drain(session_factory)
    status_body = client.get(f"/deletion-requests/{req_id}").json()
    assert status_body["status"] == "certified"
    cert = client.get(f"/deletion-requests/{req_id}/certificate").json()
    assert cert["verified"] is True
    assert verify_certificate(cert["content"], cert["sha256"])
    download = client.get(f"/deletion-requests/{req_id}/certificate", params={"download": True})
    assert "attachment" in download.headers["content-disposition"]
    assert download.json()["sha256"] == cert["sha256"]

    # Idempotent re-submission returns the same certificate.
    again = client.post(f"/subjects/{SUBJECT}/deletion-requests")
    assert again.status_code == 200
    assert again.json()["id"] == req_id
    assert again.json()["certificate"]["sha256"] == cert["sha256"]

    models = {m["id"]: m for m in client.get("/models").json()}
    assert models["support-assistant@1.0"]["needs_retraining"] is True
    assert models["support-assistant@1.1"]["needs_retraining"] is False
    assert models["support-assistant@1.0"]["retraining_reasons"][0]["subject_id"] == SUBJECT

    log = client.get("/audit-log", params={"subject_id": SUBJECT}).json()
    assert log["chain_ok"] is True
    assert "deletion.certified" in {e["event_type"] for e in log["entries"]}

    lineage = client.get(f"/subjects/{SUBJECT}/lineage").json()
    statuses = {n["status"] for n in lineage["nodes"] if n["type"] in {"chunk", "vector"}}
    assert statuses == {"purged"}

    regen = client.post("/memory/regenerate").json()["regenerated"]
    assert regen
    assert client.get("/deletion-requests/does-not-exist").status_code == 404
