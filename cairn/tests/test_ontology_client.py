"""The ontology compiler's client: ids from a request stay one path segment of the compiler's API."""

import httpx
import pytest

from cairn.context.ontology_compiler import OntologyClient, OntologyError


def _client(seen: list[str]) -> OntologyClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"runs": [{"id": "r1"}], "tables": []})
    return OntologyClient("http://compiler", "t" * 16, http=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("kind,eid", [("..", "runs"), (".", "x"), ("term", "..")])
def test_dot_segments_do_not_reach_other_routes(kind, eid):
    # GET /v1/ontology/provenance/../runs would have been resolved to the compiler's /v1/runs (a manager's read).
    seen: list[str] = []
    with pytest.raises(OntologyError) as e:
        _client(seen).provenance(kind, eid)
    assert e.value.status == 404 and seen == []


def test_ids_are_encoded_as_one_segment():
    seen: list[str] = []
    c = _client(seen)
    c.provenance("term", "a/b?c=1#d")
    c.run("r1?x=1")
    c.cancel("../catalog")
    assert seen == ["/v1/provenance/term/a%2Fb%3Fc%3D1%23d", "/v1/runs/r1%3Fx%3D1", "/v1/runs/..%2Fcatalog/cancel"]
    with pytest.raises(OntologyError):
        c.resume("..")
