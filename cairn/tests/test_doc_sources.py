"""Document sources, the parts that need no store or remote service: how a run maps files to store keys (renames,
files with the same path), whose keys a run reads, which prefixes sources may share, and how much a download may
read. The end-to-end test with the real store, catalog and pipe is ``test_doc_sources_e2e.py``."""


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
            yield (item, path, version, len(data), lambda data=data, **kw: data)


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
