"""API request and response models."""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tether.policy.tags import ConsentStatus


class SubjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    subject_id: str
    display_name: str | None
    email: str | None
    consent_status: str | None
    license: str | None
    retention_until: str | None
    restrictions: list[str]
    artifact_count: int
    deletion_request_id: str | None
    deletion_status: str | None


class NodeOut(BaseModel):
    id: str
    type: str
    ref: str
    status: str
    subject_ids: list[str]
    policy: dict[str, Any]
    attrs: dict[str, Any]


class EdgeOut(BaseModel):
    source: str
    target: str
    type: str


class LineageOut(BaseModel):
    subject_id: str
    nodes: list[NodeOut]
    edges: list[EdgeOut]


class ArtifactsOut(BaseModel):
    subject_id: str
    counts: dict[str, int]
    artifacts: dict[str, list[NodeOut]]


class ConsentIn(BaseModel):
    """Partial update: only the fields you send change. Send ``null`` to clear
    ``license`` or ``retention_until``."""

    consent_status: ConsentStatus | None = None
    license: str | None = None
    retention_until: date | None = None


class PolicyOut(BaseModel):
    subject_id: str
    consent_status: str
    license: str | None
    retention_until: date | None
    restrictions: list[str]


class DeletionRequestIn(BaseModel):
    reason: str | None = Field(default=None, examples=["GDPR Art. 17 request #4711"])


class CertificateOut(BaseModel):
    certificate_id: str
    request_id: str
    subject_id: str
    issued_at: datetime
    sha256: str
    verified: bool
    content: dict[str, Any]


class DeletionRequestOut(BaseModel):
    id: str
    subject_id: str
    status: str
    reason: str | None
    created: bool = False
    error: dict[str, Any] | None
    history: list[dict[str, Any]]
    inventory: dict[str, Any]
    purged: dict[str, Any]
    flagged: dict[str, Any]
    attempts: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    certificate: CertificateOut | None = None


class RetrainingReason(BaseModel):
    request_id: str | None = None
    subject_id: str | None = None
    snapshots: list[str] = []
    reason: str
    flagged_at: str | None = None


class ModelOut(BaseModel):
    id: str
    name: str
    version: str
    trained_on: list[str]
    status: str
    needs_retraining: bool
    retraining_reasons: list[RetrainingReason]
    created_at: datetime


class AuditEntryOut(BaseModel):
    id: int
    ts: datetime
    event_type: str
    subject_id: str | None
    entity_id: str | None
    payload: dict[str, Any]
    prev_hash: str
    hash: str


class AuditLogOut(BaseModel):
    entries: list[AuditEntryOut]
    chain_ok: bool
    chain_entries: int
    first_broken_id: int | None


class RagQueryIn(BaseModel):
    query: str = Field(min_length=1, examples=["refund for my Quasar laptop"])
    top_k: int = Field(default=5, ge=1, le=50)


class RagHitOut(BaseModel):
    vector_id: str
    collection: str
    chunk_id: str | None
    content: str
    score: float
    subject_ids: list[str]


class RagQueryOut(BaseModel):
    query: str
    results: list[RagHitOut]


class MemoryOut(BaseModel):
    id: str
    kind: str
    content: str
    subject_ids: list[str]
    derived_from: list[str]


class RegenerateOut(BaseModel):
    regenerated: list[str]
