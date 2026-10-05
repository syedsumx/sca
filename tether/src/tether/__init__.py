"""Tether: AI data lineage & deletion tracker.

The public SDK lives at the top level so pipeline code can simply do::

    import tether

    tether.record_source("support_tickets/tkt-1", ["cust-001"], {"consent_status": "granted"})
    chunks = tether.track_chunks("support_tickets/tkt-1", ["first chunk", "second chunk"])

See :mod:`tether.sdk` for details.
"""

from tether.policy.tags import ConsentStatus, PolicyTags
from tether.sdk import (
    ArtifactRef,
    TetherClient,
    configure,
    get_client,
    record_source,
    register_model,
    snapshot_dataset,
    track_chunks,
    track_embeddings,
    track_memory_write,
)

__all__ = [
    "ArtifactRef",
    "ConsentStatus",
    "PolicyTags",
    "TetherClient",
    "configure",
    "get_client",
    "record_source",
    "register_model",
    "snapshot_dataset",
    "track_chunks",
    "track_embeddings",
    "track_memory_write",
]

__version__ = "0.1.0"
