"""Thin client for the DataHub GMS APIs DHE relies on.

Only public, versioned contracts are used:

* Rest.li ``/aspects?action=ingestProposal[Batch]`` for writes (same as the Python emitter),
* OpenAPI v3 ``/openapi/v3/entity/{type}/{urn}`` for aspect reads,
* GraphQL ``/api/graphql`` for search, lineage, and identity.

The client is synchronous (httpx) because the callers are schedulers, workers, and
request handlers that already run in thread pools.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Protocol, Sequence

import httpx

from datahub_enterprise.common.proposals import encode_urn, entity_type_of, iter_batches

logger = logging.getLogger(__name__)


class GmsError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class MetadataGraph(Protocol):
    """The subset of GMS behaviour DHE modules depend on (lets tests use fakes)."""

    def emit(self, mcps: Sequence[dict]) -> None: ...

    def get_aspect(self, urn: str, aspect_name: str) -> Optional[dict]: ...

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict: ...


class GmsClient:
    def __init__(
        self,
        url: str,
        token: Optional[str] = None,
        timeout_seconds: float = 30.0,
        client: Optional[httpx.Client] = None,
    ):
        self.url = url.rstrip("/")
        headers = {
            "X-RestLi-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
            "User-Agent": "datahub-enterprise/0.1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = client or httpx.Client(timeout=timeout_seconds)
        self._headers = headers

    def close(self) -> None:
        self._http.close()

    # ---- writes -----------------------------------------------------------------
    def emit(self, mcps: Sequence[dict]) -> None:
        if not mcps:
            return
        if len(mcps) == 1:
            self._post(
                "/aspects?action=ingestProposal",
                {"proposal": mcps[0], "async": "false"},
            )
            return
        for batch in iter_batches(mcps):
            self._post(
                "/aspects?action=ingestProposalBatch",
                {"proposals": batch, "async": "false"},
            )

    # ---- reads ------------------------------------------------------------------
    def get_aspect(self, urn: str, aspect_name: str) -> Optional[dict]:
        entity_type = entity_type_of(urn)
        resp = self._http.get(
            f"{self.url}/openapi/v3/entity/{entity_type.lower()}/{encode_urn(urn)}",
            params={"aspects": aspect_name, "systemMetadata": "false"},
            headers=self._headers,
        )
        if resp.status_code == 404:
            return None
        self._raise_for_status(resp)
        body = resp.json()
        aspect = body.get(aspect_name)
        if aspect is None:
            return None
        # OpenAPI v3 wraps aspects as {"value": {...}, "systemMetadata": ...}
        return aspect.get("value", aspect) if isinstance(aspect, dict) else None

    def scroll_entities(
        self, entity_type: str, aspects: Sequence[str], page_size: int = 500
    ) -> list[dict]:
        """All entities of a type via OpenAPI v3 scroll: ``[{"urn": ..., "<aspect>": {...}}]``."""
        out: list[dict] = []
        scroll_id: Optional[str] = None
        while True:
            params: dict[str, Any] = {"count": page_size, "aspects": list(aspects), "systemMetadata": "false"}
            if scroll_id:
                params["scrollId"] = scroll_id
            resp = self._http.get(
                f"{self.url}/openapi/v3/entity/{entity_type.lower()}", params=params, headers=self._headers
            )
            self._raise_for_status(resp)
            body = resp.json()
            for ent in body.get("entities", []):
                row = {"urn": ent.get("urn")}
                for a in aspects:
                    v = ent.get(a)
                    if isinstance(v, dict):
                        row[a] = v.get("value", v)
                out.append(row)
            scroll_id = body.get("scrollId")
            if not scroll_id:
                return out

    def get_timeseries(
        self, urn: str, aspect_name: str, limit: int = 100
    ) -> list[dict]:
        """Latest timeseries aspect values (newest first) via the Rest.li getTimeseriesAspectValues action."""
        payload = {
            "urn": urn,
            "entity": entity_type_of(urn),
            "aspect": aspect_name,
            "limit": limit,
        }
        body = self._post("/aspects?action=getTimeseriesAspectValues", payload)
        values = (body or {}).get("value", {}).get("values", [])
        out = []
        for v in values:
            raw = v.get("aspect", {}).get("value")
            if raw:
                out.append(json.loads(raw))
        return out

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        body = self._post("/api/graphql", {"query": query, "variables": variables or {}})
        if body.get("errors"):
            raise GmsError(f"GraphQL errors: {body['errors']}")
        return body.get("data", {})

    # ---- helpers ----------------------------------------------------------------
    def _post(self, path: str, payload: dict) -> Any:
        resp = self._http.post(
            f"{self.url}{path}", content=json.dumps(payload), headers=self._headers
        )
        self._raise_for_status(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            raise GmsError(
                f"GMS {resp.request.method} {resp.request.url.path} failed: "
                f"{resp.status_code} {resp.text[:500]}",
                status_code=resp.status_code,
            )


class InMemoryGraph:
    """In-memory :class:`MetadataGraph` used by tests and the offline demo."""

    def __init__(self) -> None:
        self.aspects: dict[tuple[str, str], dict] = {}
        self.timeseries: dict[tuple[str, str], list[dict]] = {}
        self.emitted: list[dict] = []
        self.graphql_handler = None

    def emit(self, mcps: Sequence[dict]) -> None:
        from datahub_enterprise.common.proposals import aspect_value

        for mcp in mcps:
            self.emitted.append(mcp)
            key = (mcp["entityUrn"], mcp["aspectName"])
            value = aspect_value(mcp)
            if "timestampMillis" in value:
                self.timeseries.setdefault(key, []).insert(0, value)
            else:
                self.aspects[key] = value

    def get_aspect(self, urn: str, aspect_name: str) -> Optional[dict]:
        return self.aspects.get((urn, aspect_name))

    def get_timeseries(self, urn: str, aspect_name: str, limit: int = 100) -> list[dict]:
        return self.timeseries.get((urn, aspect_name), [])[:limit]

    def scroll_entities(self, entity_type: str, aspects: Sequence[str], page_size: int = 500) -> list[dict]:
        rows: dict[str, dict] = {}
        for (urn, aspect), value in self.aspects.items():
            if urn.split(":", 3)[2].lower() == entity_type.lower() and aspect in aspects:
                rows.setdefault(urn, {"urn": urn})[aspect] = value
        return list(rows.values())

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        if self.graphql_handler is None:
            return {}
        return self.graphql_handler(query, variables or {})
