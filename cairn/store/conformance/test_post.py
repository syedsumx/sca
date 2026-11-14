"""Browser POST uploads: presigned POST forms (SigV4 policy), conditions, redirects, size limits."""

import base64
import hashlib
import json
import os
import time

import pytest
import requests

from conftest import READER


def post(form, file_bytes, filename="upload.bin", extra=None):
    fields = dict(form["fields"])
    fields.update(extra or {})
    return requests.post(form["url"], data=fields, files={"file": (filename, file_bytes)}, allow_redirects=False, timeout=30)


def test_presigned_post_upload(s3, bucket):
    form = s3.generate_presigned_post(
        bucket, "user/42/${filename}",
        Fields={"Content-Type": "image/png", "x-amz-meta-source": "web"},
        Conditions=[["starts-with", "$key", "user/42/"], {"Content-Type": "image/png"}, {"x-amz-meta-source": "web"},
                    ["content-length-range", 1, 10 << 20]],
        ExpiresIn=300)
    data = os.urandom(6 << 20) + b"--tail-with-dashes--\r\n"  # crosses many read chunks; looks a bit like a boundary
    r = post(form, data, filename="photo.png")
    assert r.status_code == 204, r.text
    assert r.headers["ETag"] == f'"{hashlib.md5(data).hexdigest()}"'
    got = s3.get_object(Bucket=bucket, Key="user/42/photo.png")
    assert got["Body"].read() == data
    assert got["ContentType"] == "image/png" and got["Metadata"] == {"source": "web"}


def test_success_status_and_redirect(s3, bucket):
    form = s3.generate_presigned_post(bucket, "a.txt", Fields={"success_action_status": "201"},
                                      Conditions=[{"success_action_status": "201"}])
    r = post(form, b"hello")
    assert r.status_code == 201 and "<PostResponse" in r.text and "<Key>a.txt</Key>" in r.text
    form = s3.generate_presigned_post(bucket, "b.txt", Fields={"success_action_redirect": "https://app.example/done"},
                                      Conditions=[{"success_action_redirect": "https://app.example/done"}])
    r = post(form, b"hello")
    assert r.status_code == 303
    assert r.headers["Location"].startswith("https://app.example/done?bucket=") and "key=b.txt" in r.headers["Location"]


@pytest.mark.parametrize("case", ["too-large", "too-small", "wrong-prefix", "extra-field", "tampered", "missing-key"])
def test_policy_is_enforced(s3, bucket, case):
    form = s3.generate_presigned_post(bucket, "in/${filename}", Conditions=[["starts-with", "$key", "in/"],
                                                                          ["content-length-range", 10, 100]])
    extra, body = {}, b"x" * 50
    if case == "too-large":
        body = b"x" * 101
    elif case == "too-small":
        body = b"x" * 5
    elif case == "wrong-prefix":
        extra = {"key": "out/x"}
    elif case == "extra-field":
        extra = {"x-amz-meta-sneaky": "1"}
    elif case == "tampered":
        p = json.loads(base64.b64decode(form["fields"]["policy"]))
        p["conditions"][-1] = ["content-length-range", 0, 10 << 30]
        extra = {"policy": base64.b64encode(json.dumps(p).encode()).decode()}
    elif case == "missing-key":
        form = {"url": form["url"], "fields": {k: v for k, v in form["fields"].items() if k != "key"}}
    r = post(form, body, extra=extra)
    assert r.status_code in (400, 403), (case, r.status_code, r.text)
    expected = {"too-large": "EntityTooLarge", "too-small": "EntityTooSmall", "wrong-prefix": "AccessDenied",
                "extra-field": "AccessDenied", "tampered": "SignatureDoesNotMatch", "missing-key": "InvalidArgument"}[case]
    assert f"<Code>{expected}</Code>" in r.text, r.text
    assert "Contents" not in s3.list_objects_v2(Bucket=bucket)


def test_expired_policy(s3, bucket):
    form = s3.generate_presigned_post(bucket, "late.txt", ExpiresIn=1)
    time.sleep(2.2)
    r = post(form, b"x")
    assert r.status_code == 403 and "expired" in r.text


def test_signer_must_be_allowed_to_write(store, bucket):
    reader = store.client(key=READER)
    form = reader.generate_presigned_post(bucket, "r.txt")
    r = post(form, b"x")
    assert r.status_code == 403 and "<Code>AccessDenied</Code>" in r.text


def test_anonymous_post_is_refused(store, bucket):
    r = requests.post(f"{store.endpoint}/{bucket}", data={"key": "anon.txt"}, files={"file": ("a", b"x")}, timeout=10)
    assert r.status_code == 403


def test_post_with_tags_and_encryption(custom_store, tmp_path):
    kr = tmp_path / "kr.json"
    kr.write_text(json.dumps({"keys": {"k": base64.b64encode(os.urandom(32)).decode()}, "sse_s3_key": "k"}))
    st = custom_store({"CAIRN_STORE_KEYRING_FILE": str(kr)})
    s3 = st.client()
    s3.create_bucket(Bucket="forms")
    tagging = "<Tagging><TagSet><Tag><Key>origin</Key><Value>form</Value></Tag></TagSet></Tagging>"
    form = s3.generate_presigned_post("forms", "t.txt", Fields={"tagging": tagging}, Conditions=[{"tagging": tagging}])
    r = post(form, b"tagged")
    assert r.status_code == 204, r.text
    assert r.headers["x-amz-server-side-encryption"] == "AES256"
    assert s3.get_object_tagging(Bucket="forms", Key="t.txt")["TagSet"] == [{"Key": "origin", "Value": "form"}]
    assert s3.get_object(Bucket="forms", Key="t.txt")["Body"].read() == b"tagged"


def test_sigv4_policy(store, bucket):
    s3v4 = store.client(signature_version="s3v4")
    form = s3v4.generate_presigned_post(bucket, "v4/${filename}", Conditions=[["starts-with", "$key", "v4/"]])
    assert form["fields"]["x-amz-algorithm"] == "AWS4-HMAC-SHA256"
    assert post(form, b"signed with v4", filename="a.txt").status_code == 204
    assert s3v4.get_object(Bucket=bucket, Key="v4/a.txt")["Body"].read() == b"signed with v4"
    bad = dict(form["fields"], **{"x-amz-signature": "0" * 64})
    r = post({"url": form["url"], "fields": bad}, b"x", filename="b.txt")
    assert r.status_code == 403 and "SignatureDoesNotMatch" in r.text
