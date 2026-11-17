"""Governed volumes (L58) without a store: what Cairn's API does around the store's answers, with an in-memory
stand-in for Cairn Store. The store itself is covered by tests/test_volumes.py (CAIRN_STORE_BIN)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from cairn.common.db import get_engine, init_db
from cairn.dataaccess.volumes import VolumeError, Volumes, build_volumes_router

OWNER, ANA = "urn:li:corpuser:owner", "urn:li:corpuser:ana"


class FakeStore:
    """The StoreClient calls Volumes makes, kept in memory; ``types`` is each object's stored content type, as a
    writer with engine credentials can set it."""

    url, region = "http://store.invalid", "us-east-1"

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.types: dict[tuple[str, str], str] = {}
        self.buckets: set[str] = set()

    def create_bucket(self, bucket):
        self.buckets.add(bucket)

    def delete_bucket(self, bucket):
        self.buckets.discard(bucket)

    def list(self, bucket, prefix="", limit=1000, after=""):
        keys = sorted(k for b, k in self.objects if b == bucket and k.startswith(prefix) and k > after)
        return {"files": [{"path": k, "size": len(self.objects[(bucket, k)]), "modified": ""} for k in keys[:limit]],
                "truncated": len(keys) > limit}

    def head(self, bucket, key):
        if (bucket, key) not in self.objects:
            raise VolumeError("no such file", 404)
        return {"size": len(self.objects[(bucket, key)]), "type": self.types.get((bucket, key), "")}

    def get(self, bucket, key):
        yield self.objects[(bucket, key)]

    def put(self, bucket, key, body):
        self.objects[(bucket, key)] = body

    def delete(self, bucket, key):
        self.objects.pop((bucket, key), None)


@pytest.fixture
def vs(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/v.db")
    init_db(engine)
    v = Volumes(engine, FakeStore(), lambda a: [], lambda a: a == OWNER, lambda a: False)
    v.create(OWNER, "landing")
    v.grant(OWNER, "landing", ANA, "read")
    return v


def client(vs) -> TestClient:
    class P:
        def __init__(self, urn):
            self.urn = urn

    def current(request: Request):
        return P(request.headers["x-user"])
    app = FastAPI()
    app.include_router(build_volumes_router(vs, current))
    return TestClient(app)


def test_a_file_is_served_as_a_download_never_as_a_page_of_cairns(vs):
    # A writer with engine credentials stores a page with its own content type; a reader opening its link in a
    # browser signed in to Cairn must get a download, not a page run on Cairn's origin with their session.
    vs.store.put("cairn-vol-landing", "report.html", b"<script>fetch('/v1/me')</script>")
    vs.store.types[("cairn-vol-landing", "report.html")] = "text/html"
    r = client(vs).get("/v1/volumes/landing/files/report.html", headers={"x-user": ANA})
    assert r.status_code == 200 and r.content == b"<script>fetch('/v1/me')</script>"
    assert r.headers["content-disposition"] == 'attachment; filename="report.html"'
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["content-type"].split(";")[0] == "text/html"   # the type is told; the browser does not run it


def test_a_chunked_upload_is_cut_off_at_the_limit_not_read_whole(vs):
    # A chunked body has no content-length: it is counted as it arrives, and refused once over the limit.
    import asyncio

    vs.grant(OWNER, "landing", ANA, "write")
    vs.max_upload = 1000
    app = FastAPI()

    class P:
        urn = ANA
    app.include_router(build_volumes_router(vs, lambda: P()))
    sent, out = {"n": 0}, []

    async def receive():
        sent["n"] += 1
        return {"type": "http.request", "body": b"x" * 400, "more_body": sent["n"] < 10_000}

    async def send(msg):
        out.append(msg)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT", "scheme": "http",
             "path": "/v1/volumes/landing/files/big.bin", "raw_path": b"/v1/volumes/landing/files/big.bin",
             "query_string": b"", "root_path": "", "headers": [(b"transfer-encoding", b"chunked")],
             "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
    asyncio.run(app(scope, receive, send))
    assert out[0]["status"] == 413
    assert sent["n"] <= 4                                   # stopped at the limit, not after 4 MB
    assert ("cairn-vol-landing", "big.bin") not in vs.store.objects
