"""Projection of DHE monitors/evaluations into native DataHub aspects.

* monitor   → ``assertion`` entity (``assertionInfo`` with ``source.type = NATIVE``)
* evaluation → ``assertionRunEvent`` timeseries aspect (shows in the Quality tab)
* incident  → ``incident`` entity (``incidentInfo``, type mapped from the monitor type)

Shapes follow ``metadata-models/src/main/pegasus/com/linkedin/{assertion,incident}``.
"""

from __future__ import annotations

from typing import Optional

from datahub_enterprise.common.proposals import (
    DHE_ACTOR,
    assertion_urn,
    audit_stamp,
    make_mcp,
    now_ms,
    schema_field_urn,
)
from datahub_enterprise.quality.models import Evaluation, Monitor

_INCIDENT_TYPE = {
    "freshness": "FRESHNESS",
    "volume": "VOLUME",
    "field_metric": "FIELD",
    "distribution": "FIELD",
    "schema": "DATA_SCHEMA",
    "custom_sql": "SQL",
}

_RESULT_TYPE = {"PASS": "SUCCESS", "WARN": "SUCCESS", "FAIL": "FAILURE", "ERROR": "ERROR", "TRAINING": "INIT"}

_SEVERITY = {"LOW": "LOW", "MEDIUM": "MEDIUM", "HIGH": "HIGH", "CRITICAL": "HIGH"}

_PRIORITY = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

_SYSTEM_PROPS = {"dhe_source": "quality"}


def _num(v: Optional[float]) -> dict:
    return {"value": f"{v:g}", "type": "NUMBER"}


def _between(low: Optional[float], high: Optional[float]) -> tuple[str, dict]:
    if low is not None and high is not None:
        return "BETWEEN", {"minValue": _num(low), "maxValue": _num(high)}
    if low is not None:
        return "GREATER_THAN_OR_EQUAL_TO", {"value": _num(low)}
    if high is not None:
        return "LESS_THAN_OR_EQUAL_TO", {"value": _num(high)}
    return "NOT_NULL", {}


def assertion_info(monitor: Monitor) -> dict:
    ds = monitor.dataset_urn
    c = monitor.config
    info: dict = {
        "customProperties": {
            "dhe_monitor_id": monitor.id,
            "dhe_monitor_type": monitor.type,
            "dhe_sensitivity": monitor.sensitivity,
            "dhe_severity": monitor.severity,
        },
        "description": monitor.name,
        "source": {"type": "NATIVE", "created": audit_stamp(monitor.created_by or DHE_ACTOR)},
        "lastUpdated": audit_stamp(DHE_ACTOR),
        "entityUrn": ds,
    }
    if monitor.type == "freshness":
        sched = monitor.schedule
        if sched.cron:
            schedule = {"type": "CRON", "cron": {"cron": sched.cron, "timezone": "UTC"}}
        else:
            schedule = {"type": "SINCE_THE_LAST_CHECK"}
        info["type"] = "FRESHNESS"
        info["freshnessAssertion"] = {"type": "DATASET_CHANGE", "entity": ds, "schedule": schedule}
    elif monitor.type == "volume" and (c.get("min_rows") is not None or c.get("max_rows") is not None):
        op, params = _between(c.get("min_rows"), c.get("max_rows"))
        info["type"] = "VOLUME"
        info["volumeAssertion"] = {
            "type": "ROW_COUNT_TOTAL",
            "entity": ds,
            "rowCountTotal": {"operator": op, "parameters": params},
        }
    else:
        # Learned/anomaly monitors have no fixed operator, so they are modelled as CUSTOM
        # assertions. The UI still renders them with results and history.
        custom: dict = {
            "type": f"DHE {monitor.type.replace('_', ' ').title()} Anomaly",
            "entity": ds,
            "nativeType": f"dhe.{monitor.type}",
            "nativeParameters": {k: str(v) for k, v in sorted(c.items()) if k not in ("sql",)},
        }
        if c.get("field"):
            custom["fields"] = [schema_field_urn(ds, c["field"])]
        if monitor.type == "custom_sql":
            custom["logic"] = c.get("sql")
        info["type"] = "CUSTOM"
        info["customAssertion"] = custom
    return info


def monitor_mcps(monitor: Monitor) -> list[dict]:
    urn = assertion_urn(monitor.id)
    return [
        make_mcp(urn, "assertionInfo", assertion_info(monitor), system_properties=_SYSTEM_PROPS),
        make_mcp(urn, "status", {"removed": not monitor.enabled}, system_properties=_SYSTEM_PROPS),
    ]


def removal_mcps(monitor_id: str) -> list[dict]:
    return [make_mcp(assertion_urn(monitor_id), "status", {"removed": True}, system_properties=_SYSTEM_PROPS)]


def run_event_mcp(monitor: Monitor, ev: Evaluation) -> dict:
    result: dict = {
        "type": _RESULT_TYPE[ev.status],
        "nativeResults": {
            "dhe_status": ev.status,
            "message": ev.message[:1000],
            **({"expected_low": f"{ev.expected_low:g}"} if ev.expected_low is not None else {}),
            **({"expected_high": f"{ev.expected_high:g}"} if ev.expected_high is not None else {}),
            **({"score": f"{ev.score:g}"} if ev.score is not None else {}),
        },
    }
    if ev.metric_value is not None:
        result["actualAggValue"] = float(ev.metric_value)
    if ev.status == "FAIL":
        result["severity"] = _SEVERITY[monitor.severity]
    if ev.status == "ERROR":
        result["error"] = {"type": "UNKNOWN_ERROR", "properties": {"message": ev.message[:1000]}}
    event = {
        "timestampMillis": ev.ts_ms,
        "runId": ev.run_id,
        "asserteeUrn": monitor.dataset_urn,
        "assertionUrn": assertion_urn(monitor.id),
        "status": "COMPLETE",
        "result": result,
        "runtimeContext": {"dhe_evaluation_id": ev.id},
    }
    return make_mcp(assertion_urn(monitor.id), "assertionRunEvent", event, system_properties=_SYSTEM_PROPS)


def incident_mcp(monitor: Monitor, incident: str, ev: Evaluation, active: bool, started_at_ms: int) -> dict:
    t = _INCIDENT_TYPE[monitor.type]
    info = {
        "type": t,
        "title": f"[DQ] {monitor.name}",
        "description": ev.message[:4000],
        "entities": [monitor.dataset_urn],
        "priority": _PRIORITY[monitor.severity],
        "status": {
            "state": "ACTIVE" if active else "RESOLVED",
            "stage": "TRIAGE" if active else "FIXED",
            "message": "Raised by DHE quality engine" if active else "Auto-resolved: monitor recovered",
            "lastUpdated": audit_stamp(DHE_ACTOR),
        },
        "source": {"type": "ASSERTION_FAILURE", "sourceUrn": assertion_urn(monitor.id)},
        "startedAt": started_at_ms,
        "created": audit_stamp(DHE_ACTOR, started_at_ms),
    }
    return make_mcp(incident, "incidentInfo", info, system_properties=_SYSTEM_PROPS)


def incident_is_active(incident_info: Optional[dict]) -> bool:
    return bool(incident_info) and (incident_info.get("status") or {}).get("state") == "ACTIVE"


__all__ = [
    "assertion_info",
    "incident_is_active",
    "incident_mcp",
    "monitor_mcps",
    "now_ms",
    "removal_mcps",
    "run_event_mcp",
]
