"""Document sources, with the listing and the store stood in (review findings on L98; the sources against the real
store and catalog, with Graph and Drive stand-ins, are tests/test_doc_sources_e2e.py)."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.pipelines import doc_sources
from cairn.pipelines.doc_sources import DocumentSourceError, DocumentSources

OWNER, OTHER = "urn:li:corpuser:ana", "urn:li:corpuser:bob"
BODY = {"kind": "google_drive", "location": {"folder_id": "f1"}, "bucket": "landing", "prefix": "gd/",
        "secret_scope": "keys"}


class Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body):  # noqa: N803 - boto3's names
        self.objects[f"{Bucket}/{Key}"] = Body

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(f"{Bucket}/{Key}", None)


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    files: dict[str, tuple[str, str, bytes]] = {}          # id -> (path, version, content)
    readers = []

    def lister(http, keys, location):
        for fid, (path, version, data) in list(files.items()):
            yield fid, doc_sources._clean(path), version, len(data), (lambda limit, data=data: data)
    monkeypatch.setitem(doc_sources.LISTERS, "google_drive", lister)
    store = Store()

    def read_secret(owner, scope, key):
        readers.append(owner)
        if key == "service_account_json":
            return "{}"
        raise KeyError("no such secret")
    d = DocumentSources(engine, {}, read_secret)
    monkeypatch.setattr(d, "_s3", lambda: store)
    monkeypatch.setattr(d, "_keys", lambda s: (read_secret(s["owner"], s["secret_scope"], "service_account_json"),
                                                {})[1])
    return {"d": d, "files": files, "store": store, "readers": readers}


def test_a_file_renamed_into_a_deleted_files_place_is_kept(world):
    """Review finding: when a file was deleted and another renamed to its name (a new version put in place), the new
    file was written at that key and then removed as the deleted file's object: lost until it changed again."""
    d, files, store = world["d"], world["files"], world["store"]
    d.save(OWNER, {"name": "drive", **BODY})
    files.update({"x": ("report.pdf", "1", b"old"), "y": ("draft.pdf", "1", b"new")})
    assert d.run("drive")["written"] == 2
    del files["x"]
    files["y"] = ("report.pdf", "1", b"new")
    out = d.run("drive")
    assert out["status"] == "ok", out
    assert store.objects == {"landing/gd/report.pdf": b"new"}
    # Two files swapping names keep both.
    files.update({"z": ("a.pdf", "1", b"A")})
    d.run("drive")
    files.update({"y": ("a.pdf", "1", b"new"), "z": ("report.pdf", "1", b"A")})
    d.run("drive")
    assert store.objects == {"landing/gd/a.pdf": b"new", "landing/gd/report.pdf": b"A"}


def test_a_file_named_to_leave_the_folder_stays_in_it_and_the_run_goes_on(monkeypatch):
    """Review finding: Drive allows a file named "..": the listing raised, so whoever could add a file to the folder
    stopped every sync of it."""
    import cairn.pipelines.saas as saas

    monkeypatch.setattr(saas, "google_session", lambda http, keys, scope, base, what: ("https://g.example", {}, {}))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"files": [
            {"id": "1", "name": "..", "mimeType": "text/plain", "size": "1"},
            {"id": "2", "name": "a/../../up.txt", "mimeType": "text/plain", "size": "1"},
            {"id": "3", "name": "ok.txt", "mimeType": "text/plain", "size": "2", "md5Checksum": "h"}]})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    got = [(fid, path) for fid, path, *_ in doc_sources.google_drive_files(http, {}, {"folder_id": "f1"})]
    assert got == [("1", "_"), ("2", "a_.._.._up.txt"), ("3", "ok.txt")]    # renamed inside the folder


def test_whoever_changes_a_source_owns_its_keys(world):
    """Review finding: a change by another operator kept the creator as owner, so the source then read keys from
    whatever scope the editor named, as the creator."""
    d, readers = world["d"], world["readers"]
    d.save(OWNER, {"name": "drive", **BODY})
    d.save(OTHER, {**BODY, "secret_scope": "anas-private-keys"}, "drive")
    assert d.get("drive")["owner"] == OTHER
    d.run("drive")
    assert readers == [OTHER]


def test_a_next_page_link_elsewhere_is_not_followed(monkeypatch):
    """Review finding: Graph's @odata.nextLink was fetched as given, with the bearer token, wherever it pointed."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://evil.example/steal?page=2"})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    keys = {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
    with pytest.raises(DocumentSourceError) as e:
        list(doc_sources.sharepoint_files(http, keys, {"drive_id": "d1"}))
    assert "next page" in str(e.value)
    assert not any("evil.example" in c for c in calls)
