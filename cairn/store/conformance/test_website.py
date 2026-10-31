"""Static website hosting, GetObjectAttributes, and the request-payment and acceleration settings."""

import hashlib
import json

import pytest
import requests
from botocore.exceptions import ClientError

SITE = {"IndexDocument": {"Suffix": "index.html"}, "ErrorDocument": {"Key": "404.html"},
        "RoutingRules": [{"Condition": {"KeyPrefixEquals": "old/"},
                          "Redirect": {"ReplaceKeyPrefixWith": "new/", "HttpRedirectCode": "302"}}]}


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def public_read(bucket):
    return json.dumps({"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
                                      "Resource": f"arn:aws:s3:::{bucket}/*"}]})


def test_website_configuration_round_trip(s3, bucket):
    with pytest.raises(ClientError) as ei:
        s3.get_bucket_website(Bucket=bucket)
    assert code(ei) == "NoSuchWebsiteConfiguration"
    s3.put_bucket_website(Bucket=bucket, WebsiteConfiguration=SITE)
    got = s3.get_bucket_website(Bucket=bucket)
    assert got["IndexDocument"] == {"Suffix": "index.html"} and got["ErrorDocument"] == {"Key": "404.html"}
    assert got["RoutingRules"][0]["Redirect"]["ReplaceKeyPrefixWith"] == "new/"
    s3.delete_bucket_website(Bucket=bucket)
    with pytest.raises(ClientError):
        s3.get_bucket_website(Bucket=bucket)
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket=bucket, Key="r", Body=b"", WebsiteRedirectLocation="javascript:alert(1)")
    assert code(ei) == "InvalidRedirectLocation"


@pytest.fixture
def website(custom_store):
    st = custom_store({"CAIRN_STORE_BLOCK_PUBLIC_ACCESS": "false", "CAIRN_STORE_WEBSITE_DOMAIN": "web.test"})
    s3 = st.client()
    s3.create_bucket(Bucket="site")
    for key, body in {"index.html": b"<h1>home</h1>", "docs/index.html": b"<h1>docs</h1>", "404.html": b"<h1>missing</h1>",
                      "new/page.html": b"new page"}.items():
        s3.put_object(Bucket="site", Key=key, Body=body, ContentType="text/html")
    s3.put_object(Bucket="site", Key="moved.html", Body=b"", WebsiteRedirectLocation="/docs/")
    s3.put_bucket_website(Bucket="site", WebsiteConfiguration=SITE)

    def get(path, **kw):
        return requests.get(f"{st.endpoint}{path}", headers={"Host": "site.web.test", **kw.pop("headers", {})},
                            allow_redirects=False, timeout=5, **kw)
    return st, s3, get


def test_website_is_not_public_without_a_policy(website):
    _, _, get = website
    r = get("/")
    assert r.status_code == 403 and "text/html" in r.headers["Content-Type"]


def test_website_serving(website):
    _, s3, get = website
    s3.put_bucket_policy(Bucket="site", Policy=public_read("site"))
    r = get("/")
    assert r.status_code == 200 and r.content == b"<h1>home</h1>" and r.headers["Content-Type"] == "text/html"
    assert get("/docs/").content == b"<h1>docs</h1>"
    r = get("/docs")
    assert r.status_code == 302 and r.headers["Location"] == "/docs/"
    r = get("/nothing-here.html")
    assert r.status_code == 404 and r.content == b"<h1>missing</h1>"
    r = get("/old/page.html")
    assert r.status_code == 302 and r.headers["Location"] == "http://site.web.test/new/page.html"
    r = get("/moved.html")
    assert r.status_code == 301 and r.headers["Location"] == "/docs/"
    r = get("/index.html", headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.content == b"<h1>"
    assert requests.post(f"{website[0].endpoint}/", headers={"Host": "site.web.test"}, timeout=5).status_code == 405


def test_redirect_all(website):
    _, s3, get = website
    s3.put_bucket_website(Bucket="site", WebsiteConfiguration={
        "RedirectAllRequestsTo": {"HostName": "www.example.org", "Protocol": "https"}})
    r = get("/a/b.html")
    assert r.status_code == 301 and r.headers["Location"] == "https://www.example.org/a/b.html"


def test_object_attributes(s3, bucket):
    body = b"x" * (5 << 20)
    up = s3.create_multipart_upload(Bucket=bucket, Key="mp", ChecksumAlgorithm="CRC32")
    parts = []
    for n, b in [(1, body), (2, b"tail")]:
        r = s3.upload_part(Bucket=bucket, Key="mp", UploadId=up["UploadId"], PartNumber=n, Body=b, ChecksumAlgorithm="CRC32")
        parts.append({"PartNumber": n, "ETag": r["ETag"], "ChecksumCRC32": r["ChecksumCRC32"]})
    s3.complete_multipart_upload(Bucket=bucket, Key="mp", UploadId=up["UploadId"], MultipartUpload={"Parts": parts})
    a = s3.get_object_attributes(Bucket=bucket, Key="mp",
                                 ObjectAttributes=["ETag", "Checksum", "ObjectParts", "StorageClass", "ObjectSize"])
    assert a["ObjectSize"] == len(body) + 4 and a["StorageClass"] == "STANDARD"
    assert a["Checksum"]["ChecksumCRC32"].endswith("-2") and a["Checksum"]["ChecksumType"] == "COMPOSITE"
    op = a["ObjectParts"]
    assert op["TotalPartsCount"] == 2 and [p["Size"] for p in op["Parts"]] == [len(body), 4]
    assert [p["ChecksumCRC32"] for p in op["Parts"]] == [p["ChecksumCRC32"] for p in parts]
    s3.put_object(Bucket=bucket, Key="small", Body=b"abc")
    a = s3.get_object_attributes(Bucket=bucket, Key="small", ObjectAttributes=["ETag", "ObjectSize"])
    assert a["ETag"] == hashlib.md5(b"abc").hexdigest() and a["ObjectSize"] == 3 and "ObjectParts" not in a


def test_request_payment_and_acceleration(s3, bucket):
    assert s3.get_bucket_request_payment(Bucket=bucket)["Payer"] == "BucketOwner"
    s3.put_bucket_request_payment(Bucket=bucket, RequestPaymentConfiguration={"Payer": "BucketOwner"})
    assert "Status" not in s3.get_bucket_accelerate_configuration(Bucket=bucket)
    s3.put_bucket_accelerate_configuration(Bucket=bucket, AccelerateConfiguration={"Status": "Suspended"})
