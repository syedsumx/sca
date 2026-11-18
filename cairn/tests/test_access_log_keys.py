"""The API's access log never carries Trino's shared key. Trino puts it in the path of every call it makes
(``/v1/trino/<key>/allow``, ``/batch``, ``/lineage``, ``/queries``, ``/ai/...``), and the same key is the Trino
nodes' internal shared secret, so a log reader who saw it could ask Cairn's access-control endpoints anything and
join the Trino cluster. The server is started the way ``cairn api`` starts it (``_serve``) and its stdout read."""

import os
import socket
import subprocess
import sys
import textwrap
import time

import httpx

SRC = os.path.join(os.path.dirname(__file__), "..", "src")

KEY = "trino-shared-" + "q" * 24


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_access_log_redacts_the_trino_key_in_paths():
    port = _port()
    code = textwrap.dedent(f"""
        from fastapi import FastAPI
        from cairn.cli import _serve
        app = FastAPI()

        @app.post("/v1/trino/{{given}}/allow")
        def allow(given: str):
            return {{"result": True}}

        @app.get("/v1/trino/jwks.json")
        def jwks():
            return {{"keys": []}}

        @app.get("/v1/trino/oauth/authorize")
        def authorize():
            return {{}}

        _serve(app, "127.0.0.1", {port})
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**os.environ, "PYTHONPATH": os.pathsep.join([SRC, os.environ.get("PYTHONPATH", "")])})
    try:
        end = time.time() + 30
        while True:
            try:
                httpx.get(f"http://127.0.0.1:{port}/v1/trino/jwks.json", timeout=2)
                break
            except httpx.HTTPError:
                assert time.time() < end and proc.poll() is None, "the server did not start"
                time.sleep(0.2)
        assert httpx.post(f"http://127.0.0.1:{port}/v1/trino/{KEY}/allow", json={}).json() == {"result": True}
        httpx.get(f"http://127.0.0.1:{port}/v1/trino/oauth/authorize")
    finally:
        proc.terminate()
        out, _ = proc.communicate(timeout=30)
    assert "/v1/trino/{key}/allow" in out, out          # the request is still logged
    assert "/v1/trino/jwks.json" in out and "/v1/trino/oauth/authorize" in out, out   # named routes untouched
    assert KEY not in out, out
