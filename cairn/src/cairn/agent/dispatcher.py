"""Route native Cairn Core execution requests to remote agent pools by ``executorId``.

The UI / ``IngestionScheduler`` already writes ``dataHubExecutionRequestInput`` with an
``executorId``. The OSS ``ExecutorAction`` only claims its own id (``default``); any
request whose ``executorId`` names a Cairn pool is claimed here and queued for that pool.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from cairn.agent.gateway import EnqueueRequest, GatewayError, GatewayService

logger = logging.getLogger(__name__)

SUPPORTED_TASKS = {"RUN_INGEST": "ingest", "TEST_CONNECTION": "test"}
DISPATCHER_ACTOR = "urn:li:corpuser:cairn-agent-dispatcher"


class ExecutionRequestDispatcher:
    def __init__(self, gateway: GatewayService, graph):
        self.gateway = gateway
        self.graph = graph

    def _pool_names(self) -> set[str]:
        return {p["name"] for p in self.gateway.list_pools()}

    def handle(self, urn: str, request_input: dict, result: Optional[dict] = None) -> Optional[str]:
        executor_id = request_input.get("executorId")
        if not executor_id or executor_id not in self._pool_names():
            return None
        if result and result.get("status") not in (None, "", "PENDING"):
            return None  # already picked up or finished
        task = request_input.get("task")
        if task not in SUPPORTED_TASKS:
            logger.info("execution request %s task %s not supported by remote agents", urn, task)
            return None
        recipe_raw = (request_input.get("args") or {}).get("recipe")
        if not recipe_raw:
            return None
        recipe = json.loads(recipe_raw) if isinstance(recipe_raw, str) else recipe_raw
        # The central Cairn Core sink config is replaced by the agent's encrypted sink.
        recipe.pop("sink", None)
        try:
            out = self.gateway.enqueue(EnqueueRequest(pool=executor_id, recipe=recipe, execution_request_urn=urn,
                                                      kind=SUPPORTED_TASKS[task],
                                                      max_attempts=1 if task == "TEST_CONNECTION" else 3),
                                       DISPATCHER_ACTOR)
            return out["task_id"]
        except GatewayError as e:
            if e.status == 409:
                return None
            if e.status == 422:
                self.gateway._update_execution_request(urn, "FAILURE", {"error": str(e)}, None, 0)
            logger.warning("dispatch of %s failed: %s", urn, e)
            return None

    def handle_mcl(self, mcl: dict) -> Optional[str]:
        if mcl.get("entityType") != "dataHubExecutionRequest" or mcl.get("aspectName") != "dataHubExecutionRequestInput":
            return None
        aspect = mcl.get("aspect") or {}
        value = aspect.get("value") if isinstance(aspect, dict) else aspect
        return self.handle(mcl["entityUrn"], json.loads(value) if isinstance(value, str) else value)

    def poll_once(self) -> int:
        """Fallback when no MCL consumer is deployed: scan pending execution requests."""
        rows = self.graph.scroll_entities("dataHubExecutionRequest",
                                          ["dataHubExecutionRequestInput", "dataHubExecutionRequestResult"])
        n = 0
        for row in rows:
            if row.get("dataHubExecutionRequestInput") and self.handle(
                    row["urn"], row["dataHubExecutionRequestInput"], row.get("dataHubExecutionRequestResult")):
                n += 1
        return n
