"""Module-level SDK functions backed by a process-wide default :class:`TetherClient`.

Configure once (optional; defaults come from ``TETHER_*`` env vars)::

    import tether
    tether.configure(session_factory=my_sessionmaker)

Then call ``tether.record_source(...)``, ``tether.track_chunks(...)`` and so on.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from tether.lineage.openlineage import LineageExporter
from tether.lineage.store import UnknownArtifactError
from tether.sdk.client import ArtifactRef, LineageInputError, TagsLike, TetherClient

_default: TetherClient | None = None


def configure(
    session_factory: sessionmaker[Session] | None = None,
    exporter: LineageExporter | None = None,
    namespace: str | None = None,
) -> TetherClient:
    """Replace the default client used by the module-level functions."""
    global _default
    _default = TetherClient(session_factory=session_factory, exporter=exporter, namespace=namespace)
    return _default


def get_client() -> TetherClient:
    global _default
    if _default is None:
        _default = TetherClient()
    return _default


def record_source(
    record_id: str,
    subject_ids: Sequence[str],
    tags: TagsLike = None,
    *,
    attrs: Mapping[str, Any] | None = None,
    session: Session | None = None,
) -> ArtifactRef:
    return get_client().record_source(record_id, subject_ids, tags, attrs=attrs, session=session)


def track_chunks(
    source_record_id: str | Sequence[str], chunks: Sequence[str], *, session: Session | None = None
) -> list[ArtifactRef]:
    return get_client().track_chunks(source_record_id, chunks, session=session)


def track_embeddings(
    chunk_ids: Sequence[str | ArtifactRef],
    collection: str,
    vector_ids: Sequence[str],
    *,
    session: Session | None = None,
) -> list[ArtifactRef]:
    return get_client().track_embeddings(chunk_ids, collection, vector_ids, session=session)


def snapshot_dataset(
    name: str,
    record_ids: Sequence[str],
    *,
    snapshot_id: str | None = None,
    exclude_restricted: bool = True,
    session: Session | None = None,
) -> str:
    return get_client().snapshot_dataset(
        name,
        record_ids,
        snapshot_id=snapshot_id,
        exclude_restricted=exclude_restricted,
        session=session,
    )


def register_model(
    name: str,
    version: str,
    trained_on_snapshot_ids: Sequence[str],
    *,
    session: Session | None = None,
) -> ArtifactRef:
    return get_client().register_model(name, version, trained_on_snapshot_ids, session=session)


def track_memory_write(
    memory_id: str,
    derived_from_ids: Sequence[str | ArtifactRef],
    *,
    attrs: Mapping[str, Any] | None = None,
    session: Session | None = None,
) -> ArtifactRef:
    return get_client().track_memory_write(
        memory_id, derived_from_ids, attrs=attrs, session=session
    )


__all__ = [
    "ArtifactRef",
    "LineageInputError",
    "TetherClient",
    "UnknownArtifactError",
    "configure",
    "get_client",
    "record_source",
    "register_model",
    "snapshot_dataset",
    "track_chunks",
    "track_embeddings",
    "track_memory_write",
]
