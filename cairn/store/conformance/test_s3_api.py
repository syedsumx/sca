"""S3 behaviour that lakehouse clients (Trino, Spark's S3FileIO, pyiceberg, pyarrow) and common tools
rely on, checked through boto3 against a running cairn-store."""

import base64
import datetime as dt
import hashlib
import io
import os
import urllib.request
import uuid
import zlib

import boto3
import pytest
import requests
from botocore.exceptions import ClientError, ParamValidationError

from conftest import READER, SCOPED


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def status(exc_info) -> int:
    return exc_info.value.response["ResponseMetadata"]["HTTPStatusCode"]


# ------------------------------------------------------------------------------------------- buckets

def test_bucket_lifecycle(s3):
    name = "b-" + uuid.uuid4().hex[:12]
    s3.create_bucket(Bucket=name)
    with pytest.raises(ClientError) as e:
        s3.create_bucket(Bucket=name)
    assert code(e) == "BucketAlreadyOwnedByYou"
    s3.head_bucket(Bucket=name)
    assert name in [b["Name"] for b in s3.list_buckets()["Buckets"]]
    s3.put_object(Bucket=name, Key="k", Body=b"x")
    with pytest.raises(ClientError) as e:
        s3.delete_bucket(Bucket=name)
    assert code(e) == "BucketNotEmpty"
    s3.delete_object(Bucket=name, Key="k")
    s3.delete_bucket(Bucket=name)
    with pytest.raises(ClientError) as e:
        s3.head_bucket(Bucket=name)
    assert status(e) == 404
    with pytest.raises(ClientError) as e:
        s3.list_objects_v2(Bucket=name)
    assert code(e) == "NoSuchBucket"


@pytest.mark.parametrize("name", ["ab", "UPPER", "-start", "end-", "a..b", "192.168.1.1", "a" * 64, "under_score"])
def test_invalid_bucket_names(s3, name):
    with pytest.raises((ClientError, ParamValidationError)):   # some are refused by the client already
        s3.create_bucket(Bucket=name)


def test_bucket_location_and_versioning(s3, bucket):
    assert s3.get_bucket_location(Bucket=bucket)["LocationConstraint"] in (None, "")
    assert "Status" not in s3.get_bucket_versioning(Bucket=bucket)


# ------------------------------------------------------------------------------------------- objects

def test_put_get_head_with_metadata(s3, bucket):
    body = b"hello iceberg"
    r = s3.put_object(Bucket=bucket, Key="dir/obj.txt", Body=body, ContentType="text/plain",
                      Metadata={"owner": "ana", "MixedCase": "v"}, CacheControl="max-age=60", ContentDisposition="inline")
    assert r["ETag"] == '"%s"' % hashlib.md5(body).hexdigest()
    o = s3.get_object(Bucket=bucket, Key="dir/obj.txt")
    assert o["Body"].read() == body and o["ContentType"] == "text/plain"
    assert o["Metadata"] == {"owner": "ana", "mixedcase": "v"}
    assert o["CacheControl"] == "max-age=60" and o["ContentDisposition"] == "inline"
    h = s3.head_object(Bucket=bucket, Key="dir/obj.txt")
    assert h["ContentLength"] == len(body) and h["ETag"] == r["ETag"] and h["AcceptRanges"] == "bytes"


def test_empty_object_and_overwrite(s3, bucket):
    s3.put_object(Bucket=bucket, Key="empty", Body=b"")
    assert s3.get_object(Bucket=bucket, Key="empty")["Body"].read() == b""
    s3.put_object(Bucket=bucket, Key="empty", Body=b"now full")
    assert s3.get_object(Bucket=bucket, Key="empty")["Body"].read() == b"now full"


@pytest.mark.parametrize("key", ["with space", "plus+sign", "percent%20literal", "question?mark", "hash#tag",
                                 "unicode-ü-日本", "a/b/c/../d", "trailing/", "tilde~", "equals=and&amp", "x" * 1024])
def test_awkward_keys_round_trip(s3, bucket, key):
    s3.put_object(Bucket=bucket, Key=key, Body=key.encode())
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == key.encode()
    assert key in [o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"]]


def test_key_too_long(s3, bucket):
    with pytest.raises(ClientError) as e:
        s3.put_object(Bucket=bucket, Key="x" * 1025, Body=b"x")
    assert code(e) == "KeyTooLongError"


def test_missing_key(s3, bucket):
    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket=bucket, Key="nope")
    assert code(e) == "NoSuchKey"
    with pytest.raises(ClientError) as e:
        s3.head_object(Bucket=bucket, Key="nope")
    assert status(e) == 404
    s3.delete_object(Bucket=bucket, Key="nope")  # deleting a missing key succeeds


def test_ranges(s3, bucket):
    data = bytes(range(256)) * 40
    s3.put_object(Bucket=bucket, Key="r", Body=data)
    cases = {"bytes=0-9": data[:10], "bytes=100-": data[100:], "bytes=-8": data[-8:], "bytes=10000-20000": data[10000:]}
    for rng, want in cases.items():
        o = s3.get_object(Bucket=bucket, Key="r", Range=rng)
        assert o["Body"].read() == want, rng
        assert o["ResponseMetadata"]["HTTPStatusCode"] == 206
        assert o["ContentRange"].endswith("/%d" % len(data))
    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket=bucket, Key="r", Range="bytes=999999-")
    assert code(e) == "InvalidRange" and status(e) == 416


def test_parquet_style_footer_reads(s3, bucket):
    """Readers fetch the last 8 bytes, then the footer, then column chunks."""
    data = os.urandom(1 << 20)
    s3.put_object(Bucket=bucket, Key="f.parquet", Body=data)
    size = s3.head_object(Bucket=bucket, Key="f.parquet")["ContentLength"]
    tail = s3.get_object(Bucket=bucket, Key="f.parquet", Range=f"bytes={size - 8}-{size - 1}")["Body"].read()
    assert tail == data[-8:]
    mid = s3.get_object(Bucket=bucket, Key="f.parquet", Range="bytes=4096-12287")["Body"].read()
    assert mid == data[4096:12288]


def test_conditional_reads(s3, bucket):
    etag = s3.put_object(Bucket=bucket, Key="c", Body=b"v1")["ETag"]
    assert s3.get_object(Bucket=bucket, Key="c", IfMatch=etag)["Body"].read() == b"v1"
    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket=bucket, Key="c", IfMatch='"0000"')
    assert status(e) == 412
    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket=bucket, Key="c", IfNoneMatch=etag)
    assert status(e) == 304
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket=bucket, Key="c", IfModifiedSince=future)
    assert status(e) == 304
    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket=bucket, Key="c", IfUnmodifiedSince=past)
    assert status(e) == 412
    assert s3.get_object(Bucket=bucket, Key="c", IfModifiedSince=past)["Body"].read() == b"v1"


def test_conditional_writes_are_compare_and_set(s3, bucket):
    """Catalogs that commit through the object store rely on If-None-Match: * and If-Match."""
    s3.put_object(Bucket=bucket, Key="metadata/v1.json", Body=b"{}", IfNoneMatch="*")
    with pytest.raises(ClientError) as e:
        s3.put_object(Bucket=bucket, Key="metadata/v1.json", Body=b"{other}", IfNoneMatch="*")
    assert code(e) == "PreconditionFailed"
    etag = s3.head_object(Bucket=bucket, Key="metadata/v1.json")["ETag"]
    with pytest.raises(ClientError) as e:
        s3.put_object(Bucket=bucket, Key="metadata/v1.json", Body=b"x", IfMatch='"deadbeef"')
    assert code(e) == "PreconditionFailed"
    s3.put_object(Bucket=bucket, Key="metadata/v1.json", Body=b"{v2}", IfMatch=etag)
    assert s3.get_object(Bucket=bucket, Key="metadata/v1.json")["Body"].read() == b"{v2}"


def test_content_md5_is_checked(s3, bucket):
    good = base64.b64encode(hashlib.md5(b"data").digest()).decode()
    s3.put_object(Bucket=bucket, Key="m", Body=b"data", ContentMD5=good)
    bad = base64.b64encode(hashlib.md5(b"other").digest()).decode()
    with pytest.raises(ClientError) as e:
        s3.put_object(Bucket=bucket, Key="m2", Body=b"data", ContentMD5=bad)
    assert code(e) == "BadDigest"
    with pytest.raises(ClientError):
        s3.head_object(Bucket=bucket, Key="m2")


@pytest.mark.parametrize("algo", ["CRC32", "CRC32C", "SHA1", "SHA256", "CRC64NVME"])
def test_checksums(s3, bucket, algo):
    r = s3.put_object(Bucket=bucket, Key="ck", Body=b"checksummed bytes", ChecksumAlgorithm=algo)
    value = r["Checksum" + algo]
    h = s3.head_object(Bucket=bucket, Key="ck", ChecksumMode="ENABLED")
    assert h["Checksum" + algo] == value


def test_wrong_checksum_is_refused(s3, bucket):
    wrong = base64.b64encode(zlib.crc32(b"other").to_bytes(4, "big")).decode()
    with pytest.raises(ClientError) as e:
        s3.put_object(Bucket=bucket, Key="bad", Body=b"data", ChecksumCRC32=wrong)
    assert code(e) in ("BadDigest", "InvalidRequest", "XAmzContentChecksumMismatch")
    with pytest.raises(ClientError):
        s3.head_object(Bucket=bucket, Key="bad")


# ------------------------------------------------------------------------------------------- listing

def _put_many(s3, bucket, keys):
    for k in keys:
        s3.put_object(Bucket=bucket, Key=k, Body=k.encode())


def test_list_v2_prefix_delimiter_and_pages(s3, bucket):
    keys = ["data/a.parquet", "data/b.parquet", "data/part=1/c.parquet", "data/part=2/d.parquet", "metadata/v1.json", "z"]
    _put_many(s3, bucket, keys)
    r = s3.list_objects_v2(Bucket=bucket, Prefix="data/", Delimiter="/")
    assert [c["Key"] for c in r["Contents"]] == ["data/a.parquet", "data/b.parquet"]
    assert [p["Prefix"] for p in r["CommonPrefixes"]] == ["data/part=1/", "data/part=2/"]
    assert r["KeyCount"] == 4 and not r["IsTruncated"]
    seen = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, PaginationConfig={"PageSize": 2}):
        seen += [c["Key"] for c in page.get("Contents", [])]
    assert seen == sorted(keys)
    r = s3.list_objects_v2(Bucket=bucket, StartAfter="data/part=2/d.parquet")
    assert [c["Key"] for c in r["Contents"]] == ["metadata/v1.json", "z"]
    assert s3.list_objects_v2(Bucket=bucket, MaxKeys=0)["KeyCount"] == 0
    r = s3.list_objects_v2(Bucket=bucket, Prefix="data/", FetchOwner=True)
    assert all("Owner" in c for c in r["Contents"])


def test_list_v2_delimiter_pages_do_not_repeat_prefixes(s3, bucket):
    _put_many(s3, bucket, [f"p{i}/x{j}" for i in range(4) for j in range(3)] + ["top"])
    prefixes, keys = [], []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Delimiter="/", PaginationConfig={"PageSize": 1}):
        prefixes += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
        keys += [c["Key"] for c in page.get("Contents", [])]
    assert prefixes == ["p0/", "p1/", "p2/", "p3/"] and keys == ["top"]


def test_list_v1_and_url_encoding(s3, bucket):
    _put_many(s3, bucket, ["a b", "a+b", "c"])
    r = s3.list_objects(Bucket=bucket, MaxKeys=2)
    assert [c["Key"] for c in r["Contents"]] == ["a b", "a+b"] and r["IsTruncated"]
    r = s3.list_objects(Bucket=bucket, Marker="a+b")
    assert [c["Key"] for c in r["Contents"]] == ["c"]
    # Asked for explicitly, url encoding is left to the caller to decode.
    r = s3.list_objects_v2(Bucket=bucket, EncodingType="url", Delimiter="/")
    assert [c["Key"] for c in r["Contents"]] == ["a%20b", "a%2Bb", "c"]


def test_delete_objects(s3, bucket):
    _put_many(s3, bucket, ["d1", "d2", "d3"])
    r = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": "d1"}, {"Key": "d2"}, {"Key": "missing"}]})
    assert sorted(d["Key"] for d in r["Deleted"]) == ["d1", "d2", "missing"]
    r = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": "d3"}], "Quiet": True})
    assert "Deleted" not in r or r["Deleted"] == []
    assert s3.list_objects_v2(Bucket=bucket)["KeyCount"] == 0


# ------------------------------------------------------------------------------------------- copy

def test_copy_object(s3, bucket):
    s3.put_object(Bucket=bucket, Key="src", Body=b"payload", Metadata={"a": "1"}, ContentType="text/x-src")
    s3.copy_object(Bucket=bucket, Key="dst", CopySource={"Bucket": bucket, "Key": "src"})
    o = s3.get_object(Bucket=bucket, Key="dst")
    assert o["Body"].read() == b"payload" and o["Metadata"] == {"a": "1"} and o["ContentType"] == "text/x-src"
    s3.copy_object(Bucket=bucket, Key="dst2", CopySource={"Bucket": bucket, "Key": "src"}, MetadataDirective="REPLACE",
                   Metadata={"b": "2"}, ContentType="text/y")
    o = s3.get_object(Bucket=bucket, Key="dst2")
    assert o["Metadata"] == {"b": "2"} and o["ContentType"] == "text/y"
    with pytest.raises(ClientError) as e:
        s3.copy_object(Bucket=bucket, Key="src", CopySource={"Bucket": bucket, "Key": "src"})
    assert code(e) == "InvalidRequest"
    s3.copy_object(Bucket=bucket, Key="src", CopySource={"Bucket": bucket, "Key": "src"}, MetadataDirective="REPLACE",
                   Metadata={"c": "3"})
    assert s3.head_object(Bucket=bucket, Key="src")["Metadata"] == {"c": "3"}
    with pytest.raises(ClientError) as e:
        s3.copy_object(Bucket=bucket, Key="x", CopySource={"Bucket": bucket, "Key": "src"}, CopySourceIfMatch='"nope"')
    assert code(e) == "PreconditionFailed"
    with pytest.raises(ClientError) as e:
        s3.copy_object(Bucket=bucket, Key="x", CopySource={"Bucket": bucket, "Key": "missing"})
    assert code(e) == "NoSuchKey"


def test_copy_across_buckets_with_awkward_key(s3, bucket):
    other = "o-" + uuid.uuid4().hex[:12]
    s3.create_bucket(Bucket=other)
    s3.put_object(Bucket=bucket, Key="dir/with space+plus", Body=b"x")
    s3.copy_object(Bucket=other, Key="copied", CopySource={"Bucket": bucket, "Key": "dir/with space+plus"})
    assert s3.get_object(Bucket=other, Key="copied")["Body"].read() == b"x"


# ------------------------------------------------------------------------------------------- multipart

MB = 1 << 20


def test_multipart_upload(s3, bucket):
    parts_data = [os.urandom(5 * MB), os.urandom(5 * MB), os.urandom(123)]
    up = s3.create_multipart_upload(Bucket=bucket, Key="big", ContentType="application/x-parquet", Metadata={"m": "1"})
    uid = up["UploadId"]
    assert uid in [u["UploadId"] for u in s3.list_multipart_uploads(Bucket=bucket).get("Uploads", [])]
    parts = []
    for i, data in enumerate(parts_data, 1):
        etag = s3.upload_part(Bucket=bucket, Key="big", UploadId=uid, PartNumber=i, Body=data)["ETag"]
        parts.append({"PartNumber": i, "ETag": etag})
    listed = s3.list_parts(Bucket=bucket, Key="big", UploadId=uid)["Parts"]
    assert [p["Size"] for p in listed] == [len(d) for d in parts_data]
    page = s3.list_parts(Bucket=bucket, Key="big", UploadId=uid, MaxParts=2)
    assert page["IsTruncated"] and page["NextPartNumberMarker"] == 2
    r = s3.complete_multipart_upload(Bucket=bucket, Key="big", UploadId=uid, MultipartUpload={"Parts": parts})
    md5s = b"".join(hashlib.md5(d).digest() for d in parts_data)
    assert r["ETag"] == '"%s-3"' % hashlib.md5(md5s).hexdigest()
    o = s3.get_object(Bucket=bucket, Key="big")
    assert o["Body"].read() == b"".join(parts_data)
    assert o["ContentType"] == "application/x-parquet" and o["Metadata"] == {"m": "1"}
    whole = b"".join(parts_data)
    rng = s3.get_object(Bucket=bucket, Key="big", Range=f"bytes={5 * MB - 10}-{5 * MB + 9}")["Body"].read()
    assert rng == whole[5 * MB - 10:5 * MB + 10]
    p2 = s3.get_object(Bucket=bucket, Key="big", PartNumber=2)
    assert p2["Body"].read() == parts_data[1] and p2["PartsCount"] == 3
    assert not s3.list_multipart_uploads(Bucket=bucket).get("Uploads")


def test_multipart_errors(s3, bucket):
    uid = s3.create_multipart_upload(Bucket=bucket, Key="m")["UploadId"]
    e1 = s3.upload_part(Bucket=bucket, Key="m", UploadId=uid, PartNumber=1, Body=b"small")["ETag"]
    e2 = s3.upload_part(Bucket=bucket, Key="m", UploadId=uid, PartNumber=2, Body=b"small")["ETag"]
    with pytest.raises(ClientError) as e:
        s3.complete_multipart_upload(Bucket=bucket, Key="m", UploadId=uid,
                                     MultipartUpload={"Parts": [{"PartNumber": 2, "ETag": e2}, {"PartNumber": 1, "ETag": e1}]})
    assert code(e) == "InvalidPartOrder"
    with pytest.raises(ClientError) as e:
        s3.complete_multipart_upload(Bucket=bucket, Key="m", UploadId=uid,
                                     MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": e1}, {"PartNumber": 2, "ETag": e2}]})
    assert code(e) == "EntityTooSmall"
    with pytest.raises(ClientError) as e:
        s3.complete_multipart_upload(Bucket=bucket, Key="m", UploadId=uid,
                                     MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": '"0000"'}]})
    assert code(e) == "InvalidPart"
    with pytest.raises(ClientError) as e:
        s3.upload_part(Bucket=bucket, Key="m", UploadId=uid, PartNumber=10001, Body=b"x")
    assert code(e) == "InvalidArgument"
    s3.abort_multipart_upload(Bucket=bucket, Key="m", UploadId=uid)
    with pytest.raises(ClientError) as e:
        s3.upload_part(Bucket=bucket, Key="m", UploadId=uid, PartNumber=1, Body=b"x")
    assert code(e) == "NoSuchUpload"
    with pytest.raises(ClientError):
        s3.head_object(Bucket=bucket, Key="m")


def test_upload_part_copy(s3, bucket):
    src = os.urandom(6 * MB)
    s3.put_object(Bucket=bucket, Key="src", Body=src)
    uid = s3.create_multipart_upload(Bucket=bucket, Key="dst")["UploadId"]
    a = s3.upload_part_copy(Bucket=bucket, Key="dst", UploadId=uid, PartNumber=1, CopySource={"Bucket": bucket, "Key": "src"},
                            CopySourceRange=f"bytes=0-{5 * MB - 1}")["CopyPartResult"]["ETag"]
    b = s3.upload_part_copy(Bucket=bucket, Key="dst", UploadId=uid, PartNumber=2, CopySource={"Bucket": bucket, "Key": "src"},
                            CopySourceRange=f"bytes={5 * MB}-{6 * MB - 1}")["CopyPartResult"]["ETag"]
    s3.complete_multipart_upload(Bucket=bucket, Key="dst", UploadId=uid,
                                 MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": a}, {"PartNumber": 2, "ETag": b}]})
    assert s3.get_object(Bucket=bucket, Key="dst")["Body"].read() == src
    uid = s3.create_multipart_upload(Bucket=bucket, Key="dst2")["UploadId"]
    with pytest.raises(ClientError) as e:
        s3.upload_part_copy(Bucket=bucket, Key="dst2", UploadId=uid, PartNumber=1, CopySource={"Bucket": bucket, "Key": "src"},
                            CopySourceRange=f"bytes=0-{7 * MB}")
    assert code(e) == "InvalidRange"


@pytest.mark.parametrize("algo,ctype", [("CRC32", "COMPOSITE"), ("SHA256", "COMPOSITE"), ("CRC32C", "FULL_OBJECT"),
                                        ("CRC64NVME", "FULL_OBJECT")])
def test_multipart_checksums(s3, bucket, algo, ctype):
    data = [os.urandom(5 * MB), os.urandom(100)]
    uid = s3.create_multipart_upload(Bucket=bucket, Key="ck", ChecksumAlgorithm=algo, ChecksumType=ctype)["UploadId"]
    parts = []
    for i, d in enumerate(data, 1):
        r = s3.upload_part(Bucket=bucket, Key="ck", UploadId=uid, PartNumber=i, Body=d, ChecksumAlgorithm=algo)
        parts.append({"PartNumber": i, "ETag": r["ETag"], "Checksum" + algo: r["Checksum" + algo]})
    r = s3.complete_multipart_upload(Bucket=bucket, Key="ck", UploadId=uid, MultipartUpload={"Parts": parts})
    value = r["Checksum" + algo]
    assert value.endswith("-2") == (ctype == "COMPOSITE")
    h = s3.head_object(Bucket=bucket, Key="ck", ChecksumMode="ENABLED")
    assert h["Checksum" + algo] == value


def test_transfer_manager_multipart(s3, bucket):
    data = os.urandom(23 * MB + 7)
    cfg = boto3.s3.transfer.TransferConfig(multipart_threshold=8 * MB, multipart_chunksize=8 * MB, max_concurrency=4)
    s3.upload_fileobj(io.BytesIO(data), bucket, "tm.bin", Config=cfg)
    out = io.BytesIO()
    s3.download_fileobj(bucket, "tm.bin", out, Config=cfg)
    assert out.getvalue() == data


# ------------------------------------------------------------------------------------------- auth

def test_presigned_urls_v4_and_v2(store, bucket):
    s3v4 = store.client(signature_version="s3v4")
    s3v4.put_object(Bucket=bucket, Key="p", Body=b"shared")
    url = s3v4.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": "p"}, ExpiresIn=60)
    assert "X-Amz-Signature" in url and urllib.request.urlopen(url).read() == b"shared"
    url2 = store.client().generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": "p"}, ExpiresIn=60)
    assert urllib.request.urlopen(url2).read() == b"shared"
    put = s3v4.generate_presigned_url("put_object", Params={"Bucket": bucket, "Key": "up"}, ExpiresIn=60)
    assert requests.put(put, data=b"uploaded").status_code == 200
    assert s3v4.get_object(Bucket=bucket, Key="up")["Body"].read() == b"uploaded"
    tampered = url.replace("/p?", "/other?")
    assert requests.get(tampered).status_code == 403


def test_bad_credentials(store, bucket):
    with pytest.raises(ClientError) as e:
        store.client(key=("cairnadmin", "wrong-secret-value")).list_buckets()
    assert code(e) == "SignatureDoesNotMatch"
    with pytest.raises(ClientError) as e:
        store.client(key=("nobody", "whatever-secret")).list_buckets()
    assert code(e) == "InvalidAccessKeyId"
    assert requests.get(f"{store.endpoint}/{bucket}").status_code == 403  # anonymous


def test_read_only_and_scoped_keys(store, s3, bucket):
    s3.put_object(Bucket=bucket, Key="r", Body=b"x")
    reader = store.client(key=READER)
    assert reader.get_object(Bucket=bucket, Key="r")["Body"].read() == b"x"
    with pytest.raises(ClientError) as e:
        reader.put_object(Bucket=bucket, Key="w", Body=b"x")
    assert code(e) == "AccessDenied"
    lake = "lake-" + uuid.uuid4().hex[:10]
    s3.create_bucket(Bucket=lake)
    scoped = store.client(key=SCOPED)
    scoped.put_object(Bucket=lake, Key="ok", Body=b"x")
    with pytest.raises(ClientError) as e:
        scoped.get_object(Bucket=bucket, Key="r")
    assert code(e) == "AccessDenied"
    assert lake in [b["Name"] for b in scoped.list_buckets()["Buckets"]]
    assert bucket not in [b["Name"] for b in scoped.list_buckets()["Buckets"]]
    with pytest.raises(ClientError) as e:
        scoped.create_bucket(Bucket="lake-" + uuid.uuid4().hex[:10])
    assert code(e) == "AccessDenied"  # creating buckets is for admin keys


def test_health_and_metrics(store, s3, bucket):
    assert requests.get(f"{store.endpoint}/-/healthy").text.strip() == "ok"
    assert requests.get(f"{store.endpoint}/-/ready").status_code == 200
    s3.put_object(Bucket=bucket, Key="m", Body=b"x")
    m = requests.get(f"{store.endpoint}/-/metrics").text
    assert 'cairn_store_requests_total{op="PutObject",status="200"}' in m and "cairn_store_objects" in m


def test_refused_uploads_leave_the_connection_usable(store, s3, bucket):
    """A request refused before its body is read (here: anonymous) must not poison the keep-alive
    connection for the next request; a large refused body closes the connection instead."""
    session = requests.Session()
    for _ in range(20):
        r = session.put(f"{store.endpoint}/{bucket}/denied", data=b"x" * 50_000, timeout=10)
        assert r.status_code == 403
        assert session.get(f"{store.endpoint}/-/healthy", timeout=10).status_code == 200
    r = session.put(f"{store.endpoint}/{bucket}/denied", data=b"x" * (3 << 20), timeout=30)
    assert r.status_code == 403 and r.headers.get("Connection", "").lower() == "close"
    assert session.get(f"{store.endpoint}/-/healthy", timeout=10).status_code == 200
    for i in range(30):  # signed clients too: refused writes followed by normal ones
        with pytest.raises(ClientError):
            s3.put_object(Bucket=bucket, Key="acl", Body=b"y" * 10_000, ACL="public-read")
        s3.put_object(Bucket=bucket, Key=f"ok{i}", Body=b"z")
