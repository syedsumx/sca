"""Several disks per store with disk types: blob files go to disks of the bucket's type (the default type
otherwise), survive restarts and reordering of the disk list, a missing or foreign disk is refused, backups
and fsck cover every disk, and in a cluster each copy goes to a disk of the same type."""

import json
import os
import subprocess

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from conftest import ADMIN, REGION, Store, _free_port


def admin(st, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{st.endpoint}/-/admin/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=30)
    return r.status_code, (r.json() if r.content else None)


def files_in(path):
    return [f for _, _, fs in os.walk(os.path.join(path, "blobs")) for f in fs]


@pytest.fixture
def layout(tmp_path):
    d = {"hdd2": tmp_path / "hdd2", "ssd1": tmp_path / "ssd1", "ssd2": tmp_path / "ssd2"}
    spec = f"{d['hdd2']}:hdd,{d['ssd1']}:ssd,{d['ssd2']}:ssd"
    return d, spec


def test_buckets_go_to_disks_of_their_type(custom_store, layout, tmp_path):
    d, spec = layout
    st = custom_store({"CAIRN_STORE_DISKS": spec, "CAIRN_STORE_PACK_BELOW": "0", "CAIRN_STORE_BACKUP_DIR": str(tmp_path / "bk")})
    s3 = st.client()
    s3.create_bucket(Bucket="fast")
    s3.create_bucket(Bucket="bulk")
    code, disks = admin(st, "GET", "disks")
    assert code == 200 and sorted(x["type"] for x in disks["disks"]) == ["hdd", "hdd", "ssd", "ssd"]
    assert all(x["total_bytes"] > 0 for x in disks["disks"])
    assert admin(st, "PUT", "buckets/fast/disk", {"type": "nvme"})[0] == 400  # no such disk: refused, not ignored
    code, b = admin(st, "PUT", "buckets/fast/disk", {"type": "ssd"})
    assert code == 200 and b["disk_type"] == "ssd"
    bodies = {f"k{i}": os.urandom(20_000 + i) for i in range(12)}
    for k, v in bodies.items():
        s3.put_object(Bucket="fast", Key=k, Body=v)
        s3.put_object(Bucket="bulk", Key=k, Body=v)
    ssd = len(files_in(d["ssd1"])) + len(files_in(d["ssd2"]))
    hdd = len(files_in(st.data)) + len(files_in(d["hdd2"]))
    assert ssd == 12 and hdd == 12, (ssd, hdd)
    # Multipart parts and copies follow the destination bucket's type too.
    s3.copy_object(Bucket="fast", Key="copied", CopySource={"Bucket": "bulk", "Key": "k1"})
    assert len(files_in(d["ssd1"])) + len(files_in(d["ssd2"])) == 13
    for k, v in bodies.items():
        assert s3.get_object(Bucket="fast", Key=k)["Body"].read() == v
    # Restart with the list reordered: the disks keep their numbers.
    st.stop()
    st.extra_env["CAIRN_STORE_DISKS"] = ",".join(reversed(spec.split(",")))
    st.start()
    for k, v in bodies.items():
        assert st.client().get_object(Bucket="fast", Key=k)["Body"].read() == v
    # A backup covers every disk; the backup alone restores everything.
    code, rep = admin(st, "POST", "backups", {"name": "all"})
    assert code == 201 and rep["blobs"] >= 25
    st.stop()
    restored = tmp_path / "restored"
    import shutil

    shutil.copytree(tmp_path / "bk" / "all", restored)
    rst = Store(st.binary, str(restored), _free_port(), env={"CAIRN_STORE_META": ""}).start()
    try:
        for k, v in bodies.items():
            assert rst.client().get_object(Bucket="fast", Key=k)["Body"].read() == v
    finally:
        rst.stop()
    # fsck checks every disk.
    out = subprocess.run([st.binary, "fsck", "-data", st.data], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "CAIRN_STORE_DISKS": spec, **st.meta_env()})
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["objects_with_missing_blobs"] == 0


def test_missing_or_foreign_disks_are_refused(custom_store, layout, tmp_path):
    d, spec = layout
    st = custom_store({"CAIRN_STORE_DISKS": spec, "CAIRN_STORE_PACK_BELOW": "0"})
    st.client().create_bucket(Bucket="x-disks")
    st.stop()
    # A disk left out of the list: its blobs would be lost, so the store does not start.
    st.extra_env["CAIRN_STORE_DISKS"] = f"{d['hdd2']}:hdd,{d['ssd1']}:ssd"
    with pytest.raises(RuntimeError):
        st.start()
    # Another store's disk: refused.
    other = Store(st.binary, str(tmp_path / "other"), _free_port(), env={"CAIRN_STORE_DISKS": f"{d['ssd1']}:ssd", **st.meta_env()})
    with pytest.raises(RuntimeError):
        other.start()
    # Bad specs: refused.
    for bad in ["relative:ssd", f"{d['ssd1']}:SSD", f"{d['ssd1']}"]:
        st.extra_env["CAIRN_STORE_DISKS"] = bad
        with pytest.raises(RuntimeError):
            st.start()
    st.extra_env["CAIRN_STORE_DISKS"] = spec
    st.start()
    assert st.client().list_buckets()["Buckets"][0]["Name"] == "x-disks"


def test_a_bucket_type_with_no_disk_left_fails_closed(custom_store, layout):
    d, spec = layout
    st = custom_store({"CAIRN_STORE_DISKS": spec})
    s3 = st.client()
    s3.create_bucket(Bucket="was-fast")
    assert admin(st, "PUT", "buckets/was-fast/disk", {"type": "ssd"})[0] == 200
    st.stop()
    # The ssd disks are emptied and dropped by the operator (and forgotten), the setting stays.
    import shutil

    for p in ("ssd1", "ssd2"):
        shutil.rmtree(d[p])
    known = os.path.join(st.data, "disks.json")
    doc = json.load(open(known))
    doc["disks"] = [x for x in doc["disks"] if x["kind"] != "ssd"]
    json.dump(doc, open(known, "w"))
    st.extra_env["CAIRN_STORE_DISKS"] = f"{d['hdd2']}:hdd"
    st.start()
    with pytest.raises(ClientError) as ei:
        st.client().put_object(Bucket="was-fast", Key="k", Body=b"x" * 100_000)
    assert ei.value.response["ResponseMetadata"]["HTTPStatusCode"] == 503
    assert admin(st, "DELETE", "buckets/was-fast/disk")[0] == 200
    st.client().put_object(Bucket="was-fast", Key="k", Body=b"x" * 100_000)


def test_a_store_data_directory_is_not_taken_as_a_disk(custom_store, tmp_path):
    # Another store's data directory listed as a disk: its blob files are not this store's, and the startup sweep
    # of unreferenced blobs would delete them. Refused, and the other store keeps its data.
    other = custom_store({"CAIRN_STORE_PACK_BELOW": "0"})
    other.client().create_bucket(Bucket="theirs")
    body = os.urandom(50_000)
    other.client().put_object(Bucket="theirs", Key="k", Body=body)
    other.stop()
    before = files_in(other.data)
    assert before
    st = Store(other.binary, str(tmp_path / "mine"), _free_port(), env={"CAIRN_STORE_DISKS": f"{other.data}:hdd"})
    try:
        with pytest.raises(RuntimeError):
            st.start()
    finally:
        if st.proc and st.proc.poll() is None:
            st.stop(kill=True)
    assert files_in(other.data) == before
    other.start()
    assert other.client().get_object(Bucket="theirs", Key="k")["Body"].read() == body
    other.stop()
    # The other way round: a directory that is another store's disk is not taken as a data directory.
    disk = tmp_path / "ssd"
    first = custom_store({"CAIRN_STORE_DISKS": f"{disk}:ssd", "CAIRN_STORE_PACK_BELOW": "0"})
    first.client().create_bucket(Bucket="fast")
    assert admin(first, "PUT", "buckets/fast/disk", {"type": "ssd"})[0] == 200
    first.client().put_object(Bucket="fast", Key="k", Body=body)
    first.stop()
    on_disk = files_in(disk)
    assert on_disk
    second = Store(first.binary, str(disk), _free_port())
    try:
        with pytest.raises(RuntimeError):
            second.start()
    finally:
        if second.proc and second.proc.poll() is None:
            second.stop(kill=True)
    assert files_in(disk) == on_disk
