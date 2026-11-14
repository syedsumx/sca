"""Managed NiFi in Cairn Core: each flow is a ``dataFlow`` (``urn:li:dataFlow:(nifi,<flow>,<env>)``) with its owner,
team and domain; its process groups are ``dataJob``s (the root group is ``flow``: everything the flow reads and
writes, so the lineage from a source to the lakehouse table it lands in is one hop on the asset page; nested groups
are ``group:<path>``), with the datasets their processors read and write. Activity is reported as OpenLineage run
events through Cairn's receiver (``nifi://<deployment>``, job ``<flow>.<group>``), so runs are Core's
``dataProcessInstance``s and pipeline observability's runs (``nifi:<flow>``) like Airflow's DAG runs."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from cairn.common.proposals import make_mcp
from cairn.sources.openlineage import dataset_urn, job_urns

ACTOR = "urn:li:corpuser:cairn-nifi"
PRODUCER = "https://cairn.dev/nifi"
SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"


def flow_urn(flow: str, env: str = "PROD") -> str:
    return job_urns("nifi://x", f"{flow}.flow", env)[1]


def task_name(path: tuple[str, ...]) -> str:
    return "flow" if not path else "group:" + "/".join(path)


def job_urn(flow: str, path: tuple[str, ...], env: str = "PROD") -> str:
    return job_urns("nifi://x", f"{flow}.{task_name(path)}", env)[2]


def ol_dataset_of(urn: str, env: str = "PROD") -> Optional[dict]:
    """An OpenLineage dataset whose catalog URN is ``urn`` (None when no namespace and name give it back)."""
    try:
        inner = urn[len("urn:li:dataset:("):-1]
        platform, rest = inner.split(",", 1)
        name, _ = rest.rsplit(",", 1)
        platform = platform.removeprefix("urn:li:dataPlatform:")
    except ValueError:
        return None
    cand = {"namespace": platform, "name": name}
    try:
        return cand if dataset_urn(cand["namespace"], cand["name"], env) == urn else None
    except ValueError:
        return None


class Datasets:
    """The flow's datasets as URNs (for Core) and as OpenLineage datasets (for run events)."""

    def __init__(self, table_urn: Callable[[str], str], env: str = "PROD"):
        self.table_urn, self.env = table_urn, env

    def urn(self, d: dict) -> str:
        if d["namespace"] == "lakehouse":
            return self.table_urn(d["name"])
        return dataset_urn(d["namespace"], d["name"], self.env)

    def ol(self, d: dict) -> Optional[dict]:
        if d["namespace"] == "lakehouse":
            return ol_dataset_of(self.table_urn(d["name"]), self.env)
        return d


def groups_io(inferred: dict[tuple[str, ...], dict[str, list[dict]]], declared: dict, sink: Optional[dict],
              ds: Datasets) -> dict[tuple[str, ...], dict[str, list[str]]]:
    """Per process group, the dataset URNs read and written; the root group (``()``) gets the whole flow's, the
    declared lineage and the lakehouse sink."""
    out: dict[tuple[str, ...], dict[str, list[str]]] = {(): {"inputs": [], "outputs": []}}
    for path, io in inferred.items():
        for side in ("inputs", "outputs"):
            urns = [ds.urn(d) for d in io[side]]
            if path:
                out.setdefault(path, {"inputs": [], "outputs": []})[side] += urns
            out[()][side] += urns
    out[()]["inputs"] += declared.get("inputs") or []
    out[()]["outputs"] += declared.get("outputs") or []
    if sink:
        out[()]["outputs"].append(ds.urn({"namespace": "lakehouse", "name": f"{sink['namespace']}.{sink['table']}"}))
    return {p: {k: sorted(set(v)) for k, v in io.items()} for p, io in out.items()}


def register(graph, flow: str, deployment: str, owner: str, spec: dict, io: dict[tuple[str, ...], dict[str, list[str]]],
             team: Optional[str], domain: Optional[str], extra: Optional[dict] = None, env: str = "PROD") -> list[str]:
    """The flow, its process groups and their lineage in Cairn Core; the URNs written."""
    now = int(time.time() * 1000)
    furn = flow_urn(flow, env)
    owners = [{"owner": owner, "type": "DATAOWNER"}]
    if team:
        owners.append({"owner": f"urn:li:corpGroup:{team}", "type": "TECHNICAL_OWNER"})
    ownership = {"owners": owners, "lastModified": {"time": now, "actor": owner}}
    props = {"cairn.kind": "nifi_flow", "cairn.nifi.flow": flow, "cairn.nifi.deployment": deployment,
             "openlineage_namespace": f"nifi://{deployment}", "orchestrator": "nifi",
             **{k: str(v) for k, v in (extra or {}).items() if v is not None}}
    if spec.get("sink"):
        props["cairn.nifi.sink"] = f"{spec['sink']['namespace']}.{spec['sink']['table']}"
    mcps = [make_mcp(furn, "dataFlowInfo", {"name": flow, "description": str(spec.get("description") or "")[:5000],
                                            "customProperties": props}),
            make_mcp(furn, "ownership", ownership), make_mcp(furn, "status", {"removed": False})]
    dom = domain or None
    if dom:
        mcps.append(make_mcp(furn, "domains", {"domains": [dom]}))
    written = [furn]
    for path, sides in sorted(io.items()):
        jurn = job_urn(flow, path, env)
        name = task_name(path)
        mcps += [make_mcp(jurn, "dataJobInfo", {
                     "name": name, "flowUrn": furn, "type": {"com.linkedin.datajob.azkaban.AzkabanJobType": "COMMAND"},
                     "description": "The whole flow: what it reads and where it lands" if not path
                     else f"Process group {'/'.join(path)}",
                     "customProperties": {"openlineage_namespace": f"nifi://{deployment}",
                                          "openlineage_name": f"{flow}.{name}", "orchestrator": "nifi"}}),
                 make_mcp(jurn, "dataJobInputOutput", {"inputDatasets": sides["inputs"],
                                                       "outputDatasets": sides["outputs"], "fineGrainedLineages": []}),
                 make_mcp(jurn, "ownership", ownership), make_mcp(jurn, "status", {"removed": False})]
        if dom:
            mcps.append(make_mcp(jurn, "domains", {"domains": [dom]}))
        written.append(jurn)
        for urn in (*sides["inputs"], *sides["outputs"]):
            if not (graph.get_aspect(urn, "schemaMetadata") or graph.get_aspect(urn, "datasetProperties")):
                mcps.append(make_mcp(urn, "status", {"removed": False}))
    graph.emit(mcps)
    return written


def unregister(graph, flow: str, paths: list[tuple[str, ...]], env: str = "PROD") -> None:
    """The flow and its jobs marked removed (their history stays)."""
    mcps = [make_mcp(flow_urn(flow, env), "status", {"removed": True})]
    mcps += [make_mcp(job_urn(flow, p, env), "status", {"removed": True}) for p in paths or [()]]
    graph.emit(mcps)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def run_events(flow: str, deployment: str, run_id: str, start_ms: int, end_ms: int, failed: set,
               ol_io: dict[tuple[str, ...], dict[str, list[dict]]], error: str = "") -> list[dict]:
    """OpenLineage events for one window of a flow's activity: each process group as a task of the flow's run;
    ``failed``: the groups (paths) with error bulletins, and the whole flow (``()``) when any has."""
    ns = f"nifi://{deployment}"
    out = []
    for path, sides in sorted(ol_io.items()):
        task_run = str(uuid.uuid5(uuid.UUID(run_id), task_name(path)))
        base = {"producer": PRODUCER, "schemaURL": SCHEMA,
                "job": {"namespace": ns, "name": f"{flow}.{task_name(path)}",
                        "facets": {"jobType": {"_producer": PRODUCER, "processingType": "STREAMING",
                                               "integration": "nifi", "jobType": "TASK"}}},
                "inputs": sides.get("inputs") or [], "outputs": sides.get("outputs") or []}
        parent = {"parent": {"_producer": PRODUCER, "run": {"runId": run_id},
                             "job": {"namespace": ns, "name": flow}}}
        out.append({**base, "eventType": "START", "eventTime": _iso(start_ms),
                    "run": {"runId": task_run, "facets": parent}})
        facets = dict(parent)
        bad = path in failed
        if bad and error and not path:
            facets["errorMessage"] = {"_producer": PRODUCER, "message": error[:2000], "programmingLanguage": "JAVA"}
        out.append({**base, "eventType": "FAIL" if bad else "COMPLETE", "eventTime": _iso(end_ms),
                    "run": {"runId": task_run, "facets": facets}})
    return out
