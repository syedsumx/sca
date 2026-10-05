"""Write side of the lineage graph.

Every write keeps three structures consistent within the caller's transaction:

* ``lineage_nodes`` and ``lineage_edges``: the graph itself
* ``subject_artifacts``: the precomputed subject -> artifact index used for fast discovery
* ``lineage_events``: the OpenLineage event describing the write
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tether.db.models import LineageEdge, LineageEventRow, LineageNode, SubjectArtifact
from tether.lineage import openlineage
from tether.lineage.ids import EdgeType, NodeType, node_id, split
from tether.policy.tags import PolicyTags


class UnknownArtifactError(LookupError):
    """Raised when an SDK call references an artifact that was never recorded."""

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing = sorted(missing)
        super().__init__(f"unknown lineage artifact(s): {', '.join(self.missing)}")


@dataclass
class NodeSpec:
    node_type: NodeType
    ref: str
    subject_ids: list[str]
    policy: PolicyTags
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return node_id(self.node_type, self.ref)


class LineageStore:
    def __init__(self, session: Session, exporter: openlineage.LineageExporter | None = None):
        self.session = session
        self.exporter = exporter or openlineage.NoopExporter()

    # ------------------------------------------------------------------ reads

    def get_many(self, ids: Iterable[str]) -> dict[str, LineageNode]:
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return {}
        rows = self.session.scalars(select(LineageNode).where(LineageNode.id.in_(wanted)))
        return {r.id: r for r in rows}

    def require(self, ids: Sequence[str]) -> list[LineageNode]:
        found = self.get_many(ids)
        missing = [i for i in ids if i not in found]
        if missing:
            raise UnknownArtifactError(missing)
        return [found[i] for i in ids]

    # ------------------------------------------------------------------ writes

    def upsert_nodes(self, specs: Sequence[NodeSpec]) -> None:
        specs = list({s.id: s for s in specs}.values())  # last write wins within a batch
        if not specs:
            return
        now = datetime.now(UTC)
        rows = [
            {
                "id": s.id,
                "node_type": s.node_type.value,
                "ref": s.ref,
                "subject_ids": sorted(set(s.subject_ids)),
                "policy": s.policy.to_json(),
                "attrs": s.attrs,
                "status": "active",
                "updated_at": now,
            }
            for s in specs
        ]
        stmt = insert(LineageNode).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[LineageNode.id],
            set_={
                "subject_ids": stmt.excluded.subject_ids,
                "policy": stmt.excluded.policy,
                "attrs": stmt.excluded.attrs,
                "status": stmt.excluded.status,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        self.session.execute(stmt)
        self._expire({s.id for s in specs})
        self._reindex(specs)

    def _expire(self, ids: set[str]) -> None:
        """Core upserts bypass the ORM, so refresh any already-loaded copies of these nodes."""
        for obj in list(self.session.identity_map.values()):
            if isinstance(obj, LineageNode) and obj.id in ids:
                self.session.expire(obj)

    def _reindex(self, specs: Sequence[NodeSpec]) -> None:
        ids = [s.id for s in specs]
        self.session.execute(delete(SubjectArtifact).where(SubjectArtifact.node_id.in_(ids)))
        index_rows = [
            {"subject_id": sid, "node_id": s.id, "node_type": s.node_type.value}
            for s in specs
            for sid in set(s.subject_ids)
        ]
        if index_rows:
            self.session.execute(
                insert(SubjectArtifact).values(index_rows).on_conflict_do_nothing()
            )

    def add_edges(self, edges: Iterable[tuple[str, str, EdgeType]]) -> None:
        rows = [{"src_id": s, "dst_id": d, "edge_type": t.value} for s, d, t in edges]
        if rows:
            self.session.execute(insert(LineageEdge).values(rows).on_conflict_do_nothing())

    def set_status(
        self, ids: Iterable[str], status: str, attrs: dict[str, Any] | None = None
    ) -> None:
        ids = list(ids)
        if not ids:
            return
        values: dict[str, Any] = {"status": status}
        self.session.execute(update(LineageNode).where(LineageNode.id.in_(ids)).values(**values))
        if attrs:
            for node in self.get_many(ids).values():
                node.attrs = {**node.attrs, **attrs}

    # ------------------------------------------------------------------ events

    def emit(
        self,
        job_name: str,
        namespace: str,
        inputs: Sequence[LineageNode | NodeSpec],
        outputs: Sequence[LineageNode | NodeSpec],
    ) -> dict[str, Any]:
        event = openlineage.run_event(
            namespace=namespace,
            job_name=job_name,
            inputs=[_dataset(n) for n in inputs],
            outputs=[_dataset(n) for n in outputs],
        )
        exported = self.exporter.export(event)
        self.session.add(
            LineageEventRow(
                id=str(uuid.uuid4()),
                event_time=datetime.fromisoformat(event["eventTime"]),
                event_type=event["eventType"],
                job_name=job_name,
                run_id=event["run"]["runId"],
                payload=event,
                exported=exported,
            )
        )
        return event


def _dataset(n: LineageNode | NodeSpec) -> dict[str, Any]:
    if isinstance(n, NodeSpec):
        node_type, ref, subjects, policy = n.node_type, n.ref, n.subject_ids, n.policy.to_json()
    else:
        node_type, ref = split(n.id)
        subjects, policy = list(n.subject_ids), dict(n.policy)
    return openlineage.dataset(f"tether://{node_type.value}", ref, sorted(set(subjects)), policy)
