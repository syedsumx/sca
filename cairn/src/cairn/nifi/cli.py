"""``cairn nifi ...``: managed NiFi from the command line (and CI), as you (``CAIRN_URL`` and ``CAIRN_TOKEN``, or
``cairn login``)::

    cairn nifi list
    cairn nifi create deployment.yaml          # {name, environment, team, nifi_version, resources, domain}
    cairn nifi scale ingest-dev --memory-mb 8192 --heap-mb 4096 [--cpu 2]
    cairn nifi status ingest-dev
    cairn nifi logs ingest-dev
    cairn nifi delete ingest-dev
    cairn nifi template > flow.json            # a flow landing JSON records in a lakehouse table
    cairn nifi deploy flow.yaml                # {name, deployment, definition: <file or object>, parameters, secrets, sink}
    cairn nifi flows [--deployment ingest-dev]
    cairn nifi start|stop|status-flow|remove <flow>
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.parse import quote

from cairn.airflow_deploy.cli import CliError, _client, _load


def main(a) -> int:
    try:
        return _main(a)
    except CliError as e:
        print(f"cairn nifi: {e}", file=sys.stderr)
        return 1


def _flow_doc(path: str) -> dict:
    doc = _load(path)
    d = doc.get("definition")
    if isinstance(d, str):                       # a path, next to the flow file
        p = d if os.path.isabs(d) else os.path.join(os.path.dirname(os.path.abspath(path)), d)
        try:
            doc["definition"] = json.load(open(p))
        except (OSError, ValueError) as e:
            raise CliError(f"{p}: {e}")
    return doc


def _main(a) -> int:
    cmd = a.nifi_cmd
    call = _client()
    base = "/v1/nifi"
    if cmd == "list":
        for d in call("GET", f"{base}/deployments")["deployments"]:
            print(f"{d['name']:<24} {d['environment']:<8} {d['state']:<10} NiFi {d['nifi_version']:<8} "
                  f"{len(d.get('flows') or [])} flow(s)  {d.get('canvas_url') or ''}")
        return 0
    if cmd == "create":
        out = call("POST", f"{base}/deployments", _load(a.file))
        print(f"{out['name']}: {out['state']}" + (f", canvas {out['canvas_url']}" if out.get("canvas_url") else ""))
        return 0 if out["state"] in ("ready", "creating") else 1
    if cmd == "scale":
        res: dict[str, Any] = {k: v for k, v in (("cpu", a.cpu), ("memory_mb", a.memory_mb), ("heap_mb", a.heap_mb),
                                                 ("storage_gb", a.storage_gb)) if v is not None}
        out = call("PATCH", f"{base}/deployments/{quote(a.name)}", {"resources": res})
        print(f"{out['name']}: {out['state']} {out['resources']}")
        return 0
    if cmd == "status":
        s = call("GET", f"{base}/deployments/{quote(a.name)}/status")
        print(f"{a.name}: {s.get('health')} ({s.get('state')}) {json.dumps(s.get('counts') or {})}")
        for p in s.get("problems") or []:
            print(f"  ! {p}")
        return 0 if s.get("health") == "healthy" else 1
    if cmd == "logs":
        print(call("GET", f"{base}/deployments/{quote(a.name)}/logs?tail={int(a.tail)}")["log"])
        return 0
    if cmd == "delete":
        call("DELETE", f"{base}/deployments/{quote(a.name)}")
        print(f"{a.name}: deleted")
        return 0
    if cmd == "template":
        print(json.dumps(call("GET", f"{base}/flow-templates/lakehouse"), indent=2))
        return 0
    if cmd == "deploy":
        doc = _flow_doc(a.file)
        name = doc.get("name")
        if not name:
            raise CliError(f"{a.file}: a flow has a name")
        known = {f["name"] for f in call("GET", f"{base}/flows")["flows"]}
        if name in known:
            body = {k: v for k, v in doc.items() if k not in ("name", "deployment")}
            out = call("PUT", f"{base}/flows/{quote(name)}", body)
        else:
            out = call("POST", f"{base}/flows", doc)
        print(f"{out['name']}: {out['state']} (version {out['version']})" + (f": {out['error']}" if out.get("error") else ""))
        return 0 if out["state"] in ("running", "stopped") else 1
    if cmd == "flows":
        q = f"?deployment={quote(a.deployment)}" if a.deployment else ""
        for f in call("GET", f"{base}/flows{q}")["flows"]:
            sink = f.get("sink") or {}
            print(f"{f['name']:<24} {f['deployment']:<20} {f['state']:<9} v{f['version']:<4} "
                  f"{(sink.get('namespace', '') + '.' + sink.get('table', '')) if sink else ''}")
        return 0
    if cmd in ("start", "stop"):
        out = call("POST", f"{base}/flows/{quote(a.flow)}/{cmd}", {})
        print(f"{out['name']}: {out['state']}")
        return 0
    if cmd == "status-flow":
        s = call("GET", f"{base}/flows/{quote(a.flow)}/status")
        print(f"{a.flow}: {s.get('state')} {json.dumps(s.get('counts') or {})}")
        for b in s.get("bulletins") or []:
            print(f"  [{b.get('level')}] {b.get('source')}: {b.get('message')}")
        return 0
    if cmd == "remove":
        call("DELETE", f"{base}/flows/{quote(a.flow)}")
        print(f"{a.flow}: removed")
        return 0
    raise CliError(f"unknown command {cmd}")


def add_parser(sub) -> None:
    nf = sub.add_parser("nifi", help="managed NiFi: deployments and the flows Cairn deploys to them")
    s = nf.add_subparsers(dest="nifi_cmd", required=True)
    s.add_parser("list", help="your deployments")
    s.add_parser("create", help="a deployment from a YAML or JSON file").add_argument("file")
    p = s.add_parser("scale", help="a deployment's size (NiFi is made again; its flows stay)")
    p.add_argument("name")
    p.add_argument("--cpu", type=float)
    p.add_argument("--memory-mb", type=int)
    p.add_argument("--heap-mb", type=int)
    p.add_argument("--storage-gb", type=int)
    for name, what in (("status", "health"), ("delete", "delete it and its flows")):
        s.add_parser(name, help=what).add_argument("name")
    p = s.add_parser("logs", help="NiFi's log")
    p.add_argument("name")
    p.add_argument("--tail", type=int, default=200)
    s.add_parser("template", help="a flow definition landing JSON records in a lakehouse table")
    s.add_parser("deploy", help="create or change a flow from a YAML or JSON file, and deploy it").add_argument("file")
    s.add_parser("flows", help="flows").add_argument("--deployment")
    for name, what in (("start", "start a flow"), ("stop", "stop a flow"), ("status-flow", "a flow's counters and "
                                                                           "bulletins"), ("remove", "remove a flow")):
        s.add_parser(name, help=what).add_argument("flow")
    nf.set_defaults(fn=lambda a: sys.exit(main(a)))
