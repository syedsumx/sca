"""Images reach the model in each provider's own format (Anthropic content blocks; Ollama message images), and a
call without images is unchanged."""

from __future__ import annotations

import base64
import json
import types

from cairn.ai.llm import AnthropicProvider, OllamaProvider


class _Messages:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return types.SimpleNamespace(stop_reason="end_turn", model="m", content=[types.SimpleNamespace(type="text", text='{"a": 1}')],
                                     usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))


def test_anthropic_sends_images_as_content_blocks_before_the_text():
    msgs = _Messages()
    client = types.SimpleNamespace(messages=msgs, beta=types.SimpleNamespace(messages=msgs))
    p = AnthropicProvider("claude-sonnet-5-5", client=client)
    data, _ = p.generate_json("sys", "describe it", {"type": "object"}, images=[("image/jpeg", b"\xff\xd8jpeg")])
    content = msgs.kwargs["messages"][0]["content"]
    assert data == {"a": 1} and content[0] == {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(b"\xff\xd8jpeg").decode()}}
    assert content[1] == {"type": "text", "text": "describe it"}
    p.generate_json("sys", "plain", {"type": "object"})
    assert msgs.kwargs["messages"][0]["content"] == "plain"


def test_ollama_sends_images_on_the_message():
    sent = {}

    class Http:
        def post(self, url, json=None):  # noqa: A002 - httpx's name
            sent.update(json)
            return types.SimpleNamespace(status_code=200, json=lambda: {"message": {"content": '{"a": 2}'}})
    p = OllamaProvider()
    p.http = Http()
    data, _ = p.generate_json("sys", "look", {"type": "object"}, images=[("image/png", b"png")])
    assert data == {"a": 2} and sent["messages"][1]["images"] == [base64.b64encode(b"png").decode()]
    assert json.dumps(sent)  # serialisable as sent
