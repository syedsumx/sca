"""The IAM-compatible API through boto3's own IAM and STS clients: users, access keys (two at most,
inactive keys refused), inline, managed and group policies with IAM's evaluation (explicit Deny wins,
nothing allowed by default), policy versions, bucket policies naming IAM users, delegated IAM rights
through a policy, the conflicts IAM reports, and the audit trail."""

import json
import time

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from conftest import ADMIN, REGION

ACCOUNT = "000000000000"


def client(store, service, ak=ADMIN[0], sk=ADMIN[1]):
    return boto3.client(service, endpoint_url=store.endpoint, aws_access_key_id=ak, aws_secret_access_key=sk, region_name=REGION,
                        config=Config(retries={"max_attempts": 1}))


def s3_as(store, key):
    return boto3.client("s3", endpoint_url=store.endpoint, aws_access_key_id=key["AccessKeyId"],
                        aws_secret_access_key=key["SecretAccessKey"], region_name=REGION,
                        config=Config(retries={"max_attempts": 1}, s3={"addressing_style": "path"}))


def code(ei):
    return ei.value.response["Error"]["Code"]


def denied(fn, *a, **kw):
    with pytest.raises(ClientError) as ei:
        fn(*a, **kw)
    return code(ei)


def until(fn, timeout=10):
    """Key changes apply on the next request; allow a reload in a cluster."""
    end = time.time() + timeout
    while True:
        try:
            return fn()
        except ClientError:
            if time.time() > end:
                raise
            time.sleep(0.2)


@pytest.fixture
def st(custom_store):
    return custom_store({})


def test_users_keys_and_inline_policies(st):
    iam = client(st, "iam")
    s3 = st.client()
    s3.create_bucket(Bucket="analytics")
    s3.create_bucket(Bucket="finance")
    s3.put_object(Bucket="analytics", Key="report.csv", Body=b"a,b\n")
    s3.put_object(Bucket="finance", Key="ledger.csv", Body=b"secret\n")
    u = iam.create_user(UserName="alice", Path="/data/")["User"]
    assert u["Arn"] == f"arn:aws:iam::{ACCOUNT}:user/data/alice" and u["UserId"].startswith("AIDA")
    assert code_of(iam.create_user, UserName="ALICE") == "EntityAlreadyExists"  # names are case-insensitive
    assert [x["UserName"] for x in iam.list_users()["Users"]] == ["alice"]
    assert [x["UserName"] for x in iam.list_users(PathPrefix="/other/")["Users"]] == []
    key = iam.create_access_key(UserName="alice")["AccessKey"]
    assert key["AccessKeyId"].startswith("AKIA") and key["Status"] == "Active"
    # No policy yet: the key signs in but may do nothing.
    a3 = s3_as(st, key)
    assert denied(a3.get_object, Bucket="analytics", Key="report.csv") == "AccessDenied"
    assert denied(a3.list_buckets) == "AccessDenied"
    who = client(st, "sts", key["AccessKeyId"], key["SecretAccessKey"]).get_caller_identity()
    assert who["Arn"] == u["Arn"] and who["Account"] == ACCOUNT and who["UserId"] == u["UserId"]
    # An inline policy: read the analytics bucket, never anything under private/.
    iam.put_user_policy(UserName="alice", PolicyName="read-analytics", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
         "Resource": ["arn:aws:s3:::analytics", "arn:aws:s3:::analytics/*"]},
        {"Effect": "Deny", "Action": "s3:*", "Resource": "arn:aws:s3:::analytics/private/*"}]}))
    assert until(lambda: a3.get_object(Bucket="analytics", Key="report.csv"))["Body"].read() == b"a,b\n"
    assert [o["Key"] for o in a3.list_objects_v2(Bucket="analytics")["Contents"]] == ["report.csv"]
    s3.put_object(Bucket="analytics", Key="private/p.txt", Body=b"p")
    assert denied(a3.get_object, Bucket="analytics", Key="private/p.txt") == "AccessDenied"
    assert denied(a3.get_object, Bucket="finance", Key="ledger.csv") == "AccessDenied"
    assert denied(a3.put_object, Bucket="analytics", Key="w", Body=b"x") == "AccessDenied"
    got = iam.get_user_policy(UserName="alice", PolicyName="read-analytics")
    assert got["PolicyDocument"]["Statement"][1]["Effect"] == "Deny"  # boto3 decodes the URL-encoded document
    assert iam.list_user_policies(UserName="alice")["PolicyNames"] == ["read-analytics"]
    # A malformed or foreign-service policy is refused, not stored.
    assert code_of(iam.put_user_policy, UserName="alice", PolicyName="bad", PolicyDocument="{not json") == "MalformedPolicyDocument"
    bad = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "ec2:*", "Resource": "*"}]})
    assert code_of(iam.put_user_policy, UserName="alice", PolicyName="bad", PolicyDocument=bad) == "MalformedPolicyDocument"
    # Two keys at most; inactive keys are refused; a key cannot disable itself.
    key2 = iam.create_access_key(UserName="alice")["AccessKey"]
    assert code_of(iam.create_access_key, UserName="alice") == "LimitExceeded"
    listed = {k["AccessKeyId"]: k["Status"] for k in iam.list_access_keys(UserName="alice")["AccessKeyMetadata"]}
    assert listed == {key["AccessKeyId"]: "Active", key2["AccessKeyId"]: "Active"}
    iam.update_access_key(UserName="alice", AccessKeyId=key2["AccessKeyId"], Status="Inactive")
    assert denied(s3_as(st, key2).get_object, Bucket="analytics", Key="report.csv") == "InvalidAccessKeyId"
    iam.update_access_key(UserName="alice", AccessKeyId=key2["AccessKeyId"], Status="Active")
    assert until(lambda: s3_as(st, key2).get_object(Bucket="analytics", Key="report.csv"))["Body"].read() == b"a,b\n"
    # Deleting a user needs its keys and policies gone first, as in IAM.
    assert code_of(iam.delete_user, UserName="alice") == "DeleteConflict"
    for k in (key, key2):
        iam.delete_access_key(UserName="alice", AccessKeyId=k["AccessKeyId"])
    assert denied(a3.get_object, Bucket="analytics", Key="report.csv") == "InvalidAccessKeyId"
    assert code_of(iam.delete_user, UserName="alice") == "DeleteConflict"
    iam.delete_user_policy(UserName="alice", PolicyName="read-analytics")
    iam.delete_user(UserName="alice")
    assert code_of(iam.get_user, UserName="alice") == "NoSuchEntity"
    assert code_of(iam.tag_user, UserName="x", Tags=[]) == "InvalidAction"  # not supported: said so


def code_of(fn, **kw):
    with pytest.raises(ClientError) as ei:
        fn(**kw)
    return code(ei)


def test_groups_managed_policies_and_versions(st):
    iam = client(st, "iam")
    s3 = st.client()
    s3.create_bucket(Bucket="lake")
    s3.put_object(Bucket="lake", Key="raw/a.json", Body=b"{}")
    doc_read = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
                                                        "Resource": ["arn:aws:s3:::lake", "arn:aws:s3:::lake/*"]}]}
    p = iam.create_policy(PolicyName="LakeRead", Path="/team/", PolicyDocument=json.dumps(doc_read), Description="read the lake")["Policy"]
    assert p["Arn"] == f"arn:aws:iam::{ACCOUNT}:policy/team/LakeRead" and p["DefaultVersionId"] == "v1" and p["AttachmentCount"] == 0
    iam.create_group(GroupName="analysts")
    iam.attach_group_policy(GroupName="analysts", PolicyArn=p["Arn"])
    iam.create_user(UserName="bob")
    key = iam.create_access_key(UserName="bob")["AccessKey"]
    b3 = s3_as(st, key)
    assert denied(b3.get_object, Bucket="lake", Key="raw/a.json") == "AccessDenied"
    iam.add_user_to_group(GroupName="analysts", UserName="bob")
    assert until(lambda: b3.get_object(Bucket="lake", Key="raw/a.json"))["Body"].read() == b"{}"
    g = iam.get_group(GroupName="analysts")
    assert [u["UserName"] for u in g["Users"]] == ["bob"]
    assert [x["GroupName"] for x in iam.list_groups_for_user(UserName="bob")["Groups"]] == ["analysts"]
    assert iam.get_policy(PolicyArn=p["Arn"])["Policy"]["AttachmentCount"] == 1
    assert [x["PolicyName"] for x in iam.list_attached_group_policies(GroupName="analysts")["AttachedPolicies"]] == ["LakeRead"]
    assert [x["PolicyName"] for x in iam.list_policies(Scope="Local", OnlyAttached=True)["Policies"]] == ["LakeRead"]
    # A new default version that also allows writes to raw/ takes effect for every member.
    doc_rw = {"Version": "2012-10-17", "Statement": doc_read["Statement"] + [
        {"Effect": "Allow", "Action": "s3:PutObject", "Resource": "arn:aws:s3:::lake/raw/*"}]}
    v2 = iam.create_policy_version(PolicyArn=p["Arn"], PolicyDocument=json.dumps(doc_rw), SetAsDefault=True)["PolicyVersion"]
    assert v2["VersionId"] == "v2" and v2["IsDefaultVersion"]
    until(lambda: b3.put_object(Bucket="lake", Key="raw/b.json", Body=b"[]"))
    assert denied(b3.put_object, Bucket="lake", Key="curated/c.json", Body=b"x") == "AccessDenied"
    got = iam.get_policy_version(PolicyArn=p["Arn"], VersionId="v1")["PolicyVersion"]
    assert got["Document"] == doc_read and not got["IsDefaultVersion"]
    assert [v["VersionId"] for v in iam.list_policy_versions(PolicyArn=p["Arn"])["Versions"]] == ["v2", "v1"]
    assert code_of(iam.delete_policy_version, PolicyArn=p["Arn"], VersionId="v2") == "DeleteConflict"
    iam.set_default_policy_version(PolicyArn=p["Arn"], VersionId="v1")
    assert until(lambda: denied(b3.put_object, Bucket="lake", Key="raw/d.json", Body=b"x")) == "AccessDenied"
    iam.delete_policy_version(PolicyArn=p["Arn"], VersionId="v2")
    for _ in range(4):
        iam.create_policy_version(PolicyArn=p["Arn"], PolicyDocument=json.dumps(doc_read))
    assert code_of(iam.create_policy_version, PolicyArn=p["Arn"], PolicyDocument=json.dumps(doc_read)) == "LimitExceeded"
    # An explicit Deny in one policy beats an Allow in another (user inline vs group managed).
    iam.put_user_policy(UserName="bob", PolicyName="no-raw", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Deny", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::lake/raw/*"}]}))
    assert until(lambda: denied(b3.get_object, Bucket="lake", Key="raw/a.json")) == "AccessDenied"
    # A policy in use cannot be deleted; nor a group with members.
    assert code_of(iam.delete_policy, PolicyArn=p["Arn"]) == "DeleteConflict"
    assert code_of(iam.delete_group, GroupName="analysts") == "DeleteConflict"
    iam.remove_user_from_group(GroupName="analysts", UserName="bob")
    iam.detach_group_policy(GroupName="analysts", PolicyArn=p["Arn"])
    iam.delete_policy(PolicyArn=p["Arn"])
    iam.delete_group(GroupName="analysts")
    assert iam.list_policies(Scope="Local")["Policies"] == [] and iam.list_groups()["Groups"] == []


def test_bucket_policies_name_iam_users_and_iam_rights_can_be_delegated(st):
    iam = client(st, "iam")
    s3 = st.client()
    s3.create_bucket(Bucket="shared")
    s3.put_object(Bucket="shared", Key="hello.txt", Body=b"hi")
    iam.create_user(UserName="carol")
    key = iam.create_access_key(UserName="carol")["AccessKey"]
    c3 = s3_as(st, key)
    assert denied(c3.get_object, Bucket="shared", Key="hello.txt") == "AccessDenied"
    s3.put_bucket_policy(Bucket="shared", Policy=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:user/carol"}, "Action": "s3:GetObject",
         "Resource": "arn:aws:s3:::shared/*"}]}))
    assert c3.get_object(Bucket="shared", Key="hello.txt")["Body"].read() == b"hi"
    # carol is not an IAM admin...
    ciam = client(st, "iam", key["AccessKeyId"], key["SecretAccessKey"])
    assert code_of(ciam.create_user, UserName="dave") == "AccessDenied"
    # ...until a policy lets her manage users under /contractors/ (and nothing else).
    iam.put_user_policy(UserName="carol", PolicyName="iam-contractors", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["iam:CreateUser", "iam:CreateAccessKey", "iam:GetUser"],
         "Resource": f"arn:aws:iam::{ACCOUNT}:user/contractors/*"}]}))
    until(lambda: ciam.create_user(UserName="dave", Path="/contractors/"))
    assert ciam.create_access_key(UserName="dave")["AccessKey"]["AccessKeyId"].startswith("AKIA")
    assert code_of(ciam.create_user, UserName="erin") == "AccessDenied"  # path /: not hers
    assert code_of(ciam.list_users) == "AccessDenied"
    assert code_of(ciam.put_user_policy, UserName="carol", PolicyName="more", PolicyDocument=json.dumps(
        {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]})) == "AccessDenied"
    # IAM users' keys stay out of reach of the key API's settings, and every change is audited.
    import requests
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    def admin(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        req = AWSRequest(method=method, url=f"{st.endpoint}/-/admin/v1/{path}", data=data)
        S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
        r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=30)
        return r.status_code, (r.json() if r.content else None)

    code_, k = admin("GET", f"keys/{key['AccessKeyId']}")
    assert code_ == 200 and k["user"] == "carol" and k["status"] == "Active"
    assert admin("PATCH", f"keys/{key['AccessKeyId']}", {"admin": True})[0] == 409
    actions = [e["action"] for e in admin("GET", "audit?after=0")[1]["entries"] if e["action"].startswith("iam.")]
    assert actions == ["iam.CreateUser", "iam.CreateAccessKey", "iam.PutUserPolicy", "iam.CreateUser", "iam.CreateAccessKey"]


def test_iam_needs_a_signed_iam_request(st):
    import requests

    r = requests.post(st.endpoint + "/", data={"Action": "ListUsers", "Version": "2010-05-08"}, timeout=10)
    assert r.status_code == 403
    iam = client(st, "iam", ADMIN[0], "wrong-secret")
    assert code_of(iam.list_users) == "SignatureDoesNotMatch"
