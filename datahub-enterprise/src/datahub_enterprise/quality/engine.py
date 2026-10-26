"""The evaluation pipeline: collect → detect → persist → emit → incident lifecycle."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from datahub_enterprise.common.audit import AuditLog
from datahub_enterprise.common.gms import MetadataGraph
from datahub_enterprise.common.proposals import assertion_urn, incident_urn, make_mcp, now_ms
from datahub_enterprise.quality import detectors as d
from datahub_enterprise.quality.collectors import Collector, CollectorError, Sample
from datahub_enterprise.quality.emitter import (
    incident_is_active,
    incident_mcp,
    monitor_mcps,
    removal_mcps,
    run_event_mcp,
    run_summary,
)
from datahub_enterprise.quality.models import Evaluation, Monitor
from datahub_enterprise.quality.store import QualityStore

logger = logging.getLogger(__name__)

Notifier = Callable[[Monitor, Evaluation, str], None]


def webhook_notifier(timeout: float = 5.0) -> Notifier:
    """POSTs a Slack/Teams-compatible ``{"text": ...}`` payload to each monitor webhook."""
    client = httpx.Client(timeout=timeout)

    def notify(monitor: Monitor, ev: Evaluation, kind: str) -> None:
        icon = ":red_circle:" if kind == "failing" else ":large_green_circle:"
        text = f"{icon} *{monitor.name}* is {kind} on `{monitor.dataset_urn}`: {ev.message}"
        for url in monitor.notify:
            try:
                client.post(url, json={"text": text, "monitor_id": monitor.id, "status": ev.status})
            except httpx.HTTPError as e:  # pragma: no cover - network
                logger.warning("notification to %s failed: %s", url, e)

    return notify


class QualityEngine:
    def __init__(
        self,
        store: QualityStore,
        graph: Optional[MetadataGraph],
        collectors: dict[str, Collector],
        notifier: Optional[Notifier] = None,
        audit: Optional[AuditLog] = None,
    ):
        self.store = store
        self.graph = graph
        self.collectors = collectors
        self.notifier = notifier
        self.audit = audit

    # ---- projection ----------------------------------------------------------------
    def sync_monitor(self, monitor: Monitor) -> None:
        self._emit(monitor_mcps(monitor) + self._summary_mcps(monitor))

    def _summary_mcps(self, monitor: Monitor, ev: Optional[Evaluation] = None) -> list[dict]:
        """Maintain ``assertionRunSummary`` (last passed/failed/errored, status) for DHE assertions.

        GMS's AssertionRunSummaryHook would do this by PATCH, but OSS GMS has no patch template for the
        aspect (the hook fails), so DHE, the only writer of its assertions' run events, writes it. The
        current value is read first so concurrent evaluators (scheduler, manual runs) keep each other's
        timestamps.
        """
        if self.graph is None:
            return []
        urn = assertion_urn(monitor.id)
        try:
            current = self.graph.get_aspect(urn, "assertionRunSummary")
        except Exception:
            return []
        if ev is None and current is not None:
            return []
        return [make_mcp(urn, "assertionRunSummary", run_summary(current, ev, now_ms()))]

    def remove_monitor(self, monitor_id: str) -> None:
        self._emit(removal_mcps(monitor_id))

    def flush_unemitted(self) -> int:
        """Re-emit evaluations that could not reach GMS earlier (GMS outage recovery)."""
        sent = 0
        for ev in self.store.unemitted():
            monitor = self.store.get_monitor(ev.monitor_id)
            if monitor and self._emit([run_event_mcp(monitor, ev)] + self._summary_mcps(monitor, ev)):
                self.store.mark_emitted(ev.id)
                sent += 1
        return sent

    # ---- evaluation ----------------------------------------------------------------
    def evaluate(self, monitor: Monitor, now: Optional[int] = None, scheduled_ms: Optional[int] = None) -> Evaluation:
        now = now if now is not None else now_ms()
        run_id = f"{monitor.id}:{scheduled_ms if scheduled_ms is not None else now}"
        sample = Sample(None)
        try:
            collector = self.collectors.get(monitor.config.get("source", "datahub"))
            if collector is None:
                raise RuntimeError(f"no collector configured for source {monitor.config.get('source')!r}")
            sample = collector.collect(monitor, now)
            detection = self._detect(monitor, sample, now)
        except CollectorError as e:  # expected (no profile yet, source unreachable): recorded as ERROR
            logger.warning("monitor %s could not collect: %s", monitor.id, e)
            detection = d.Detection("ERROR", f"{type(e).__name__}: {e}")
        except Exception as e:  # collectors talk to external systems; never crash the loop
            logger.exception("monitor %s evaluation failed", monitor.id)
            detection = d.Detection("ERROR", f"{type(e).__name__}: {e}")

        point_id = None
        if sample.value is not None and detection.status != "ERROR":
            point_id = self.store.add_point(monitor.id, now, sample.value, _slim_payload(sample.payload))

        ev = Evaluation(
            id=f"ev_{uuid.uuid4().hex[:20]}",
            monitor_id=monitor.id,
            ts=datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat(),
            ts_ms=now,
            run_id=run_id,
            metric_value=sample.value,
            expected_low=detection.expected_low,
            expected_high=detection.expected_high,
            status=detection.status,
            score=detection.score,
            message=detection.message,
            details=detection.details,
        )
        if not self.store.add_evaluation(ev, point_id):
            logger.info("duplicate run %s ignored", run_id)
            existing = [e for e in self.store.evaluations(monitor.id, limit=50) if e.run_id == run_id]
            return existing[0] if existing else ev

        if detection.status not in ("TRAINING", "ERROR") and monitor.status == "TRAINING":
            self.store.set_status(monitor.id, "ACTIVE")

        if self._emit([run_event_mcp(monitor, ev)] + self._summary_mcps(monitor, ev)):
            self.store.mark_emitted(ev.id)
            ev.emitted = True
        self._incident_lifecycle(monitor, ev)
        return ev

    # ---- detection per monitor type --------------------------------------------------
    def _detect(self, monitor: Monitor, sample: Sample, now: int) -> d.Detection:
        c = monitor.config
        sens = monitor.sensitivity
        if monitor.type == "freshness":
            history_updates = [int(p["value"]) for p in self.store.history(monitor.id) if p["value"] is not None]
            updates = list(sample.payload.get("update_times_ms", [])) + history_updates
            return d.detect_freshness(updates, now, sens, c.get("sla_seconds"), int(c.get("min_history", 5)))

        if monitor.type == "schema":
            state = self.store.get_state(monitor.id)
            baseline = (state.get("reference") or {}).get("fields") or {}
            current = sample.payload.get("fields", {})
            det = d.detect_schema_change(baseline, current, bool(c.get("allow_additions", True)))
            if det.status == "TRAINING" or (det.status == "WARN" and c.get("auto_accept_additions", True)):
                self.store.save_state(monitor.id, reference={"fields": current})
            return det

        if monitor.type == "distribution":
            return self._detect_distribution(monitor, sample)

        if sample.value is None:
            return d.Detection("ERROR", "collector returned no value")
        history = [d.Point(p["ts_ms"], float(p["value"])) for p in self.store.history(monitor.id)
                   if p["value"] is not None]
        current = d.Point(now, float(sample.value))
        lo, hi = _bounds(monitor)
        if monitor.type == "field_metric":
            return d.detect_ewma(history, current, sens, min_history=int(c.get("min_history", 10)),
                                 min_value=lo, max_value=hi)
        return d.detect_seasonal_robust_z(
            history, current, sens,
            min_history=int(c.get("min_history", 14)),
            seasonality=c.get("seasonality", "hour_of_week"),
            min_value=lo, max_value=hi,
            max_change_pct=c.get("max_change_pct"),
        )

    def _detect_distribution(self, monitor: Monitor, sample: Sample) -> d.Detection:
        c = monitor.config
        state = self.store.get_state(monitor.id)
        ref = state.get("reference") or {}
        if c.get("kind", "categorical") == "categorical":
            current_hist = sample.payload.get("histogram", {})
            det = d.detect_distribution(ref.get("histogram") or {}, current_hist,
                                        float(c.get("psi_warn", 0.2)), float(c.get("psi_fail", 0.3)))
            new_ref = {"histogram": current_hist}
        else:
            cur_samples = sample.payload.get("samples", [])
            ref_samples = ref.get("samples") or []
            edges = d.quantile_edges(ref_samples, int(c.get("buckets", 10))) if ref_samples else []
            det = d.detect_distribution(
                d.histogram(ref_samples, edges) if ref_samples else {},
                d.histogram(cur_samples, edges),
                float(c.get("psi_warn", 0.2)), float(c.get("psi_fail", 0.3)),
                reference_samples=ref_samples or None, current_samples=cur_samples or None,
            )
            new_ref = {"samples": cur_samples[:5000]}
        if det.status == "TRAINING" or (c.get("reference", "pinned") == "trailing" and det.status == "PASS"):
            self.store.save_state(monitor.id, reference=new_ref)
        return det

    # ---- incidents -------------------------------------------------------------------
    def _incident_lifecycle(self, monitor: Monitor, ev: Evaluation) -> None:
        if ev.status in ("TRAINING", "ERROR"):
            return
        state = self.store.get_state(monitor.id)
        open_urn = state.get("open_incident_urn")
        if ev.status == "FAIL":
            failures = int(state.get("consecutive_failures") or 0) + 1
            self.store.save_state(monitor.id, consecutive_failures=failures)
            if failures >= monitor.incident_after_failures and not open_urn:
                urn = incident_urn(monitor.id, ev.ts_ms)
                self._emit([incident_mcp(monitor, urn, ev, active=True, started_at_ms=ev.ts_ms)])
                self.store.save_state(monitor.id, open_incident_urn=urn)
                if self.audit:
                    self.audit.record("quality", "incident.raised", "urn:li:corpuser:dhe-system", urn,
                                      {"monitor_id": monitor.id, "evaluation_id": ev.id})
                if self.notifier:
                    self.notifier(monitor, ev, "failing")
            return

        # PASS / WARN → recovered
        self.store.save_state(monitor.id, consecutive_failures=0)
        if open_urn:
            current = self.graph.get_aspect(open_urn, "incidentInfo") if self.graph else None
            # Only auto-resolve incidents that are still active; never touch one a human
            # already resolved (and never reopen it either; the next failure opens a new one).
            if current is None or incident_is_active(current):
                started = int((current or {}).get("startedAt") or ev.ts_ms)
                self._emit([incident_mcp(monitor, open_urn, ev, active=False, started_at_ms=started)])
            self.store.save_state(monitor.id, open_incident_urn=None)
            if self.notifier:
                self.notifier(monitor, ev, "recovered")

    def _emit(self, mcps: list[dict]) -> bool:
        if self.graph is None:
            return False
        try:
            self.graph.emit(mcps)
            return True
        except Exception as e:
            logger.warning("emit to GMS failed (will retry later): %s", e)
            return False


def _bounds(monitor: Monitor) -> tuple[Optional[float], Optional[float]]:
    c = monitor.config
    if monitor.type == "volume":
        return c.get("min_rows"), c.get("max_rows")
    return c.get("min"), c.get("max")


def _slim_payload(payload: dict) -> Optional[dict]:
    """Keep stored history small: raw samples / histograms live in monitor state only."""
    if not payload:
        return None
    return {k: v for k, v in payload.items() if k not in ("samples", "histogram", "update_times_ms", "fields")} or None
