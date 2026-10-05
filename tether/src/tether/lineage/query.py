"""Read side of the lineage graph: discovery and subject-centric views."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from tether.db.models import LineageEdge, LineageNode, SubjectArtifact
from tether.lineage.ids import EdgeType, NodeType, node_id

# Walk "downstream" from the subject node: everything derived from it, directly or not.
# Edge semantics: derived_from and trained_on point child -> parent (walk them backwards);
# contained_in points record -> snapshot (walk it forwards). stored_in is a container
# relation, not derivation, so it is not followed.
_DOWNSTREAM_SQL = text(
    """
    WITH RECURSIVE downstream(id) AS (
        SELECT CAST(:start AS varchar)
        UNION
        SELECT CASE WHEN e.edge_type = 'contained_in' THEN e.dst_id ELSE e.src_id END
        FROM lineage_edges e
        JOIN downstream d ON (
            (e.edge_type IN ('derived_from', 'trained_on') AND e.dst_id = d.id)
            OR (e.edge_type = 'contained_in' AND e.src_id = d.id)
        )
    )
    SELECT id FROM downstream
    """
)


def downstream_ids(session: Session, start_id: str) -> set[str]:
    return set(session.scalars(_DOWNSTREAM_SQL, {"start": start_id}))


def subject_artifact_ids(session: Session, subject_id: str) -> set[str]:
    """Every node touching ``subject_id``: the precomputed index plus a graph walk.

    The index makes the lookup fast. The walk also catches artifacts (such as models) that
    reached the subject through a path the index never recorded.
    """
    indexed = set(
        session.scalars(
            select(SubjectArtifact.node_id).where(SubjectArtifact.subject_id == subject_id)
        )
    )
    return indexed | downstream_ids(session, node_id(NodeType.SUBJECT, subject_id))


def load_nodes(session: Session, ids: set[str]) -> list[LineageNode]:
    if not ids:
        return []
    return list(session.scalars(select(LineageNode).where(LineageNode.id.in_(ids))))


@dataclass(frozen=True)
class SubjectGraph:
    nodes: list[LineageNode]
    edges: list[LineageEdge]


def subject_graph(session: Session, subject_id: str) -> SubjectGraph:
    """Every node that touched the subject, the collections holding its vectors, and the
    edges among them."""
    ids = subject_artifact_ids(session, subject_id)
    if ids:
        collection_ids = set(
            session.scalars(
                select(LineageEdge.dst_id).where(
                    LineageEdge.src_id.in_(ids),
                    LineageEdge.edge_type == EdgeType.STORED_IN.value,
                )
            )
        )
        ids |= collection_ids
    nodes = load_nodes(session, ids)
    edges = (
        list(
            session.scalars(
                select(LineageEdge)
                .where(LineageEdge.src_id.in_(ids), LineageEdge.dst_id.in_(ids))
                .order_by(LineageEdge.id)
            )
        )
        if ids
        else []
    )
    return SubjectGraph(nodes=nodes, edges=edges)


def artifacts_by_type(session: Session, subject_id: str) -> dict[str, list[LineageNode]]:
    grouped: dict[str, list[LineageNode]] = defaultdict(list)
    for node in sorted(load_nodes(session, subject_artifact_ids(session, subject_id)), key=_key):
        grouped[node.node_type].append(node)
    return dict(grouped)


def models_trained_on_subject(session: Session, subject_id: str) -> list[LineageNode]:
    """Model versions trained on any snapshot whose manifest contains the subject."""
    snapshots = select(LineageNode.id).where(
        LineageNode.node_type == NodeType.SNAPSHOT.value,
        LineageNode.subject_ids.contains([subject_id]),
    )
    model_ids = select(LineageEdge.src_id).where(
        LineageEdge.edge_type == EdgeType.TRAINED_ON.value,
        LineageEdge.dst_id.in_(snapshots),
    )
    return list(
        session.scalars(
            select(LineageNode).where(
                LineageNode.id.in_(model_ids), LineageNode.node_type == NodeType.MODEL.value
            )
        )
    )


def neighbours(session: Session, ids: set[str]) -> list[LineageEdge]:
    if not ids:
        return []
    return list(
        session.scalars(
            select(LineageEdge).where(or_(LineageEdge.src_id.in_(ids), LineageEdge.dst_id.in_(ids)))
        )
    )


def node_summary(node: LineageNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.node_type,
        "ref": node.ref,
        "status": node.status,
        "subject_ids": list(node.subject_ids),
        "policy": dict(node.policy),
        "attrs": dict(node.attrs),
    }


def _key(node: LineageNode) -> str:
    return node.id
