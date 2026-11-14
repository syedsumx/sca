"""Grafana dashboards in and out (``GET /sql/dashboards/{id}/grafana``, ``POST /sql/dashboards/import/grafana``).

**Export** writes Grafana's dashboard JSON (schema 39): PromQL and LogQL tiles become panels on the Prometheus and Loki
data sources, TraceQL tiles table panels on Tempo; each data source is a dashboard variable (``ds_prometheus``,
``ds_loki``, ``ds_tempo``) so the dashboard imports into any Grafana and its data sources are picked there. Views map
to panel types (timeseries, area and bars → ``timeseries``, stat, gauge, table, logs, heatmap), ``display`` to
``fieldConfig.defaults`` (unit, decimals, min, max, thresholds), the width to ``gridPos`` (1 → 12 columns, 2 → 24),
``variable`` controls to template variables (``label_values`` queries, custom lists) and the ``time`` control to the
time range and refresh. Tiles Grafana cannot run (SQL, metrics, workbook elements, SLOs, alerts, PostgreSQL instances:
they run in Cairn, as the viewer) become text panels that say so, in their place.

**Import** reads a Grafana dashboard (also in the ``{"dashboard": ...}`` envelope its API answers, with collapsed
rows and old ``rows``): every panel on Prometheus, Loki or Tempo (TraceQL) becomes a tile; hidden targets are left
out; query and custom variables become ``variable`` controls; the time range and refresh the ``time`` control.
What cannot be a tile (other data sources, text panels, more than 24 tiles) is listed in ``skipped`` with why.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from cairn.observability.dashboards import (DEFAULT_VIEW, RELATIVE, SOURCES, VAR_VALUE, OpsError, seconds, source_of,
                                            validate_control, validate_tile)

SCHEMA_VERSION = 39
DS = {"promql": ("prometheus", "ds_prometheus", "Prometheus"), "logql": ("loki", "ds_loki", "Loki"),
      "traces": ("tempo", "ds_tempo", "Tempo")}
PANEL = {"timeseries": "timeseries", "area": "timeseries", "bars": "timeseries", "stat": "stat", "gauge": "gauge",
         "table": "table", "logs": "logs", "heatmap": "heatmap"}
NOT_EXPORTABLE = {"query": "a SQL tile (a saved query)", "element": "a workbook tile", "metric": "a metric tile",
                  "slo": "an SLO tile", "alerts": "an alerts tile", "postgres": "a PostgreSQL instance tile"}
UNIT_OUT = {"count": "short"}


def _ds(src: str) -> dict:
    kind, var, _ = DS[src]
    return {"type": kind, "uid": "${" + var + "}"}


def _thresholds(steps: list[dict]) -> dict:
    out = [{"color": s["color"], "value": s["value"]} for s in steps]
    if not out or out[0]["value"] is not None:
        out.insert(0, {"color": "green", "value": None})
    return {"mode": "absolute", "steps": out}


def _panel(t: dict, pid: int, grid: dict) -> tuple[dict, Optional[str]]:
    src = None
    try:
        src = source_of(t)
    except OpsError:
        pass
    if src not in DS:
        what = next((v for k, v in NOT_EXPORTABLE.items() if t.get(k) is not None), "this tile")
        return ({"id": pid, "type": "text", "title": t.get("title") or "", "gridPos": grid,
                 "options": {"mode": "markdown", "content": f"Not exportable to Grafana: {what} runs in Cairn, as the "
                                                            "person looking. Open the dashboard in Cairn to see it."}},
                what)
    spec, view, display = t[src], t.get("view") or DEFAULT_VIEW[src], t.get("display") or {}
    panel: dict[str, Any] = {"id": pid, "type": PANEL[view], "title": t.get("title") or "", "gridPos": grid,
                             "datasource": _ds(src)}
    defaults: dict[str, Any] = {}
    if display.get("unit"):
        defaults["unit"] = UNIT_OUT.get(display["unit"], display["unit"])
    for k in ("decimals", "min", "max"):
        if display.get(k) is not None:
            defaults[k] = display[k]
    if display.get("thresholds"):
        defaults["thresholds"] = _thresholds(display["thresholds"])
    if view in ("timeseries", "area", "bars"):
        defaults["custom"] = {"drawStyle": "bars" if view == "bars" else "line",
                              "fillOpacity": 30 if view == "area" else (80 if view == "bars" else 0)}
    panel["fieldConfig"] = {"defaults": defaults, "overrides": []}
    refs = "ABCDEFGH"
    if src == "promql":
        panel["targets"] = [{"refId": refs[i], "datasource": _ds(src), "expr": q["expr"],
                             "legendFormat": q["legend"] or "__auto", "range": not spec["instant"],
                             "instant": spec["instant"], **({"interval": spec["step"]} if spec.get("step") else {})}
                            for i, q in enumerate(spec["queries"])]
    elif src == "logql":
        panel["targets"] = [{"refId": refs[i], "datasource": _ds(src), "expr": q["expr"], "queryType": "range",
                             "legendFormat": q["legend"], **({"maxLines": spec["limit"]} if spec["mode"] == "logs" else {}),
                             **({"step": spec["step"]} if spec.get("step") else {})}
                            for i, q in enumerate(spec["queries"])]
    else:
        panel["targets"] = [{"refId": "A", "datasource": _ds(src), "queryType": "traceql", "query": spec["query"],
                             "limit": spec["limit"], "tableType": "traces"}]
    return panel, None


def to_grafana(board: dict) -> dict:
    """A Cairn dashboard as Grafana dashboard JSON (``skipped`` lists the tiles left as text panels)."""
    panels, skipped, used = [], [], set()
    x = y = 0
    for i, t in enumerate(board["tiles"]):
        w = 24 if t.get("width") == 2 else 12
        if x + w > 24:
            x, y = 0, y + 8
        panel, why = _panel(t, i + 1, {"h": 8, "w": w, "x": x, "y": y})
        x += w
        if x >= 24:
            x, y = 0, y + 8
        panels.append(panel)
        if why:
            skipped.append({"tile": i, "title": t.get("title") or "", "reason": f"{why} is not exportable"})
        else:
            used.add(source_of(t))
    variables: list[dict] = []
    for src in ("promql", "logql", "traces"):
        if src in used or (src == "promql" and any(c.get("kind") == "variable" and (c.get("query") or {}).get("source")
                                                    == "label_values" for c in board.get("controls") or [])):
            kind, var, label = DS[src]
            variables.append({"type": "datasource", "name": var, "label": label, "query": kind, "hide": 0,
                              "current": {}, "options": [], "refresh": 1})
    time_range, refresh = {"from": "now-1h", "to": "now"}, ""
    for c in board.get("controls") or []:
        if c.get("kind") == "time":
            time_range = {"from": f"now-{c['default']}", "to": "now"}
            refresh = f"{c['refresh']}s" if c.get("refresh") else ""
        elif c.get("kind") == "variable":
            variables.append(_variable(c))
            if (c.get("query") or {}).get("source") == "loki_label_values" and "logql" not in used:
                kind, var, label = DS["logql"]
                variables.insert(0, {"type": "datasource", "name": var, "label": label, "query": kind, "hide": 0,
                                     "current": {}, "options": [], "refresh": 1})
                used.add("logql")
    uid = re.sub(r"[^A-Za-z0-9_-]", "", "cairn-" + str(board.get("id") or ""))[:40]
    return {"id": None, "uid": uid, "title": board["name"], "description": board.get("description") or "",
            "tags": ["cairn"], "editable": True, "schemaVersion": SCHEMA_VERSION, "graphTooltip": 1,
            "time": time_range, "refresh": refresh, "timezone": "", "panels": panels,
            "templating": {"list": variables}, "annotations": {"list": []},
            "cairn": {"source": "cairn", "skipped": skipped}}


def _variable(c: dict) -> dict:
    cur = c.get("default") or ""
    base = {"name": c["id"], "label": c.get("label") or c["id"], "hide": 0, "multi": bool(c.get("multi")),
            "includeAll": False, "current": {"text": cur, "value": cur}, "skipUrlSync": False}
    q = c.get("query") or {}
    if q.get("source") == "label_values":
        text = f"label_values({q['match']}, {q['label']})" if q.get("match") else f"label_values({q['label']})"
        return {**base, "type": "query", "datasource": _ds("promql"), "definition": text, "refresh": 2, "sort": 1,
                "query": {"query": text, "refId": "PrometheusVariableQueryEditor-VariableQuery"}, "options": []}
    if q.get("source") == "loki_label_values":
        text = f"label_values({q['label']})"
        return {**base, "type": "query", "datasource": _ds("logql"), "definition": text, "refresh": 2, "sort": 1,
                "query": text, "options": []}
    if c.get("options"):
        return {**base, "type": "custom", "query": ",".join(c["options"]),
                "options": [{"text": o, "value": o, "selected": o == cur} for o in c["options"]]}
    return {**base, "type": "textbox", "query": cur, "options": []}


# ---- import ------------------------------------------------------------------------------------------------------
def _panels(doc: dict) -> list[dict]:
    out = []
    for p in doc.get("panels") or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "row":
            out += [x for x in p.get("panels") or [] if isinstance(x, dict)]
        else:
            out.append(p)
    for row in doc.get("rows") or []:                     # dashboards from before schema 16
        out += [x for x in (row or {}).get("panels") or [] if isinstance(x, dict)]
    return out


def _ds_kind(ref: Any, ds_vars: dict[str, str]) -> Optional[str]:
    if isinstance(ref, dict):
        if ref.get("type") in ("prometheus", "loki", "tempo"):
            return ref["type"]
        uid = str(ref.get("uid") or "")
    elif isinstance(ref, str):
        uid = ref
    else:
        return None
    m = re.match(r"^\$\{?([A-Za-z0-9_]+)\}?$", uid)
    if m and m.group(1) in ds_vars:
        return ds_vars[m.group(1)]
    low = uid.lower()
    return next((k for k in ("prometheus", "loki", "tempo") if k in low), None)


def _expr(text: str) -> str:
    text = re.sub(r"\[\[([A-Za-z0-9_]+)(?::[a-z]+)?\]\]", r"$\1", text)          # [[var]] (old syntax)
    return re.sub(r"\$\{([A-Za-z0-9_]+):[a-z]+\}", r"${\1}", text)               # ${var:format}


def _color(c: Any) -> str:
    c = str(c or "")
    if re.match(r"^#[0-9a-fA-F]{6}$", c):
        return c
    for name in ("red", "orange", "yellow", "green", "blue", "purple"):
        if name in c.lower():
            return name
    return "grey"


def _display(p: dict) -> dict:
    d = (p.get("fieldConfig") or {}).get("defaults") or {}
    out: dict[str, Any] = {}
    unit = d.get("unit")
    if isinstance(unit, str) and re.match(r"^[A-Za-z0-9_:./%-]{1,32}$", unit):
        out["unit"] = unit
    if isinstance(d.get("decimals"), int) and 0 <= d["decimals"] <= 6:
        out["decimals"] = d["decimals"]
    for k in ("min", "max"):
        if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool):
            out[k] = d[k]
    steps = ((d.get("thresholds") or {}).get("steps") or [])[:10]
    if steps and (d.get("thresholds") or {}).get("mode", "absolute") == "absolute":
        out["thresholds"] = [{"value": s.get("value") if isinstance(s.get("value"), (int, float)) else None,
                              "color": _color(s.get("color"))} for s in steps if isinstance(s, dict)]
    return out


def _view(p: dict, src: str, mode: str = "") -> str:
    t = p.get("type")
    custom = ((p.get("fieldConfig") or {}).get("defaults") or {}).get("custom") or {}
    if src == "traces":
        return "table"
    if src == "logql" and mode == "logs":
        return "logs"
    if t in ("timeseries", "graph"):
        if custom.get("drawStyle") == "bars" or p.get("bars"):
            return "bars"
        return "area" if (custom.get("fillOpacity") or 0) > 0 or (p.get("fill") or 0) > 1 else "timeseries"
    return {"stat": "stat", "singlestat": "stat", "gauge": "gauge", "bargauge": "gauge", "table": "table",
            "heatmap": "heatmap", "barchart": "bars"}.get(t, "timeseries")


def from_grafana(raw: Any) -> tuple[dict, list[dict]]:
    """A Grafana dashboard as the body of a Cairn dashboard, and the panels and variables left out (with why)."""
    if not isinstance(raw, dict):
        raise OpsError("a Grafana dashboard is a JSON object", 422)
    doc = raw.get("dashboard") if isinstance(raw.get("dashboard"), dict) else raw
    skipped: list[dict] = []
    templ = [v for v in (doc.get("templating") or {}).get("list") or [] if isinstance(v, dict)]
    ds_vars = {v["name"]: str(v.get("query") or "") for v in templ if v.get("type") == "datasource" and v.get("name")}
    tiles: list[dict] = []
    for p in _panels(doc):
        title = str(p.get("title") or "")[:256]
        if p.get("type") == "text":
            skipped.append({"panel": title, "reason": "a text panel is not a tile"})
            continue
        targets = [x for x in p.get("targets") or [] if isinstance(x, dict) and not x.get("hide")]
        if not targets:
            skipped.append({"panel": title, "reason": "no query"})
            continue
        kind = _ds_kind(targets[0].get("datasource"), ds_vars) or _ds_kind(p.get("datasource"), ds_vars)
        if kind is None and targets[0].get("queryType") == "traceql":
            kind = "tempo"
        if kind is None and targets[0].get("expr"):
            kind = "loki" if p.get("type") == "logs" else "prometheus"
        try:
            if kind == "prometheus":
                step = targets[0].get("interval")
                tile: dict[str, Any] = {"promql": {
                    "queries": [{"expr": _expr(str(x.get("expr") or "")), "legend": "" if x.get("legendFormat") in
                                 (None, "__auto") else str(x["legendFormat"])[:200]} for x in targets[:8]],
                    "instant": all(bool(x.get("instant")) and not x.get("range") for x in targets),
                    "step": step if isinstance(step, str) and RELATIVE.match(step) else None}}
                src = "promql"
            elif kind == "loki":
                first = str(targets[0].get("expr") or "")
                mode = "logs" if p.get("type") == "logs" or first.lstrip().startswith("{") else "metric"
                limit = targets[0].get("maxLines") if isinstance(targets[0].get("maxLines"), int) else 100
                tile = {"logql": {"queries": [{"expr": _expr(str(x.get("expr") or "")),
                                               "legend": str(x.get("legendFormat") or "")[:200]} for x in targets[:8]],
                                  "mode": mode, "limit": max(1, min(limit, 5000))}}
                src = "logql"
            elif kind == "tempo" and targets[0].get("queryType") in ("traceql", "traceqlSearch", None) \
                    and targets[0].get("query"):
                limit = targets[0].get("limit") if isinstance(targets[0].get("limit"), int) else 20
                tile = {"traces": {"query": _expr(str(targets[0]["query"])), "limit": max(1, min(limit, 500))}}
                src = "traces"
            else:
                skipped.append({"panel": title, "reason": f"its data source ({kind or 'unknown'}) is not Prometheus, "
                                                          "Loki or Tempo (TraceQL)"})
                continue
            if len(targets) > 8:
                skipped.append({"panel": title, "reason": f"{len(targets) - 8} queries past the 8 a tile holds"})
            tile.update({"view": _view(p, src, tile[src].get("mode", "")), "display": _display(p)})
            clean = validate_tile(tile)
        except OpsError as e:
            skipped.append({"panel": title, "reason": str(e)})
            continue
        w = (p.get("gridPos") or {}).get("w") or p.get("span", 6) * 2
        tiles.append({**clean, "title": title or f"Panel {len(tiles) + 1}", "width": 2 if w >= 18 else 1})
    if len(tiles) > 24:
        skipped += [{"panel": t["title"], "reason": "a dashboard holds 24 tiles"} for t in tiles[24:]]
        tiles = tiles[:24]
    if not tiles:
        raise OpsError("nothing in this dashboard can be a tile: " +
                       "; ".join(f"{s['panel'] or 'a panel'}: {s['reason']}" for s in skipped[:5]), 422)
    return {"name": str(doc.get("title") or "Imported from Grafana")[:256],
            "description": str(doc.get("description") or "")[:2000], "tiles": tiles,
            "controls": _controls(doc, templ, skipped)}, skipped


def _controls(doc: dict, templ: list[dict], skipped: list[dict]) -> list[dict]:
    controls: list[dict] = []
    t = doc.get("time") or {}
    m = re.match(r"^now-(\d+[smhdw])$", str(t.get("from") or ""))
    default = m.group(1) if m and str(t.get("to") or "now") == "now" else "1h"
    refresh = 0
    if isinstance(doc.get("refresh"), str) and RELATIVE.match(doc["refresh"]):
        refresh = seconds(doc["refresh"])
    controls.append(validate_control({"kind": "time", "label": "Time range", "default": default, "refresh": refresh},
                                     "time"))
    for v in templ:
        name, typ = str(v.get("name") or ""), v.get("type")
        if typ == "datasource":
            continue
        if not re.match(r"^[a-z0-9_-]{1,40}$", name) or name == "time":
            skipped.append({"variable": name, "reason": "a control id is a-z, 0-9, _ and - (and not time)"})
            continue
        cur = (v.get("current") or {}).get("value")
        cur = cur[0] if isinstance(cur, list) and cur else cur
        cur = cur if isinstance(cur, str) and cur not in ("$__all",) and VAR_VALUE.match(cur) else None
        body: dict[str, Any] = {"kind": "variable", "label": v.get("label") or name, "default": cur,
                                "multi": bool(v.get("multi"))}
        if typ == "query":
            q = v.get("query")
            q = q.get("query") if isinstance(q, dict) else q
            m = re.match(r"^\s*label_values\(\s*(?:(.+?)\s*,\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*$", str(q or ""))
            kind = _ds_kind(v.get("datasource"), {})
            if not m:
                skipped.append({"variable": name, "reason": "only label_values(...) query variables are imported"})
                continue
            body["query"] = {"source": "loki_label_values" if kind == "loki" else "label_values", "label": m.group(2),
                             **({"match": m.group(1)} if m.group(1) and kind != "loki" else {})}
        elif typ == "custom":
            body["options"] = [o.strip() for o in str(v.get("query") or "").split(",") if o.strip()
                               and VAR_VALUE.match(o.strip())][:500]
        elif typ in ("textbox", "constant"):
            body["default"] = cur if cur is not None else (str(v.get("query") or "") if VAR_VALUE.match(
                str(v.get("query") or "")) else None)
        else:
            skipped.append({"variable": name, "reason": f"{typ} variables are not imported ($__interval and "
                                                         "$__rate_interval are built in)"})
            continue
        try:
            controls.append(validate_control(body, name))
        except OpsError as e:
            skipped.append({"variable": name, "reason": str(e)})
        if len(controls) >= 12:
            break
    return controls


__all__ = ["to_grafana", "from_grafana", "SOURCES"]
