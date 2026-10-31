"""Access keys managed while the store runs (/-/admin/v1/keys and cairn-store admin key-*): created keys work
at once with their scope, rotation and deletion take effect immediately, changes are audited without secrets,
and stored secrets survive restarts (wrapped by the keyring when there is one)."""

import base64
import json
import os
import subprocess

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from conftest import ADMIN, READER, REGION

MASTER = base64.b64encode(b"k" * 32).decode()


def admin(store, method, path, body=None, key=ADMIN):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{store.endpoint}/-/admin/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*key), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=10)
    return r.status_code, (r.json() if r.content else None)


def code(ei):
    return ei.value.response["Error"]["Code"]


@pytest.fixture
def ks(custom_store):
    st = custom_store({"CAIRN_STORE_MASTER_KEY": MASTER})
    st.client().create_bucket(Bucket="lake-keys")
    st.client().create_bucket(Bucket="other-keys")
    return st


def test_create_use_scope_and_audit(ks):
    status, k = admin(ks, "POST", "keys", {"buckets": ["lake-*"], "description": "ingest job"})
    assert status == 201 and k["access_key"].startswith("CK") and len(k["secret_key"]) >= 40
    assert k["source"] == "api" and k["secret_protected"] is True
    s3 = ks.client((k["access_key"], k["secret_key"]))
    s3.put_object(Bucket="lake-keys", Key="a.txt", Body=b"a")  # works at once
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket="other-keys", Key="a.txt", Body=b"a")
    assert code(ei) == "AccessDenied"
    # Listing never shows secrets; configuration keys are listed but fixed.
    status, listed = admin(ks, "GET", "keys")
    by = {x["access_key"]: x for x in listed["keys"]}
    assert "secret_key" not in json.dumps(listed) and k["secret_key"] not in json.dumps(listed)
    assert by[ADMIN[0]]["source"] == "configuration" and by[k["access_key"]]["description"] == "ingest job"
    assert admin(ks, "DELETE", f"keys/{ADMIN[0]}")[0] == 409
    assert admin(ks, "PATCH", f"keys/{READER[0]}", {"read_only": False})[0] == 409
    # Scope changes apply to the next request.
    assert admin(ks, "PATCH", f"keys/{k['access_key']}", {"read_only": True})[0] == 200
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket="lake-keys", Key="b.txt", Body=b"b")
    assert code(ei) == "AccessDenied"
    assert s3.get_object(Bucket="lake-keys", Key="a.txt")["Body"].read() == b"a"
    # The audit trail records each change, never a secret.
    entries = [e for e in admin(ks, "GET", "audit?after=0")[1]["entries"] if e["action"].startswith("key.")]
    assert [e["action"] for e in entries] == ["key.create", "key.update"]
    assert all(e["subject"] == k["access_key"] and e["actor"] == ADMIN[0] for e in entries)
    assert k["secret_key"] not in json.dumps(entries)


def test_rotate_and_delete(ks):
    _, k = admin(ks, "POST", "keys", {"access_key": "etl-runner", "admin": False})
    old = ks.client((k["access_key"], k["secret_key"]))
    old.list_buckets()
    status, r = admin(ks, "POST", "keys/etl-runner/rotate")
    assert status == 200 and r["secret_key"] != k["secret_key"] and r["rotated"]
    with pytest.raises(ClientError) as ei:
        old.list_buckets()
    assert code(ei) == "SignatureDoesNotMatch"
    new = ks.client(("etl-runner", r["secret_key"]))
    new.list_buckets()
    assert admin(ks, "DELETE", "keys/etl-runner")[0] == 204
    with pytest.raises(ClientError) as ei:
        new.list_buckets()
    assert code(ei) == "InvalidAccessKeyId"
    assert admin(ks, "DELETE", "keys/etl-runner")[0] == 404


def test_validation_and_admin_only(ks):
    assert admin(ks, "POST", "keys", {"access_key": "bad name!"})[0] == 400
    assert admin(ks, "POST", "keys", {"access_key": ADMIN[0]})[0] == 409
    assert admin(ks, "POST", "keys", {"policy": {"Statement": [{"Effect": "Maybe"}]}})[0] == 400
    assert admin(ks, "POST", "keys", {"ssh_keys": ["not a key"]})[0] == 400
    assert admin(ks, "POST", "keys", {"secret_key": "chosen"})[0] == 400  # secrets are always generated
    assert admin(ks, "GET", "keys", key=READER)[0] == 403  # only admin keys manage keys
    _, a = admin(ks, "POST", "keys", {"admin": True})
    assert admin(ks, "DELETE", f"keys/{a['access_key']}", key=(a["access_key"], a["secret_key"]))[0] == 409  # not itself


def test_keys_survive_restarts_and_need_their_keyring(ks):
    _, k = admin(ks, "POST", "keys", {"access_key": "durable", "buckets": ["lake-*"]})
    ks.stop()
    ks.start()
    ks.client(("durable", k["secret_key"])).put_object(Bucket="lake-keys", Key="d", Body=b"d")
    # Wrapped secrets are not in the index in the clear.
    ks.stop()
    index = open(os.path.join(ks.data, "index.redb"), "rb").read()
    assert k["secret_key"].encode() not in index
    # Without the keyring key the store refuses to start (rather than dropping the key).
    ks.extra_env = {}
    with pytest.raises(RuntimeError):
        ks.start()
        if ks.proc.wait(timeout=5) != 0:
            raise RuntimeError("refused to start")
    ks.extra_env = {"CAIRN_STORE_MASTER_KEY": MASTER}


def test_cli(ks):
    env = {**os.environ, "CAIRN_STORE_ENDPOINT": ks.endpoint, "CAIRN_STORE_ACCESS_KEY": ADMIN[0], "CAIRN_STORE_SECRET_KEY": ADMIN[1]}

    def cli(*args):
        r = subprocess.run([ks.binary, "admin", *args], env=env, capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout

    rc, out = cli("key-create", "--name", "from-cli", "--read-only", "--buckets", "lake-*", "--description", "made by CLI")
    created = json.loads(out)
    assert rc == 0 and created["read_only"] and created["buckets"] == ["lake-*"]
    ks.client(("from-cli", created["secret_key"])).list_objects_v2(Bucket="lake-keys")
    rc, out = cli("keys")
    assert rc == 0 and "from-cli" in out and created["secret_key"] not in out
    assert cli("key-update", "from-cli", "--read-write")[0] == 0
    assert json.loads(cli("key", "from-cli")[1])["read_only"] is False
    assert cli("key-delete", "from-cli")[0] == 0
    assert cli("key", "from-cli")[0] == 1
