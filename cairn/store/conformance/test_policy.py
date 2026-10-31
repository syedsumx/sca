"""Access policies: bucket policies, key policies, conditions, public access and Cairn's policy decision point."""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests
from botocore.exceptions import ClientError

from conftest import ANALYST, RAWONLY, SCOPED


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def denied(call) -> None:
    with pytest.raises(ClientError) as ei:
        call()
    assert code(ei) == "AccessDenied", ei.value


def policy(bucket: str, *statements) -> str:
    return json.dumps({"Version": "2012-10-17", "Statement": list(statements)})


def grant(bucket, actions, resource_suffix="/*", who=ANALYST[0], effect="Allow", **extra):
    return {"Effect": effect, "Principal": {"AWS": [f"arn:aws:iam::000000000000:user/{who}"]}, "Action": actions,
            "Resource": f"arn:aws:s3:::{bucket}{resource_suffix}", **extra}


def test_bucket_policy_round_trip(s3, bucket):
    with pytest.raises(ClientError) as ei:
        s3.get_bucket_policy(Bucket=bucket)
    assert code(ei) == "NoSuchBucketPolicy"
    doc = policy(bucket, grant(bucket, "s3:GetObject"))
    s3.put_bucket_policy(Bucket=bucket, Policy=doc)
    assert json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"]) == json.loads(doc)
    assert s3.get_bucket_policy_status(Bucket=bucket)["PolicyStatus"]["IsPublic"] is False
    s3.delete_bucket_policy(Bucket=bucket)
    with pytest.raises(ClientError):
        s3.get_bucket_policy(Bucket=bucket)


@pytest.mark.parametrize("doc", [
    "not json",
    json.dumps({"Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::BUCKET/*"}]}),
    json.dumps({"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::other/*"}]}),
    json.dumps({"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "iam:PassRole", "Resource": "arn:aws:s3:::BUCKET/*"}]}),
    json.dumps({"Statement": [{"Effect": "Maybe", "Principal": "*", "Action": "s3:*", "Resource": "arn:aws:s3:::BUCKET/*"}]}),
    json.dumps({"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:*", "Resource": "arn:aws:s3:::BUCKET/*",
                               "Condition": {"StringSoundsLike": {"s3:prefix": "x"}}}]}),
    json.dumps({"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:*", "Resource": "arn:aws:s3:::BUCKET/*",
                               "Condition": {"StringEquals": {"aws:MadeUpKey": "x"}}}]}),
])
def test_malformed_policies_are_refused(s3, bucket, doc):
    """A policy is validated when stored, so no part of a stored policy is ever ignored."""
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_policy(Bucket=bucket, Policy=doc.replace("BUCKET", bucket))
    assert code(ei) == "MalformedPolicy"


def test_bucket_policy_grants_a_key(store, s3, bucket):
    s3.put_object(Bucket=bucket, Key="shared/a.csv", Body=b"a")
    s3.put_object(Bucket=bucket, Key="private/b.csv", Body=b"b")
    analyst = store.client(key=ANALYST)
    denied(lambda: analyst.get_object(Bucket=bucket, Key="shared/a.csv"))
    s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, grant(bucket, "s3:GetObject", "/shared/*"),
                                                      grant(bucket, "s3:ListBucket", "", Condition={"StringLike": {"s3:prefix": "shared/*"}})))
    assert analyst.get_object(Bucket=bucket, Key="shared/a.csv")["Body"].read() == b"a"
    denied(lambda: analyst.get_object(Bucket=bucket, Key="private/b.csv"))
    denied(lambda: analyst.put_object(Bucket=bucket, Key="shared/c.csv", Body=b"c"))
    assert [o["Key"] for o in analyst.list_objects_v2(Bucket=bucket, Prefix="shared/")["Contents"]] == ["shared/a.csv"]
    denied(lambda: analyst.list_objects_v2(Bucket=bucket))  # the condition needs a shared/ prefix


def test_explicit_deny_wins(store, s3, bucket):
    s3.put_object(Bucket=bucket, Key="keep", Body=b"x")
    s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, {"Effect": "Deny", "Principal": "*", "Action": "s3:DeleteObject",
                                                               "Resource": f"arn:aws:s3:::{bucket}/*"}))
    denied(lambda: s3.delete_object(Bucket=bucket, Key="keep"))  # even for the admin key
    res = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": "keep"}]})
    assert res["Errors"][0]["Code"] == "AccessDenied" and "Deleted" not in res
    s3.delete_bucket_policy(Bucket=bucket)  # admins can always remove a policy: no lock-out
    s3.delete_object(Bucket=bucket, Key="keep")


def test_conditions_on_object_tags_and_source_ip(store, s3, bucket):
    s3.put_object(Bucket=bucket, Key="pii.csv", Body=b"p", Tagging="pii=true")
    s3.put_object(Bucket=bucket, Key="ok.csv", Body=b"o")
    s3.put_bucket_policy(Bucket=bucket, Policy=policy(
        bucket,
        grant(bucket, "s3:GetObject"),
        grant(bucket, "s3:GetObject", effect="Deny", Condition={"StringEquals": {"s3:ExistingObjectTag/pii": "true"}}),
    ))
    analyst = store.client(key=ANALYST)
    assert analyst.get_object(Bucket=bucket, Key="ok.csv")["Body"].read() == b"o"
    denied(lambda: analyst.get_object(Bucket=bucket, Key="pii.csv"))
    s3.put_bucket_policy(Bucket=bucket, Policy=policy(
        bucket, grant(bucket, "s3:GetObject", Condition={"IpAddress": {"aws:SourceIp": "10.0.0.0/8"}})))
    denied(lambda: analyst.get_object(Bucket=bucket, Key="ok.csv"))  # the suite connects from 127.0.0.1
    s3.put_bucket_policy(Bucket=bucket, Policy=policy(
        bucket, grant(bucket, "s3:GetObject", Condition={"IpAddress": {"aws:SourceIp": "127.0.0.0/8"}})))
    assert analyst.get_object(Bucket=bucket, Key="ok.csv")["Body"].read() == b"o"


def test_copy_needs_read_on_the_source(store, s3, bucket):
    other = "t-" + uuid.uuid4().hex[:16]
    s3.create_bucket(Bucket=other)
    s3.put_object(Bucket=bucket, Key="src", Body=b"s")
    s3.put_bucket_policy(Bucket=other, Policy=policy(other, grant(other, "s3:PutObject")))
    analyst = store.client(key=ANALYST)
    denied(lambda: analyst.copy_object(Bucket=other, Key="dst", CopySource={"Bucket": bucket, "Key": "src"}))
    s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, grant(bucket, "s3:GetObject")))
    analyst.copy_object(Bucket=other, Key="dst", CopySource={"Bucket": bucket, "Key": "src"})
    assert s3.get_object(Bucket=other, Key="dst")["Body"].read() == b"s"


def test_key_policy(store, s3):
    b = "pol-" + uuid.uuid4().hex[:10]
    s3.create_bucket(Bucket=b)
    raw = store.client(key=RAWONLY)
    raw.put_object(Bucket=b, Key="raw/a.json", Body=b"{}")
    assert raw.get_object(Bucket=b, Key="raw/a.json")["Body"].read() == b"{}"
    assert [o["Key"] for o in raw.list_objects_v2(Bucket=b, Prefix="raw/")["Contents"]] == ["raw/a.json"]
    denied(lambda: raw.put_object(Bucket=b, Key="curated/a.json", Body=b"{}"))
    denied(lambda: raw.list_objects_v2(Bucket=b))
    denied(lambda: raw.delete_object(Bucket=b, Key="raw/a.json"))
    denied(lambda: raw.put_bucket_tagging(Bucket=b, Tagging={"TagSet": []}))


def test_bucket_administration_needs_an_admin_key(store, s3):
    lake = "lake-" + uuid.uuid4().hex[:10]
    s3.create_bucket(Bucket=lake)
    scoped = store.client(key=SCOPED)
    scoped.put_object(Bucket=lake, Key="x", Body=b"x")
    for call in (
        lambda: scoped.put_bucket_tagging(Bucket=lake, Tagging={"TagSet": [{"Key": "a", "Value": "b"}]}),
        lambda: scoped.put_bucket_policy(Bucket=lake, Policy=policy(lake, grant(lake, "s3:GetObject"))),
        lambda: scoped.put_bucket_cors(Bucket=lake, CORSConfiguration={"CORSRules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["*"]}]}),
        lambda: scoped.delete_bucket(Bucket=lake),
    ):
        denied(call)
    assert scoped.get_bucket_location(Bucket=lake)["ResponseMetadata"]["HTTPStatusCode"] == 200


PUBLIC_READ = lambda b: policy(b, {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{b}/*"})  # noqa: E731


def test_public_access_is_blocked_by_default(store, s3, bucket):
    s3.put_object(Bucket=bucket, Key="p", Body=b"p")
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_policy(Bucket=bucket, Policy=PUBLIC_READ(bucket))
    assert code(ei) == "AccessDenied"
    assert requests.get(f"{store.endpoint}/{bucket}/p", timeout=5).status_code == 403


def test_public_read_when_allowed(custom_store):
    st = custom_store({"CAIRN_STORE_BLOCK_PUBLIC_ACCESS": "false"})
    s3 = st.client()
    s3.create_bucket(Bucket="site")
    s3.put_object(Bucket="site", Key="index.html", Body=b"<h1>hi</h1>")
    url = f"{st.endpoint}/site/index.html"
    assert requests.get(url, timeout=5).status_code == 403  # no policy yet
    s3.put_bucket_policy(Bucket="site", Policy=PUBLIC_READ("site"))
    assert s3.get_bucket_policy_status(Bucket="site")["PolicyStatus"]["IsPublic"] is True
    r = requests.get(url, timeout=5)
    assert r.status_code == 200 and r.content == b"<h1>hi</h1>"
    assert requests.put(f"{st.endpoint}/site/evil", data=b"x", timeout=5).status_code == 403
    assert requests.get(f"{st.endpoint}/site", timeout=5).status_code == 403  # listing was not granted
    s3.put_public_access_block(Bucket="site", PublicAccessBlockConfiguration={"RestrictPublicBuckets": True})
    assert requests.get(url, timeout=5).status_code == 403
    assert s3.get_public_access_block(Bucket="site")["PublicAccessBlockConfiguration"]["RestrictPublicBuckets"] is True
    s3.put_public_access_block(Bucket="site", PublicAccessBlockConfiguration={"BlockPublicPolicy": True})
    with pytest.raises(ClientError):
        s3.put_bucket_policy(Bucket="site", Policy=PUBLIC_READ("site"))
    s3.delete_public_access_block(Bucket="site")
    assert requests.get(url, timeout=5).status_code == 200


class FakePdp(BaseHTTPRequestHandler):
    """Denies s3:GetObject on keys containing 'secret' for urn:li:corpuser:analyst."""
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakePdp.seen.append((self.headers.get("Authorization"), body))
        deny = body["actor"] == "urn:li:corpuser:analyst" and body["privilege"] == "s3:GetObject" and "secret" in body["resource"]["urn"]
        out = json.dumps({"decision": "DENY" if deny else "NOT_APPLICABLE", "reason": "test"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_cairn_pdp_can_deny_and_fails_closed(custom_store):
    pdp = HTTPServer(("127.0.0.1", 0), FakePdp)
    threading.Thread(target=pdp.serve_forever, daemon=True).start()
    st = custom_store({"CAIRN_STORE_PDP_URL": f"http://127.0.0.1:{pdp.server_port}", "CAIRN_STORE_PDP_TOKEN": "svc-token-for-tests"})
    s3 = st.client()
    s3.create_bucket(Bucket="gov")
    for k in ("open.csv", "secret.csv", "later.csv"):
        s3.put_object(Bucket="gov", Key=k, Body=b"x")
    s3.put_bucket_policy(Bucket="gov", Policy=policy("gov", grant("gov", "s3:GetObject")))
    analyst = st.client(key=ANALYST)
    assert analyst.get_object(Bucket="gov", Key="open.csv")["Body"].read() == b"x"
    denied(lambda: analyst.get_object(Bucket="gov", Key="secret.csv"))
    auth, body = FakePdp.seen[-1]
    assert auth == "Bearer svc-token-for-tests" and body["resource"] == {"type": "storeObject", "urn": "arn:aws:s3:::gov/secret.csv"}
    pdp.shutdown()
    pdp.server_close()
    denied(lambda: analyst.get_object(Bucket="gov", Key="later.csv"))  # PDP unreachable: refused
