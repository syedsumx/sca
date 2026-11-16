"""Document sources, the parts that need no store or remote service: how a run maps files to store keys (renames,
files with the same path), whose keys a run reads, which prefixes sources may share, and how much a download may
read. The end-to-end test with the real store, catalog and pipe is ``test_doc_sources_e2e.py``."""


import json

import httpx
import pytest
from sqlalchemy import create_engine

from cairn.pipelines import doc_sources as ds

A, B = "urn:li:corpuser:alice", "urn:li:corpuser:bob"


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[f"{Bucket}/{Key}"] = Body

    def delete_object(self, Bucket, Key):
        self.objects.pop(f"{Bucket}/{Key}", None)


class Folder:
    """A remote folder: item id -> (path, version, bytes)."""

    def __init__(self, files: dict):
        self.files = files

    def __call__(self, http, keys, location):
        for item, (path, version, data) in list(self.files.items()):
            yield (item, path, version, len(data), lambda limit, data=data: data if len(data) <= limit else None)


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/d.db")
    ds.document_sources.create(engine)
    ds.document_source_items.create(engine)
    reads = []

    def read_secret(owner, scope, key):
        reads.append((owner, scope, key))
        if key in ds.OPTIONAL["sharepoint"]:
            raise Exception("no such secret")
        return "v"
    d = ds.DocumentSources(engine, {"endpoint": "http://store"}, read_secret)
    s3 = FakeS3()
    monkeypatch.setattr(d, "_s3", lambda: s3)
    folder = Folder({})
    monkeypatch.setitem(ds.LISTERS, "sharepoint", folder)
    d.save(A, {"name": "contracts", "kind": "sharepoint", "location": {"drive_id": "d1"}, "bucket": "docs",
               "prefix": "sp/", "secret_scope": "sp-keys"})
    return d, s3, folder, reads


def test_files_that_swap_names_both_stay(world):
    d, s3, folder, _ = world
    folder.files.update({"a": ("x.txt", "1", b"A"), "b": ("y.txt", "1", b"B")})
    assert d.run("contracts")["status"] == "ok"
    folder.files.update({"a": ("y.txt", "2", b"A"), "b": ("x.txt", "2", b"B")})
    assert d.run("contracts")["status"] == "ok"
    assert s3.objects == {"docs/sp/y.txt": b"A", "docs/sp/x.txt": b"B"}


def test_a_file_deleted_does_not_take_another_with_the_same_path(world):
    d, s3, folder, _ = world
    # Google Drive (and a SharePoint name with a "/" in another folder's spelling) can hold two files at one path.
    folder.files.update({"a": ("notes.txt", "1", b"A"), "b": ("notes.txt", "1", b"B")})
    r = d.run("contracts")
    assert r["status"] == "ok" and len(r["skipped"]) == 1
    assert s3.objects == {"docs/sp/notes.txt": b"A"}
    del folder.files["b"]
    assert d.run("contracts")["status"] == "ok"
    assert s3.objects == {"docs/sp/notes.txt": b"A"}, "the object of the file that stayed was deleted"
    del folder.files["a"]
    d.run("contracts")
    assert s3.objects == {}


def test_a_changed_source_reads_its_keys_as_who_changed_it(world):
    d, _, folder, reads = world
    # Bob, another operator, points Alice's source at a scope only Alice may read: the keys are read as Bob now.
    d.save(B, {"kind": "sharepoint", "location": {"drive_id": "hr"}, "bucket": "docs", "prefix": "sp/",
               "secret_scope": "hr-keys"}, "contracts")
    reads.clear()
    d.run("contracts")
    assert reads and {owner for owner, _, _ in reads} == {B}
    assert d.get("contracts")["owner"] == B


def test_sources_may_not_share_a_prefix(world):
    d, *_ = world
    for prefix in ("sp/", "sp/sub/", ""):
        with pytest.raises(ds.DocumentSourceError) as e:
            d.save(A, {"name": f"other-{len(prefix)}", "kind": "sharepoint", "location": {"drive_id": "d2"},
                       "bucket": "docs", "prefix": prefix, "secret_scope": "sp-keys"})
        assert e.value.status == 409
    d.save(A, {"name": "elsewhere", "kind": "sharepoint", "location": {"drive_id": "d2"}, "bucket": "docs",
               "prefix": "spx/", "secret_scope": "sp-keys"})
    d.save(A, {"kind": "sharepoint", "location": {"drive_id": "d1"}, "bucket": "docs", "prefix": "sp/",
               "secret_scope": "sp-keys"}, "contracts")                         # itself is no conflict


def test_a_download_reads_no_more_than_the_limit():
    """A Google Doc is listed without a size (it is exported): its export must not be read whole into memory."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    sent = {"bytes": 0}

    class Endless(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10_000):                     # 640 MB, if read whole
                sent["bytes"] += 65536
                yield b"x" * 65536

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t"})
        if request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [
                {"id": "g1", "name": "Big", "mimeType": "application/vnd.google-apps.document"}]})
        if request.url.path == "/drive/v3/files/g1/export":
            return httpx.Response(200, stream=Endless())
        return httpx.Response(404)

    keys = {"service_account_json": json.dumps({"private_key": pem, "client_email": "sa@example.iam",
                                                "token_uri": "https://oauth2.example/token"}),
            "base_url": "https://drive.example"}
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        [(item, path, version, size, fetch)] = list(ds.google_drive_files(http, keys, {"folder_id": "gf1"}))
        assert size == 0 and path == "Big.docx"
        assert fetch(1_000_000) is None
    assert sent["bytes"] < 5_000_000, f"{sent['bytes']} bytes read for a 1 MB limit"
