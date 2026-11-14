"""Temporary credentials (STS) through boto3's own STS client: AssumeRole with trust policies, session
policies that narrow a role, GetSessionToken, AssumeRoleWithWebIdentity with an OIDC token (RS256, from
a JWKS file as Cairn publishes it), presigned URLs with session tokens, and every way a session is
refused: no token, an edited token, a token for another key, a deleted role, an untrusted caller or
subject, a forged or expired identity token, STS off."""

import base64
import json
import time
import urllib.request

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from conftest import ADMIN, READER, REGION

ACCOUNT = "000000000000"
STS_SECRET = "sts-secret-" + "x" * 32
ISSUER = "cairn"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def signer(tmp_path_factory):
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    n = key.public_key().public_numbers()
    d = tmp_path_factory.mktemp("jwks")
    jwks = d / "jwks.json"
    jwks.write_text(json.dumps({"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig",
                                          "n": b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")),
                                          "e": b64(n.e.to_bytes(3, "big"))}]}))
    return key, str(jwks)


def jwt(key, sub, aud="cairn-store", iss=ISSUER, exp_in=600, kid="k1"):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}).encode())
    body = b64(json.dumps({"iss": iss, "aud": aud, "sub": sub, "iat": now, "exp": now + exp_in}).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(sig)}"


@pytest.fixture(scope="module")
def st(signer, tmp_path_factory, request):
    import os
    import subprocess
    import tempfile

    from conftest import Store, _free_port

    if os.environ.get("CAIRN_STORE_ENDPOINT"):
        pytest.skip("needs a store this suite starts")
    binary = request.getfixturevalue("binary")
    providers = [{"issuer": ISSUER, "audiences": ["cairn-store"], "jwks_file": signer[1]}]
    d = tempfile.mkdtemp()
    s = Store(binary, os.path.join(d, "store"), _free_port(),
              env={"CAIRN_STORE_STS_SECRET": STS_SECRET, "CAIRN_STORE_STS_PROVIDERS": json.dumps(providers)}).start()
    s3 = s.client()
    for b in ("lake-a", "lake-b"):
        s3.create_bucket(Bucket=b)
        s3.put_object(Bucket=b, Key="x/1.csv", Body=b"one")
        s3.put_object(Bucket=b, Key="y/2.csv", Body=b"two")
    iam = client(s, "iam")
    iam.create_user(UserName="catalog")
    k = iam.create_access_key(UserName="catalog")["AccessKey"]
    s.catalog = (k["AccessKeyId"], k["SecretAccessKey"])
    iam.create_user(UserName="eve")
    k = iam.create_access_key(UserName="eve")["AccessKey"]
    s.eve = (k["AccessKeyId"], k["SecretAccessKey"])
    reads_a = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "arn:aws:s3:::lake-a/*"},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::lake-a"}]}
    trust = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:user/catalog"}, "Action": "sts:AssumeRole"},
        {"Effect": "Allow", "Principal": {"Federated": ISSUER}, "Action": "sts:AssumeRoleWithWebIdentity",
         "Condition": {"StringEquals": {f"{ISSUER}:aud": "cairn-store"}, "StringLike": {f"{ISSUER}:sub": "data-*"}}}]}
    iam.create_role(RoleName="lake-a-rw", AssumeRolePolicyDocument=json.dumps(trust), MaxSessionDuration=7200)
    iam.put_role_policy(RoleName="lake-a-rw", PolicyName="lake-a", PolicyDocument=json.dumps(reads_a))
    yield s
    s.stop(kill=True)
    subprocess.run(["rm", "-rf", d])


def client(store, service, ak=ADMIN[0], sk=ADMIN[1], token=None):
    return boto3.client(service, endpoint_url=store.endpoint, aws_access_key_id=ak, aws_secret_access_key=sk,
                        aws_session_token=token, region_name=REGION,
                        config=Config(retries={"max_attempts": 1}, s3={"addressing_style": "path"}))


def s3_with(store, creds):
    return client(store, "s3", creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"])


def code(fn, *a, **kw):
    with pytest.raises(ClientError) as ei:
        fn(*a, **kw)
    return ei.value.response["Error"]["Code"]


ROLE = f"arn:aws:iam::{ACCOUNT}:role/lake-a-rw"


def test_assume_role_gives_the_roles_permissions_only(st):
    sts = client(st, "sts", *st.catalog)
    out = sts.assume_role(RoleArn=ROLE, RoleSessionName="catalog-1", DurationSeconds=900)
    creds = out["Credentials"]
    assert creds["AccessKeyId"].startswith("ASIA") and out["AssumedRoleUser"]["Arn"].endswith("assumed-role/lake-a-rw/catalog-1")
    s3 = s3_with(st, creds)
    assert s3.get_object(Bucket="lake-a", Key="x/1.csv")["Body"].read() == b"one"
    s3.put_object(Bucket="lake-a", Key="x/new.csv", Body=b"n")
    assert code(s3.get_object, Bucket="lake-b", Key="x/1.csv") == "AccessDenied"
    assert code(s3.create_bucket, Bucket="lake-c") == "AccessDenied"
    who = client(st, "sts", creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"]).get_caller_identity()
    assert who["Arn"] == f"arn:aws:sts::{ACCOUNT}:assumed-role/lake-a-rw/catalog-1"
    # Temporary keys do not manage IAM.
    iam = client(st, "iam", creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"])
    assert code(iam.list_users) == "AccessDenied"
    # Presigned URLs carry the session token.
    url = s3.generate_presigned_url("get_object", Params={"Bucket": "lake-a", "Key": "x/1.csv"}, ExpiresIn=300)
    assert "x-amz-security-token" in url.lower() and urllib.request.urlopen(url).read() == b"one"


def test_a_session_policy_narrows_the_role(st):
    only_x = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::lake-a/x/*"}]}
    creds = client(st, "sts", *st.catalog).assume_role(RoleArn=ROLE, RoleSessionName="table-x", Policy=json.dumps(only_x))["Credentials"]
    s3 = s3_with(st, creds)
    assert s3.get_object(Bucket="lake-a", Key="x/1.csv")["Body"].read() == b"one"
    assert code(s3.get_object, Bucket="lake-a", Key="y/2.csv") == "AccessDenied"     # the role allows it; the session does not
    assert code(s3.put_object, Bucket="lake-a", Key="x/w.csv", Body=b"w") == "AccessDenied"
    # A session policy cannot widen the role.
    everything = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}
    wide = s3_with(st, client(st, "sts", *st.catalog).assume_role(RoleArn=ROLE, RoleSessionName="wide",
                                                                Policy=json.dumps(everything))["Credentials"])
    assert code(wide.get_object, Bucket="lake-b", Key="x/1.csv") == "AccessDenied"


def test_sessions_are_refused_without_a_valid_token(st):
    creds = client(st, "sts", *st.catalog).assume_role(RoleArn=ROLE, RoleSessionName="t1")["Credentials"]
    other = client(st, "sts", *st.catalog).assume_role(RoleArn=ROLE, RoleSessionName="t2")["Credentials"]
    no_token = client(st, "s3", creds["AccessKeyId"], creds["SecretAccessKey"])
    assert code(no_token.get_object, Bucket="lake-a", Key="x/1.csv") == "InvalidAccessKeyId"
    swapped = client(st, "s3", creds["AccessKeyId"], creds["SecretAccessKey"], other["SessionToken"])
    assert code(swapped.get_object, Bucket="lake-a", Key="x/1.csv") == "InvalidToken"
    payload, sig = creds["SessionToken"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["role"] = "admin"
    edited = client(st, "s3", creds["AccessKeyId"], creds["SecretAccessKey"], b64(json.dumps(claims).encode()) + "." + sig)
    assert code(edited.get_object, Bucket="lake-a", Key="x/1.csv") == "InvalidToken"
    wrong_secret = client(st, "s3", creds["AccessKeyId"], "not-the-secret", creds["SessionToken"])
    assert code(wrong_secret.get_object, Bucket="lake-a", Key="x/1.csv") == "SignatureDoesNotMatch"


def test_who_may_assume_a_role(st):
    sts = client(st, "sts", *st.eve)
    assert code(sts.assume_role, RoleArn=ROLE, RoleSessionName="eve") == "AccessDenied"   # not trusted
    assert code(client(st, "sts", *st.catalog).assume_role, RoleArn=f"arn:aws:iam::{ACCOUNT}:role/none",
                RoleSessionName="xx") == "AccessDenied"
    assert code(client(st, "sts", *st.catalog).assume_role, RoleArn=ROLE, RoleSessionName="bad name!") == "ValidationError"
    assert code(client(st, "sts", *st.catalog).assume_role, RoleArn=ROLE, RoleSessionName="long",
                DurationSeconds=10000) == "ValidationError"   # over the role's 7200
    # Trusting the account needs the caller's own policy too.
    iam = client(st, "iam")
    iam.create_role(RoleName="acct", AssumeRolePolicyDocument=json.dumps({"Statement": [
        {"Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"}, "Action": "sts:AssumeRole"}]}))
    assert code(sts.assume_role, RoleArn=f"arn:aws:iam::{ACCOUNT}:role/acct", RoleSessionName="eve") == "AccessDenied"
    iam.put_user_policy(UserName="eve", PolicyName="may-assume", PolicyDocument=json.dumps({"Statement": [
        {"Effect": "Allow", "Action": "sts:AssumeRole", "Resource": f"arn:aws:iam::{ACCOUNT}:role/acct"}]}))
    time.sleep(0.2)
    assert sts.assume_role(RoleArn=f"arn:aws:iam::{ACCOUNT}:role/acct", RoleSessionName="eve")["Credentials"]["AccessKeyId"]
    # Only sts: actions in a trust policy.
    assert code(iam.create_role, RoleName="bad", AssumeRolePolicyDocument=json.dumps({"Statement": [
        {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject"}]})) == "MalformedPolicyDocument"


def test_deleting_a_role_ends_its_sessions(st):
    iam = client(st, "iam")
    trust = {"Statement": [{"Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:user/catalog"}, "Action": "sts:AssumeRole"}]}
    iam.create_role(RoleName="short-lived", AssumeRolePolicyDocument=json.dumps(trust))
    iam.put_role_policy(RoleName="short-lived", PolicyName="p", PolicyDocument=json.dumps({"Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::lake-b/*"}]}))
    creds = client(st, "sts", *st.catalog).assume_role(RoleArn=f"arn:aws:iam::{ACCOUNT}:role/short-lived",
                                                      RoleSessionName="sess")["Credentials"]
    s3 = s3_with(st, creds)
    assert s3.get_object(Bucket="lake-b", Key="x/1.csv")["Body"].read() == b"one"
    assert code(iam.delete_role, RoleName="short-lived") == "DeleteConflict"
    iam.delete_role_policy(RoleName="short-lived", PolicyName="p")
    assert code(s3.get_object, Bucket="lake-b", Key="x/1.csv") == "AccessDenied"   # its policy is gone at once
    iam.delete_role(RoleName="short-lived")
    assert code(s3.get_object, Bucket="lake-b", Key="x/1.csv") == "InvalidToken"
    roles = [r["RoleName"] for r in iam.list_roles()["Roles"]]
    assert "lake-a-rw" in roles and "short-lived" not in roles
    assert iam.get_role(RoleName="lake-a-rw")["Role"]["MaxSessionDuration"] == 7200


def test_get_session_token_keeps_the_keys_limits(st):
    reader = client(st, "sts", *READER).get_session_token(DurationSeconds=900)["Credentials"]
    s3 = s3_with(st, reader)
    assert s3.get_object(Bucket="lake-a", Key="x/1.csv")["Body"].read() == b"one"
    assert code(s3.put_object, Bucket="lake-a", Key="r", Body=b"r") == "AccessDenied"   # read-only stays read-only
    admin = s3_with(st, client(st, "sts").get_session_token()["Credentials"])
    assert code(admin.create_bucket, Bucket="from-a-session") == "AccessDenied"         # a session is never an admin key
    assert code(client(st, "sts", reader["AccessKeyId"], reader["SecretAccessKey"], reader["SessionToken"]).get_session_token) \
        == "AccessDenied"


def test_web_identity(st, signer):
    key, _ = signer
    sts = boto3.client("sts", endpoint_url=st.endpoint, region_name=REGION, config=Config(retries={"max_attempts": 1}))
    ok = sts.assume_role_with_web_identity(RoleArn=ROLE, RoleSessionName="ana", WebIdentityToken=jwt(key, "data-ana"))
    assert ok["SubjectFromWebIdentityToken"] == "data-ana" and ok["Provider"] == ISSUER
    s3 = s3_with(st, ok["Credentials"])
    assert s3.get_object(Bucket="lake-a", Key="x/1.csv")["Body"].read() == b"one"
    assert code(s3.get_object, Bucket="lake-b", Key="x/1.csv") == "AccessDenied"
    # A subject the trust policy does not allow, the wrong audience, an expired, unsigned or foreign token.
    assert code(sts.assume_role_with_web_identity, RoleArn=ROLE, RoleSessionName="bob", WebIdentityToken=jwt(key, "bob")) == "AccessDenied"
    for bad in (jwt(key, "data-ana", aud="trino"), jwt(key, "data-ana", exp_in=-5), jwt(key, "data-ana", iss="elsewhere")):
        assert code(sts.assume_role_with_web_identity, RoleArn=ROLE, RoleSessionName="xx", WebIdentityToken=bad) == "InvalidIdentityToken"
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert code(sts.assume_role_with_web_identity, RoleArn=ROLE, RoleSessionName="xx",
                WebIdentityToken=jwt(other, "data-ana")) == "InvalidIdentityToken"
    # Other STS actions still need a signature.
    req = urllib.request.Request(st.endpoint + "/", data=b"Action=GetSessionToken&Version=2011-06-15",
                                 headers={"content-type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req)
    assert ei.value.code == 403


def test_sts_is_off_without_a_secret(custom_store):
    s = custom_store({})
    assert code(client(s, "sts").get_session_token) == "InvalidAction"
