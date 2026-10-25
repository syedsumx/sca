"""Explain lineage anomalies in plain English.

The ranking is **deterministic and evidence-based**: candidate causes are the asset itself and
its upstreams (≤ 3 hops); each signal (open incident, failing/erroring monitor, breaking schema
change, stale freshness, failing job runs) gets a base weight, decayed by lineage distance and
age. An LLM, when configured, only *narrates* the ranked evidence (it may cite nothing else).
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from datahub_enterprise.ai.guardrails import UNTRUSTED_NOTICE, redact, untrusted
from datahub_enterprise.ai.llm import LLMError, validate_against_schema

SIGNAL_WEIGHTS = {"incident": 1.0, "monitor_fail": 0.9, "schema_break": 0.85, "freshness": 0.8,
                  "run_failure": 0.75, "monitor_error": 0.5, "monitor_warn": 0.4}
HOUR_MS = 3_600_000

NARRATIVE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary_md"],
    "properties": {"summary_md": {"type": "string"}},
}
NARRATIVE_SYSTEM = f"""You explain data incidents to business users in 2-4 short sentences of markdown.
Use ONLY the ranked evidence provided. Name the most likely cause first, say how it reaches the asset
(lineage hops), mention whether it is still ongoing, and suggest one next step. Never invent assets or events.
{UNTRUSTED_NOTICE}"""


def _iso(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else None


class LineageExplainer:
    def __init__(self, catalog, quality_store, findings=None, llm=None, model: Optional[str] = None,
                 can_view: Optional[Callable[[str], bool]] = None):
        self.catalog = catalog
        self.quality = quality_store
        self.findings = findings
        self.llm = llm
        self.model = model
        self.can_view = can_view

    def _signals(self, urn: str, now: int) -> list[dict]:
        out: list[dict] = []
        if self.quality is None:
            return out
        for m in self.quality.list_monitors(urn):
            state = self.quality.get_state(m.id)
            ev = m.last_evaluation
            if state.get("open_incident_urn"):
                out.append({"kind": "incident", "ts": ev.ts_ms if ev else now, "ref": state["open_incident_urn"],
                            "text": f"open data-quality incident from monitor “{m.name}”", "ongoing": True})
            if ev is None:
                continue
            if m.type == "schema" and ev.status == "FAIL":
                out.append({"kind": "schema_break", "ts": ev.ts_ms, "ref": m.id, "text": ev.message,
                            "ongoing": True})
            elif m.type == "freshness" and ev.status == "FAIL":
                out.append({"kind": "freshness", "ts": ev.ts_ms, "ref": m.id, "text": ev.message, "ongoing": True})
            elif ev.status == "FAIL":
                out.append({"kind": "monitor_fail", "ts": ev.ts_ms, "ref": m.id,
                            "text": f"monitor “{m.name}”: {ev.message}", "ongoing": True})
            elif ev.status == "ERROR":
                out.append({"kind": "monitor_error", "ts": ev.ts_ms, "ref": m.id,
                            "text": f"monitor “{m.name}” could not run: {ev.message}", "ongoing": True})
            elif ev.status == "WARN":
                out.append({"kind": "monitor_warn", "ts": ev.ts_ms, "ref": m.id,
                            "text": f"monitor “{m.name}” warning: {ev.message}", "ongoing": False})
        if self.findings is not None:
            for f in self.findings.list("RUN_ANOMALY", "OPEN", entity_urn=None, limit=200):
                if any(urn in e or e in urn for e in f["entities"]):
                    out.append({"kind": "run_failure", "ts": now, "ref": f["id"], "text": f["summary_md"],
                                "ongoing": True})
        return out

    def explain(self, urn: str, now_ms: Optional[int] = None) -> dict:
        now = now_ms or int(time.time() * 1000)
        try:
            upstream = self.catalog.lineage(urn, "UPSTREAM", 3, 100) if self.catalog else []
        except Exception:
            upstream = []
        candidates = [{"urn": urn, "name": urn.split(",")[-2] if "," in urn else urn, "degree": 0}]
        hidden = 0
        for u in upstream:
            if self.can_view and not self.can_view(u["urn"]):
                hidden += 1
                continue
            deg = u.get("degree") or 1
            candidates.append({"urn": u["urn"], "name": u.get("name") or u["urn"], "degree": int(deg)})
        causes, timeline = [], []
        for c in candidates:
            sigs = self._signals(c["urn"], now)
            for s in sigs:
                timeline.append({"at": _iso(s["ts"]), "event": s["text"][:300], "urn": c["urn"], "_ts": s["ts"]})
            if not sigs:
                continue
            score = 0.0
            for s in sigs:
                age_h = max(0.0, (now - s["ts"]) / HOUR_MS)
                score = max(score, SIGNAL_WEIGHTS[s["kind"]] * (1 / (1 + 0.3 * c["degree"]))
                            * (1.0 if s.get("ongoing") else math.exp(-age_h / 24)))
            hop = "this asset" if c["degree"] == 0 else f"{c['degree']} hop{'s' if c['degree'] > 1 else ''} upstream"
            causes.append({"urn": c["urn"], "name": c["name"], "score": round(score, 3),
                           "reason": f"{sigs[0]['text']} ({hop})", "evidence": [s["text"][:300] for s in sigs[:5]],
                           "degree": c["degree"]})
        causes.sort(key=lambda x: (-x["score"], x["degree"]))
        timeline.sort(key=lambda t: t.pop("_ts"))
        summary = self._summary(urn, causes, len(candidates) - 1, hidden)
        used_llm = False
        if self.llm is not None and causes:
            listing = "\n".join(f"{i + 1}. {c['name']} ({c['urn']}), score {c['score']}: {'; '.join(c['evidence'])}"
                                for i, c in enumerate(causes[:5]))
            try:
                out, _ = self.llm.generate_json(NARRATIVE_SYSTEM, f"Asset: {urn}\nRanked evidence:\n"
                                                + untrusted(redact(listing)), NARRATIVE_SCHEMA, model=self.model,
                                                max_tokens=2000)
                validate_against_schema(out, NARRATIVE_SCHEMA)
                summary, used_llm = out["summary_md"], True
            except LLMError:
                pass
        return {"urn": urn, "summary_md": summary, "likely_causes": causes[:10], "timeline": timeline[-50:],
                "upstreams_checked": len(candidates) - 1, "hidden_upstreams": hidden, "used_llm": used_llm}

    @staticmethod
    def _summary(urn: str, causes: list[dict], upstreams: int, hidden: int) -> str:
        if not causes:
            extra = f" ({hidden} upstream assets are hidden from you.)" if hidden else ""
            return (f"No active quality problems found on this asset or its {upstreams} upstream "
                    f"dependencies.{extra} If numbers look wrong, the change may be a real business change.")
        top = causes[0]
        where = "on this asset itself" if top["degree"] == 0 else \
            f"{top['degree']} step{'s' if top['degree'] > 1 else ''} upstream in **{top['name']}**"
        more = f" {len(causes) - 1} other upstream signal(s) were found." if len(causes) > 1 else ""
        return f"Most likely cause: {top['reason'].split(' (')[0]}, {where}.{more}"


def explain_markdown(result: dict) -> str:
    lines = [result["summary_md"], ""]
    for c in result["likely_causes"][:5]:
        lines.append(f"- **{c['name']}** (score {c['score']}): {c['reason']}")
    return "\n".join(lines)


__all__ = ["LineageExplainer", "explain_markdown"]
