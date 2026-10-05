"""Run every demo pipeline in order."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from tether.embeddings.base import Embedder
from tether.pipelines.memory import MemoryResult, build_memory
from tether.pipelines.rag import IngestResult, ingest_tickets, record_customers
from tether.pipelines.training import TrainingResult, run_training
from tether.policy.consent import sync_consent_restrictions


@dataclass(frozen=True)
class PipelineResult:
    customers: int
    rag: IngestResult
    training: TrainingResult
    memory: MemoryResult


def run_pipelines(session: Session, embedder: Embedder) -> PipelineResult:
    sync_consent_restrictions(session)
    customers = record_customers(session)
    rag = ingest_tickets(session, embedder)
    training = run_training(session)
    memory = build_memory(session)
    return PipelineResult(customers, rag, training, memory)
