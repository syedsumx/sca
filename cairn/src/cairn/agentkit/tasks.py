"""``cairn tasks``: CoWork scheduled tasks and their outbox from the command line (L108).

The assistant's scheduled tasks (:mod:`cairn.ai.cowork`, ``/ai/scheduled-tasks``) and the outbox of reports waiting
for approval (``/ai/outbox``), for the person whose token is used. ``CAIRN_URL`` and ``CAIRN_TOKEN`` (or
``CAIRN_TOKEN_FILE``) are read from the environment, never from arguments, so the token stays out of shell history
and process lists. The token must be a person's: agent credentials are refused by the API.

* ``list``: your tasks; ``show <id>``: one task and its last runs; ``output <id> [--run <run id>]``: a run's report;
* ``create --name --prompt --cron [--max-steps] [--slack <channel> | --email <a,b>]``;
* ``run <id>``: run it now (waits for the run); ``pause <id>``, ``resume <id>``, ``delete <id>``;
* ``outbox [--status pending]``; ``approve <outbox id>``; ``reject <outbox id>``.

``--json`` prints the API's answer as JSON. Exit status: 0 done, 1 the API refused (its reason on stderr),
2 a usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Optional


class TasksError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _token() -> str:
    token = os.environ.get("CAIRN_TOKEN", "").strip()
    path = os.environ.get("CAIRN_TOKEN_FILE", "").strip()
    if not token and path:
        try:
            with open(path, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as e:
            raise TasksError(f"cannot read CAIRN_TOKEN_FILE: {e.strerror}", 2)
    if not token:
        raise TasksError("set CAIRN_TOKEN (or CAIRN_TOKEN_FILE) to your Cairn token; it is read from the "
                         "environment, never an argument", 2)
    return token


def client():
    import httpx

    url = os.environ.get("CAIRN_URL", "").strip().rstrip("/")
    if not url:
        raise TasksError("set CAIRN_URL to your Cairn (e.g. https://cairn.example.com/cairn/api)", 2)
    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {_token()}"}, timeout=900)


def _call(c, method: str, path: str, body: Any = None, params: Optional[dict] = None) -> Any:
    import httpx

    try:
        r = c.request(method, path, json=body, params=params)
    except httpx.HTTPError as e:
        raise TasksError(f"cannot reach Cairn: {type(e).__name__}")
    if r.status_code >= 400:
        try:
            why = r.json().get("detail") or r.text
        except ValueError:
            why = r.text
        raise TasksError(f"{r.status_code}: {str(why)[:500]}")
    return None if r.status_code == 204 or not r.content else r.json()


def _when(ms: Optional[int]) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000)) if ms else "-"


def _table(rows: list[dict], cols: list[tuple[str, str]]) -> str:
    cells = [[h for _, h in cols]] + [[str(v) for v in (fmt(r) for fmt in (_fmt(k) for k, _ in cols))] for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(cols))]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in cells)


def _fmt(key: str):
    def get(r: dict) -> Any:
        v = r.get(key)
        if key.endswith("_ms"):
            return _when(v)
        if key == "send":
            return "-" if not v else ", ".join(f"{k}: {x if isinstance(x, str) else ','.join(x)}" for k, x in v.items())
        if key == "target":
            return v if isinstance(v, str) else ",".join(v or [])
        return "-" if v is None else v
    return get


def _send(a) -> Optional[dict]:
    if a.slack and a.email:
        raise TasksError("give --slack or --email, not both", 2)
    if a.slack:
        return {"slack": a.slack}
    if a.email:
        return {"email": [x.strip() for x in a.email.split(",") if x.strip()]}
    return None


def _output(c, tid: str, run_id: Optional[str]) -> dict:
    task = _call(c, "GET", f"/ai/scheduled-tasks/{tid}")
    runs = [r for r in task["runs"] if r["status"] == "done" and r.get("artifact_id")]
    if run_id:
        runs = [r for r in task["runs"] if r["id"] == run_id]
        if not runs:
            raise TasksError(f"no run {run_id} in this task's last runs")
        if not runs[0].get("artifact_id"):
            raise TasksError(f"run {run_id} has no report ({runs[0]['status']}: {runs[0].get('error') or ''})")
    if not runs:
        raise TasksError("this task has no finished run with a report yet")
    art = _call(c, "GET", f"/ai/artifacts/{runs[0]['artifact_id']}")
    return {"run": runs[0]["id"], "artifact_id": art["id"], "title": art.get("title"), "content": art.get("content")}


def run(a) -> Any:
    """One ``cairn tasks`` command; returns what to print (or the API's answer with ``--json``)."""
    cmd = a.tasks_cmd
    with client() as c:
        if cmd == "list":
            out = _call(c, "GET", "/ai/scheduled-tasks")
            return out if a.json else (_table(out, [("id", "ID"), ("name", "NAME"), ("cron", "CRON"),
                                                     ("enabled", "ENABLED"), ("last_status", "LAST"),
                                                     ("last_run_ms", "LAST RUN"), ("next_run_ms", "NEXT RUN"),
                                                     ("send", "SEND")]) if out else "no scheduled tasks")
        if cmd == "create":
            body: dict[str, Any] = {"name": a.name, "prompt": a.prompt, "cron": a.cron, "max_steps": a.max_steps}
            if (send := _send(a)) is not None:
                body["send"] = send
            out = _call(c, "POST", "/ai/scheduled-tasks", body)
            return out if a.json else f"created {out['id']} ({out['name']}), next run {_when(out['next_run_ms'])}"
        if cmd == "show":
            out = _call(c, "GET", f"/ai/scheduled-tasks/{a.id}")
            if a.json:
                return out
            head = (f"{out['name']} ({out['id']})\n  cron {out['cron']}  enabled {out['enabled']}  "
                    f"next run {_when(out['next_run_ms'])}  send {_fmt('send')(out)}\n  prompt: {out['prompt']}\n")
            runs = _table(out["runs"], [("id", "RUN"), ("status", "STATUS"), ("started_ms", "STARTED"),
                                        ("artifact_id", "REPORT"), ("outbox_id", "OUTBOX"), ("error", "ERROR")])
            return head + (runs if out["runs"] else "  no runs yet")
        if cmd == "output":
            out = _output(c, a.id, a.run)
            return out if a.json else f"# {out['title']}  (run {out['run']}, artifact {out['artifact_id']})\n\n{out['content']}"
        if cmd == "run":
            out = _call(c, "POST", f"/ai/scheduled-tasks/{a.id}/run")
            if a.json:
                return out
            if out["status"] != "done":
                raise TasksError(f"the run failed: {out.get('error') or 'unknown error'}")
            tail = f"; waiting for approval in the outbox ({out['outbox_id']})" if out.get("outbox_id") else ""
            return f"run {out['id']} done: report {out['artifact_id']}{tail}"
        if cmd in ("pause", "resume"):
            out = _call(c, "PUT", f"/ai/scheduled-tasks/{a.id}", {"enabled": cmd == "resume"})
            return out if a.json else f"{out['name']} ({out['id']}) {'resumed' if out['enabled'] else 'paused'}"
        if cmd == "delete":
            _call(c, "DELETE", f"/ai/scheduled-tasks/{a.id}")
            return {"deleted": a.id} if a.json else f"deleted {a.id}"
        if cmd == "outbox":
            out = _call(c, "GET", "/ai/outbox", params={"status": a.status} if a.status else None)
            return out if a.json else (_table(out, [("id", "ID"), ("status", "STATUS"), ("kind", "KIND"),
                                                     ("target", "TARGET"), ("subject", "SUBJECT"),
                                                     ("created_ms", "CREATED")]) if out else "the outbox is empty")
        if cmd in ("approve", "reject"):
            out = _call(c, "POST", f"/ai/outbox/{a.id}/{cmd}")
            if a.json:
                return out
            if out["status"] not in ("sent", "rejected"):
                raise TasksError(f"{out['status']}: {out.get('error') or ''}".rstrip(": "))
            return f"{a.id} {out['status']}"
    raise TasksError(f"unknown command {cmd}", 2)


def add_parser(sub) -> None:
    tk = sub.add_parser("tasks", help="CoWork scheduled tasks and their outbox, as you (CAIRN_URL, CAIRN_TOKEN)")
    tk.add_argument("--json", action="store_true", help="print the API's answer as JSON")
    ts = tk.add_subparsers(dest="tasks_cmd", required=True)
    ts.add_parser("list", help="your scheduled tasks")
    x = ts.add_parser("create", help="a task the assistant runs on a schedule (at most hourly, cron in UTC)")
    x.add_argument("--name", required=True)
    x.add_argument("--prompt", required=True, help="what to research and report, in plain words")
    x.add_argument("--cron", required=True, help='five fields, UTC, e.g. "0 7 * * 1"')
    x.add_argument("--max-steps", type=int, default=4, help="research steps, 1 to 8")
    x.add_argument("--slack", help="send each report to this Slack channel once you approve it")
    x.add_argument("--email", help="send each report to these addresses (comma-separated) once you approve it")
    for name, text in (("show", "a task and its last runs"), ("run", "run a task now and wait for it"),
                       ("pause", "stop a task running on its schedule"), ("resume", "start it again"),
                       ("delete", "delete a task")):
        ts.add_parser(name, help=text).add_argument("id")
    x = ts.add_parser("output", help="the report of a task's latest finished run (or of --run)")
    x.add_argument("id")
    x.add_argument("--run")
    x = ts.add_parser("outbox", help="reports waiting for your approval, and the ones decided")
    x.add_argument("--status", choices=("pending", "sent", "rejected", "refused", "failed", "expired"))
    ts.add_parser("approve", help="send an outbox item").add_argument("id")
    ts.add_parser("reject", help="do not send an outbox item").add_argument("id")
    for p in ts.choices.values():                     # --json after the subcommand works too
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    tk.set_defaults(fn=cli)


def cli(a) -> None:
    """The ``cairn tasks`` command."""
    import logging

    if not hasattr(a, "json"):
        a.json = False
    logging.getLogger("httpx").setLevel(logging.WARNING)   # the output is the command's answer, not request logs
    try:
        out = run(a)
    except TasksError as e:
        print(f"cairn tasks: {e}", file=sys.stderr)
        sys.exit(e.code)
    print(json.dumps(out, indent=2, default=str) if a.json else out)
