"""AI usage lineage in the catalog's own lineage graph.

Every agent becomes a task (``dataJob``) whose inputs are the datasets it read in the last 30 days (AI
usage lineage, ``ai.traces``), under a pipeline (``dataFlow``) per AI system it is registered in, or
"Agents without an AI system". A dataset's lineage then shows the agents it feeds, and an AI system's
pipeline shows everything its agents use. The notifier publishes it hourly; agents that are deleted, or
read nothing any more, are removed from the graph.
"""

from __future__ import annotations

from typing import Any

from cairn.common.proposals import make_mcp

ORCHESTRATOR = "cairn-ai"
UNREGISTERED = "unregistered"


def flow_urn(system_id: str, env: str = "PROD") -> str:
    return f"urn:li:dataFlow:({ORCHESTRATOR},{system_id},{env})"


def job_urn(system_id: str, agent_id: str, env: str = "PROD") -> str:
    return f"urn:li:dataJob:({flow_urn(system_id, env)},{agent_id})"


def publish(services, days: int = 30) -> dict[str, Any]:
    graph, traces, agents = services.graph, services.traces, services.agents
    if graph is None or traces is None or agents is None:
        return {"skipped": True}
    systems: dict[str, dict] = {}
    if services.ai_systems is not None:
        systems = {s["id"]: s for s in services.ai_systems.list()}
    mcps: list[dict] = []
    wanted: set[str] = set()
    for a in agents.list():
        reads = sorted({r["dataset"] for r in traces.datasets_read(agent=a["id"], days=days)})
        if not reads:
            continue
        sid = a.get("ai_system") if a.get("ai_system") in systems else UNREGISTERED
        flow, job = flow_urn(sid), job_urn(sid, a["id"])
        wanted.add(job)
        system = systems.get(sid)
        flow_info = {"name": system["name"] if system else "Agents without an AI system",
                     "description": (system or {}).get("purpose") or "AI agents that are not registered as an AI system.",
                     "customProperties": {k: str(v) for k, v in {
                         "ai_system": sid, "state": (system or {}).get("state"), "risk_tier": (system or {}).get("tier"),
                         "owner": (system or {}).get("owner")}.items() if v}}
        job_info = {"name": a["name"], "flowUrn": flow,
                    "type": {"com.linkedin.datajob.azkaban.AzkabanJobType": "COMMAND"},
                    "description": a.get("description") or f"AI agent ({a.get('kind')}) acting through Cairn.",
                    "customProperties": {k: str(v) for k, v in {
                        "agent_id": a["id"], "kind": a.get("kind"), "owner": a.get("owner"),
                        "scopes": ",".join(a.get("scopes") or []), "enabled": a.get("enabled")}.items() if v is not None}}
        io = {"inputDatasets": reads, "outputDatasets": []}
        for urn, aspect, value in ((flow, "dataFlowInfo", flow_info), (job, "dataJobInfo", job_info),
                                   (job, "dataJobInputOutput", io), (job, "status", {"removed": False})):
            if graph.get_aspect(urn, aspect) != value:
                mcps.append(make_mcp(urn, aspect, value))
    removed = 0
    try:
        existing = [r["urn"] for r in graph.scroll_entities("dataJob", ["dataJobInfo", "status"])
                    if r["urn"].startswith(f"urn:li:dataJob:(urn:li:dataFlow:({ORCHESTRATOR},")
                    and not (r.get("status") or {}).get("removed")]
    except Exception:  # noqa: BLE001 - removal is best effort
        existing = []
    for urn in existing:
        if urn not in wanted:
            mcps.append(make_mcp(urn, "status", {"removed": True}))
            removed += 1
    if mcps:
        graph.emit(mcps)
    return {"agents": len(wanted), "changes": len(mcps), "removed": removed}
