"""Bucket versioning: versions, delete markers, version-addressed reads, copies, tags and deletes."""

import pytest
from botocore.exceptions import ClientError

from conftest import ANALYST


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def headers(resp) -> dict:
    return resp["ResponseMetadata"]["HTTPHeaders"]


def enable(s3, bucket, status="Enabled"):
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": status})


def test_versioning_configuration(s3, bucket):
    assert "Status" not in s3.get_bucket_versioning(Bucket=bucket)
    enable(s3, bucket)
    assert s3.get_bucket_versioning(Bucket=bucket)["Status"] == "Enabled"
    enable(s3, bucket, "Suspended")
    assert s3.get_bucket_versioning(Bucket=bucket)["Status"] == "Suspended"
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled", "MFADelete": "Enabled"})
    assert code(ei) == "NotImplemented"


def test_versions_and_delete_markers(s3, bucket):
    s3.put_object(Bucket=bucket, Key="k", Body=b"before")  # the null version
    enable(s3, bucket)
    v1 = s3.put_object(Bucket=bucket, Key="k", Body=b"one")["VersionId"]
    v2 = s3.put_object(Bucket=bucket, Key="k", Body=b"two")["VersionId"]
    assert v1 != v2 and "null" not in (v1, v2)
    got = s3.get_object(Bucket=bucket, Key="k")
    assert got["Body"].read() == b"two" and got["VersionId"] == v2
    assert s3.get_object(Bucket=bucket, Key="k", VersionId=v1)["Body"].read() == b"one"
    assert s3.get_object(Bucket=bucket, Key="k", VersionId="null")["Body"].read() == b"before"
    assert s3.head_object(Bucket=bucket, Key="k", VersionId=v1)["ContentLength"] == 3

    d = s3.delete_object(Bucket=bucket, Key="k")
    assert d["DeleteMarker"] is True
    marker = d["VersionId"]
    with pytest.raises(ClientError) as ei:
        s3.get_object(Bucket=bucket, Key="k")
    assert code(ei) == "NoSuchKey"
    assert ei.value.response["ResponseMetadata"]["HTTPHeaders"]["x-amz-delete-marker"] == "true"
    with pytest.raises(ClientError) as ei:
        s3.get_object(Bucket=bucket, Key="k", VersionId=marker)
    assert code(ei) == "MethodNotAllowed"
    assert s3.list_objects_v2(Bucket=bucket).get("KeyCount") == 0  # current listing hides the key

    lv = s3.list_object_versions(Bucket=bucket)
    assert [(m["VersionId"], m["IsLatest"]) for m in lv["DeleteMarkers"]] == [(marker, True)]
    assert [v["VersionId"] for v in lv["Versions"]] == [v2, v1, "null"]
    assert not any(v["IsLatest"] for v in lv["Versions"])

    s3.delete_object(Bucket=bucket, Key="k", VersionId=marker)  # undelete
    assert s3.get_object(Bucket=bucket, Key="k")["Body"].read() == b"two"
    r = s3.delete_object(Bucket=bucket, Key="k", VersionId=v2)  # permanent: the previous version is current again
    assert r["VersionId"] == v2
    assert s3.get_object(Bucket=bucket, Key="k")["Body"].read() == b"one"
    with pytest.raises(ClientError) as ei:
        s3.get_object(Bucket=bucket, Key="k", VersionId=v2)
    assert code(ei) == "NoSuchVersion"


def test_version_listing_pages_and_prefixes(s3, bucket):
    enable(s3, bucket)
    ids = {}
    for k in ["a/1", "a/2", "b"]:
        ids[k] = [s3.put_object(Bucket=bucket, Key=k, Body=str(i).encode())["VersionId"] for i in range(3)]
    seen, kw = [], {}
    while True:
        page = s3.list_object_versions(Bucket=bucket, MaxKeys=2, **kw)
        seen += [(v["Key"], v["VersionId"]) for v in page.get("Versions", [])]
        if not page["IsTruncated"]:
            break
        kw = {"KeyMarker": page["NextKeyMarker"], "VersionIdMarker": page["NextVersionIdMarker"]}
    assert seen == [(k, v) for k in ["a/1", "a/2", "b"] for v in reversed(ids[k])]
    top = s3.list_object_versions(Bucket=bucket, Delimiter="/")
    assert [p["Prefix"] for p in top["CommonPrefixes"]] == ["a/"] and {v["Key"] for v in top["Versions"]} == {"b"}


def test_copy_tags_and_multipart_by_version(s3, bucket):
    enable(s3, bucket)
    v1 = s3.put_object(Bucket=bucket, Key="src", Body=b"first", Tagging="v=1")["VersionId"]
    s3.put_object(Bucket=bucket, Key="src", Body=b"second")
    r = s3.copy_object(Bucket=bucket, Key="dst", CopySource={"Bucket": bucket, "Key": "src", "VersionId": v1})
    assert r["CopySourceVersionId"] == v1 and r["VersionId"]
    assert s3.get_object(Bucket=bucket, Key="dst")["Body"].read() == b"first"
    s3.put_object_tagging(Bucket=bucket, Key="src", VersionId=v1, Tagging={"TagSet": [{"Key": "v", "Value": "old"}]})
    assert s3.get_object_tagging(Bucket=bucket, Key="src", VersionId=v1)["TagSet"] == [{"Key": "v", "Value": "old"}]
    assert s3.get_object_tagging(Bucket=bucket, Key="src")["TagSet"] == []
    up = s3.create_multipart_upload(Bucket=bucket, Key="src")
    p = s3.upload_part(Bucket=bucket, Key="src", UploadId=up["UploadId"], PartNumber=1, Body=b"third")
    done = s3.complete_multipart_upload(Bucket=bucket, Key="src", UploadId=up["UploadId"],
                                        MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": p["ETag"]}]})
    assert done["VersionId"]
    assert len(s3.list_object_versions(Bucket=bucket, Prefix="src")["Versions"]) == 3


def test_delete_objects_with_versions(s3, bucket):
    enable(s3, bucket)
    v = s3.put_object(Bucket=bucket, Key="a", Body=b"a")["VersionId"]
    s3.put_object(Bucket=bucket, Key="b", Body=b"b")
    res = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": "a", "VersionId": v}, {"Key": "b"}]})
    by_key = {d["Key"]: d for d in res["Deleted"]}
    assert by_key["a"]["VersionId"] == v and "DeleteMarker" not in by_key["a"]
    assert by_key["b"]["DeleteMarker"] is True and by_key["b"]["DeleteMarkerVersionId"]
    assert "Versions" not in s3.list_object_versions(Bucket=bucket, Prefix="a")


def test_suspended_versioning(s3, bucket):
    enable(s3, bucket)
    v1 = s3.put_object(Bucket=bucket, Key="k", Body=b"one")["VersionId"]
    enable(s3, bucket, "Suspended")
    assert s3.put_object(Bucket=bucket, Key="k", Body=b"n1")["VersionId"] == "null"
    s3.put_object(Bucket=bucket, Key="k", Body=b"n2")
    assert [x["VersionId"] for x in s3.list_object_versions(Bucket=bucket)["Versions"]] == ["null", v1]


def test_versioned_bucket_is_not_empty_until_versions_are_gone(s3, bucket):
    enable(s3, bucket)
    v = s3.put_object(Bucket=bucket, Key="k", Body=b"x")["VersionId"]
    m = s3.delete_object(Bucket=bucket, Key="k")["VersionId"]
    with pytest.raises(ClientError) as ei:
        s3.delete_bucket(Bucket=bucket)
    assert code(ei) == "BucketNotEmpty"
    s3.delete_object(Bucket=bucket, Key="k", VersionId=v)
    s3.delete_object(Bucket=bucket, Key="k", VersionId=m)
    s3.delete_bucket(Bucket=bucket)


def test_version_reads_need_version_permission(store, s3, bucket):
    import json
    enable(s3, bucket)
    v1 = s3.put_object(Bucket=bucket, Key="k", Body=b"old")["VersionId"]
    s3.put_object(Bucket=bucket, Key="k", Body=b"new")
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps({"Statement": [{
        "Effect": "Allow", "Principal": {"AWS": ANALYST[0]}, "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/*"}]}))
    analyst = store.client(key=ANALYST)
    assert analyst.get_object(Bucket=bucket, Key="k")["Body"].read() == b"new"
    with pytest.raises(ClientError) as ei:
        analyst.get_object(Bucket=bucket, Key="k", VersionId=v1)
    assert code(ei) == "AccessDenied"


def test_versioned_crash_recovery_keeps_versions(fresh_store):
    st = fresh_store.start()
    s3 = st.client()
    s3.create_bucket(Bucket="ver")
    enable(s3, "ver")
    v1 = s3.put_object(Bucket="ver", Key="k", Body=b"one")["VersionId"]
    s3.put_object(Bucket="ver", Key="k", Body=b"two")
    st.stop(kill=True)
    s3 = st.start().client()
    assert s3.get_object(Bucket="ver", Key="k", VersionId=v1)["Body"].read() == b"one"
    assert s3.get_object(Bucket="ver", Key="k")["Body"].read() == b"two"
