"""WebDAV (class 1) at /-/dav/: a real client (webdav4) and protocol-level checks. Items are authorized as
the S3 operations they stand for; collection deletes, copies and moves are all-or-nothing on authorization."""

import base64
import io
import uuid
import xml.etree.ElementTree as ET

import pytest
import requests
from webdav4.client import Client

from conftest import ADMIN, READER

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


def test_options_advertise_class_1_only(dav):
    st, _ = dav
    r = requests.options(f"{st.endpoint}/-/dav/", timeout=10)
    assert r.status_code == 200 and r.headers["dav"] == "1"
    assert "PROPFIND" in r.headers["allow"] and "LOCK" not in r.headers["allow"]


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
    req(st, "PUT", f"{b}/keep/a.txt", data=b"a")
    req(st, "PUT", f"{b}/keep/b.txt", data=b"b")
    r = req(st, "PROPFIND", f"{b}/", key=None, headers={"Depth": "1"})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert req(st, "PROPFIND", f"{b}/", key=READER, headers={"Depth": "1"}).status_code == 207
    assert req(st, "PUT", f"{b}/keep/c.txt", key=READER, data=b"c").status_code == 403
    assert req(st, "DELETE", f"{b}/keep/", key=READER).status_code == 403
    assert req(st, "MOVE", f"{b}/keep/", key=READER, headers={"Destination": f"/-/dav/{b}/moved/"}).status_code == 403
    keys = sorted(o["Key"] for o in st.client().list_objects_v2(Bucket=b)["Contents"])
    assert keys == ["keep/a.txt", "keep/b.txt"]  # nothing changed
    assert req(st, "LOCK", f"{b}/keep/a.txt").status_code == 405
    assert req(st, "DELETE", f"{b}/").status_code == 403  # whole buckets: the S3 API


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
