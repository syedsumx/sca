"""The agent runtime: serves a project's ``respond(ctx, messages)`` over HTTP.

Two modes:

* **dev** (``cairn agents dev``): on your machine, with your own Cairn token (``CAIRN_TOKEN``) for every call, sessions
  kept in ``.cairn/sessions.db`` in the project, and a chat page at ``/``.
* **app** (run by Cairn for a deployed agent app, as a local process or a pod): reachable only by Cairn (a shared
  secret on every request); Cairn sends the conversation (it keeps the sessions) and a short-lived token for the
  person who asked, narrowed to the app's scopes, which the agent's model and data calls use. The runtime keeps
  nothing. On Kubernetes, ``--bundle`` unpacks the project first and exits (3) naming any requirement it lacks.

``POST /invocations`` (app): ``{"messages", "session_id", "user"}`` with ``X-Cairn-Runtime-Secret`` and
``X-Cairn-Run-Token``; ``POST /chat`` (dev): ``{"message", "session_id"}``; ``GET /healthz``; ``GET /`` (dev: the
chat page).
"""

from __future__ import annotations

import argparse
import hmac
import importlib.util
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_UI = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width, initial-scale=1"><title>Agent chat</title><style>
body{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;background:#fafafa;color:#111}
#log div{margin:.5rem 0;padding:.6rem .8rem;border-radius:8px;white-space:pre-wrap}
.user{background:#e8eefc}.assistant{background:#fff;border:1px solid #ddd}.error{background:#fde8e8}
form{display:flex;gap:.5rem}input{flex:1;padding:.6rem;font-size:1rem}button{padding:.6rem 1rem}
</style></head><body><h1 id="name">Agent</h1><div id="log" aria-live="polite"></div>
<form id="f"><label for="q" style="position:absolute;left:-9999px">Message</label>
<input id="q" autocomplete="off" placeholder="Ask something"><button>Send</button></form><script>
const sid = (crypto.randomUUID && crypto.randomUUID()) || String(Date.now());
const log = document.getElementById('log');
function add(role, text){ const d=document.createElement('div'); d.className=role; d.textContent=text; log.appendChild(d); }
document.getElementById('f').onsubmit = async (e) => { e.preventDefault(); const q=document.getElementById('q');
  const text=q.value.trim(); if(!text) return; q.value=''; add('user', text);
  try { const r = await fetch('chat', {method:'POST', headers:{'content-type':'application/json'},
          body: JSON.stringify({message:text, session_id:sid})});
        const body = await r.json(); add(r.ok ? 'assistant' : 'error', r.ok ? body.output : (body.detail || r.status)); }
  catch(err){ add('error', String(err)); } };
</script></body></html>"""


class Context:
    """What ``respond`` gets: model clients and data access as the person the agent works for."""

    def __init__(self, cairn_url: str, token: str, session_id: str, user: str, mcp_url: Optional[str] = None):
        self.cairn_url, self._token, self.session_id, self.user = cairn_url.rstrip("/"), token, session_id, user
        self.mcp_url = mcp_url

    @property
    def gateway(self) -> dict:
        """Base URLs and key for frameworks that take them (LangChain, the OpenAI Agents SDK, ...)."""
        return {"anthropic_base_url": f"{self.cairn_url}/v1/ai/gateway/anthropic",
                "openai_base_url": f"{self.cairn_url}/v1/ai/gateway/openai/v1", "api_key": self._token}

    def anthropic(self):
        import anthropic

        return anthropic.Anthropic(base_url=self.gateway["anthropic_base_url"], api_key=self._token, max_retries=2)

    def openai(self):
        import openai

        return openai.OpenAI(base_url=self.gateway["openai_base_url"], api_key=self._token, max_retries=2)

    def sql(self, sql: str, connection: Optional[str] = None, limit: int = 200) -> dict:
        """Runs SQL through Cairn as the person (their policies, masks and row filters apply)."""
        import httpx

        r = httpx.post(f"{self.cairn_url}/data/query", json={"sql": sql, "connection": connection, "limit": limit},
                       headers={"Authorization": f"Bearer {self._token}"}, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"Cairn refused the query ({r.status_code}): {r.text[:300]}")
        return r.json()


def load_agent(project: Path) -> Callable[..., Any]:
    """The project's ``respond`` function (``agent.py``)."""
    path = project / "agent.py"
    if not path.is_file():
        raise RuntimeError(f"{project} has no agent.py")
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))
    spec = importlib.util.spec_from_file_location("cairn_agent_project", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "respond", None)
    if not callable(fn):
        raise RuntimeError("agent.py defines no respond(ctx, messages)")
    return fn


def _text(out: Any) -> str:
    if isinstance(out, str):
        return out
    if isinstance(out, dict) and isinstance(out.get("output"), str):
        return out["output"]
    raise RuntimeError("respond() returned neither text nor {'output': text}")


class Sessions:
    """dev mode's session store: ``.cairn/sessions.db`` in the project."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS messages (session TEXT, seq INTEGER, role TEXT, content TEXT)")
        self.lock = threading.Lock()

    def history(self, session: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT role, content FROM messages WHERE session = ? ORDER BY seq", (session,))
            return [{"role": r, "content": c} for r, c in rows.fetchall()]

    def add(self, session: str, role: str, content: str) -> None:
        with self.lock:
            n = self.db.execute("SELECT count(*) FROM messages WHERE session = ?", (session,)).fetchone()[0]
            self.db.execute("INSERT INTO messages VALUES (?, ?, ?, ?)", (session, n, role, content))
            self.db.commit()


def build_app(project: Path, mode: str, cairn_url: str, token: str = "", secret: str = "", user: str = "",
              mcp_url: Optional[str] = None):
    from fastapi import Body, FastAPI, Header, HTTPException
    from fastapi.responses import HTMLResponse

    respond = load_agent(project)
    app = FastAPI(title="Cairn agent runtime", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    def run(messages: list, session_id: str, who: str, run_token: str) -> dict:
        if not isinstance(messages, list) or not messages:
            raise HTTPException(400, "messages: the conversation, newest last")
        started = time.monotonic()
        ctx = Context(cairn_url, run_token, session_id, who, mcp_url)
        try:
            out = _text(respond(ctx, messages))
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 - the agent's own failure, reported to the caller
            raise HTTPException(502, f"the agent failed: {type(e).__name__}: {str(e)[:500]}")
        return {"output": out, "elapsed_ms": int((time.monotonic() - started) * 1000)}

    if mode == "app":
        @app.post("/invocations")
        def invocations(body: dict = Body(...), x_cairn_runtime_secret: str = Header(""),
                        x_cairn_run_token: str = Header("")):
            if not secret or not hmac.compare_digest(x_cairn_runtime_secret.encode(), secret.encode()):
                raise HTTPException(404)                  # only Cairn reaches an app's runtime
            if not x_cairn_run_token:
                raise HTTPException(400, "no run token")
            return run(body.get("messages"), str(body.get("session_id") or ""), str(body.get("user") or ""),
                       x_cairn_run_token)
        return app

    sessions = Sessions(project / ".cairn" / "sessions.db")
    ui = project / "ui" / "index.html"
    name = _manifest(project).get("name", "agent")

    @app.get("/", response_class=HTMLResponse)
    def page():
        html = ui.read_text() if ui.is_file() else DEFAULT_UI
        return html.replace('<h1 id="name">Agent</h1>', f'<h1 id="name">{name}</h1>')

    @app.post("/chat")
    def chat(body: dict = Body(...)):
        message, session = str(body.get("message") or "").strip(), str(body.get("session_id") or "default")
        if not message:
            raise HTTPException(400, "message: what to ask")
        sessions.add(session, "user", message)
        out = run(sessions.history(session), session, user, token)
        sessions.add(session, "assistant", out["output"])
        return out
    return app


def _manifest(project: Path) -> dict:
    path = project / "agent.toml"
    if not path.is_file():
        return {}
    import tomllib

    return tomllib.loads(path.read_text())


def main(argv: Optional[list[str]] = None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m cairn.agentkit.runtime")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--mode", choices=("dev", "app"), default="app")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--secret-file", default="")       # app mode: the shared secret, from a file (never argv)
    # On Kubernetes: the project arrives as its bundle (a mounted Secret) and is unpacked into --dir first.
    ap.add_argument("--bundle", default="")
    a = ap.parse_args(argv)
    project = Path(a.dir).resolve()
    if a.bundle:
        from cairn.agentkit.bundle import BundleError, missing_requirements, unpack

        try:
            unpack(Path(a.bundle).read_bytes(), project)
        except (BundleError, OSError) as e:
            print(f"cairn agent runtime: {e}", file=sys.stderr)
            sys.exit(2)
        missing = missing_requirements(project)
        if missing:                                    # Cairn reads this from the pod's termination message
            print(f"the runtime lacks the project's requirements: {', '.join(missing)} (add them to the agent "
                  "runtime image, or remove them from requirements.txt)", file=sys.stderr)
            sys.exit(3)
    secret = Path(a.secret_file).read_text().strip() if a.secret_file else ""
    app = build_app(project, a.mode, os.environ.get("CAIRN_URL", "http://127.0.0.1:9000"),
                    os.environ.get("CAIRN_TOKEN", ""), secret, os.environ.get("CAIRN_USER", ""),
                    os.environ.get("CAIRN_MCP_URL") or None)
    uvicorn.run(app, host=a.host, port=a.port, log_level=os.environ.get("CAIRN_AGENT_LOG", "warning"))


if __name__ == "__main__":
    main()

