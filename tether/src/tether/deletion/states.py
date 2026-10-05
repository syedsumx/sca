"""Deletion request lifecycle."""

from enum import StrEnum


class DeletionStatus(StrEnum):
    RECEIVED = "received"
    RETRIEVAL_BLOCKED = "retrieval_blocked"
    DISCOVERED = "discovered"
    PURGING = "purging"
    PURGED = "purged"
    MODELS_FLAGGED = "models_flagged"
    CERTIFIED = "certified"
    FAILED = "failed"


PIPELINE: list[DeletionStatus] = [
    DeletionStatus.RECEIVED,
    DeletionStatus.RETRIEVAL_BLOCKED,
    DeletionStatus.DISCOVERED,
    DeletionStatus.PURGING,
    DeletionStatus.PURGED,
    DeletionStatus.MODELS_FLAGGED,
    DeletionStatus.CERTIFIED,
]

TERMINAL = {DeletionStatus.CERTIFIED}
