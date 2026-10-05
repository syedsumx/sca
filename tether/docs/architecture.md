# Tether architecture

## Components

```mermaid
flowchart LR
    subgraph Sources["Source systems (Postgres)"]
        C[(customers)]
        T[(support_tickets)]
        P[(policy_tags)]
    end

    subgraph Pipelines["Your AI pipelines"]
        RAG[RAG ingest<br/>chunk → embed]
        TRN[Training<br/>dataset snapshots]
        MEM[Agent memory<br/>summaries / digests]
    end

    subgraph SDK["tether SDK"]
        RS[record_source]
        TC[track_chunks]
        TE[track_embeddings]
        SD[snapshot_dataset]
        RM[register_model]
        TM[track_memory_write]
    end

    subgraph Stores["AI artifact stores"]
        CH[(chunks)]
        VEC[(vectors · pgvector)]
        ME[(memory_entries)]
        DS[(dataset_snapshots)]
        MV[(model_versions)]
    end

    subgraph Lineage["Lineage store"]
        N[(lineage_nodes)]
        E[(lineage_edges)]
        IDX[(subject_artifacts index)]
        EV[(lineage_events<br/>OpenLineage JSON)]
    end

    subgraph Gov["Governance"]
        R[(subject_restrictions<br/>live block list)]
        DR[(deletion_requests)]
        Q[(jobs queue)]
        CERT[(deletion_certificates)]
        AUD[(audit_log<br/>hash-chained, append-only)]
    end

    API[FastAPI] --> Gov
    API --> Lineage
    UI[React UI] --> API
    W[Worker] --> Q
    W --> Stores
    W --> Lineage
    W --> CERT
    W --> AUD

    Sources --> Pipelines
    Pipelines --> Stores
    Pipelines --> SDK --> Lineage
    EV -. optional HttpExporter .-> MQ[Marquez / any OpenLineage backend]
    R -. filters every read .-> VEC
    R -. filters every read .-> ME
```

Everything lives in a single Postgres 16 database with pgvector in the MVP. The
`VectorStore` protocol and the purger registry are the seams where other backends
(Qdrant, Snowflake, S3, ...) plug in.

## Lineage graph model

```mermaid
flowchart LR
    S((Subject)) -->|derived_from⁻¹| SR[SourceRecord]
    SR -->|derived_from⁻¹| CK[Chunk]
    CK -->|derived_from⁻¹| V[VectorEntry]
    V -->|stored_in| COL[Collection]
    SR -->|contained_in| SNAP[DatasetSnapshot]
    SNAP -->|trained_on⁻¹| M[ModelVersion]
    SR -->|derived_from⁻¹| MEMN[MemoryEntry]
    CK -.->|derived_from⁻¹| MEMN
```

Edges are stored child → parent for `derived_from` and `trained_on`, so the diagram
shows them inverted (⁻¹) to read downstream. `contained_in` is stored record →
snapshot and `stored_in` is stored vector → collection.

**Granularity:**

- Chunks, vectors and memory are tracked per record: every artifact is a node with
  its own subject ids.
- Training is tracked per snapshot. The snapshot manifest lists each member record and
  its subjects, but a model version links only to snapshots.

**Propagation:**

- Each node stores `subject_ids`, the union over its parents.
- Each node stores `policy`, the most restrictive combination of its parents' policies:
  - consent: `revoked > pending > granted`
  - license: `restricted > confidential > internal > public`, and an unknown license
    counts as `restricted`
  - retention: the earliest date wins
- Every write also updates the `subject_artifacts` index. "Everything for subject X" is
  one indexed lookup, plus a recursive CTE walk as a cross-check.

## Deletion workflow

```mermaid
sequenceDiagram
    autonumber
    participant Client
    participant API
    participant DB as Postgres
    participant Worker
    Client->>API: POST /subjects/X/deletion-requests
    API->>DB: insert request (received)
    API->>DB: subject_restrictions += X (retrieval_blocked)
    API->>DB: enqueue job
    API-->>Client: 202 {status: retrieval_blocked}
    Note over Client,DB: From here on /rag/query and memory reads exclude X,<br/>even though nothing has been purged yet
    Worker->>DB: claim job (FOR UPDATE SKIP LOCKED)
    Worker->>DB: discover via subject index + graph walk (discovered)
    Worker->>DB: purge memory → vectors → chunks → source rows (purging → purged)
    Note right of Worker: multi-subject memory → deleted + needs_regeneration<br/>nodes become content-free tombstones
    Worker->>DB: flag models trained on snapshots containing X (models_flagged)
    Worker->>DB: certificate JSON + SHA-256 and audit entry (certified)
    Client->>API: GET /deletion-requests/{id}/certificate
```

**Guarantees:**

- **Immediate block.** The block list is applied inside the vector similarity query
  (before `LIMIT`) and inside memory reads, in the same SQL statement. It is written
  and committed before the API responds.
- **Idempotency.**
  - `deletion_requests.subject_id` is unique, so re-submitting returns the existing
    request and certificate.
  - Every step is safe to re-run: purges of missing rows delete 0 rows, and flags and
    restrictions are upserts.
  - The certificate is unique per request.
- **Failure handling.**
  - A failing step moves the request to `failed` with `error.failed_in_status`.
  - The job is retried with exponential backoff. Calling `POST` again or `/retry`
    re-queues it, and processing resumes at the failed step.
  - Retrieval stays blocked throughout.
- **Evidence.**
  - Every state change is logged and appended to the hash-chained audit log.
  - A database trigger rejects `UPDATE`, `DELETE` and `TRUNCATE` on that table.
  - The certificate hash is SHA-256 over canonical JSON (sorted keys, compact
    separators).

## What is (deliberately) not deleted

- **Dataset snapshot manifests.** They are immutable training history. The models
  trained on them are flagged `needs_retraining` instead.
- **Lineage tombstones.** They keep only the pseudonymous subject id and structural
  attributes; content hashes are stripped.
- **`subject_restrictions` row with reason `deleted`.** It acts as a suppression list,
  so pipelines never re-ingest the subject.
