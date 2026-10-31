"""Bucket and object configuration: tags, ACLs, ownership, and refusal of configuration the store does
not support (it must never accept a setting and then ignore it)."""

import pytest
from botocore.exceptions import ClientError


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def tagset(d: dict) -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in d.items()]


def as_dict(tags: list[dict]) -> dict:
    return {t["Key"]: t["Value"] for t in tags}


def test_object_tags_round_trip(s3, bucket):
    s3.put_object(Bucket=bucket, Key="t.csv", Body=b"x")
    assert s3.get_object_tagging(Bucket=bucket, Key="t.csv")["TagSet"] == []
    s3.put_object_tagging(Bucket=bucket, Key="t.csv", Tagging={"TagSet": tagset({"team": "finance", "pii": "email"})})
    assert as_dict(s3.get_object_tagging(Bucket=bucket, Key="t.csv")["TagSet"]) == {"team": "finance", "pii": "email"}
    head = s3.head_object(Bucket=bucket, Key="t.csv")
    assert head["ResponseMetadata"]["HTTPHeaders"]["x-amz-tagging-count"] == "2"
    before = head["ETag"]
    s3.delete_object_tagging(Bucket=bucket, Key="t.csv")
    assert s3.get_object_tagging(Bucket=bucket, Key="t.csv")["TagSet"] == []
    assert s3.head_object(Bucket=bucket, Key="t.csv")["ETag"] == before  # tags do not change the object
    with pytest.raises(ClientError) as ei:
        s3.get_object_tagging(Bucket=bucket, Key="missing")
    assert code(ei) == "NoSuchKey"


def test_tags_on_put_copy_and_multipart(s3, bucket):
    s3.put_object(Bucket=bucket, Key="a", Body=b"x", Tagging="team=finance&cost%20centre=a%2Fb")
    assert as_dict(s3.get_object_tagging(Bucket=bucket, Key="a")["TagSet"]) == {"team": "finance", "cost centre": "a/b"}
    s3.copy_object(Bucket=bucket, Key="b", CopySource={"Bucket": bucket, "Key": "a"})
    assert as_dict(s3.get_object_tagging(Bucket=bucket, Key="b")["TagSet"])["team"] == "finance"
    s3.copy_object(Bucket=bucket, Key="c", CopySource={"Bucket": bucket, "Key": "a"}, TaggingDirective="REPLACE", Tagging="x=1")
    assert as_dict(s3.get_object_tagging(Bucket=bucket, Key="c")["TagSet"]) == {"x": "1"}
    # Replacing only the tags of an object with itself is a valid copy.
    s3.copy_object(Bucket=bucket, Key="a", CopySource={"Bucket": bucket, "Key": "a"}, TaggingDirective="REPLACE", Tagging="y=2")
    assert as_dict(s3.get_object_tagging(Bucket=bucket, Key="a")["TagSet"]) == {"y": "2"}
    up = s3.create_multipart_upload(Bucket=bucket, Key="m", Tagging="stage=raw")
    p = s3.upload_part(Bucket=bucket, Key="m", UploadId=up["UploadId"], PartNumber=1, Body=b"z")
    s3.complete_multipart_upload(Bucket=bucket, Key="m", UploadId=up["UploadId"],
                                 MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": p["ETag"]}]})
    assert as_dict(s3.get_object_tagging(Bucket=bucket, Key="m")["TagSet"]) == {"stage": "raw"}


def test_tags_survive_restart(fresh_store):
    st = fresh_store.start()
    s3 = st.client()
    s3.create_bucket(Bucket="keep")
    s3.put_object(Bucket="keep", Key="d", Body=b"x", Tagging="keep=yes")
    s3.put_bucket_tagging(Bucket="keep", Tagging={"TagSet": tagset({"owner": "data-platform"})})
    st.stop(kill=True)
    s3 = st.start().client()
    assert as_dict(s3.get_object_tagging(Bucket="keep", Key="d")["TagSet"]) == {"keep": "yes"}
    assert as_dict(s3.get_bucket_tagging(Bucket="keep")["TagSet"]) == {"owner": "data-platform"}


@pytest.mark.parametrize("tags,err", [
    ({f"k{i}": "v" for i in range(11)}, "BadRequest"),
    ({"aws:reserved": "x"}, "InvalidTag"),
    ({"k": "v" * 257}, "InvalidTag"),
])
def test_invalid_object_tags(s3, bucket, tags, err):
    s3.put_object(Bucket=bucket, Key="t", Body=b"x")
    with pytest.raises(ClientError) as ei:
        s3.put_object_tagging(Bucket=bucket, Key="t", Tagging={"TagSet": tagset(tags)})
    assert code(ei) == err


def test_duplicate_tag_keys_are_refused(s3, bucket):
    s3.put_object(Bucket=bucket, Key="t", Body=b"x")
    with pytest.raises(ClientError) as ei:
        s3.put_object_tagging(Bucket=bucket, Key="t", Tagging={"TagSet": [{"Key": "a", "Value": "1"}, {"Key": "a", "Value": "2"}]})
    assert code(ei) == "InvalidTag"


def test_bucket_tags(s3, bucket):
    with pytest.raises(ClientError) as ei:
        s3.get_bucket_tagging(Bucket=bucket)
    assert code(ei) == "NoSuchTagSet"
    tags = {f"k{i}": str(i) for i in range(50)}
    s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": tagset(tags)})
    assert as_dict(s3.get_bucket_tagging(Bucket=bucket)["TagSet"]) == tags
    s3.delete_bucket_tagging(Bucket=bucket)
    with pytest.raises(ClientError) as ei:
        s3.get_bucket_tagging(Bucket=bucket)
    assert code(ei) == "NoSuchTagSet"


def test_bucket_config_goes_with_the_bucket(s3):
    s3.create_bucket(Bucket="cfg-gone")
    s3.put_bucket_tagging(Bucket="cfg-gone", Tagging={"TagSet": tagset({"a": "1"})})
    s3.delete_bucket(Bucket="cfg-gone")
    s3.create_bucket(Bucket="cfg-gone")
    with pytest.raises(ClientError) as ei:
        s3.get_bucket_tagging(Bucket="cfg-gone")
    assert code(ei) == "NoSuchTagSet"
    s3.delete_bucket(Bucket="cfg-gone")


def test_acls_are_owner_only(s3, bucket):
    s3.put_object(Bucket=bucket, Key="a", Body=b"x", ACL="private")
    s3.put_object(Bucket=bucket, Key="b", Body=b"x", ACL="bucket-owner-full-control")
    s3.put_bucket_acl(Bucket=bucket, ACL="private")
    s3.put_object_acl(Bucket=bucket, Key="a", ACL="private")
    acl = s3.get_object_acl(Bucket=bucket, Key="a")
    s3.put_object_acl(Bucket=bucket, Key="a", AccessControlPolicy={"Grants": acl["Grants"], "Owner": acl["Owner"]})
    for call in (
        lambda: s3.put_object(Bucket=bucket, Key="c", Body=b"x", ACL="public-read"),
        lambda: s3.put_bucket_acl(Bucket=bucket, ACL="public-read"),
        lambda: s3.put_object_acl(Bucket=bucket, Key="a", ACL="authenticated-read"),
        lambda: s3.put_object_acl(Bucket=bucket, Key="a", GrantRead="uri=http://acs.amazonaws.com/groups/global/AllUsers"),
    ):
        with pytest.raises(ClientError) as ei:
            call()
        assert code(ei) == "AccessControlListNotSupported"
    with pytest.raises(ClientError):
        s3.head_object(Bucket=bucket, Key="c")  # the refused write stored nothing


def test_ownership_is_bucket_owner_enforced(s3, bucket):
    got = s3.get_bucket_ownership_controls(Bucket=bucket)["OwnershipControls"]["Rules"]
    assert got == [{"ObjectOwnership": "BucketOwnerEnforced"}]
    s3.put_bucket_ownership_controls(Bucket=bucket, OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]})
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_ownership_controls(Bucket=bucket, OwnershipControls={"Rules": [{"ObjectOwnership": "ObjectWriter"}]})
    assert code(ei) == "AccessControlListNotSupported"


UNSUPPORTED = [
    lambda s3, b: s3.put_bucket_versioning(Bucket=b, VersioningConfiguration={"Status": "Enabled"}),
    lambda s3, b: s3.put_bucket_policy(Bucket=b, Policy='{"Version":"2012-10-17","Statement":[]}'),
    lambda s3, b: s3.put_bucket_lifecycle_configuration(Bucket=b, LifecycleConfiguration={"Rules": [
        {"ID": "x", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 1}}]}),
    lambda s3, b: s3.put_public_access_block(Bucket=b, PublicAccessBlockConfiguration={"BlockPublicAcls": True}),
    lambda s3, b: s3.put_bucket_encryption(Bucket=b, ServerSideEncryptionConfiguration={"Rules": [
        {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}),
]


@pytest.mark.parametrize("call", UNSUPPORTED)
def test_unsupported_config_is_refused_not_ignored(s3, bucket, call):
    """Until a feature is implemented the store says so (501), instead of accepting and ignoring it."""
    with pytest.raises(ClientError) as ei:
        call(s3, bucket)
    assert code(ei) == "NotImplemented"


def test_unsupported_object_lock_is_refused(s3, bucket):
    s3.put_object(Bucket=bucket, Key="l", Body=b"x")
    with pytest.raises(ClientError) as ei:
        s3.put_object_legal_hold(Bucket=bucket, Key="l", LegalHold={"Status": "ON"})
    assert code(ei) == "NotImplemented"
    with pytest.raises(ClientError) as ei:
        s3.create_bucket(Bucket="lock-me", ObjectLockEnabledForBucket=True)
    assert code(ei) == "NotImplemented"
    with pytest.raises(ClientError):
        s3.head_bucket(Bucket="lock-me")
