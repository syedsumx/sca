"""Transparent compression (CAIRN_STORE_COMPRESSION=zstd): compressible objects take less disk, reads and
ranges (across chunk boundaries) return the original bytes, incompressible data and already-compressed types
are stored as they are, it works under encryption, multipart and copies, and objects stay readable when
compression is turned off later."""

import base64
import hashlib
import json
import os
import subprocess

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from conftest import ADMIN, REGION, Store, _free_port

MiB = 1 << 20


def csv(n):
    rows, i, size = [], 0, 0
    while size < n:
        row = f"{i},2026-10-{1 + i % 28:02d},customer-{i % 977},{(i * 37) % 10000 / 100:.2f},EUR\n"
        rows.append(row)
        size += len(row)
        i += 1
    return "".join(rows).encode()[:n]


def disk_bytes(st):
    total = 0
    for sub in ("blobs", "packs"):
        for d, _, files in os.walk(os.path.join(st.data, sub)):
            total += sum(os.path.getsize(os.path.join(d, f)) for f in files)
    return total


def stats(st):
    req = AWSRequest(method="GET", url=f"{st.endpoint}/-/admin/v1/stats", data=b"")
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    return requests.get(req.url, headers=dict(req.headers), timeout=10).json()


@pytest.mark.parametrize("encrypted", [False, True])
def test_compressible_objects_take_less_disk(custom_store, encrypted):
    env = {"CAIRN_STORE_COMPRESSION": "zstd"}
    if encrypted:
        env["CAIRN_STORE_MASTER_KEY"] = base64.b64encode(b"z" * 32).decode()
    st = custom_store(env)
    s3 = st.client()
    s3.create_bucket(Bucket="lake")
    text = csv(5 * MiB + 12345)
    s3.put_object(Bucket="lake", Key="orders.csv", Body=text, ContentType="text/csv")
    assert disk_bytes(st) < len(text) // 3, disk_bytes(st)
    assert s3.get_object(Bucket="lake", Key="orders.csv")["Body"].read() == text
    for a, b in [(0, 0), (MiB - 5, MiB + 5), (2 * MiB, 4 * MiB + 17), (len(text) - 100, len(text) - 1), (12345, 12345 + 300000)]:
        assert s3.get_object(Bucket="lake", Key="orders.csv", Range=f"bytes={a}-{b}")["Body"].read() == text[a:b + 1], (a, b)
    h = s3.head_object(Bucket="lake", Key="orders.csv")
    assert h["ContentLength"] == len(text)  # sizes and ETags are the object's, not the stored bytes'
    assert h["ETag"].strip('"') == hashlib.md5(text).hexdigest()
    if encrypted:
        assert h["ServerSideEncryption"] == "AES256"
        assert all(text[:200] not in open(os.path.join(d, f), "rb").read()
                   for sub in ("blobs", "packs") for d, _, fs in os.walk(os.path.join(st.data, sub)) for f in fs)
    # Small objects (packed) are compressed too, and copies keep working.
    small = csv(30_000)
    s3.put_object(Bucket="lake", Key="small.csv", Body=small)
    assert s3.get_object(Bucket="lake", Key="small.csv")["Body"].read() == small
    s3.copy_object(Bucket="lake", Key="copy.csv", CopySource={"Bucket": "lake", "Key": "orders.csv"})
    assert s3.get_object(Bucket="lake", Key="copy.csv", Range="bytes=3000000-3000099")["Body"].read() == text[3000000:3000100]
    c = stats(st)["compression"]
    assert c["algorithm"] == "zstd" and c["stored_bytes"] < c["bytes_in"] // 3, c


def test_incompressible_data_is_stored_as_is(custom_store):
    st = custom_store({"CAIRN_STORE_COMPRESSION": "zstd", "CAIRN_STORE_PACK_BELOW": "0"})
    s3 = st.client()
    s3.create_bucket(Bucket="media")
    noise = os.urandom(3 * MiB)
    s3.put_object(Bucket="media", Key="noise.bin", Body=noise)
    assert abs(disk_bytes(st) - len(noise)) < 1024
    assert s3.get_object(Bucket="media", Key="noise.bin", Range="bytes=1048570-1048590")["Body"].read() == noise[1048570:1048591]
    # Already-compressed content types are not even tried (compressible bytes stay as they are).
    before = disk_bytes(st)
    s3.put_object(Bucket="media", Key="photo.png", Body=csv(2 * MiB), ContentType="image/png")
    assert disk_bytes(st) - before == 2 * MiB


def test_multipart_and_turning_it_off(custom_store):
    st = custom_store({"CAIRN_STORE_COMPRESSION": "zstd"})
    s3 = st.client()
    s3.create_bucket(Bucket="parts")
    text = csv(6 * MiB + 777)
    up = s3.create_multipart_upload(Bucket="parts", Key="big.csv")["UploadId"]
    p1 = s3.upload_part(Bucket="parts", Key="big.csv", UploadId=up, PartNumber=1, Body=text[:5 * MiB])["ETag"]
    p2 = s3.upload_part(Bucket="parts", Key="big.csv", UploadId=up, PartNumber=2, Body=text[5 * MiB:])["ETag"]
    s3.complete_multipart_upload(Bucket="parts", Key="big.csv", UploadId=up,
                                 MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": p1}, {"PartNumber": 2, "ETag": p2}]})
    assert disk_bytes(st) < len(text) // 3
    assert s3.get_object(Bucket="parts", Key="big.csv")["Body"].read() == text
    edge = s3.get_object(Bucket="parts", Key="big.csv", Range=f"bytes={5 * MiB - 10}-{5 * MiB + 10}")["Body"].read()
    assert edge == text[5 * MiB - 10:5 * MiB + 11]
    assert s3.get_object(Bucket="parts", Key="big.csv", PartNumber=2)["Body"].read() == text[5 * MiB:]
    # Turned off later: what was compressed stays readable; new objects are stored as they are.
    st.stop()
    st.extra_env = {"CAIRN_STORE_COMPRESSION": "off"}
    st.start()
    s3 = st.client()
    assert s3.get_object(Bucket="parts", Key="big.csv")["Body"].read() == text
    before = disk_bytes(st)
    s3.put_object(Bucket="parts", Key="plain.csv", Body=csv(2 * MiB))
    assert disk_bytes(st) - before >= 2 * MiB
    assert stats(st)["compression"]["algorithm"] == "off"


def test_unknown_algorithm_is_refused(custom_store):
    with pytest.raises(RuntimeError):
        st = custom_store({"CAIRN_STORE_COMPRESSION": "gzip"})
        if st.proc.wait(timeout=5) != 0:
            raise RuntimeError("refused")


def _table(entries):
    """A chunk table as the store records it: LEB128 lengths, base64 without padding."""
    out = bytearray()
    for v in entries:
        while True:
            b, v = v & 0x7F, v >> 7
            out.append(b | (0x80 if v else 0))
            if not v:
                break
    return base64.b64encode(bytes(out)).decode().rstrip("=")


def _zstd_raw_frame(data):
    """A valid zstd frame holding `data` in raw (stored) blocks."""
    out = bytearray(b"\x28\xb5\x2f\xfd\x00\x50")  # magic; no content size; 1 MiB window
    blocks = [data[i:i + (128 << 10)] for i in range(0, len(data), 128 << 10)] or [b""]
    for i, b in enumerate(blocks):
        out += ((len(b) << 3) | (1 if i == len(blocks) - 1 else 0)).to_bytes(3, "little") + b
    return bytes(out)


def _read(fn):
    try:
        return fn()["Body"].read()
    except Exception:
        return None


def test_untrusted_chunk_tables_and_chunks_are_checked(custom_store, tmp_path, binary):
    # A restore from a backup sink takes the chunk table from the sink's journal and the chunks from its
    # blobs: neither is trusted. A table claiming a huge chunk must not be trusted for an allocation (it
    # aborted the whole store), and a chunk that decompresses to fewer bytes than its place in the object
    # must be an error, not a read that silently slides on to the next chunk's bytes.
    from test_backup_sinks import admin, drained

    sink = custom_store({})
    sink.client().create_bucket(Bucket="offsite")
    sinks = tmp_path / "sinks.json"
    sinks.write_text(json.dumps([{"id": "offsite", "endpoint": sink.endpoint, "bucket": "offsite", "prefix": "cairn",
                                  "access_key": ADMIN[0], "secret_key": ADMIN[1]}]))
    src = custom_store({"CAIRN_STORE_BACKUP_SINKS_FILE": str(sinks), "CAIRN_STORE_BACKUP_CONTINUOUS": "offsite",
                        "CAIRN_STORE_COMPRESSION": "zstd"})
    s3 = src.client()
    s3.create_bucket(Bucket="lake")
    assert admin(src, "POST", "backups", {"name": "base", "sink": "offsite"})[0] == 201
    text = csv(2 * MiB)
    for k in ("bomb.csv", "short.csv", "good.csv"):
        s3.put_object(Bucket="lake", Key=k, Body=text)
    drained(src)
    so = sink.client()
    keys = [o["Key"] for p in so.get_paginator("list_objects_v2").paginate(Bucket="offsite", Prefix="cairn/journal/")
            for o in p.get("Contents", [])]
    for k in keys:
        rec = json.loads(so.get_object(Bucket="offsite", Key=k)["Body"].read())
        if rec.get("op") != "put" or rec["key"] not in ("bomb.csv", "short.csv"):
            continue
        seg = rec["object"]["segments"][0]
        assert seg.get("z"), seg  # compressed
        if rec["key"] == "bomb.csv":
            seg["z"] = _table([1 << 62, 100])
        else:
            blob = _zstd_raw_frame(b"A" * 1000) + b"B" * MiB
            seg["z"] = _table([len(blob) - MiB, MiB | (1 << 63)])
            seg.pop("n", None)
            so.put_object(Bucket="offsite", Key=f"cairn/blobs/{rec['sink_blobs'][0]}", Body=blob)
        so.put_object(Bucket="offsite", Key=k, Body=json.dumps(rec).encode())
    out = subprocess.run([binary, "restore-backup", "offsite", "latest", str(tmp_path / "r")],
                         env={**os.environ, "CAIRN_STORE_BACKUP_SINKS_FILE": str(sinks)}, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    st = Store(binary, str(tmp_path / "r"), _free_port(), env={"CAIRN_STORE_META": ""}).start()
    try:
        r = st.client()
        # A short chunk: an error, not the next chunk's bytes in its place.
        got = _read(lambda: r.get_object(Bucket="lake", Key="short.csv", Range="bytes=5000-5009"))
        assert got is None, got
        assert _read(lambda: r.get_object(Bucket="lake", Key="short.csv")) is None
        assert _read(lambda: r.get_object(Bucket="lake", Key="bomb.csv")) is None
        assert st.proc.poll() is None, "the store died on a chunk table claiming a huge chunk"
        assert r.get_object(Bucket="lake", Key="good.csv")["Body"].read() == text
    finally:
        st.stop(kill=True)
