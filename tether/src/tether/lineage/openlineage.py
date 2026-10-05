"""OpenLineage-shaped events with a custom ``tether`` dataset facet.

Every SDK call is a small "job run" that reads input datasets (the parents) and writes
output datasets (the new artifacts). Each dataset carries a ``tether`` facet with its
subject ids and the effective policy. Events are stored in ``lineage_events``. They can
also be forwarded to an OpenLineage backend such as Marquez through :class:`HttpExporter`.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

PRODUCER = "https://github.com/syedsumx/sca/tree/main/tether"
SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
TETHER_FACET_SCHEMA = f"{PRODUCER}/docs/tether-facet.json"


def dataset(
    namespace: str, name: str, subject_ids: list[str], policy: dict[str, Any]
) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "name": name,
        "facets": {
            "tether": {
                "_producer": PRODUCER,
                "_schemaURL": TETHER_FACET_SCHEMA,
                "subjectIds": subject_ids,
                "policy": policy,
            }
        },
    }


def run_event(
    *,
    namespace: str,
    job_name: str,
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    event_type: str = "COMPLETE",
    run_id: str | None = None,
    event_time: datetime | None = None,
) -> dict[str, Any]:
    return {
        "eventType": event_type,
        "eventTime": (event_time or datetime.now(UTC)).isoformat(),
        "run": {"runId": run_id or str(uuid.uuid4())},
        "job": {"namespace": namespace, "name": job_name},
        "inputs": inputs,
        "outputs": outputs,
        "producer": PRODUCER,
        "schemaURL": SCHEMA_URL,
    }


class LineageExporter(Protocol):
    def export(self, event: dict[str, Any]) -> bool:
        """Send one event. Return True when it was delivered."""
        ...


class NoopExporter:
    def export(self, event: dict[str, Any]) -> bool:
        return False


class HttpExporter:
    """POSTs events to an OpenLineage HTTP endpoint (e.g. Marquez ``/api/v1/lineage``).

    Export is best-effort: a failure is logged and never breaks the pipeline. The event is
    already persisted in ``lineage_events`` and can be replayed.
    """

    def __init__(self, url: str, timeout: float = 2.0) -> None:
        self.url = url
        self.timeout = timeout

    def export(self, event: dict[str, Any]) -> bool:
        try:
            resp = httpx.post(self.url, json=event, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logging.getLogger(__name__).warning("OpenLineage export failed: %s", exc)
            return False


def exporter_from_url(url: str | None) -> LineageExporter:
    return HttpExporter(url) if url else NoopExporter()
