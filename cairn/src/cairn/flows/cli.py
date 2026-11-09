"""``cairn flows list|validate|deploy|run|logs``: flows from the command line (and from CI, or an agent's shell).

``validate FILE`` checks a definition here, without Cairn. The others call Cairn's API as you: ``CAIRN_URL`` and
``CAIRN_TOKEN`` (or ``CAIRN_TOKEN_FILE``; never on the command line), ``CAIRN_CA_FILE`` for a private CA.

    cairn flows validate flows/nightly.yaml
    cairn flows deploy flows/nightly.yaml            # creates or updates the flow, then publishes it
    cairn flows run nightly --param date=2026-11-01 --wait
    cairn flows logs <run id> --step report
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional
from urllib.parse import quote


class CliError(Exception):
    pass


def _client():
    import httpx

    url = os.environ.get("CAIRN_URL", "").rstrip("/")
    token = os.environ.get("CAIRN_TOKEN", "")
    if not token and os.environ.get("CAIRN_TOKEN_FILE"):
        token = open(os.environ["CAIRN_TOKEN_FILE"]).read().strip()
    if not url or not token:
        raise CliError("set CAIRN_URL and CAIRN_TOKEN (or CAIRN_TOKEN_FILE)")
    http = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"},
                        verify=os.environ.get("CAIRN_CA_FILE") or True, timeout=120)

    def call(method: str, path: str, body: Any = None) -> Any:
        r = http.request(method, path, json=body)
        if r.status_code >= 300:
            try:
                doc = r.json()
                why = doc.get("detail", r.text)
                errors = doc.get("errors") or []
            except ValueError:
                why, errors = r.text, []
            lines = [f"{method} {path}: {r.status_code} {why}"] + [
                f"  {e.get('path') or 'flow'}: {e.get('message')}" for e in errors[1:]]
            raise CliError("\n".join(lines))
        return r.json() if r.content else None
    return call


def _load(path: str) -> dict:
    from cairn.flows import model

    try:
        text = open(path).read()
    except OSError as e:
        raise CliError(f"{path}: {e}")
    raw = model.from_text(text)
    return raw


def _status_line(s: dict, indent: str = "  ") -> str:
    took = ""
    if s.get("started_ms") and s.get("finished_ms"):
        took = f" {(s['finished_ms'] - s['started_ms']) / 1000:.1f}s"
    extra = f" ({s['error']})" if s.get("error") and s["status"] != "ok" else ""
    return f"{indent}{s['key']:<28} {s['status']:<10}{took}{extra}"


def main(a) -> int:
    from cairn.flows import model
    from cairn.flows.model import FlowError

    try:
        if a.flows_cmd == "validate":
            ok = True
            for path in a.files:
                out = model.validate(_load(path))
                if out["valid"]:
                    f = out["flow"]
                    print(f"{path}: flow {f['name']}, {len(f['steps'])} steps, valid")
                else:
                    ok = False
                    for e in out["errors"]:
                        print(f"{path}: {e['path'] or 'flow'}: {e['message']}")
            return 0 if ok else 1
        call = _client()
        if a.flows_cmd == "list":
            for f in call("GET", "/v1/flows"):
                last = (f.get("last_run") or {}).get("status") or "-"
                v = f.get("published_version") or "draft"
                print(f"{f['name']:<32} v{v!s:<6} {('on' if f['enabled'] else 'off'):<4} last run: {last:<10} {f['owner']}")
            return 0
        if a.flows_cmd == "deploy":
            for path in a.files:
                raw = _load(path)
                out = model.validate(raw)
                if not out["valid"]:
                    for e in out["errors"]:
                        print(f"{path}: {e['path'] or 'flow'}: {e['message']}")
                    return 1
                name = out["flow"]["name"]
                q = f"?publish={'false' if a.draft else 'true'}" + (f"&note={quote(a.note)}" if a.note else "")
                existing = {f["name"] for f in call("GET", "/v1/flows")}
                if name in existing:
                    got = call("PUT", f"/v1/flows/{quote(name)}{q}", raw)
                else:
                    got = call("POST", f"/v1/flows{q}", raw)
                version = (got or {}).get("published_version")
                print(f"{name}: saved as a draft" if a.draft else f"{name}: published version {version}")
            return 0
        if a.flows_cmd == "run":
            params: dict[str, Any] = {}
            for p in a.param or []:
                k, sep, v = p.partition("=")
                if not sep:
                    raise CliError(f"--param {p}: give NAME=value")
                params[k] = v
            run = call("POST", f"/v1/flows/{quote(a.name)}/runs",
                       {"params": params, **({"version": "draft"} if a.draft else {})})
            print(f"run {run['id']} of {run['flow']} (version {run['version'] or 'draft'}): {run['status']}")
            if not a.wait:
                return 0
            return _follow(call, run["id"], a.timeout)
        if a.flows_cmd == "logs":
            if a.step:
                out = call("GET", f"/v1/flows/runs/{quote(a.run)}/steps/{quote(a.step)}/log")
                print(out["log"] or "(no log)")
                if out.get("error"):
                    print(f"error: {out['error']}")
                if out.get("output") is not None:
                    print("output: " + json.dumps(out["output"], indent=2, default=str))
                return 0
            if a.follow:
                return _follow(call, a.run, a.timeout)
            run = call("GET", f"/v1/flows/runs/{quote(a.run)}")
            _print_run(run)
            return 0 if run["status"] in ("succeeded", "queued", "running", "waiting") else 1
    except (CliError, FlowError) as e:
        print(f"cairn flows: {e}", file=sys.stderr)
        return 1
    return 2


def _print_run(run: dict) -> None:
    print(f"run {run['id']} of {run['flow']} v{run['version'] or 'draft'}: {run['status']}"
          + (f" ({run['error']})" if run.get("error") else ""))
    for s in run["steps"]:
        print(_status_line(s))
        for it in s.get("items") or []:
            print(_status_line(it, "    "))


def _follow(call, rid: str, timeout: Optional[float]) -> int:
    end = time.monotonic() + (timeout or 3600)
    seen: dict[str, str] = {}
    while True:
        run = call("GET", f"/v1/flows/runs/{quote(rid)}")
        for s in run["steps"]:
            if seen.get(s["key"]) != s["status"]:
                seen[s["key"]] = s["status"]
                print(_status_line(s))
        if run["status"] in ("succeeded", "failed", "cancelled", "skipped"):
            print(f"run {rid}: {run['status']}")
            return 0 if run["status"] == "succeeded" else 1
        if time.monotonic() > end:
            print(f"run {rid}: still {run['status']} after the time limit")
            return 3
        time.sleep(2)


def add_parser(sub) -> None:
    fl = sub.add_parser("flows", help="Cairn Flows: validate, deploy and run flows; read their runs")
    fs = fl.add_subparsers(dest="flows_cmd", required=True)
    fs.add_parser("list", help="the flows you may see")
    v = fs.add_parser("validate", help="check flow definitions (YAML or JSON) here, without Cairn")
    v.add_argument("files", nargs="+")
    d = fs.add_parser("deploy", help="create or update flows from files and publish them")
    d.add_argument("files", nargs="+")
    d.add_argument("--draft", action="store_true", help="save the drafts only, do not publish")
    d.add_argument("--note", help="a note on the published version")
    r = fs.add_parser("run", help="start a run (of the published version)")
    r.add_argument("name")
    r.add_argument("--param", action="append", help="NAME=value (repeat)")
    r.add_argument("--draft", action="store_true", help="run the draft (its owner only)")
    r.add_argument("--wait", action="store_true", help="follow the run until it ends; the exit code is its result")
    r.add_argument("--timeout", type=float, default=3600)
    lg = fs.add_parser("logs", help="a run's steps, or one step's log and output")
    lg.add_argument("run")
    lg.add_argument("--step", help="a step (a for-each's items are name[i])")
    lg.add_argument("--follow", action="store_true")
    lg.add_argument("--timeout", type=float, default=3600)
    fl.set_defaults(fn=lambda a: sys.exit(main(a)))
