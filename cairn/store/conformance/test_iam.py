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


def test_a_user_who_manages_its_own_keys_cannot_touch_another_users(st):
    # The usual self-service policy: a user may list, disable and delete its own keys. Leaving UserName out
    # means "my own keys", as in IAM; it must not reach another user's key.
    iam = client(st, "iam")
    for n in ("ana", "ben"):
        iam.create_user(UserName=n)
    ana = iam.create_access_key(UserName="ana")["AccessKey"]
    ben = iam.create_access_key(UserName="ben")["AccessKey"]
    iam.put_user_policy(UserName="ana", PolicyName="own-keys", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["iam:ListAccessKeys", "iam:UpdateAccessKey", "iam:DeleteAccessKey"],
         "Resource": f"arn:aws:iam::{ACCOUNT}:user/ana"}]}))
    aiam = client(st, "iam", ana["AccessKeyId"], ana["SecretAccessKey"])
    assert [k["AccessKeyId"] for k in until(aiam.list_access_keys)["AccessKeyMetadata"]] == [ana["AccessKeyId"]]
    assert code_of(aiam.update_access_key, AccessKeyId=ben["AccessKeyId"], Status="Inactive") == "NoSuchEntity"
    assert code_of(aiam.delete_access_key, AccessKeyId=ben["AccessKeyId"]) == "NoSuchEntity"
    assert code_of(aiam.delete_access_key, UserName="ben", AccessKeyId=ben["AccessKeyId"]) == "AccessDenied"
    assert {k["AccessKeyId"]: k["Status"] for k in iam.list_access_keys(UserName="ben")["AccessKeyMetadata"]} == {
        ben["AccessKeyId"]: "Active"}
    # Her own key, without UserName, still works.
    second = iam.create_access_key(UserName="ana")["AccessKey"]
    aiam.update_access_key(AccessKeyId=second["AccessKeyId"], Status="Inactive")
    aiam.delete_access_key(AccessKeyId=second["AccessKeyId"])


def test_iam_rights_are_evaluated_with_the_request_context(st):
    # A Deny that names the caller through ${aws:username}, or that holds on the connection (plain HTTP,
    # the client's address), applies to IAM calls as it does to S3 calls. Without the request's context
    # such a Deny never applied, and the Allow beside it won.
    iam = client(st, "iam")
    iam.create_user(UserName="ops")
    ops = iam.create_access_key(UserName="ops")["AccessKey"]
    iam.put_user_policy(UserName="ops", PolicyName="manage-others", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "iam:*", "Resource": "*"},
        {"Effect": "Deny", "Action": "iam:*", "Resource": f"arn:aws:iam::{ACCOUNT}:user/${{aws:username}}"}]}))
    oiam = client(st, "iam", ops["AccessKeyId"], ops["SecretAccessKey"])
    until(lambda: oiam.create_user(UserName="temp"))
    everything = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]})
    assert code_of(oiam.put_user_policy, UserName="ops", PolicyName="all", PolicyDocument=everything) == "AccessDenied"
    assert code_of(oiam.create_access_key, UserName="ops") == "AccessDenied"
    assert iam.list_user_policies(UserName="ops")["PolicyNames"] == ["manage-others"]
    # Conditions on the connection: no IAM over plain HTTP, or from loopback.
    for name, cond in [("no-plain-http", {"Bool": {"aws:SecureTransport": "false"}}),
                       ("not-from-here", {"IpAddress": {"aws:SourceIp": ["127.0.0.0/8", "::1/128"]}})]:
        iam.create_user(UserName=name)
        k = iam.create_access_key(UserName=name)["AccessKey"]
        kiam = client(st, "iam", k["AccessKeyId"], k["SecretAccessKey"])
        iam.put_user_policy(UserName=name, PolicyName="p", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "iam:GetUser", "Resource": "*"}]}))
        until(lambda: kiam.get_user(UserName="temp"))
        iam.put_user_policy(UserName=name, PolicyName="p", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "iam:GetUser", "Resource": "*"},
            {"Effect": "Deny", "Action": "iam:*", "Resource": "*", "Condition": cond}]}))
        time.sleep(0.5)
        assert code_of(kiam.get_user, UserName="temp") == "AccessDenied", name


def _status_line(sock):
    sock.settimeout(10)
    try:
        return sock.recv(4096)
    except (TimeoutError, OSError):
        return b""
    finally:
        sock.close()


def test_iam_endpoint_refuses_an_oversized_body_without_buffering_it(st):
    # The IAM endpoint verifies its signature over the whole body, so it reads the body before it knows
    # who sent it: it must stop at its limit (1 MiB) rather than buffer whatever anyone sends.
    import socket
    from urllib.parse import urlparse

    u = urlparse(st.endpoint)
    head = ("POST / HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded\r\n"
            "Authorization: AWS4-HMAC-SHA256 Credential=AK/20260101/us-east-1/iam/aws4_request, SignedHeaders=host, Signature=00\r\n")
    # A declared length over the limit is answered at once.
    s = socket.create_connection((u.hostname, u.port), timeout=10)
    s.sendall((head + "Content-Length: 4000000000\r\n\r\n").encode() + b"Action=ListUsers&" + b"x" * 65536)
    reply = _status_line(s)
    assert reply.startswith(b"HTTP/1.1 400") and b"EntityTooLarge" in reply, reply[:300]
    # A chunked body is cut off at the limit, signed or not (an unsigned STS call).
    for h in (head, head.split("Authorization")[0]):
        s = socket.create_connection((u.hostname, u.port), timeout=10)
        s.sendall((h + "Transfer-Encoding: chunked\r\n\r\n").encode())
        chunk = b"x" * 65536
        try:
            for _ in range(20):  # 1.25 MiB, then keep the connection open without finishing the body
                s.sendall(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
        except OSError:
            pass
        reply = _status_line(s)
        assert reply.startswith(b"HTTP/1.1 400") and b"EntityTooLarge" in reply, reply[:300]


def test_inline_policies_share_one_size_quota_per_entity(st):
    # As in IAM, the size limit for inline policies is per user (2048), group (5120) or role (10240), for
    # all of its inline policies together: one policy name after another must not grow it without bound.
    iam = client(st, "iam")
    iam.create_user(UserName="big")
    iam.create_group(GroupName="bigs")

    def doc(n):
        res = [f"arn:aws:s3:::bucket-{i:04d}-{'x' * 20}/*" for i in range(n)]
        return json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": res}]})

    one = doc(17)  # about 940 characters
    assert 800 < len(one.replace(" ", "")) < 1000
    iam.put_user_policy(UserName="big", PolicyName="p1", PolicyDocument=one)
    iam.put_user_policy(UserName="big", PolicyName="p2", PolicyDocument=one)
    assert code_of(iam.put_user_policy, UserName="big", PolicyName="p3", PolicyDocument=one) == "LimitExceeded"
    iam.put_user_policy(UserName="big", PolicyName="p2", PolicyDocument=doc(2))  # replacing one counts only the new one
    iam.put_user_policy(UserName="big", PolicyName="p3", PolicyDocument=doc(2))
    for i in range(5):
        iam.put_group_policy(GroupName="bigs", PolicyName=f"g{i}", PolicyDocument=one)
    assert code_of(iam.put_group_policy, GroupName="bigs", PolicyName="g5", PolicyDocument=one) == "LimitExceeded"
    assert len(iam.list_group_policies(GroupName="bigs")["PolicyNames"]) == 5


def test_the_key_api_cannot_take_an_iam_users_name_or_key(st):
    # The reverse of CreateUser refusing a key's id: a key created through the key API is the principal
    # user/<access key>, so it must not take an IAM user's name; and it must not overwrite an IAM user's
    # key that is inactive (inactive keys are not loaded, but they are still that user's).
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

    iam = client(st, "iam")
    iam.create_user(UserName="frank")
    k = iam.create_access_key(UserName="frank")["AccessKey"]
    iam.update_access_key(UserName="frank", AccessKeyId=k["AccessKeyId"], Status="Inactive")
    assert admin("POST", "keys", {"access_key": k["AccessKeyId"], "admin": True})[0] == 409
    assert admin("GET", f"keys/{k['AccessKeyId']}")[1]["user"] == "frank"
    assert admin("POST", "keys", {"access_key": "Frank"})[0] == 409
    assert admin("POST", "keys", {"access_key": "frank-etl"})[0] == 201


def test_a_policy_arn_must_name_the_policys_own_path(st):
    # Rights on managed policies are delegated by ARN (here: policies under /sandbox/). The ARN a request
    # names is what it is authorized on, so it must be the policy's real ARN: naming a policy under the
    # path one may manage must not reach a policy of the same name elsewhere.
    iam = client(st, "iam")
    everything = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]})
    read = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]})
    admin_p = iam.create_policy(PolicyName="Ops", PolicyDocument=read)["Policy"]
    iam.create_user(UserName="dev")
    iam.attach_user_policy(UserName="dev", PolicyArn=admin_p["Arn"])
    iam.create_user(UserName="sandboxer")
    k = iam.create_access_key(UserName="sandboxer")["AccessKey"]
    iam.put_user_policy(UserName="sandboxer", PolicyName="sandbox", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["iam:CreatePolicy", "iam:GetPolicy", "iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion",
                                       "iam:DeletePolicyVersion", "iam:DeletePolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions"],
         "Resource": f"arn:aws:iam::{ACCOUNT}:policy/sandbox/*"}]}))
    siam = client(st, "iam", k["AccessKeyId"], k["SecretAccessKey"])
    own = until(lambda: siam.create_policy(PolicyName="Mine", Path="/sandbox/", PolicyDocument=read))["Policy"]
    siam.create_policy_version(PolicyArn=own["Arn"], PolicyDocument=read, SetAsDefault=True)
    assert code_of(siam.create_policy_version, PolicyArn=admin_p["Arn"], PolicyDocument=everything) == "AccessDenied"
    forged = f"arn:aws:iam::{ACCOUNT}:policy/sandbox/Ops"
    assert code_of(siam.create_policy_version, PolicyArn=forged, PolicyDocument=everything, SetAsDefault=True) == "NoSuchEntity"
    assert code_of(siam.get_policy, PolicyArn=forged) == "NoSuchEntity"
    assert code_of(siam.get_policy_version, PolicyArn=forged, VersionId="v1") == "NoSuchEntity"
    assert code_of(siam.set_default_policy_version, PolicyArn=forged, VersionId="v1") == "NoSuchEntity"
    assert code_of(siam.delete_policy_version, PolicyArn=forged, VersionId="v1") == "NoSuchEntity"
    assert [v["VersionId"] for v in iam.list_policy_versions(PolicyArn=admin_p["Arn"])["Versions"]] == ["v1"]
    # The admin's own calls too: a wrong path is no such policy, for attachments as well.
    assert code_of(iam.attach_user_policy, UserName="sandboxer", PolicyArn=forged) == "NoSuchEntity"
    assert code_of(iam.detach_user_policy, UserName="dev", PolicyArn=forged) == "NoSuchEntity"
    iam.detach_user_policy(UserName="dev", PolicyArn=admin_p["Arn"])
    assert code_of(siam.delete_policy, PolicyArn=forged) == "NoSuchEntity"
    assert [p["PolicyName"] for p in iam.list_policies(Scope="Local")["Policies"]] == ["Mine", "Ops"]


def test_bucket_policies_name_users_with_a_path_by_their_full_arn(st):
    # A user's ARN includes its path (user/team/dan). A bucket policy that names it must apply to that user:
    # a Deny for user/team/dan that did not apply would leave dan with what his own policy allows. And an
    # ARN with another path is another user.
    iam = client(st, "iam")
    s3 = st.client()
    s3.create_bucket(Bucket="pathed")
    s3.put_object(Bucket="pathed", Key="a.txt", Body=b"a")
    dan = iam.create_user(UserName="dan", Path="/team/")["User"]
    k = iam.create_access_key(UserName="dan")["AccessKey"]
    iam.put_user_policy(UserName="dan", PolicyName="read", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::pathed/*"}]}))
    d3 = s3_as(st, k)
    until(lambda: d3.get_object(Bucket="pathed", Key="a.txt"))
    s3.put_bucket_policy(Bucket="pathed", Policy=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Deny", "Principal": {"AWS": dan["Arn"]}, "Action": "s3:GetObject", "Resource": "arn:aws:s3:::pathed/*"}]}))
    assert denied(d3.get_object, Bucket="pathed", Key="a.txt") == "AccessDenied"
    # Another path is not dan: a Deny for user/other/dan leaves him be, an Allow for it grants him nothing.
    s3.put_bucket_policy(Bucket="pathed", Policy=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Deny", "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:user/other/dan"}, "Action": "s3:GetObject",
         "Resource": "arn:aws:s3:::pathed/*"},
        {"Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:user/other/dan"}, "Action": "s3:PutObject",
         "Resource": "arn:aws:s3:::pathed/*"}]}))
    assert d3.get_object(Bucket="pathed", Key="a.txt")["Body"].read() == b"a"
    assert denied(d3.put_object, Bucket="pathed", Key="b.txt", Body=b"b") == "AccessDenied"
    # Named by its full ARN, an Allow in the bucket policy grants him what his own policy does not.
    s3.put_bucket_policy(Bucket="pathed", Policy=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"AWS": dan["Arn"]}, "Action": "s3:PutObject", "Resource": "arn:aws:s3:::pathed/*"}]}))
    d3.put_object(Bucket="pathed", Key="b.txt", Body=b"b")


def test_managed_policy_names_are_unique_in_any_case(st):
    # As users, groups and roles: Admin and admin would be two policies that read alike in every listing,
    # and attaching "the admin policy" could attach either.
    iam = client(st, "iam")
    read = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]})
    iam.create_policy(PolicyName="Admin", PolicyDocument=read)
    assert code_of(iam.create_policy, PolicyName="admin", PolicyDocument=read) == "EntityAlreadyExists"
    assert code_of(iam.create_policy, PolicyName="ADMIN", Path="/other/", PolicyDocument=read) == "EntityAlreadyExists"
    assert [p["PolicyName"] for p in iam.list_policies(Scope="Local")["Policies"]] == ["Admin"]
