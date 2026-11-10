"""A stand-in for the model provider's Messages API, for checking the live-model suite's plumbing without a model.

``tests/test_live_model.py`` normally reaches the real provider through Cairn's AI gateway. Pointed at this server
instead (``CAIRN_LIVE_PROVIDER_URL=http://127.0.0.1:<port>``), the same suite runs end to end: the gateway reads the
provider key from its file and sends it here (anything else gets a 401), every feature's request goes through the
gateway as the person, and the usage report is written. The answers are scripted from what each feature sends (its
system prompt, tools and listed tables), so a pass here proves the wiring only, never a model's quality.

    python tests/live_model_standin.py --port 8899 --key-file /path/to/key-file
"""

from __future__ import annotations

import argparse
import http.server
import json
import re
from typing import Any

FLOW_AND_QUERY = """queries:
  - {name: live-revenue-by-region, connection: lakehouse,
     sql: "SELECT region, sum(amount) AS revenue FROM sales.orders GROUP BY region", description: Revenue by region}
flows:
  - name: live-nightly-revenue
    spec:
      triggers: [{type: schedule, cron: "0 2 * * *", timezone: UTC}]
      steps:
        - {name: refresh, type: sql, sql: "SELECT region, sum(amount) AS revenue FROM sales.orders GROUP BY region",
           connection: lakehouse}
        - {name: tell, type: notify, after: [refresh], message: "revenue refreshed"}
"""


def example(schema: dict) -> Any:
    """A value that satisfies ``schema`` (the subset Cairn's schemas use)."""
    t = schema.get("type")
    t = next((x for x in t if x != "null"), "null") if isinstance(t, list) else t
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        return {k: example(v) for k, v in (schema.get("properties") or {}).items()
                if k in (schema.get("required") or [])}
    if t == "array":
        return []
    return {"string": "x", "integer": 1, "number": 1.0, "boolean": False, "null": None}.get(t)


def _results(body: dict) -> list[dict]:
    return [b for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


def _user_text(body: dict) -> str:
    out = []
    for m in body["messages"]:
        if m["role"] == "user":
            c = m["content"]
            out.append(c if isinstance(c, str) else " ".join(str(b.get("text") or b.get("content") or "")
                                                               for b in c if isinstance(b, dict)))
    return "\n".join(out)


def _system(body: dict) -> str:
    s = body.get("system") or ""
    return s if isinstance(s, str) else " ".join(b.get("text", "") for b in s)


def structured(system: str, user: str, schema: dict) -> dict:
    if "You design a dashboard" in system:
        metric = re.search(r"^- (\w+) \(.*?\) on ([\w.]+):.*?dimensions ([\w, ]+)", user, re.M)
        table = re.search(r"^- ([\w.]+) \(([^)]*)\)\s*$", user, re.M)
        tiles = []
        if metric:
            tiles.append({"kind": "metric", "title": "By region",
                          "metric": {"metrics": [metric.group(1)], "dimensions": [metric.group(3).split(",")[0].strip()]}})
        if table:
            col = "status" if "status" in table.group(2) else table.group(2).split(",")[0].strip()
            tiles.append({"kind": "element", "title": f"Rows by {col}", "chart": {"type": "bar", "x": col, "y": ["Rows"]},
                          "element": {"source": {"table": table.group(1)}, "group_by": [col],
                                      "aggregates": [{"name": "Rows", "formula": "Count()"}], "sort": [{"column": col}]}})
        return {"title": "Revenue overview", "tiles": tiles, "controls": []}
    if "You are a data engineer" in system:
        current = re.search(r"SQL: (SELECT .*)$", user, re.M)
        missing = re.search(r"Column '(\w+)' cannot be resolved", user)
        if current and missing:                       # a fix: the column that does not exist is replaced
            sql = re.sub(rf"\b{missing.group(1)}\b", "1.1e0", current.group(1))
            return {"sql": sql, "mode": "full", "key": [], "watermark": None, "every_minutes": 60, "on_change": False,
                    "checks": [], "explanation": "Replaced a column that does not exist."}
        table = re.search(r"^- ([\w.]+) \(", user, re.M)
        return {"sql": f"SELECT region, sum(amount) AS revenue FROM {table.group(1)} WHERE status = 'paid' GROUP BY region",
                "mode": "merge", "key": ["region"], "watermark": None, "every_minutes": None, "on_change": True,
                "checks": [{"name": "no_null_region", "sql": "SELECT count(*) FROM candidate WHERE region IS NULL"}],
                "explanation": "Paid revenue per region."}
    if "arithmetic" in user:                       # the LLM layer's own check
        return {"answer": 51, "capital": "Paris"}
    return example(schema)


def _used(body: dict) -> list[dict]:
    """The tool calls made so far in this conversation, with their results: [{name, input, ok, evidence, data}]."""
    by_id = {b["tool_use_id"]: b for b in _results(body)}
    out = []
    for m in body["messages"]:
        if m["role"] != "assistant" or not isinstance(m["content"], list):
            continue
        for b in m["content"]:
            if b.get("type") != "tool_use":
                continue
            r = by_id.get(b["id"]) or {}
            text = r.get("content") if isinstance(r.get("content"), str) else json.dumps(r.get("content"))
            ev = re.match(r'^\{"evidence_id": "(e\d+)"\}\n', text or "")
            data = None
            if ev:
                try:
                    data = json.loads(text[ev.end():])
                except ValueError:
                    data = None
            out.append({"name": b["name"], "input": b.get("input") or {}, "ok": not r.get("is_error"),
                        "evidence": ev.group(1) if ev else None, "data": data, "text": text or ""})
    return out


def sre_turn(body: dict) -> list[dict]:
    """Cairn's SRE agent: read the incident (and the instance it points at), write a finding citing the evidence,
    rank the causes, propose the remediation the diagnosis suggests, then summarise. An ask: check, then answer."""
    used = _used(body)
    names = {u["name"] for u in used}
    user = _user_text(body)
    inst = re.search(r'"pg_instance": "([a-z0-9-]+)"', user) or re.search(r"Instance ([a-z0-9-]+)\.", user)
    services = re.search(r'"services": \[([^\]]*)\]', user)
    if not inst and services:
        inst = re.search(r'"([a-z0-9-]+-pg)"', services.group(1))
    instance = inst.group(1) if inst else None
    asking = "The on-call person asks:" in user or "The person asks:" in user
    if asking:
        if not used:
            calls = [{"type": "tool_use", "id": "tu_s1", "name": "pg_status" if instance else "logs",
                      "input": {"instance": instance} if instance else {"service": "cairn-api", "level": "ERROR"}}]
            return calls
        u = used[-1]
        if not u["ok"]:
            return [{"type": "text", "text": f"I could not check: {u['name']} failed ({u['text'][:200]})."}]
        health = (u["data"] or {}).get("health") if isinstance(u["data"], dict) else None
        return [{"type": "text", "text": f"{instance or 'The component'} is {health or 'as the logs show'} [{u['evidence']}]."}]
    if not used:
        calls = [{"type": "tool_use", "id": "tu_ctx", "name": "incident_context", "input": {}}]
        if instance:
            calls.append({"type": "tool_use", "id": "tu_diag", "name": "pg_diagnose", "input": {"instance": instance}})
        else:
            calls.append({"type": "tool_use", "id": "tu_logs", "name": "logs",
                          "input": {"service": "cairn-api", "level": "ERROR", "since_minutes": 30}})
        return calls
    if "write_note" not in names:
        diag = next((u for u in used if u["name"] == "pg_diagnose" and u["ok"]), None)
        failed = [u for u in used if not u["ok"]]
        evidence = [u["evidence"] for u in used if u["ok"] and u["evidence"]]
        if diag and isinstance(diag["data"], dict):
            d = diag["data"]
            facts = [f["text"] for f in d.get("findings") or []] or [f"{instance} is {d.get('verdict')}"]
            calls = [{"type": "tool_use", "id": "tu_note", "name": "write_note", "input": {
                "kind": "finding", "text": f"{instance}: " + " ".join(facts[:3]), "evidence": [diag["evidence"]]}}]
            causes = [{"cause": facts[0], "likelihood": 0.8, "evidence": [diag["evidence"]]}]
            if len(facts) > 1:
                causes.append({"cause": facts[1], "likelihood": 0.3, "evidence": [diag["evidence"]]})
            calls.append({"type": "tool_use", "id": "tu_rank", "name": "rank_causes", "input": {"causes": causes}})
            s = next(iter(d.get("suggestions") or []), None)
            if s and s["kind"] == "change_request":
                calls.append({"type": "tool_use", "id": "tu_prop", "name": "propose_change", "input": {
                    "title": s["title"], "intent": s["rationale"], "bundle_yaml": s["bundle_yaml"],
                    "evidence": [diag["evidence"]]}})
            elif s and s["kind"] == "runbook_action":
                calls.append({"type": "tool_use", "id": "tu_prop", "name": "propose_action", "input": {
                    "action": s["action"], "params": s["params"], "rationale": s["rationale"],
                    "evidence": [diag["evidence"]]}})
            return calls
        text = ("I could not read everything: " + "; ".join(f"{u['name']} failed" for u in failed)) if failed else \
            "The incident's context shows no component at fault yet."
        return [{"type": "tool_use", "id": "tu_note", "name": "write_note",
                 "input": {"kind": "finding", "text": text, "evidence": evidence[:3]}}]
    proposed = [u for u in used if u["name"] in ("propose_change", "propose_action")]
    if proposed and proposed[-1]["ok"]:
        return [{"type": "text", "text": f"Most likely cause found on {instance}; I proposed a remediation "
                                         f"({proposed[-1]['data'].get('proposal')}) that waits for a person's approval."}]
    return [{"type": "text", "text": "I wrote what I found; I proposed nothing."}]


def postmortem_text(user: str) -> str:
    change = re.search(r"Proposed change request: ([^(\\\n]+)", user)
    action = re.search(r"Proposed: ([^.\\\n]+)", user)
    fix = (change or action).group(1).strip() if (change or action) else "unknown"
    return (f"## Cause\n\nThe agent's ranked causes point at the database: {fix}.\n\n## What fixed it\n\n"
            f"{fix}, approved by a person and applied.\n\n## Follow-ups\n\n- Add an alert on slow statements (owner: "
            "database team)\n- Review the runbook (owner: on-call)\n")


def tool_turn(body: dict) -> list[dict]:
    names = [t["name"] for t in body.get("tools") or []]
    if "rank_causes" in names or ("pg_diagnose" in names and "Cairn's SRE agent" in _system(body)):
        return sre_turn(body)
    done = _results(body)
    user = _user_text(body)
    urns = re.findall(r"urn:li:dataset:\([^)]*\)", user)
    if "iac_propose" in names:
        if not done:
            return [{"type": "tool_use", "id": "tu_plan", "name": "iac_plan", "input": {"bundle_yaml": FLOW_AND_QUERY}}]
        if len(done) == 1:
            return [{"type": "tool_use", "id": "tu_propose", "name": "iac_propose", "input": {
                "title": "Revenue query and nightly flow", "intent": "A saved query and a nightly flow, as asked",
                "bundle_yaml": FLOW_AND_QUERY}}]
        return [{"type": "text", "text": "I proposed a saved query and a nightly flow; they wait for approval."}]
    if names == ["get_quality_status"]:
        if not done:
            return [{"type": "tool_use", "id": "tu_q", "name": "get_quality_status",
                     "input": {"dataset_urn": urns[0] if urns else ""}}]
        return [{"type": "text", "text": f"Quality of [{urns[0] if urns else ''}] checked; I cannot change tags."}]
    if "search_assets" in names:
        if not done:
            return [{"type": "tool_use", "id": "tu_s", "name": "search_assets", "input": {"query": "orders"}}]
        cited = re.findall(r"urn:li:dataset:\([^)]*\)", json.dumps(done))
        return [{"type": "text", "text": f"Orders live in [{cited[0]}]." if cited else "No orders dataset found."}]
    return [{"type": "text", "text": "done"}]


def reply(body: dict) -> list[dict]:
    system = _system(body)
    fmt = ((body.get("output_config") or {}).get("format") or {})
    if fmt.get("type") == "json_schema":
        return [{"type": "text", "text": json.dumps(structured(system, _user_text(body), fmt["schema"]))}]
    if body.get("tools"):
        return tool_turn(body)
    if "blameless postmortem" in system:
        return [{"type": "text", "text": postmortem_text(_user_text(body))}]
    if "You plan research" in system:
        return [{"type": "text", "text": '{"steps": ["Which tables hold orders?"]}'}]
    if "Write the research report" in system:
        cited = sorted(set(re.findall(r"urn:li:dataset:\([^)]*\)", _user_text(body))))
        return [{"type": "text", "text": "# Orders\n\n" + " ".join(f"[{u}]" for u in cited) + " hold the orders."}]
    return [{"type": "text", "text": "done"}]


def handler(key: str):
    class Provider(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.headers.get("x-api-key") != key:
                return self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})
            if self.path != "/v1/messages":
                return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})
            body = json.loads(raw)
            content = [{"type": "thinking", "thinking": "", "signature": "standin"}] + reply(body)
            stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
            return self._json(200, {"id": "msg_standin", "type": "message", "role": "assistant", "model": body["model"],
                                    "content": content, "stop_reason": stop, "stop_sequence": None,
                                    "usage": {"input_tokens": len(raw) // 4, "output_tokens": len(json.dumps(content)) // 4}})

        def _json(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    return Provider


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--key-file", required=True, help="the provider key the gateway must send (read from a file)")
    a = ap.parse_args()
    with open(a.key_file) as f:
        key = f.read().strip()
    http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler(key)).serve_forever()


if __name__ == "__main__":
    main()
