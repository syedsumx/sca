"""Testing an agent project without a model: :class:`FakeContext` points the agent's model clients at a local server
that speaks both the Anthropic and the OpenAI-compatible protocols and answers with scripted replies, so tests run the
agent's real code (whatever framework it uses) offline, and can check what it sent.

    from cairn.agentkit.testing import FakeContext

    def test_it_answers():
        with FakeContext(replies=["Paris"]) as ctx:
            assert agent.respond(ctx, [{"role": "user", "content": "Capital of France?"}]) == "Paris"
            assert ctx.requests[0]["messages"][-1]["content"] == "Capital of France?"
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any, Optional

from cairn.agentkit.runtime import Context


class _Handler(http.server.BaseHTTPRequestHandler):
    owner: "FakeContext"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.owner.requests.append(body)
        text = self.owner.replies.pop(0) if self.owner.replies else "ok"
        if self.path.endswith("/messages"):
            out = {"id": "msg_test", "type": "message", "role": "assistant", "model": body.get("model", "test"),
                   "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
                   "usage": {"input_tokens": 1, "output_tokens": 1}}
        else:
            out = {"id": "chat_test", "object": "chat.completion", "created": 0, "model": body.get("model", "test"),
                   "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class FakeContext(Context):
    def __init__(self, replies: Optional[list[str]] = None, user: str = "urn:li:corpuser:test",
                 session_id: str = "test", rows: Optional[list] = None):
        handler = type("H", (_Handler,), {"owner": self})
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.replies, self.requests, self.queries = list(replies or []), [], []
        self._rows = rows or []
        super().__init__(f"http://127.0.0.1:{self._server.server_address[1]}", "test-token", session_id, user)

    @property
    def gateway(self) -> dict:
        return {"anthropic_base_url": self.cairn_url, "openai_base_url": f"{self.cairn_url}/v1",
                "api_key": "test-token"}

    def anthropic(self):
        import anthropic

        return anthropic.Anthropic(base_url=self.cairn_url, api_key="test-token", max_retries=0)

    def openai(self):
        import openai

        return openai.OpenAI(base_url=f"{self.cairn_url}/v1", api_key="test-token", max_retries=0)

    def sql(self, sql: str, connection: Optional[str] = None, limit: int = 200) -> dict:
        self.queries.append(sql)
        return {"columns": [], "rows": self._rows[:limit]}

    def close(self) -> None:
        self._server.shutdown()

    def __enter__(self) -> "FakeContext":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
