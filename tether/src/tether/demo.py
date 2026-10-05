"""The end-to-end demo scenario (``tether demo`` / ``make demo``).

seed -> pipelines -> RAG query -> delete customer X -> watch it go through every state
-> check that nothing references X -> verify the certificate -> confirm a re-run is a no-op.

There are two modes:

* ``--api-url URL`` drives the running API and worker over HTTP (what ``make demo`` does)
* ``--in-process`` calls the services directly and drains the job queue in this process
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from alembic import command
from sqlalchemy import select

from tether.db.models import Customer, DatasetSnapshotRow, MemoryEntryRow, ModelVersionRow
from tether.db.session import session_scope
from tether.deletion import service, worker
from tether.deletion.certificate import verify_certificate
from tether.deletion.verify import residual_references
from tether.embeddings import get_embedder
from tether.pipelines import rag
from tether.pipelines.run import run_pipelines
from tether.pipelines.seed import seed

DEFAULT_SUBJECT = "cust-007"


class DemoFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise DemoFailure(message)
    print(f"   [ok] {message}")


def step(n: int, title: str) -> None:
    print(f"\n== {n}. {title}")


class Backend(Protocol):
    def rag_query(self, query: str, top_k: int) -> list[dict[str, Any]]: ...
    def submit(self, subject_id: str, reason: str) -> dict[str, Any]: ...
    def status(self, request_id: str) -> dict[str, Any]: ...
    def certificate(self, request_id: str) -> dict[str, Any]: ...
    def models(self) -> list[dict[str, Any]]: ...
    def advance(self) -> None: ...


@dataclass
class HttpBackend:
    base_url: str
    client: httpx.Client = field(init=False)

    def __post_init__(self) -> None:
        self.client = httpx.Client(base_url=self.base_url, timeout=30)

    def rag_query(self, query: str, top_k: int) -> list[dict[str, Any]]:
        resp = self.client.post("/rag/query", json={"query": query, "top_k": top_k})
        resp.raise_for_status()
        return list(resp.json()["results"])

    def submit(self, subject_id: str, reason: str) -> dict[str, Any]:
        resp = self.client.post(
            f"/subjects/{subject_id}/deletion-requests", json={"reason": reason}
        )
        resp.raise_for_status()
        return dict(resp.json())

    def status(self, request_id: str) -> dict[str, Any]:
        resp = self.client.get(f"/deletion-requests/{request_id}")
        resp.raise_for_status()
        return dict(resp.json())

    def certificate(self, request_id: str) -> dict[str, Any]:
        resp = self.client.get(f"/deletion-requests/{request_id}/certificate")
        resp.raise_for_status()
        return dict(resp.json())

    def models(self) -> list[dict[str, Any]]:
        resp = self.client.get("/models")
        resp.raise_for_status()
        return list(resp.json())

    def advance(self) -> None:
        time.sleep(0.5)  # the worker container does the work


class InProcessBackend:
    def __init__(self) -> None:
        self.embedder = get_embedder()

    def rag_query(self, query: str, top_k: int) -> list[dict[str, Any]]:
        with session_scope() as s:
            return [
                {"vector_id": h.id, "content": h.content, "subject_ids": h.subject_ids}
                for h in rag.query(s, self.embedder, query, top_k)
            ]

    def submit(self, subject_id: str, reason: str) -> dict[str, Any]:
        with session_scope() as s:
            result = service.submit(s, subject_id, reason)
            return {
                "id": result.request.id,
                "status": result.request.status,
                "created": result.created,
            }

    def status(self, request_id: str) -> dict[str, Any]:
        with session_scope() as s:
            r = service.get_request(s, request_id)
            return {"id": r.id, "status": r.status, "error": r.error, "history": r.history}

    def certificate(self, request_id: str) -> dict[str, Any]:
        with session_scope() as s:
            c = service.get_certificate(s, request_id)
            if c is None:
                raise DemoFailure("certificate missing")
            return {"content": c.content, "sha256": c.sha256}

    def models(self) -> list[dict[str, Any]]:
        with session_scope() as s:
            return [
                {"id": m.id, "needs_retraining": m.status == "needs_retraining"}
                for m in s.scalars(select(ModelVersionRow))
            ]

    def advance(self) -> None:
        worker.run_once()


def reset_database() -> None:
    from tether.cli import alembic_config

    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def run_demo(
    backend: Backend,
    subject_id: str = DEFAULT_SUBJECT,
    *,
    n_customers: int = 50,
    n_tickets: int = 500,
    reset: bool = True,
    out_dir: Path | None = Path("certificates"),
    timeout: float = 120.0,
) -> dict[str, Any]:
    step(1, "Reset database and seed demo source data")
    if reset:
        reset_database()
    with session_scope() as s:
        seeded = seed(s, n_customers=n_customers, n_tickets=n_tickets)
    print(f"   {seeded.customers} customers, {seeded.tickets} support tickets")

    step(2, "Run AI pipelines (RAG ingest, training snapshots, agent memory) via the SDK")
    with session_scope() as s:
        res = run_pipelines(s, get_embedder())
    print(
        f"   RAG: {res.rag.records} tickets -> {res.rag.chunks} chunks -> {res.rag.vectors} vectors"
    )
    print(f"   training: snapshots {list(res.training.snapshots.values())}")
    print(f"   models: {res.training.models}")
    print(f"   memory: {res.memory.summaries} summaries, {res.memory.digests} digests")

    with session_scope() as s:
        customer = s.get(Customer, subject_id)
        if customer is None:
            raise DemoFailure(f"{subject_id} not in seed data")
        name, email, phone = customer.name, customer.email, customer.phone
        snaps = {x.id for x in s.scalars(select(DatasetSnapshotRow)) if subject_id in x.subject_ids}
        expected_models = {
            m.id for m in s.scalars(select(ModelVersionRow)) if set(m.trained_on) & snaps
        }
        shared_memory = {
            m.id
            for m in s.scalars(select(MemoryEntryRow))
            if subject_id in m.subject_ids and len(m.subject_ids) > 1
        }
    query = f"{name} {email} {phone}"

    step(3, f"RAG query about customer X = {subject_id} ({name})")
    hits = backend.rag_query(query, 5)
    for h in hits[:3]:
        print(f"   - {h['subject_ids']} {h['content'][:90]}...")
    check(any(subject_id in h["subject_ids"] for h in hits), f"RAG returns {subject_id}'s data")

    step(4, f"Submit deletion request for {subject_id}")
    req = backend.submit(subject_id, "GDPR Art. 17 erasure request (demo)")
    print(f"   request {req['id']} -> status {req['status']}")
    check(req["status"] != "received", "retrieval blocked on submit")
    window_hits = backend.rag_query(query, 20)
    check(
        all(subject_id not in h["subject_ids"] for h in window_hits),
        "no X content is served in the window before the purge",
    )

    step(5, "Watch the deletion job")
    seen: list[str] = []
    deadline = time.monotonic() + timeout
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = backend.status(req["id"])
        if not seen or seen[-1] != status["status"]:
            seen.append(status["status"])
            print(f"   status: {status['status']}")
        if status["status"] in {"certified", "failed"}:
            break
        backend.advance()
    check(status.get("status") == "certified", f"request certified (error: {status.get('error')})")
    history = [h["status"] for h in status.get("history", [])]
    print(f"   full history: {' -> '.join(history)}")

    step(6, "Verify that nothing references X")
    final_hits = backend.rag_query(query, 50)
    check(all(subject_id not in h["subject_ids"] for h in final_hits), "RAG never returns X")
    with session_scope() as s:
        residual = residual_references(s, subject_id, [email, phone])
    print(f"   residual references: {residual}")
    check(all(v == 0 for v in residual.values()), "zero source rows, chunks, vectors, memory for X")
    flagged = {m["id"] for m in backend.models() if m["needs_retraining"]}
    check(
        flagged == expected_models,
        f"exactly these models flagged for retraining: {sorted(flagged)}",
    )
    cert = backend.certificate(req["id"])
    regen = set(cert["content"]["memory_needing_regeneration"])
    check(
        regen == {f"memory:{m}" for m in shared_memory},
        f"{len(regen)} multi-subject memory entries removed and marked needs_regeneration",
    )

    step(7, "Verify the deletion certificate")
    check(verify_certificate(cert["content"], cert["sha256"]), f"sha256 {cert['sha256']} verifies")
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"deletion-certificate-{subject_id}.json"
        path.write_text(
            json.dumps({"certificate": cert["content"], "sha256": cert["sha256"]}, indent=2)
        )
        print(f"   saved {path} (verify with: tether verify-certificate {path})")

    step(8, "Re-run the deletion request (must be a no-op)")
    again = backend.submit(subject_id, "duplicate request")
    check(again["id"] == req["id"], "same request returned")
    check(backend.certificate(req["id"])["sha256"] == cert["sha256"], "same certificate returned")

    counts = cert["content"]["purged"]["counts"]
    print(f"\nDONE. Purged {counts}; models flagged: {sorted(flagged)}.")
    return {"request_id": req["id"], "sha256": cert["sha256"], "history": history}
