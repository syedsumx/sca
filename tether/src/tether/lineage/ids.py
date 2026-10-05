"""Typed lineage node ids: ``<node_type>:<ref>``."""

from enum import StrEnum


class NodeType(StrEnum):
    SUBJECT = "subject"
    SOURCE_RECORD = "source_record"
    CHUNK = "chunk"
    VECTOR = "vector"
    COLLECTION = "collection"
    SNAPSHOT = "snapshot"
    MODEL = "model"
    MEMORY = "memory"


class EdgeType(StrEnum):
    DERIVED_FROM = "derived_from"
    STORED_IN = "stored_in"
    CONTAINED_IN = "contained_in"
    TRAINED_ON = "trained_on"


_PREFIXES = tuple(f"{t.value}:" for t in NodeType)


def node_id(node_type: NodeType, ref: str) -> str:
    return f"{node_type.value}:{ref}"


def is_typed(value: str) -> bool:
    return value.startswith(_PREFIXES)


def ensure_typed(value: str, default_type: NodeType) -> str:
    """Return ``value`` unchanged if it is already typed, else prefix ``default_type``."""
    return value if is_typed(value) else node_id(default_type, value)


def split(value: str) -> tuple[NodeType, str]:
    prefix, _, ref = value.partition(":")
    return NodeType(prefix), ref


def vector_ref(collection: str, vector_id: str) -> str:
    return f"{collection}/{vector_id}"


def model_ref(name: str, version: str) -> str:
    return f"{name}@{version}"
