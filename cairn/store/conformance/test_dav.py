"""WebDAV (classes 1 and 2) at /-/dav/: a real client (webdav4) and protocol-level checks. Items are authorized as
the S3 operations they stand for; collection deletes, copies and moves are all-or-nothing on authorization."""

import base64
import io
import uuid
import xml.etree.ElementTree as ET

import pytest
import requests
from webdav4.client import Client

from conftest import ADMIN, READER, SCOPED

D = "{DAV:}"


def auth(key):
    return {"Authorization": "Basic " + base64.b64encode(f"{key[0]}:{key[1]}".encode()).decode()}


@pytest.fixture
def dav(custom_store):
    st = custom_store({"CAIRN_STORE_FILES_BASIC_AUTH": "true", "CAIRN_STORE_FILES_BASIC_ALLOW_HTTP": "true"})
    b = "dav-" + uuid.uuid4().hex[:10]
    st.client().create_bucket(Bucket=b)
    return st, b


def req(st, method, path, key=ADMIN, **kw):
    headers = {**(auth(key) if key else {}), **kw.pop("headers", {})}
    return requests.request(method, f"{st.endpoint}/-/dav/{path}", headers=headers, timeout=10, **kw)


def responses(r):
    root = ET.fromstring(r.content)
    out = {}
    for resp in root.findall(f"{D}response"):
        prop = resp.find(f"{D}propstat/{D}prop")
        out[resp.find(f"{D}href").text] = prop
    return out


def test_options_advertise_classes_1_and_2(dav):
    st, _ = dav
    r = requests.options(f"{st.endpoint}/-/dav/", timeout=10)
    assert r.status_code == 200 and r.headers["dav"] == "1, 2"
    assert "PROPFIND" in r.headers["allow"] and "LOCK" in r.headers["allow"] and "UNLOCK" in r.headers["allow"]


def test_real_client_round_trip(dav):
    st, b = dav
    c = Client(f"{st.endpoint}/-/dav/{b}", auth=ADMIN)
    c.mkdir("reports")
    c.mkdir("reports/2026")
    c.upload_fileobj(io.BytesIO(b"q1,10\nq2,20\n"), "reports/2026/sales.csv", overwrite=True)
    c.upload_fileobj(io.BytesIO(b"x" * 300_000), "reports/big.bin", overwrite=True)
    assert c.isdir("reports") and c.isfile("reports/2026/sales.csv")
    names = sorted(e["name"] for e in c.ls("reports", detail=True))
    assert names == ["reports/2026", "reports/big.bin"]
    assert c.content_length("reports/big.bin") == 300_000
    out = io.BytesIO()
    c.download_fileobj("reports/2026/sales.csv", out)
    assert out.getvalue() == b"q1,10\nq2,20\n"
    c.copy("reports/2026/sales.csv", "reports/sales-copy.csv")
    c.mkdir("archive")
    c.move("reports/big.bin", "archive/big.bin")
    assert not c.exists("reports/big.bin") and c.content_length("archive/big.bin") == 300_000
    c.move("reports", "old-reports")  # a collection
    assert not c.exists("reports") and c.isfile("old-reports/2026/sales.csv") and c.isfile("old-reports/sales-copy.csv")
    c.remove("old-reports")
    assert not c.exists("old-reports")
    # The S3 API sees what WebDAV wrote.
    keys = sorted(o["Key"] for o in st.client().list_objects_v2(Bucket=b).get("Contents", []))
    assert keys == ["archive/", "archive/big.bin"]


def test_propfind(dav):
    st, b = dav
    req(st, "MKCOL", f"{b}/docs")
    req(st, "PUT", f"{b}/docs/a b.txt", data=b"hello", headers={"Content-Type": "text/plain"})
    r = req(st, "PROPFIND", f"{b}/docs/", headers={"Depth": "1"})
    assert r.status_code == 207
    res = responses(r)
    assert set(res) == {f"/-/dav/{b}/docs/", f"/-/dav/{b}/docs/a%20b.txt"}
    f = res[f"/-/dav/{b}/docs/a%20b.txt"]
    assert f.find(f"{D}getcontentlength").text == "5" and f.find(f"{D}getcontenttype").text == "text/plain"
    assert f.find(f"{D}resourcetype/{D}collection") is None
    assert res[f"/-/dav/{b}/docs/"].find(f"{D}resourcetype/{D}collection") is not None
    # Depth 0 on a file; requested properties only; infinity refused; missing is 404.
    body = '<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:getetag/></D:prop></D:propfind>'
    r = req(st, "PROPFIND", f"{b}/docs/a%20b.txt", headers={"Depth": "0"}, data=body)
    (prop,) = responses(r).values()
    assert [p.tag for p in prop] == [f"{D}getetag"]
    r = req(st, "PROPFIND", f"{b}/docs/", headers={"Depth": "infinity"})
    assert r.status_code == 207 and len(responses(r)) == 2
    assert req(st, "PROPFIND", f"{b}/nope/", headers={"Depth": "0"}).status_code == 404
    # The root lists buckets.
    r = req(st, "PROPFIND", "", headers={"Depth": "1"})
    assert f"/-/dav/{b}/" in responses(r)


def test_authorization(dav):
    st, b = dav
    assert req(st, "MKCOL", f"{b}/keep/").status_code == 201
    assert req(st, "PUT", f"{b}/keep/a.txt", data=b"a").status_code == 201
    req(st, "PUT", f"{b}/keep/b.txt", data=b"b")
    r = req(st, "PROPFIND", f"{b}/", key=None, headers={"Depth": "1"})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert req(st, "PROPFIND", f"{b}/", key=READER, headers={"Depth": "1"}).status_code == 207
    assert req(st, "PUT", f"{b}/keep/c.txt", key=READER, data=b"c").status_code == 403
    assert req(st, "DELETE", f"{b}/keep/", key=READER).status_code == 403
    assert req(st, "MOVE", f"{b}/keep/", key=READER, headers={"Destination": f"/-/dav/{b}/moved/"}).status_code == 403
    keys = sorted(o["Key"] for o in st.client().list_objects_v2(Bucket=b)["Contents"])
    assert keys == ["keep/", "keep/a.txt", "keep/b.txt"]  # nothing changed
    assert req(st, "LOCK", f"{b}/keep/a.txt", key=READER, data=LOCKINFO).status_code == 403  # locking is writing
    assert req(st, "DELETE", f"{b}/").status_code == 403  # whole buckets: the S3 API


def test_put_needs_its_parent_collection(dav):
    """RFC 4918 9.7.1: a PUT whose parent collection is missing is a 409, and creates nothing."""
    st, b = dav
    assert req(st, "PUT", f"{b}/missing/a.txt", data=b"a").status_code == 409
    assert "Contents" not in st.client().list_objects_v2(Bucket=b, Prefix="missing/")
    assert req(st, "MKCOL", f"{b}/missing/deeper/").status_code == 409  # still no parent
    assert req(st, "PUT", f"{b}/top.txt", data=b"t").status_code == 201  # the bucket is always a collection
    assert req(st, "MKCOL", f"{b}/missing/").status_code == 201
    assert req(st, "PUT", f"{b}/missing/a.txt", data=b"a").status_code == 201
    # A reader is refused the write itself (403), whatever exists.
    assert req(st, "PUT", f"{b}/nowhere/x.txt", key=READER, data=b"x").status_code == 403


def test_copy_depth(dav):
    """RFC 4918 9.8.3: COPY Depth 0 copies the collection alone; other depths than 0 and infinity are
    refused, and a MOVE always moves everything."""
    st, b = dav
    assert req(st, "MKCOL", f"{b}/src/").status_code == 201
    assert req(st, "PUT", f"{b}/src/foo", data=b"f").status_code == 201
    r = req(st, "COPY", f"{b}/src/", headers={"Destination": f"{st.endpoint}/-/dav/{b}/shallow/", "Depth": "0"})
    assert r.status_code == 201
    keys = sorted(o["Key"] for o in st.client().list_objects_v2(Bucket=b, Prefix="shallow")["Contents"])
    assert keys == ["shallow/"]  # the collection, empty
    assert req(st, "DELETE", f"{b}/shallow/foo").status_code == 404
    r = req(st, "COPY", f"{b}/src/", headers={"Destination": f"{st.endpoint}/-/dav/{b}/deep/"})
    assert r.status_code == 201 and req(st, "GET", f"{b}/deep/foo").content == b"f"
    # A collection that exists only by its members still copies shallowly, as an empty collection.
    st.client().put_object(Bucket=b, Key="implicit/x", Body=b"x")
    r = req(st, "COPY", f"{b}/implicit/", headers={"Destination": f"{st.endpoint}/-/dav/{b}/imp2/", "Depth": "0"})
    assert r.status_code == 201
    assert [o["Key"] for o in st.client().list_objects_v2(Bucket=b, Prefix="imp2")["Contents"]] == ["imp2/"]
    for bad in ("1", "2", "zero"):
        r = req(st, "COPY", f"{b}/src/", headers={"Destination": f"{st.endpoint}/-/dav/{b}/x{bad}/", "Depth": bad})
        assert r.status_code == 400, bad
    r = req(st, "MOVE", f"{b}/src/", headers={"Destination": f"{st.endpoint}/-/dav/{b}/moved/", "Depth": "0"})
    assert r.status_code == 400
    assert req(st, "GET", f"{b}/src/foo").content == b"f"  # nothing moved


def test_copy_rules(dav):
    st, b = dav
    req(st, "PUT", f"{b}/a.txt", data=b"a")
    req(st, "PUT", f"{b}/b.txt", data=b"b")
    r = req(st, "COPY", f"{b}/a.txt", headers={"Destination": f"{st.endpoint}/-/dav/{b}/b.txt", "Overwrite": "F"})
    assert r.status_code == 412
    r = req(st, "COPY", f"{b}/a.txt", headers={"Destination": f"{st.endpoint}/-/dav/{b}/b.txt"})
    assert r.status_code == 204 and req(st, "GET", f"{b}/b.txt").content == b"a"
    r = req(st, "COPY", f"{b}/a.txt", headers={"Destination": f"{st.endpoint}/-/dav/{b}/c.txt"})
    assert r.status_code == 201
    r = req(st, "COPY", f"{b}/a.txt", headers={"Destination": "http://elsewhere.example/-/dav/x/y"})
    assert r.status_code == 502
    req(st, "MKCOL", f"{b}/d")
    assert req(st, "MKCOL", f"{b}/d").status_code == 405  # exists
    assert req(st, "MKCOL", f"{b}/x/y").status_code == 409  # no parent
    r = req(st, "MOVE", f"{b}/d/", headers={"Destination": f"/-/dav/{b}/d/inner/"})
    assert r.status_code == 409  # into itself
    r = req(st, "PROPPATCH", f"{b}/a.txt", data='<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:"><D:set><D:prop>'
            '<D:displayname>x</D:displayname></D:prop></D:set></D:propertyupdate>')
    assert r.status_code == 207 and b"403 Forbidden" in r.content


LOCKINFO = ('<?xml version="1.0"?><D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope>'
            '<D:locktype><D:write/></D:locktype><D:owner><D:href>ana@example.com</D:href></D:owner></D:lockinfo>')


def lock(st, path, key=ADMIN, depth="infinity", timeout="Second-120"):
    r = req(st, "LOCK", path, key=key, data=LOCKINFO, headers={"Depth": depth, "Timeout": timeout})
    return r, r.headers.get("lock-token", "").strip("<>")


def test_locks(custom_store):
    """Exclusive write locks: a held lock refuses other writers (423) until its holder sends the token;
    tokens are bound to the key that took them; collection locks cover their members; refresh, discovery,
    unlock, and a lock on an unmapped URL that creates the file."""
    st = custom_store({"CAIRN_STORE_FILES_BASIC_AUTH": "true", "CAIRN_STORE_FILES_BASIC_ALLOW_HTTP": "true"})
    b = "lake-" + uuid.uuid4().hex[:8]  # SCOPED may write lake-* buckets too: a second writer
    st.client().create_bucket(Bucket=b)
    req(st, "PUT", f"{b}/doc.txt", data=b"v1")
    r, tok = lock(st, f"{b}/doc.txt")
    assert r.status_code == 200 and tok.startswith("opaquelocktoken:")
    active = ET.fromstring(r.content).find(f"{D}lockdiscovery/{D}activelock")
    assert active.find(f"{D}locktoken/{D}href").text == tok and "ana@example.com" in ET.tostring(active).decode()
    # Others, and the holder without the token, are refused.
    assert req(st, "PUT", f"{b}/doc.txt", key=SCOPED, data=b"other").status_code == 423
    assert req(st, "PUT", f"{b}/doc.txt", data=b"no token").status_code == 423
    assert req(st, "DELETE", f"{b}/doc.txt", key=SCOPED).status_code == 423
    assert req(st, "MOVE", f"{b}/doc.txt", key=SCOPED, headers={"Destination": f"/-/dav/{b}/x.txt"}).status_code == 423
    assert lock(st, f"{b}/doc.txt", key=SCOPED)[0].status_code == 423
    # A token from someone else's lock does not help, and an unknown token fails the request.
    assert req(st, "PUT", f"{b}/doc.txt", key=SCOPED, data=b"x", headers={"If": f"(<{tok}>)"}).status_code == 423
    assert req(st, "PUT", f"{b}/doc.txt", data=b"x", headers={"If": "(<opaquelocktoken:nope>)"}).status_code == 412
    assert st.client().get_object(Bucket=b, Key="doc.txt")["Body"].read() == b"v1"
    # The holder writes with the token; discovery shows the lock; a refresh renews it.
    assert req(st, "PUT", f"{b}/doc.txt", data=b"v2", headers={"If": f"(<{tok}>)"}).status_code in (200, 201, 204)
    r = req(st, "PROPFIND", f"{b}/doc.txt", headers={"Depth": "0"})
    assert tok in r.text and "supportedlock" in r.text
    r = req(st, "LOCK", f"{b}/doc.txt", headers={"If": f"(<{tok}>)", "Timeout": "Second-300"})
    assert r.status_code == 200 and ("Second-300<" in r.text or "Second-299<" in r.text)  # 300, or a second less by now
    assert req(st, "LOCK", f"{b}/doc.txt", key=SCOPED, headers={"If": f"(<{tok}>)"}).status_code == 409  # not theirs
    # Only the holder unlocks; then anyone may write.
    assert req(st, "UNLOCK", f"{b}/doc.txt", key=SCOPED, headers={"Lock-Token": f"<{tok}>"}).status_code == 409
    assert req(st, "UNLOCK", f"{b}/doc.txt", headers={"Lock-Token": f"<{tok}>"}).status_code == 204
    assert req(st, "PUT", f"{b}/doc.txt", key=SCOPED, data=b"v3").status_code in (200, 201, 204)
    # A collection locked to any depth covers its members, new ones too, and moves into it.
    req(st, "MKCOL", f"{b}/team/")
    req(st, "PUT", f"{b}/team/a.txt", data=b"a")
    r, ctok = lock(st, f"{b}/team/")
    assert r.status_code == 200
    assert req(st, "PUT", f"{b}/team/a.txt", key=SCOPED, data=b"x").status_code == 423
    assert req(st, "PUT", f"{b}/team/new.txt", key=SCOPED, data=b"x").status_code == 423
    assert req(st, "MOVE", f"{b}/doc.txt", key=SCOPED, headers={"Destination": f"/-/dav/{b}/team/doc.txt"}).status_code == 423
    assert req(st, "DELETE", f"{b}/team/", key=SCOPED).status_code == 423
    assert lock(st, f"{b}/team/a.txt", key=SCOPED)[0].status_code == 423  # inside a deep lock
    assert req(st, "PUT", f"{b}/team/new.txt", data=b"n", headers={"If": f"(<{ctok}>)"}).status_code in (200, 201)
    # A deep lock cannot be taken over a locked member.
    req(st, "UNLOCK", f"{b}/team/", headers={"Lock-Token": f"<{ctok}>"})
    _, mtok = lock(st, f"{b}/team/a.txt", key=SCOPED)
    assert lock(st, f"{b}/team/")[0].status_code == 423
    req(st, "UNLOCK", f"{b}/team/a.txt", key=SCOPED, headers={"Lock-Token": f"<{mtok}>"})
    # LOCK on an unmapped URL creates an empty file, held by the lock.
    r, ntok = lock(st, f"{b}/fresh.txt", depth="0")
    assert r.status_code == 201 and st.client().head_object(Bucket=b, Key="fresh.txt")["ContentLength"] == 0
    assert req(st, "PUT", f"{b}/fresh.txt", key=SCOPED, data=b"x").status_code == 423
    # Shared locks live alongside each other, never alongside an exclusive one; a write needs one of them.
    shared = LOCKINFO.replace("<D:exclusive/>", "<D:shared/>")
    req(st, "PUT", f"{b}/shared.txt", data=b"s")
    r1 = req(st, "LOCK", f"{b}/shared.txt", data=shared)
    r2 = req(st, "LOCK", f"{b}/shared.txt", key=SCOPED, data=shared)
    assert r1.status_code == 200 and r2.status_code == 200
    assert lock(st, f"{b}/shared.txt")[0].status_code == 423
    t2 = r2.headers["lock-token"].strip("<>")
    assert req(st, "PUT", f"{b}/shared.txt", key=SCOPED, data=b"x").status_code == 423
    assert req(st, "PUT", f"{b}/shared.txt", key=SCOPED, data=b"x", headers={"If": f"(<{t2}>)"}).status_code in (200, 201, 204)
    # Dead properties: kept, returned, removed; live ones are protected.
    ns = "http://example.com/ns"
    patch = (f'<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:" xmlns:Z="{ns}"><D:set><D:prop><Z:color>red</Z:color>'
             '</D:prop></D:set></D:propertyupdate>')
    assert req(st, "PROPPATCH", f"{b}/doc.txt", data=patch).status_code == 207
    find = f'<?xml version="1.0"?><D:propfind xmlns:D="DAV:" xmlns:Z="{ns}"><D:prop><Z:color/><Z:size/></D:prop></D:propfind>'
    r = req(st, "PROPFIND", f"{b}/doc.txt", data=find, headers={"Depth": "0"})
    stats = ET.fromstring(r.content).findall(f"{D}response/{D}propstat")
    found = {s.find(f"{D}status").text: [p.tag for p in s.find(f"{D}prop")] for s in stats}
    assert found["HTTP/1.1 200 OK"] == [f"{{{ns}}}color"] and found["HTTP/1.1 404 Not Found"] == [f"{{{ns}}}size"]
    assert ET.fromstring(r.content).find(f".//{{{ns}}}color").text == "red"
    assert st.client().get_object(Bucket=b, Key="doc.txt")["Body"].read() == b"v3"  # content untouched
    live = ('<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:" xmlns:Z="http://example.com/ns"><D:set><D:prop>'
            '<D:getetag>x</D:getetag><Z:other>1</Z:other></D:prop></D:set></D:propertyupdate>')
    r = req(st, "PROPPATCH", f"{b}/doc.txt", data=live)
    assert "403 Forbidden" in r.text and "424 Failed Dependency" in r.text


def test_locks_expire(custom_store):
    st = custom_store({"CAIRN_STORE_FILES_BASIC_AUTH": "true", "CAIRN_STORE_FILES_BASIC_ALLOW_HTTP": "true"})
    b = "lake-" + uuid.uuid4().hex[:8]
    st.client().create_bucket(Bucket=b)
    req(st, "PUT", f"{b}/t.txt", data=b"t")
    _, tok = lock(st, f"{b}/t.txt", timeout="Second-1")
    assert req(st, "PUT", f"{b}/t.txt", key=SCOPED, data=b"x").status_code == 423
    import time
    time.sleep(2.2)
    assert req(st, "PUT", f"{b}/t.txt", key=SCOPED, data=b"x").status_code in (200, 201, 204)
    assert req(st, "PUT", f"{b}/t.txt", data=b"y", headers={"If": f"(<{tok}>)"}).status_code == 412  # expired token


@pytest.mark.skipif(not __import__("shutil").which("litmus"), reason="needs litmus (the WebDAV test suite)")
def test_litmus(dav):
    """litmus, the WebDAV protocol test suite: basic, copymove, props and locks all pass."""
    import subprocess
    st, b = dav
    out = subprocess.run(["litmus", f"{st.endpoint}/-/dav/{b}/", *ADMIN], capture_output=True, text=True, timeout=300,
                         env={"TESTS": "basic copymove props locks", "PATH": __import__("os").environ["PATH"]})
    summary = [line for line in out.stdout.splitlines() if "<- summary" in line]
    lines = out.stdout.splitlines()
    failures = [f"{lines[i]} | {lines[i + 1] if i + 1 < len(lines) else ''}" for i, line in enumerate(lines) if "FAIL" in line]
    assert len(summary) == 4 and all("0 failed" in line for line in summary), "\n".join(failures)
