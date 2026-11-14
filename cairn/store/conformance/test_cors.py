"""CORS: rules per bucket, browser preflight (unsigned OPTIONS) and CORS headers on signed requests."""

import pytest
import requests
from botocore.exceptions import ClientError

UI = "https://app.cairn.example"
RULES = {"CORSRules": [
    {"ID": "ui", "AllowedOrigins": ["https://*.cairn.example"], "AllowedMethods": ["GET", "PUT", "HEAD"],
     "AllowedHeaders": ["content-*", "x-amz-*", "authorization"], "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 600},
    {"ID": "public-read", "AllowedOrigins": ["*"], "AllowedMethods": ["GET"]},
]}


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def preflight(store, bucket, origin, method, headers=""):
    h = {"Origin": origin, "Access-Control-Request-Method": method}
    if headers:
        h["Access-Control-Request-Headers"] = headers
    return requests.options(f"{store.endpoint}/{bucket}/some/key.parquet", headers=h, timeout=5)


def test_cors_configuration_round_trip(s3, bucket):
    with pytest.raises(ClientError) as ei:
        s3.get_bucket_cors(Bucket=bucket)
    assert code(ei) == "NoSuchCORSConfiguration"
    s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=RULES)
    got = s3.get_bucket_cors(Bucket=bucket)["CORSRules"]
    assert [r["ID"] for r in got] == ["ui", "public-read"]
    assert got[0]["MaxAgeSeconds"] == 600 and got[0]["ExposeHeaders"] == ["ETag"]
    s3.delete_bucket_cors(Bucket=bucket)
    with pytest.raises(ClientError):
        s3.get_bucket_cors(Bucket=bucket)


def test_invalid_cors_rules(s3, bucket):
    for rules, err in [
        ({"CORSRules": [{"AllowedOrigins": ["*"], "AllowedMethods": ["PATCH"]}]}, "InvalidRequest"),
        ({"CORSRules": [{"AllowedOrigins": ["https://*.*.x"], "AllowedMethods": ["GET"]}]}, "InvalidRequest"),
    ]:
        with pytest.raises(ClientError) as ei:
            s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=rules)
        assert code(ei) == err


def test_preflight(store, s3, bucket):
    r = preflight(store, bucket, UI, "PUT")
    assert r.status_code == 403 and "not enabled" in r.text  # no rules yet
    s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=RULES)
    r = preflight(store, bucket, UI, "PUT", "Content-Type, X-Amz-Date, Authorization")
    assert r.status_code == 200, r.text
    assert r.headers["Access-Control-Allow-Origin"] == UI
    assert "PUT" in r.headers["Access-Control-Allow-Methods"]
    assert r.headers["Access-Control-Allow-Headers"] == "content-type, x-amz-date, authorization"
    assert r.headers["Access-Control-Max-Age"] == "600" and r.headers["Access-Control-Expose-Headers"] == "ETag"
    assert preflight(store, bucket, UI, "DELETE").status_code == 403  # method not allowed
    assert preflight(store, bucket, UI, "PUT", "x-forbidden").status_code == 403  # header not allowed
    assert preflight(store, bucket, "https://evil.example", "PUT").status_code == 403
    r = preflight(store, bucket, "https://evil.example", "GET")
    assert r.status_code == 200 and r.headers["Access-Control-Allow-Origin"] == "*"
    assert requests.options(f"{store.endpoint}/{bucket}/k", timeout=5).status_code == 400  # not a preflight


def test_cors_headers_on_signed_requests(store, s3, bucket):
    s3.put_object(Bucket=bucket, Key="k", Body=b"hello")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": "k"})
    assert "Access-Control-Allow-Origin" not in requests.get(url, headers={"Origin": UI}, timeout=5).headers
    s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=RULES)
    r = requests.get(url, headers={"Origin": UI}, timeout=5)
    assert r.status_code == 200 and r.content == b"hello"
    assert r.headers["Access-Control-Allow-Origin"] == UI and r.headers["Access-Control-Expose-Headers"] == "ETag"
    # Errors carry CORS headers too, so the browser can read them; anonymous requests stay refused.
    r = requests.get(f"{store.endpoint}/{bucket}/k", headers={"Origin": UI}, timeout=5)
    assert r.status_code == 403 and r.headers["Access-Control-Allow-Origin"] == UI
    other = requests.get(url, headers={"Origin": "https://evil.example.org"}, timeout=5)
    assert other.headers["Access-Control-Allow-Origin"] == "*"  # the public-read rule
