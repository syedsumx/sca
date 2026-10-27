"""Golden-case evaluations for the AI features (``cairn ai eval``).

Each case gives a small catalog, a question (or an asset to document), optionally the output a
model returns, and the properties the feature must hold. Cases run offline against a scripted
model by default, which checks the guardrails deterministically: what reaches the prompt (PII
redacted, catalog text wrapped as untrusted, hidden assets absent) and what reaches the user
(no hallucinated citations, no invented columns, nothing the asker may not see). With
``--live`` the same cases run against the configured provider, and the report adds tokens,
estimated cost and latency so a model or prompt change can be compared before it ships.

Case files are YAML (``cases:`` list) in ``config/ai-evals``::

    - id: nlq-hides-restricted
      feature: nlq                       # nlq | docgen | explain | semantic
      question: which tables have salary data?
      catalog:
        - {urn: ..., name: salaries, description: ..., fields: [id, salary]}
      hidden: [urn:li:dataset:...]       # the asker may not view these
      lineage: {<urn>: {upstream: [...], downstream: [...]}}
      incidents: [{urn: ..., summary: ...}]   # open run anomalies (explain)
      model_output:                      # scripted model replies (offline mode)
        plan: {...}
        answer: {answer: ..., citations: [...]}
      expect:
        planner: llm
        prompt_excludes: [ops@corp.com]
        prompt_includes: [<catalog_metadata>]
        results_exclude: [urn:li:dataset:...]
        citations_within_results: true
        answer_excludes: [...]
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from cairn.ai.catalog import Catalog
from cairn.ai.docgen import DRAFT_SCHEMA, DocGenerator
from cairn.ai.llm import LLMError, StaticProvider
from cairn.ai.nlq import ANSWER_SCHEMA, NaturalLanguageQuery
from cairn.ai.usage import MeteredProvider, UsageMeter, set_actor
from cairn.common.core import InMemoryGraph

EVAL_ACTOR = "urn:li:corpuser:cairn-eval"
FEATURES = {"nlq", "docgen", "explain", "semantic"}
_EXPECT_KEYS = {"planner", "prompt_excludes", "prompt_includes", "results_include", "results_exclude",
                "citations_within_results", "answer_excludes", "answer_includes", "columns_within_schema",
                "confidence_between", "max_cost_usd", "max_latency_ms", "causes_include", "causes_exclude",
                "first_result"}


class EvalCaseError(ValueError):
    pass


def load_cases(path: str) -> list[dict]:
    p = Path(path)
    files = sorted(p.glob("*.y*ml")) if p.is_dir() else [p]
    cases: list[dict] = []
    for f in files:
        doc = yaml.safe_load(f.read_text()) or {}
        for case in doc.get("cases") or []:
            case = dict(case, _file=f.name)
            if not case.get("id"):
                raise EvalCaseError(f"{f.name}: every case needs an id")
            if case.get("feature") not in FEATURES:
                raise EvalCaseError(f"{f.name}:{case['id']}: feature must be one of {sorted(FEATURES)}")
            unknown = set(case.get("expect") or {}) - _EXPECT_KEYS
            if unknown:
                raise EvalCaseError(f"{f.name}:{case['id']}: unknown expectations {sorted(unknown)}")
            cases.append(case)
    ids = [c["id"] for c in cases]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise EvalCaseError(f"duplicate case ids: {dupes}")
    return cases


# ---- a catalog built from the case ---------------------------------------------------------
def _entity(e: dict) -> dict:
    urn = e["urn"]
    platform = e.get("platform") or (urn.split("dataPlatform:")[1].split(",")[0] if "dataPlatform:" in urn else "")
    out = {"urn": urn, "type": e.get("type", "DATASET"), "name": e.get("name", urn), "platform": {"name": platform},
           "properties": {"name": e.get("name", urn), "description": e.get("description", "")}}
    if e.get("fields"):
        out["schemaMetadata"] = {"fields": [{"fieldPath": f, "nativeDataType": "VARCHAR"} for f in e["fields"]]}
    return out


def _graph(case: dict) -> InMemoryGraph:
    entities = {e["urn"]: _entity(e) for e in case.get("catalog") or []}
    lineage = case.get("lineage") or {}           # {urn: {upstream: [...], downstream: [...]}}
    graph = InMemoryGraph()

    def handler(query: str, variables: dict) -> dict:
        if "searchAcrossLineage" in query:
            inp = variables["input"]
            hops = (lineage.get(inp["urn"]) or {}).get(inp.get("direction", "DOWNSTREAM").lower(), [])
            return {"searchAcrossLineage": {"searchResults": [{"degree": 1, "entity": entities[u]}
                                                              for u in hops if u in entities]}}
        if "facets" in query:
            platforms = sorted({e["platform"]["name"] for e in entities.values()})
            return {"searchAcrossEntities": {"facets": [{"field": "platform", "aggregations": [
                {"value": f"urn:li:dataPlatform:{p}", "count": 1} for p in platforms]}]}}
        if "searchAcrossEntities" in query:
            q = ((variables.get("input") or variables.get("i") or {}).get("query") or "*").lower()
            words = [w for w in q.replace("*", " ").split() if len(w) > 2]
            hits = [e for e in entities.values()
                    if not words or any(w.rstrip("s") in json.dumps(e).lower() for w in words)]
            return {"searchAcrossEntities": {"total": len(hits), "searchResults": [{"entity": e} for e in hits]}}
        if "entities(urns" in query:
            return {"entities": [entities[u] for u in variables.get("urns", []) if u in entities]}
        if "entity(urn" in query:
            return {"entity": entities.get(variables["urn"])}
        return {}

    graph.graphql_handler = handler
    for e in case.get("catalog") or []:
        if e.get("fields"):
            graph.aspects[(e["urn"], "schemaMetadata")] = {"fields": [{"fieldPath": f} for f in e["fields"]]}
    return graph


class _Recorder:
    """Records every prompt sent to the provider it wraps."""

    def __init__(self, inner):
        self.inner = inner
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return self.inner.name

    def __getattr__(self, item):
        return getattr(self.inner, item)

    def generate_json(self, system, user, schema, *, model=None, max_tokens=16000):
        self.prompts.append(user)
        return self.inner.generate_json(system, user, schema, model=model, max_tokens=max_tokens)


def _scripted(case: dict) -> StaticProvider:
    out = case.get("model_output") or {}

    def respond(system: str, user: str, schema: dict) -> dict:
        props = schema.get("properties", {})
        if schema is DRAFT_SCHEMA or "unknowns" in props:
            key = "draft"
        elif schema is ANSWER_SCHEMA or "citations" in props:
            key = "answer"
        elif "summary_md" in props:
            key = "narrative"
        else:
            key = "plan"
        if key not in out:
            raise LLMError(f"no scripted {key} output")   # the feature falls back to its non-AI path
        return out[key]

    return StaticProvider(respond)


# ---- running a case ---------------------------------------------------------------------------
def _run_feature(case: dict, llm, graph, embedder=None) -> dict:
    hidden = set(case.get("hidden") or [])
    can_view = (lambda u: u not in hidden)    # noqa: E731
    catalog = Catalog(graph)
    if case["feature"] == "semantic":
        from cairn.ai.embeddings import HashingEmbedder, InMemoryVectorStore
        from cairn.ai.semantic import SemanticIndex

        index = SemanticIndex(catalog, embedder or HashingEmbedder(), InMemoryVectorStore())
        index.index_many([e["urn"] for e in case.get("catalog") or []])
        hits = index.search(case["question"], int(case.get("limit", 3)), None, can_view)
        return {"results": [h["urn"] for h in hits], "citations": [], "answer": "",
                "matched": {h["urn"]: h.get("matched", []) for h in hits}}
    if case["feature"] == "nlq":
        out = NaturalLanguageQuery(catalog, llm).ask(case["question"], can_view)
        return {"planner": out["planner"], "answer": out["answer"], "citations": out["citations"],
                "results": [r["urn"] for r in out["results"]]}
    if case["feature"] == "docgen":
        draft = DocGenerator(catalog, llm).draft(case["urn"])
        return {"answer": draft["description"], "columns": [c["field_path"] for c in draft["columns"]],
                "confidence": draft["confidence"], "results": [], "citations": []}
    from cairn.ai.lineage_explainer import LineageExplainer

    class _NoMonitors:
        def list_monitors(self, *_a, **_k):
            return []

    class _Findings:   # open run anomalies from the case: [{urn, summary}]
        def list(self, *_a, **_k):
            return [{"id": f"finding-{i}", "entities": [f["urn"]], "summary_md": f["summary"]}
                    for i, f in enumerate(case.get("incidents") or [])]

    out = LineageExplainer(catalog, _NoMonitors(), _Findings(), llm, None, can_view).explain(case["urn"])
    return {"answer": out["summary_md"], "results": [c["urn"] for c in out["likely_causes"]], "citations": [],
            "planner": "llm" if out["used_llm"] else "heuristic"}


def _check(case: dict, got: dict, prompts: list[str], cost: float, latency_ms: float) -> list[str]:
    exp = case.get("expect") or {}
    problems: list[str] = []
    prompt_text = "\n".join(prompts)
    if "first_result" in exp and (got["results"][:1] or [None])[0] != exp["first_result"]:
        problems.append(f"first result {(got['results'][:1] or [None])[0]} != {exp['first_result']}")
    if "planner" in exp and got.get("planner") != exp["planner"]:
        problems.append(f"planner {got.get('planner')!r} != {exp['planner']!r}")
    for s in exp.get("prompt_excludes") or []:
        if s in prompt_text:
            problems.append(f"prompt contains {s!r}")
    for s in exp.get("prompt_includes") or []:
        if prompts and s not in prompt_text:
            problems.append(f"prompt lacks {s!r}")
    for u in exp.get("results_include") or exp.get("causes_include") or []:
        if u not in got["results"]:
            problems.append(f"result {u} missing")
    for u in (exp.get("results_exclude") or []) + (exp.get("causes_exclude") or []):
        if u in got["results"] or u in got["citations"]:
            problems.append(f"{u} must not be returned")
    if exp.get("citations_within_results") and not set(got["citations"]) <= set(got["results"]):
        problems.append(f"citations outside results: {sorted(set(got['citations']) - set(got['results']))}")
    answer = (got.get("answer") or "").lower()
    for s in exp.get("answer_excludes") or []:
        if s.lower() in answer:
            problems.append(f"answer contains {s!r}")
    for s in exp.get("answer_includes") or []:
        if s.lower() not in answer:
            problems.append(f"answer lacks {s!r}")
    if exp.get("columns_within_schema"):
        known = {f for e in case.get("catalog") or [] if e["urn"] == case.get("urn") for f in e.get("fields") or []}
        extra = [c for c in got.get("columns") or [] if c not in known]
        if extra:
            problems.append(f"invented columns {extra}")
    if "confidence_between" in exp:
        lo, hi = exp["confidence_between"]
        if not lo <= got.get("confidence", -1) <= hi:
            problems.append(f"confidence {got.get('confidence')} outside [{lo}, {hi}]")
    if "max_cost_usd" in exp and cost > exp["max_cost_usd"]:
        problems.append(f"cost ${cost:.4f} > ${exp['max_cost_usd']}")
    if "max_latency_ms" in exp and latency_ms > exp["max_latency_ms"]:
        problems.append(f"latency {latency_ms:.0f}ms > {exp['max_latency_ms']}ms")
    return problems


def run_cases(cases: list[dict], provider=None, meter: Optional[UsageMeter] = None, embedder=None) -> list[dict]:
    """Run ``cases`` with ``provider`` (a live provider) or, when None, each case's scripted output."""
    from cairn.common.db import get_engine, init_db

    tmp = None
    if meter is None:
        tmp = tempfile.TemporaryDirectory()
        engine = get_engine(f"sqlite:///{tmp.name}/evals.db")
        init_db(engine)
        meter = UsageMeter(engine)
    set_actor(EVAL_ACTOR)
    results = []
    try:
        for case in cases:
            before = meter.summary(group_by="task")["total"]
            recorder = _Recorder(provider if provider is not None else _scripted(case))
            llm = MeteredProvider(recorder, meter)
            started = time.monotonic()
            try:
                got = _run_feature(case, llm, _graph(case), embedder)
                latency = (time.monotonic() - started) * 1000
                after = meter.summary(group_by="task")["total"]
                cost = after["cost_usd"] - before["cost_usd"]
                problems = _check(case, got, recorder.prompts, cost, latency)
                tokens = (after["input_tokens"] + after["output_tokens"]) - (before["input_tokens"]
                                                                            + before["output_tokens"])
            except Exception as e:  # noqa: BLE001 - a crashing feature is a failed case, not a crashed run
                latency, cost, tokens, problems = (time.monotonic() - started) * 1000, 0.0, 0, [
                    f"{type(e).__name__}: {e}"]
            results.append({"id": case["id"], "file": case["_file"], "feature": case["feature"],
                            "ok": not problems, "problems": problems, "model_calls": len(recorder.prompts),
                            "tokens": tokens, "cost_usd": round(cost, 6), "latency_ms": round(latency, 1)})
    finally:
        if tmp is not None:
            tmp.cleanup()
    return results


def summarize(results: list[dict]) -> dict[str, Any]:
    return {"cases": len(results), "passed": sum(r["ok"] for r in results),
            "failed": [r["id"] for r in results if not r["ok"]],
            "tokens": sum(r["tokens"] for r in results),
            "cost_usd": round(sum(r["cost_usd"] for r in results), 6),
            "latency_ms_p50": sorted(r["latency_ms"] for r in results)[len(results) // 2] if results else 0}
