# Tether — Implementation Plan

Tether tracks personal data from the source table into AI systems (chunks, vectors,
training snapshots, models and agent memory). It can answer "where did subject X's data
go?", and it can carry out a deletion request from start to finish, ending with a signed
(hashed) certificate.

## Deviations and decisions (read first)

| Topic | Decision | Why |
|---|---|---|
| Location | The project lives in `tether/` inside the existing `sca` repository. | The repository already holds an unrelated MIT-licensed project (CodeScope). A subdirectory keeps that project untouched, and `tether/` carries its own Apache-2.0 `LICENSE`. Every command (`make demo`, `docker compose up`) runs from `tether/`. |
| Lineage node ids | Every node has a *typed* id such as `source_record:support_tickets/tkt-0001`, `chunk:<uuid>`, `vector:<collection>/<id>`, `snapshot:<id>`, `model:<name>@<version>`, `memory:<id>` or `subject:<id>`. SDK arguments accept either raw or typed ids. | Ids stay unique across artifact types, and `track_memory_write` can derive from any artifact type. |
| Purged artifacts | Artifact **data** is physically deleted: the source rows, the chunk rows, the vectors and the memory rows. The **lineage node** stays as a content-free tombstone (`status=purged`) that keeps the pseudonymous subject id. | The certificate and the audit trail have to prove *what* was deleted. A tombstone holds no content or PII; it keeps only the opaque subject id. A suppression entry (`subject_restrictions`) also stays, so the subject is never re-ingested. |
| Dataset snapshots | Snapshot manifests are immutable training history and are **not** purged. Deleting a subject flags every model trained on a snapshot that contains it. | This matches the brief: "track per snapshot for training". Retraining is the remedy, and rewriting history is not. |
| Blocking | Retrieval blocking happens **synchronously** inside `POST /deletion-requests`, before the response returns. The rest of the workflow runs on a Postgres-backed job queue (`SELECT … FOR UPDATE SKIP LOCKED`). | The window in which data can leak is zero. |
| License ranking | Licenses rank `public < internal < confidential < restricted`. An unknown license counts as `restricted`. | The "most restrictive policy" rule needs a total order, and an unknown license should fail closed. |
| Audit log | The audit log is hash-chained (`prev_hash` → `hash`). A Postgres trigger rejects `UPDATE` and `DELETE`. | This makes the log append-only and tamper-evident. |
| React Flow | The UI uses `@xyflow/react`, the current package name of React Flow v12. | — |

## Architecture

```
pipelines (RAG / training / memory) ──calls──▶ tether SDK ──writes──▶ lineage_nodes / lineage_edges
         │                                         │                   subject_artifacts (index)
         ▼                                         ▼                   lineage_events (OpenLineage JSON)
 stores (chunks, pgvector, memory)          exporter (noop | HTTP → Marquez)

API (FastAPI) ── services ── policy enforcement (subject_restrictions) ── retrieval
              └─ deletion service ── job queue ── worker ── purgers (per node type) ── certificate + audit log
```

## Package layout

```
tether/
  src/tether/
    config.py            settings (env / .env)
    db/                  engine, session, ORM models
    policy/              PolicyTags + most-restrictive merge, enforcement (blocked subjects)
    lineage/             graph store, queries, OpenLineage events + exporters
    sdk/                 public SDK client (record_source, track_chunks, ...)
    embeddings/          Embedder protocol, FakeEmbedder, SentenceTransformerEmbedder
    stores/              source tables, chunk store, vector store (pgvector), memory store
    pipelines/           seed, RAG, training snapshot, memory summaries
    deletion/            state machine, purgers, certificates, job queue, worker
    audit.py             hash-chained audit log
    api/                 FastAPI app, schemas, routes (thin)
    cli.py               seed / pipelines / worker / demo / verify-certificate
  migrations/            Alembic
  tests/                 pytest (needs Postgres + pgvector)
  ui/                    React + Vite + TS + React Flow
```

## Milestones

1. Scaffold, Docker Compose, schema and migrations, seed data (50 customers, 500 tickets)
2. SDK and lineage store, with unit tests for the propagation and most-restrictive-policy rules
3. Demo pipelines (RAG, training snapshot, memory) wired to the SDK
4. Retrieval policy enforcement for vectors and memory
5. Deletion workflow, purgers, certificates and the append-only audit log
6. API completion
7. UI
8. `make demo`, README, Mermaid architecture diagram, cleanup

## Extension points (non-goals today)

- **Auth / multi-tenancy:** `api/deps.py::get_principal` is a stub that returns an anonymous principal. Tables can gain a `tenant_id` column.
- **Connectors:** `stores/vector.py::VectorStore` is a protocol, so a Qdrant implementation can be added. `deletion/purgers.py` keeps a purger registry keyed by node type. Source tables are registered in `stores/source.py::SOURCE_TABLES`.
- **OpenLineage:** `lineage/openlineage.py::HttpExporter` posts events to Marquez (`TETHER_OPENLINEAGE_URL`).
