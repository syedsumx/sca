"""Bundles: Cairn resources declared in YAML, kept in git, planned on a pull request and applied on merge.

```yaml
bundle: sales-lakehouse            # what this bundle is called (its state is kept under this name)
pipes:
  - {name: orders-files, bucket: landing, prefix: orders/, format: csv, target: raw.orders, create_table: true}
streams:
  - {name: customers-cdc, topic: pg.public.customers, format: debezium, key: [id], target: cdc.customers,
     create_table: true, source: "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.customers,PROD)"}
derived:
  - {name: revenue, target: marts.revenue, mode: full, on_change: true,
     sql: "SELECT region, sum(amount) AS total FROM raw.orders GROUP BY region"}
maintenance:
  - {scope: "*", optimize: true, expire_days: 7, orphan_days: 7, every_hours: 24}
vectors:
  - {name: articles, source: support.articles, key: id, text: body, every_minutes: 60}
queries:                           # saved queries, dashboards, alerts and SQL workflows are the token user's own
  - {name: revenue-by-region, connection: lakehouse, sql: "SELECT * FROM marts.revenue", chart: {type: bar, x: region, y: [total]}}
dashboards:
  - {name: sales, shared: true, tiles: [{query: revenue-by-region, width: 2}]}
alerts:
  - {name: revenue-drop, query: revenue-by-region, measure: total, op: "<", value: 1000, every_minutes: 60}
workflows:
  - name: nightly
    cron: "0 2 * * *"
    steps:
      - {name: refresh, type: derived, table: revenue}
      - {name: not-empty, type: check, query: revenue-by-region, op: ">", value: 0, after: [refresh]}
```

Dashboards, alerts and workflow steps name their saved queries (``query: <name>``): any of the token user's
saved queries, this bundle's or not; Cairn's ids are looked up at apply time.

``${NAME}`` is replaced from ``--var NAME=value``, then from the environment (a missing one is an error),
so one file serves several environments. ``cairn bundle validate`` checks the file alone; ``plan`` shows
what ``apply`` would create, update and (with ``--prune``) delete; ``apply`` does it. A resource is known by
its name (``scope`` for maintenance policies). Prune deletes only what this bundle created before (kept by
Cairn under the bundle's name), never resources made by hand or by other bundles. Each change is made
through Cairn's API as the token's user, so its permissions and audit apply; the bundle's state is
recorded after each change, so a failure halfway leaves an accurate record and the next apply finishes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

KINDS: dict[str, dict] = {
    "pipes": {"path": "/v1/pipes", "key": "name", "required": ("name", "bucket", "format", "target"),
              "defaults": {"prefix": "", "create_table": False, "enabled": True}},
    "streams": {"path": "/v1/streams", "key": "name", "required": ("name", "topic", "target"),
                "defaults": {"format": "json", "encoding": "json", "mode": "append", "key": [], "create_table": False, "evolve": False,
                             "start": "earliest", "source": None, "batch_rows": 5000, "batch_seconds": 10, "enabled": True}},
    "derived": {"path": "/v1/derived", "key": "name", "required": ("name", "sql", "target"),
                "defaults": {"mode": "full", "watermark": None, "key": [], "every_minutes": None, "on_change": False,
                             "enabled": True}},
    "maintenance": {"path": "/maintenance/policies", "key": "scope", "required": ("scope",),
                    "defaults": {"optimize": True, "optimize_threshold": "128MB", "expire_days": None, "orphan_days": None,
                                 "every_hours": 24, "enabled": True}},
    "vectors": {"path": "/v1/vectors", "key": "name", "required": ("name", "source", "key", "text"),
                "defaults": {"chunk_chars": 1500, "every_minutes": None, "enabled": True}, "fixed": ("source",)},
    # The token user's own: saved queries, dashboards, alerts and SQL workflows.
    "queries": {"path": "/sql/saved", "key": "name", "required": ("name", "sql", "connection"), "mine": True,
                "defaults": {"description": "", "shared": False, "chart": {}}},
    "dashboards": {"path": "/sql/dashboards", "key": "name", "required": ("name", "tiles"), "mine": True,
                   "defaults": {"description": "", "shared": False}},
    "alerts": {"path": "/sql/alerts", "key": "name", "required": ("name", "query", "op", "value"), "mine": True,
               "defaults": {"measure": "rows", "every_minutes": 60, "recipients": [], "enabled": True}},
    "workflows": {"path": "/v1/sql-workflows", "key": "name", "required": ("name", "steps"), "mine": True,
                  "defaults": {"cron": None, "recipients": [], "notify": "failure", "enabled": True}},
}
# Created and changed in this order (derived tables may read the others' tables; dashboards, alerts and
# workflows name saved queries); deleted in the reverse order.
ORDER = ("maintenance", "pipes", "streams", "derived", "vectors", "queries", "dashboards", "alerts", "workflows")
CHART = {"type": "table", "x": "", "y": []}
STEP = {"after": [], "retries": 0}
VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
BUNDLE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


class BundleError(Exception):
    pass


@dataclass
class Change:
    action: str          # create | update | delete | keep
    kind: str
    key: str
    diff: dict = field(default_factory=dict)   # field: [current, desired]
    desired: Optional[dict] = None
    current: Optional[dict] = None

    def line(self) -> str:
        sign = {"create": "+", "update": "~", "delete": "-", "keep": " "}[self.action]
        out = f"{sign} {self.kind}/{self.key}"
        for k, (a, b) in sorted(self.diff.items()):
            out += f"\n      {k}: {json.dumps(a)} -> {json.dumps(b)}"
        return out


def _subst(v: Any, vars_: dict) -> Any:
    if isinstance(v, str):
        def one(m):
            name = m.group(1)
            if name in vars_:
                return vars_[name]
            if name in os.environ:
                return os.environ[name]
            raise BundleError(f"${{{name}}} is not set (--var {name}=... or the environment)")
        return VAR.sub(one, v)
    if isinstance(v, list):
        return [_subst(x, vars_) for x in v]
    if isinstance(v, dict):
        return {k: _subst(x, vars_) for k, x in v.items()}
    return v


def load(path: str, vars_: Optional[dict] = None) -> dict:
    """The bundle in ``path``, variables replaced and checked: {"bundle": name, kind: {key: spec}}."""
    import yaml

    try:
        raw = yaml.safe_load(open(path).read())
    except (OSError, yaml.YAMLError) as e:
        raise BundleError(f"{path}: {e}")
    if not isinstance(raw, dict):
        raise BundleError(f"{path}: a bundle is a mapping")
    raw = _subst(raw, vars_ or {})
    name = str(raw.pop("bundle", "") or "")
    if not BUNDLE.match(name):
        raise BundleError("bundle: a name of lowercase letters, digits, '.', '_' and '-'")
    unknown = set(raw) - set(KINDS)
    if unknown:
        raise BundleError(f"unknown sections: {', '.join(sorted(unknown))} (the sections are {', '.join(KINDS)})")
    out: dict[str, Any] = {"bundle": name}
    for kind, cfg in KINDS.items():
        items = raw.get(kind) or []
        if not isinstance(items, list):
            raise BundleError(f"{kind}: a list")
        allowed = set(cfg["required"]) | set(cfg["defaults"]) | ({"owner"} if kind == "derived" else set())
        seen: dict[str, dict] = {}
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                raise BundleError(f"{kind}[{i}]: a mapping")
            missing = [k for k in cfg["required"] if it.get(k) in (None, "")]
            if missing:
                raise BundleError(f"{kind}[{i}]: needs {', '.join(missing)}")
            extra = set(it) - allowed
            if extra:
                raise BundleError(f"{kind}[{i}]: unknown fields {', '.join(sorted(extra))}")
            key = str(it[cfg["key"]])
            if key in seen:
                raise BundleError(f"{kind}: {key} is declared twice")
            seen[key] = desired(kind, it)
        out[kind] = seen
    return out


def desired(kind: str, spec: dict) -> dict:
    """A spec with Cairn's defaults filled in, normalized the way Cairn returns it."""
    d = {**KINDS[kind]["defaults"], **spec}
    if kind in ("streams", "derived") and "key" in d and isinstance(d["key"], str):
        d["key"] = [k.strip() for k in d["key"].split(",") if k.strip()]
    if kind == "streams" and d.get("format") == "debezium":
        d["mode"] = "upsert"
    if kind == "derived":
        d["sql"] = str(d["sql"]).strip().rstrip(";")
    if kind == "queries":
        d["chart"] = {**CHART, **(d.get("chart") or {})}
    if kind == "dashboards":
        if not isinstance(d["tiles"], list) or not all(isinstance(t, dict) and t.get("query") for t in d["tiles"]):
            raise BundleError(f"dashboards/{d['name']}: tiles is a list of {{query: <saved query>, title, width}}")
        d["tiles"] = [{"title": t.get("title") or str(t["query"]), "width": 2 if t.get("width") == 2 else 1,
                       "query": str(t["query"])} for t in d["tiles"]]
    if kind == "alerts":
        d["value"] = _number(d["value"], f"alerts/{d['name']}: value")
    if kind == "workflows":
        if not isinstance(d["steps"], list) or not all(isinstance(x, dict) and x.get("name") for x in d["steps"]):
            raise BundleError(f"workflows/{d['name']}: steps is a list of steps, each with a name")
        steps = []
        for x in d["steps"]:
            x = {**STEP, **x}
            if isinstance(x["after"], str):
                x["after"] = [x["after"]]
            if x.get("type") == "check":
                x.setdefault("measure", "rows")
                x["value"] = _number(x.get("value"), f"workflows/{d['name']}: step {x['name']}: value")
            if x.get("type") == "derived":
                x.setdefault("full", False)
            steps.append(x)
        d["steps"] = steps
    return d


def _number(v: Any, what: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise BundleError(f"{what} is a number")


def _same(want: Any, have: Any) -> bool:
    """Does Cairn's ``have`` hold everything the bundle declares? Fields Cairn adds (ids, timestamps, state)
    are not drift."""
    if isinstance(want, dict):
        return isinstance(have, dict) and all(_same(v, have.get(k)) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(have, list) and len(want) == len(have) and all(_same(a, b) for a, b in zip(want, have))
    if isinstance(want, (int, float)) and not isinstance(want, bool) and isinstance(have, (int, float)):
        return float(want) == float(have)
    return want == have


def _as_declared(kind: str, r: dict, names: dict[str, str]) -> dict:
    """Cairn's record as a bundle declares it: saved queries by name, not id (``names``: id -> name)."""
    if kind == "dashboards":
        r = {**r, "tiles": [{**t, "query": names.get(t["query"], t["query"])} for t in r.get("tiles") or []]}
    elif kind == "alerts":
        r = {**r, "query": names.get(r.get("query"), r.get("query"))}
    elif kind == "workflows":
        r = {**r, "steps": [{**{k: v for k, v in x.items() if k != "derived_id"},
                             **({"query": names.get(x["query"], x["query"])} if x.get("query") else {})}
                            for x in r.get("steps") or []]}
    return r


def _with_ids(kind: str, body: dict, ids: dict[str, str]) -> dict:
    """The API body: saved query names replaced by Cairn's ids (``ids``: name -> id)."""
    def one(name: str) -> str:
        if name not in ids:
            raise BundleError(f"{kind}/{body['name']}: no saved query of yours named {name!r}")
        return ids[name]

    if kind == "dashboards":
        return {**body, "tiles": [{**t, "query": one(t["query"])} for t in body["tiles"]]}
    if kind == "alerts":
        return {**body, "query": one(body["query"])}
    if kind == "workflows":
        return {**body, "steps": [{**x, "query": one(x["query"])} if x.get("query") else x for x in body["steps"]]}
    return body


def _refs(kind: str, spec: dict) -> list[str]:
    if kind == "dashboards":
        return [t["query"] for t in spec["tiles"]]
    if kind == "alerts":
        return [spec["query"]]
    if kind == "workflows":
        return [x["query"] for x in spec["steps"] if x.get("query")]
    return []


def digest(bundle: dict) -> str:
    return hashlib.sha256(json.dumps(bundle, sort_keys=True, default=str).encode()).hexdigest()


class Client:
    def __init__(self, url: str, token: str, verify=True, http=None):
        import httpx

        self.url = url.rstrip("/")
        self.http = http or httpx.Client(headers={"Authorization": f"Bearer {token}"}, verify=verify, timeout=60)

    def call(self, method: str, path: str, body: Any = None) -> Any:
        r = self.http.request(method, self.url + path, json=body)
        if r.status_code >= 300:
            try:
                why = r.json().get("detail", r.text)
            except ValueError:
                why = r.text
            raise BundleError(f"{method} {path}: {r.status_code} {why}")
        return r.json() if r.content else None


def plan(client: Client, bundle: dict, prune: bool = False) -> tuple[list[Change], dict]:
    """What applying ``bundle`` would do, and the bundle's recorded state."""
    state = client.call("GET", f"/v1/bundles/{bundle['bundle']}")
    owned = {k: set(v) for k, v in (state.get("resources") or {}).items()}
    me = None
    if any(bundle[k] or (prune and owned.get(k)) for k in KINDS if KINDS[k].get("mine")):
        me = client.call("GET", "/me")["urn"]
    changes: list[Change] = []
    deletes: list[Change] = []
    names: dict[str, str] = {}
    for kind in ORDER:
        cfg = KINDS[kind]
        if cfg.get("mine") and me is None:
            continue
        rows = client.call("GET", cfg["path"])
        if cfg.get("mine"):
            rows = [r for r in rows if r.get("owner") == me]          # shared ones of others are not this bundle's
        current: dict[str, dict] = {}
        for r in rows:
            key = str(r[cfg["key"]])
            if key in current and (key in bundle[kind] or key in owned.get(kind, set())):
                raise BundleError(f"{kind}/{key}: you have two of that name; rename or delete one")
            current[key] = r
        if kind == "queries":
            names = {r["id"]: str(r["name"]) for r in current.values()}
            known = set(current) | set(bundle["queries"])
            for k2 in ("dashboards", "alerts", "workflows"):
                for key, spec in bundle[k2].items():
                    missing = [q for q in _refs(k2, spec) if q not in known]
                    if missing:
                        raise BundleError(f"{k2}/{key}: no saved query of yours named {missing[0]!r}")
        for key, want in bundle[kind].items():
            have = current.get(key)
            if have is None:
                changes.append(Change("create", kind, key, desired=want))
                continue
            seen = _as_declared(kind, have, names)
            diff = {k: [seen.get(k), v] for k, v in want.items() if k != "owner" and not _same(v, seen.get(k))}
            if kind == "derived" and want.get("owner") and want["owner"] != have.get("owner"):
                raise BundleError(f"derived/{key}: its owner is {have.get('owner')}; delete it to change the owner")
            for f in cfg.get("fixed", ()):
                if f in diff:
                    raise BundleError(f"{kind}/{key}: its {f} is {have.get(f)}; delete it to change the {f}")
            changes.append(Change("update" if diff else "keep", kind, key, diff, want, have))
        if prune:
            for key in sorted(owned.get(kind, set()) - set(bundle[kind])):
                if key in current:
                    deletes.append(Change("delete", kind, key, current=current[key]))
    return changes + deletes[::-1], state


def apply(client: Client, bundle: dict, changes: list[Change], state: dict, log=print) -> dict:
    """Makes the planned changes, recording the bundle's state after each."""
    owned = {k: set(v) for k, v in (state.get("resources") or {}).items()}
    for kind in KINDS:
        owned.setdefault(kind, set())
    version = state.get("version", 0)

    def record():
        nonlocal version
        version = client.call("PUT", f"/v1/bundles/{bundle['bundle']}",
                              {"version": version, "digest": digest(bundle),
                               "resources": {k: sorted(v) for k, v in owned.items()}})["version"]

    # Adopt what already matches or will be updated, so a failure later does not leave it unowned.
    adopted = {(c.kind, c.key) for c in changes if c.action in ("keep", "update")}
    if any(c.key not in owned[c.kind] for c in changes if (c.kind, c.key) in adopted):
        for kind, key in adopted:
            owned[kind].add(key)
        record()
    ids = {str(c.current["name"]): c.current["id"] for c in changes if c.kind == "queries" and c.current}
    if any(KINDS[c.kind].get("mine") for c in changes):
        me = client.call("GET", "/me")["urn"]
        ids = {**{str(r["name"]): r["id"] for r in client.call("GET", "/sql/saved") if r.get("owner") == me}, **ids}
    for c in changes:
        cfg = KINDS[c.kind]
        body = _with_ids(c.kind, dict(c.desired or {}), ids) if c.action != "delete" else {}
        if c.action == "keep":
            continue
        if c.kind == "maintenance":
            path = f"{cfg['path']}/{quote(c.key, safe='')}"
            if c.action == "delete":
                client.call("DELETE", path)
            else:
                client.call("PUT", path, {k: v for k, v in body.items() if k != "scope"})
        elif c.action == "create":
            made = client.call("POST", cfg["path"], body)
            if c.kind == "queries":
                ids[c.key] = made["id"]
        elif c.action == "update":
            client.call("PUT", f"{cfg['path']}/{c.current['id']}", {k: v for k, v in body.items() if k != "owner"})
        else:
            client.call("DELETE", f"{cfg['path']}/{c.current['id']}")
        if c.action == "delete":
            owned[c.kind].discard(c.key)
        else:
            owned[c.kind].add(c.key)
        record()
        log(c.line())
    if not any(c.action != "keep" for c in changes):
        record()   # the digest of what was applied, even when nothing changed
    return {"bundle": bundle["bundle"], "version": version,
            "changed": sum(1 for c in changes if c.action != "keep")}


def main(args) -> int:
    """``cairn bundle validate|plan|apply FILE``."""
    vars_ = {}
    for v in args.var or []:
        k, sep, val = v.partition("=")
        if not sep:
            raise SystemExit(f"--var {v}: give NAME=value")
        vars_[k] = val
    try:
        bundle = load(args.file, vars_)
        if args.action == "validate":
            n = sum(len(bundle[k]) for k in KINDS)
            print(f"{args.file}: bundle {bundle['bundle']}, {n} resources, valid")
            return 0
        url = args.url or os.environ.get("CAIRN_URL", "")
        token = args.token or os.environ.get("CAIRN_TOKEN", "")
        if os.environ.get("CAIRN_TOKEN_FILE") and not token:
            token = open(os.environ["CAIRN_TOKEN_FILE"]).read().strip()
        if not url or not token:
            raise BundleError("set CAIRN_URL and CAIRN_TOKEN (or --url and --token)")
        client = Client(url, token, os.environ.get("CAIRN_CA_FILE") or True)
        changes, state = plan(client, bundle, args.prune)
        for c in changes:
            if c.action != "keep" or args.verbose:
                print(c.line())
        counts = {a: sum(1 for c in changes if c.action == a) for a in ("create", "update", "delete", "keep")}
        print(f"plan: {counts['create']} to create, {counts['update']} to update, {counts['delete']} to delete, "
              f"{counts['keep']} unchanged")
        if args.action == "plan":
            return 2 if args.detailed_exitcode and any(c.action != "keep" for c in changes) else 0
        out = apply(client, bundle, changes, state, log=lambda line: None)
        print(f"applied: {out['changed']} changes (bundle {out['bundle']}, version {out['version']})")
        return 0
    except BundleError as e:
        print(f"cairn bundle: {e}")
        return 1
