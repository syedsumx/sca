"""Online backups: consistent while the store keeps serving writes, incremental, restorable."""

import json
import os
import shutil
import subprocess
import threading

import pytest

from conftest import ADMIN, Store, _free_port


def test_online_backup_and_restore(custom_store, tmp_path):
    root = tmp_path / "backups"
    st = custom_store({"CAIRN_STORE_BACKUP_DIR": str(root)})
    s3 = st.client()
    s3.create_bucket(Bucket="data")
    s3.put_bucket_versioning(Bucket="data", VersioningConfiguration={"Status": "Enabled"})
    for i in range(50):
        s3.put_object(Bucket="data", Key=f"k{i:03}", Body=os.urandom(2000) + str(i).encode())
    s3.put_bucket_tagging(Bucket="data", Tagging={"TagSet": [{"Key": "team", "Value": "lake"}]})
    env = {**os.environ, "CAIRN_STORE_ENDPOINT": st.endpoint, "CAIRN_STORE_ACCESS_KEY": ADMIN[0], "CAIRN_STORE_SECRET_KEY": ADMIN[1]}

    def cli(*args):
        r = subprocess.run([st.binary, "admin", *args], env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, json.loads(r.stdout) if r.stdout.strip() else {}

    stop = threading.Event()
    writes = []

    def writer():  # keeps writing (and overwriting) while the backups run
        i = 0
        while not stop.is_set():
            s3.put_object(Bucket="data", Key=f"live{i % 5}", Body=b"x" * 500)
            if i % 7 == 0:
                s3.delete_object(Bucket="data", Key=f"k{i % 50:03}")
            writes.append(i)
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    code, first = cli("backup", "first")
    code2, second = cli("backup", "second")
    stop.set()
    t.join()
    assert code == 0 and code2 == 0 and writes, (first, second)
    assert first["objects"] >= 1 and second["blobs_linked"] > 0  # the second backup reuses the first's blobs
    assert cli("backups")[1]["backups"] == ["first", "second"]
    assert cli("backup", "../escape")[0] == 1
    assert cli("backup", "first")[0] == 1  # exists

    # Restore: a store started on a copy of the backup has the data as of its snapshot.
    restored_dir = tmp_path / "restored"
    shutil.copytree(root / "second", restored_dir)
    assert subprocess.run([st.binary, "fsck", "-data", str(restored_dir)], capture_output=True, timeout=60).returncode == 0
    rst = Store(st.binary, str(restored_dir), _free_port()).start()
    try:
        r3 = rst.client()
        keys = {o["Key"] for o in r3.list_objects_v2(Bucket="data")["Contents"]}
        deletable = {f"k{i % 50:03}" for i in writes if i % 7 == 0}
        assert {f"k{i:03}" for i in range(50)} - deletable <= keys  # every key never deleted is in the backup
        assert r3.get_bucket_versioning(Bucket="data")["Status"] == "Enabled"
        assert r3.get_bucket_tagging(Bucket="data")["TagSet"] == [{"Key": "team", "Value": "lake"}]
        for k in list(keys)[:10]:
            assert r3.get_object(Bucket="data", Key=k)["Body"].read()
        versions = r3.list_object_versions(Bucket="data")
        for v in versions.get("Versions", [])[:20]:  # old versions came along, with their bytes
            assert r3.get_object(Bucket="data", Key=v["Key"], VersionId=v["VersionId"])["Body"].read()
    finally:
        rst.stop()


def test_backups_are_off_without_a_directory(store):
    import requests
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    from conftest import REGION
    if not getattr(store, "binary", None):
        pytest.skip("external store")
    data = b'{"name": "x"}'
    req = AWSRequest(method="POST", url=f"{store.endpoint}/-/admin/v1/backups", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.post(req.url, data=data, headers=dict(req.headers), timeout=10)
    assert r.status_code == 501 and r.json()["error"] == "NotImplemented"
