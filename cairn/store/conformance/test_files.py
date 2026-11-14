"""The plain HTTP file API and directory browser (/-/files/): reads, writes, deletes and listings are the
S3 operations they stand for, with the same authorization; HTTP Basic sign-in only when enabled."""

import base64
import urllib.parse
import uuid

import pytest
import requests
from botocore.auth import S3SigV4Auth, S3SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from conftest import ADMIN, READER, REGION, SCOPED


def call(store, method, path, key=ADMIN, data=None, headers=None, params=""):
    url = f"{store.endpoint}/-/files/{urllib.parse.quote(path)}" + (f"?{params}" if params else "")
    req = AWSRequest(method=method, url=url, data=data, headers=headers or {})
    if key:
        S3SigV4Auth(Credentials(*key), "s3", REGION).add_auth(req)
    return requests.request(method, url, data=data, headers=dict(req.headers), timeout=10)


def basic(key):
    return {"Authorization": "Basic " + base64.b64encode(f"{key[0]}:{key[1]}".encode()).decode()}


def test_write_read_list_delete(store, s3, bucket):
    r = call(store, "PUT", f"{bucket}/docs/a.txt", data=b"hello files", headers={"Content-Type": "text/plain"})
    assert r.status_code == 200, r.text
    call(store, "PUT", f"{bucket}/docs/sub/b.bin", data=b"\x00" * 2048)
    call(store, "PUT", f"{bucket}/top.txt", data=b"top")
    # The S3 API sees the same objects, and the file API sees S3's.
    assert s3.get_object(Bucket=bucket, Key="docs/a.txt")["Body"].read() == b"hello files"
    s3.put_object(Bucket=bucket, Key="docs/from-s3.txt", Body=b"s3")
    r = call(store, "GET", f"{bucket}/docs/a.txt")
    assert r.status_code == 200 and r.content == b"hello files" and r.headers["content-type"] == "text/plain"
    assert r.headers["content-security-policy"] == "sandbox" and r.headers["x-content-type-options"] == "nosniff"
    r = call(store, "GET", f"{bucket}/docs/a.txt", headers={"Range": "bytes=6-10"})
    assert r.status_code == 206 and r.content == b"files"
    r = call(store, "HEAD", f"{bucket}/docs/a.txt")
    assert r.status_code == 200 and r.headers["content-length"] == "11"
    # Directories.
    top = call(store, "GET", f"{bucket}/").json()
    assert top["path"] == f"/{bucket}/"
    assert [(en["name"], en["type"]) for en in top["entries"]] == [("docs/", "directory"), ("top.txt", "file")]
    docs = call(store, "GET", f"{bucket}/docs/").json()
    assert [en["name"] for en in docs["entries"]] == ["sub/", "a.txt", "from-s3.txt"]
    a = docs["entries"][1]
    assert a["size"] == 11 and a["storage_class"] == "STANDARD" and a["modified"].endswith("Z") and docs["next"] is None
    assert any(en["name"] == f"{bucket}/" for en in call(store, "GET", "").json()["entries"])
    # Deletes; a missing file is a JSON 404.
    assert call(store, "DELETE", f"{bucket}/docs/a.txt").status_code == 204
    r = call(store, "GET", f"{bucket}/docs/a.txt")
    assert r.status_code == 404 and r.json()["error"] == "NoSuchKey"


def test_paging(store, bucket):
    for i in range(5):
        call(store, "PUT", f"{bucket}/p/f{i}", data=b"x")
    names, after = [], ""
    while True:
        q = "limit=2" + (f"&after={urllib.parse.quote(after, safe='')}" if after else "")
        page = call(store, "GET", f"{bucket}/p/", params=q).json()
        names += [en["name"] for en in page["entries"]]
        if not page["next"]:
            break
        after = page["next"]
    assert names == [f"f{i}" for i in range(5)]


def test_html_listing_is_escaped_and_scriptless(store, bucket):
    call(store, "PUT", f"{bucket}/<img src=x onerror=alert(1)>.txt", data=b"x")
    call(store, "PUT", f"{bucket}/a dir/x", data=b"x")
    r = call(store, "GET", f"{bucket}/", headers={"Accept": "text/html"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<img" not in r.text and "&lt;img src=x onerror=alert(1)&gt;.txt" in r.text
    assert 'href="./a%20dir/"' in r.text and 'href="../"' in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script" not in csp


def test_authorization_is_s3s(store, bucket):
    call(store, "PUT", f"{bucket}/f", data=b"x")
    r = call(store, "GET", f"{bucket}/f", key=None)
    assert r.status_code == 403 and r.json()["error"] == "AccessDenied"
    assert call(store, "GET", f"{bucket}/", key=SCOPED).status_code == 403  # outside lake-*
    assert call(store, "GET", f"{bucket}/f", key=READER).status_code == 200
    assert call(store, "PUT", f"{bucket}/g", key=READER, data=b"x").status_code == 403  # read only
    assert all(en["name"] != f"{bucket}/" for en in call(store, "GET", "", key=SCOPED).json()["entries"])
    bad = Credentials(ADMIN[0], "wrong")
    req = AWSRequest(method="GET", url=f"{store.endpoint}/-/files/{bucket}/f")
    S3SigV4Auth(bad, "s3", REGION).add_auth(req)
    assert requests.get(req.url, headers=dict(req.headers), timeout=10).status_code == 403
    # Only the file API's own parameters; S3 subresources stay on the S3 API.
    r = call(store, "GET", f"{bucket}/f", params="acl")
    assert r.status_code == 400 and r.json()["error"] == "InvalidArgument"
    assert call(store, "POST", f"{bucket}/f", data=b"x").status_code == 405
    # Basic sign-in is off by default.
    r = requests.get(f"{store.endpoint}/-/files/{bucket}/f", headers=basic(ADMIN), timeout=10)
    assert r.status_code == 400 and r.json()["error"] == "InvalidRequest"


def test_presigned_url(store, bucket):
    call(store, "PUT", f"{bucket}/shared.txt", data=b"shared")
    req = AWSRequest(method="GET", url=f"{store.endpoint}/-/files/{bucket}/shared.txt")
    S3SigV4QueryAuth(Credentials(*ADMIN), "s3", REGION, expires=60).add_auth(req)
    r = requests.get(req.url, timeout=10)
    assert r.status_code == 200 and r.content == b"shared"
    tampered = req.url.replace("shared.txt", "other.txt")
    assert requests.get(tampered, timeout=10).status_code == 403


def test_basic_sign_in(custom_store):
    st = custom_store({"CAIRN_STORE_FILES_BASIC_AUTH": "true"})
    b = "t-" + uuid.uuid4().hex[:12]
    st.client().create_bucket(Bucket=b)
    r = requests.get(f"{st.endpoint}/-/files/{b}/", headers=basic(ADMIN), timeout=10)
    assert r.status_code == 400 and r.json()["error"] == "InsecureTransport"  # no TLS
    st2 = custom_store({"CAIRN_STORE_FILES_BASIC_AUTH": "true", "CAIRN_STORE_FILES_BASIC_ALLOW_HTTP": "true"})
    s3 = st2.client()
    s3.create_bucket(Bucket=b)
    url = f"{st2.endpoint}/-/files/{b}/note.txt"
    assert requests.put(url, data=b"via basic", headers=basic(ADMIN), timeout=10).status_code == 200
    assert requests.get(url, headers=basic(ADMIN), timeout=10).content == b"via basic"
    assert requests.get(url, headers=basic((ADMIN[0], "wrong")), timeout=10).status_code == 403
    assert requests.put(url, data=b"x", headers=basic(READER), timeout=10).status_code == 403
    r = requests.get(url, timeout=10)  # a browser without credentials is asked to sign in
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert s3.get_object(Bucket=b, Key="note.txt")["Body"].read() == b"via basic"


@pytest.mark.parametrize("path", ["../etc/passwd", "%2e%2e/x"])
def test_odd_paths_stay_in_the_bucket_space(store, path):
    r = requests.get(f"{store.endpoint}/-/files/{path}", timeout=10)
    assert r.status_code in (400, 403, 404)
