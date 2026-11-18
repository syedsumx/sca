use std::sync::Arc;

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::Request;
use time::macros::datetime;

use super::*;
use crate::engine::Engine;

// AWS's published example of a SigV4 streaming upload (s3/latest/API/sigv4-streaming.html): the same
// credentials, request and chunk signatures must verify here.
const AK: &str = "AKIAIOSFODNN7EXAMPLE";
const SK: &str = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY";
const SEED: &str = "4f232c4386841ef735655705268965c44a0e4690baa4adea153f7db9fa80a0a9";
const CHUNK1: &str = "ad80c730a21e5b8d04586a2213dd63b9a0e99e0e2307b0ade35a65485a288648";
const CHUNK2: &str = "0055627c9e194cb4542bae2aa5492e3c1575bbb81b612b7d234b86a503ef5497";
const FINAL: &str = "b6c6ea8a5354eaf15b3cb7646744f4275b71ea724fed81ceb9323e279d449df9";

fn server() -> (Arc<Server>, Arc<Engine>, tempfile::TempDir) {
    let creds = vec![Credential { access_key: AK.into(), secret_key: SK.into(), admin: true, ..Default::default() }];
    server_with(creds, None)
}

fn server_with(creds: Vec<Credential>, sts: Option<sts::StsConfig>) -> (Arc<Server>, Arc<Engine>, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let eng = Arc::new(Engine::open(dir.path(), false).unwrap());
    eng.create_bucket("examplebucket").unwrap();
    let mut srv = Server::new(
        eng.clone(),
        Config {
            region: String::new(),
            domain: String::new(),
            credentials: creds,
            max_object: 0,
            block_public: true,
            pdp: None,
            lifecycle_day_secs: 0,
            keyring: Default::default(),
            encrypt_by_default: false,
            sse_c_over_http: false,
            website_domain: String::new(),
            event_targets: vec![],
            event_max_age_hours: 168,
            log_requests: false,
            tls: false,
            backup_root: None,
            replication_targets: vec![],
            tiers: vec![],
            files_basic: false,
            files_basic_http: false,
            cluster: None,
            backup_sinks: vec![],
            remotes: vec![],
            residency: Default::default(),
            kafka: None,
            lifecycle_secs: 3600,
            sts,
        },
    )
    .unwrap();
    srv.set_clock(|| datetime!(2013-05-24 00:00:30 UTC));
    (Arc::new(srv), eng, dir)
}

fn example_request(chunk2_sig: &str) -> Request<Full<Bytes>> {
    let mut body = format!("10000;chunk-signature={CHUNK1}\r\n{}\r\n", "a".repeat(65536));
    body += &format!("400;chunk-signature={chunk2_sig}\r\n{}\r\n", "a".repeat(1024));
    body += &format!("0;chunk-signature={FINAL}\r\n\r\n");
    Request::builder()
        .method("PUT")
        .uri("/examplebucket/chunkObject.txt")
        .header("host", "s3.amazonaws.com")
        .header("content-length", body.len().to_string())
        .header("x-amz-date", "20130524T000000Z")
        .header("x-amz-storage-class", "REDUCED_REDUNDANCY")
        .header("x-amz-content-sha256", auth::STREAMING_SIGNED)
        .header("content-encoding", "aws-chunked")
        .header("x-amz-decoded-content-length", "66560")
        .header("authorization", format!("AWS4-HMAC-SHA256 Credential={AK}/20130524/us-east-1/s3/aws4_request,\
            SignedHeaders=content-encoding;content-length;host;x-amz-content-sha256;x-amz-date;x-amz-decoded-content-length;x-amz-storage-class,\
            Signature={SEED}"))
        .body(Full::new(Bytes::from(body)))
        .unwrap()
}

async fn body_text(r: Resp) -> String {
    String::from_utf8_lossy(&r.into_body().collect().await.unwrap().to_bytes()).into_owned()
}

#[tokio::test(flavor = "multi_thread")]
async fn aws_streaming_example_verifies() {
    let (srv, eng, _dir) = server();
    let r = srv.handle(example_request(CHUNK2)).await.unwrap();
    let status = r.status();
    assert_eq!(status, StatusCode::OK, "{}", body_text(r).await);
    let obj = eng.get_object("examplebucket", "chunkObject.txt").unwrap();
    assert_eq!(obj.size, 66560);
    assert!(!obj.headers.contains_key("content-encoding"), "aws-chunked is not stored as the object's encoding");
    let mut out = vec![];
    std::io::Read::read_to_end(&mut eng.reader(&obj, 0, obj.size, None).unwrap(), &mut out).unwrap();
    assert!(out.iter().all(|&b| b == b'a'));
}

#[tokio::test(flavor = "multi_thread")]
async fn tampered_chunk_is_rejected_and_nothing_stored() {
    let (srv, eng, _dir) = server();
    let r = srv.handle(example_request(&"0".repeat(64))).await.unwrap();
    assert_eq!(r.status(), StatusCode::FORBIDDEN);
    assert!(body_text(r).await.contains("SignatureDoesNotMatch"));
    assert!(matches!(eng.get_object("examplebucket", "chunkObject.txt"), Err(crate::engine::Error::NoSuchKey)));
}

#[tokio::test(flavor = "multi_thread")]
async fn unsigned_requests_are_refused_and_health_is_open() {
    let (srv, _eng, _dir) = server();
    let get = |uri: &str| Request::builder().uri(uri).header("host", "localhost").body(Full::new(Bytes::new())).unwrap();
    assert_eq!(srv.clone().handle(get("/examplebucket/x")).await.unwrap().status(), StatusCode::FORBIDDEN);
    assert_eq!(srv.clone().handle(get("/-/healthy")).await.unwrap().status(), StatusCode::OK);
}

#[test]
fn range_parsing() {
    use handlers::parse_range;
    let ok = |h: &str, size: u64| parse_range(h, size).unwrap();
    assert_eq!(ok("bytes=0-9", 100), Some((0, 10)));
    assert_eq!(ok("bytes=90-", 100), Some((90, 10)));
    assert_eq!(ok("bytes=-10", 100), Some((90, 10)));
    assert_eq!(ok("bytes=95-200", 100), Some((95, 5)));
    assert_eq!(ok("bytes=-500", 100), Some((0, 100)));
    assert_eq!(ok("bytes=5-2", 100), None);
    assert_eq!(ok("items=0-1", 100), None);
    assert_eq!(ok("bytes=0-1,4-5", 100), None);
    assert!(parse_range("bytes=100-", 100).is_err());
    assert!(parse_range("bytes=0-0", 0).is_err());
}

// ------------------------------------------------------------------------------- temporary credentials

fn sts_server() -> (Arc<Server>, Credential, Credential, tempfile::TempDir) {
    let admin = Credential { access_key: "admin".into(), secret_key: "admin-secret".into(), admin: true, ..Default::default() };
    let eve = Credential { access_key: "eve".into(), secret_key: "eve-secret".into(), ..Default::default() };
    let (srv, _eng, dir) = server_with(vec![admin.clone(), eve.clone()], Some(sts::StsConfig::new(&"s".repeat(32), "").unwrap()));
    (srv, admin, eve, dir)
}

fn call(srv: &Server, service: &str, cred: &Credential, form: &[(&str, &str)]) -> (StatusCode, String) {
    let form = form.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
    tokio::task::block_in_place(|| srv.iam_call(service, cred, form, "test"))
}

fn create_role(srv: &Server, admin: &Credential, name: &str, trust: &str, policy: &str) {
    let (st, doc) = call(srv, "iam", admin, &[("Action", "CreateRole"), ("RoleName", name), ("AssumeRolePolicyDocument", trust)]);
    assert_eq!(st, StatusCode::OK, "{doc}");
    let (st, doc) =
        call(srv, "iam", admin, &[("Action", "PutRolePolicy"), ("RoleName", name), ("PolicyName", "p"), ("PolicyDocument", policy)]);
    assert_eq!(st, StatusCode::OK, "{doc}");
}

const ALL_S3: &str = r#"{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:*","Resource":"*"}]}"#;

#[tokio::test(flavor = "multi_thread")]
async fn a_statement_naming_the_caller_does_not_waive_its_own_permission_for_account_trust() {
    // The role trusts the account (callers need their own sts:AssumeRole permission) and denies eve
    // production sessions. Eve has no permission of her own: naming her (to deny her) must not let her in.
    let (srv, admin, eve, _dir) = sts_server();
    let trust = r#"{"Version":"2012-10-17","Statement":[
        {"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::000000000000:root"},"Action":"sts:AssumeRole"},
        {"Effect":"Deny","Principal":{"AWS":"arn:aws:iam::000000000000:user/eve"},"Action":"sts:AssumeRole",
         "Condition":{"StringLike":{"sts:RoleSessionName":"prod-*"}}}]}"#;
    create_role(&srv, &admin, "etl", trust, ALL_S3);
    let arn = "arn:aws:iam::000000000000:role/etl";
    let (st, doc) = call(&srv, "sts", &eve, &[("Action", "AssumeRole"), ("RoleArn", arn), ("RoleSessionName", "dev-1")]);
    assert_eq!(st, StatusCode::FORBIDDEN, "{doc}");
    let (st, _) = call(&srv, "sts", &eve, &[("Action", "AssumeRole"), ("RoleArn", arn), ("RoleSessionName", "prod-1")]);
    assert_eq!(st, StatusCode::FORBIDDEN);
    // A statement that allows eve by name (and whose conditions hold) still lets her in on its own.
    let trust = r#"{"Version":"2012-10-17","Statement":[
        {"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::000000000000:user/eve"},"Action":"sts:AssumeRole",
         "Condition":{"StringLike":{"sts:RoleSessionName":"dev-*"}}}]}"#;
    create_role(&srv, &admin, "dev", trust, ALL_S3);
    let dev = "arn:aws:iam::000000000000:role/dev";
    let (st, doc) = call(&srv, "sts", &eve, &[("Action", "AssumeRole"), ("RoleArn", dev), ("RoleSessionName", "dev-1")]);
    assert_eq!(st, StatusCode::OK, "{doc}");
    let (st, _) = call(&srv, "sts", &eve, &[("Action", "AssumeRole"), ("RoleArn", dev), ("RoleSessionName", "other")]);
    assert_eq!(st, StatusCode::FORBIDDEN);
}

#[tokio::test(flavor = "multi_thread")]
async fn an_iam_user_cannot_take_the_name_of_an_access_key() {
    // A key that is not an IAM user's is the principal arn:aws:iam::000000000000:user/<access key> in trust
    // policies (the catalog's vending role trusts its key so). An IAM user of that name would have that ARN too,
    // and its keys would assume every role that trusts the access key.
    let (srv, admin, eve, _dir) = sts_server();
    let trust = r#"{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::000000000000:user/eve"},"Action":"sts:AssumeRole"}]}"#;
    create_role(&srv, &admin, "eves", trust, ALL_S3);
    for name in ["eve", "EVE"] {
        let (st, doc) = call(&srv, "iam", &admin, &[("Action", "CreateUser"), ("UserName", name)]);
        assert_eq!(st, StatusCode::CONFLICT, "{doc}");
    }
    // The key itself still assumes the role.
    let (st, doc) =
        call(&srv, "sts", &eve, &[("Action", "AssumeRole"), ("RoleArn", "arn:aws:iam::000000000000:role/eves"), ("RoleSessionName", "s1")]);
    assert_eq!(st, StatusCode::OK, "{doc}");
}

fn element<'a>(doc: &'a str, name: &str) -> &'a str {
    let start = doc.find(&format!("<{name}>")).unwrap_or_else(|| panic!("no {name} in {doc}")) + name.len() + 2;
    &doc[start..start + doc[start..].find('<').unwrap()]
}

#[tokio::test(flavor = "multi_thread")]
async fn a_deleted_roles_sessions_stay_ended_when_a_role_of_the_same_name_is_created() {
    use auth::Keys as _;
    let (srv, admin, _eve, _dir) = sts_server();
    let trust = r#"{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::000000000000:user/admin"},"Action":"sts:AssumeRole"}]}"#;
    create_role(&srv, &admin, "etl", trust, ALL_S3);
    let etl = "arn:aws:iam::000000000000:role/etl";
    let (st, doc) = call(&srv, "sts", &admin, &[("Action", "AssumeRole"), ("RoleArn", etl), ("RoleSessionName", "s1")]);
    assert_eq!(st, StatusCode::OK, "{doc}");
    let (ak, token) = (element(&doc, "AccessKeyId").to_string(), element(&doc, "SessionToken").to_string());
    let now = crate::engine::now_ms() as i64 / 1000;
    assert!(srv.keyset().lookup(&ak, &token, now).is_ok());
    // Deleting the role ends its sessions...
    let (st, doc) = call(&srv, "iam", &admin, &[("Action", "DeleteRolePolicy"), ("RoleName", "etl"), ("PolicyName", "p")]);
    assert_eq!(st, StatusCode::OK, "{doc}");
    let (st, doc) = call(&srv, "iam", &admin, &[("Action", "DeleteRole"), ("RoleName", "etl")]);
    assert_eq!(st, StatusCode::OK, "{doc}");
    assert!(srv.keyset().lookup(&ak, &token, now).is_err());
    // ... and a new role that happens to have the same name does not bring them back.
    create_role(&srv, &admin, "etl", trust, ALL_S3);
    assert_eq!(srv.keyset().lookup(&ak, &token, now).unwrap_err().code, "InvalidToken");
}

/// A request's context for `cred`, as the SFTP server builds one for each operation.
fn ctx_as(cred: &Credential, bucket: &str, key: &str) -> Ctx {
    let mut auth = Auth::anonymous();
    auth.cred = cred.clone();
    Ctx {
        method: Method::GET,
        bucket: bucket.into(),
        key: key.into(),
        path: format!("/{bucket}/{key}"),
        query: Default::default(),
        headers: HeaderMap::new(),
        auth,
        content_length: None,
        op: "unknown",
        peer: None,
        request_id: "test".into(),
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_revoked_or_edited_key_is_refused_even_with_a_copy_of_its_credential() {
    let (srv, eng, _dir) = server();
    for b in ["a", "b"] {
        eng.create_bucket(b).unwrap();
    }
    // A key whose policy allows reading bucket a only (its bucket scope is empty: all buckets).
    let policy = serde_json::json!({"Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::a/*"}]});
    let spec = serde_json::json!({"access_key": "narrow", "policy": policy}).to_string();
    srv.keys_api(&Method::POST, &[], spec.as_bytes(), AK).unwrap();
    // An open SFTP session holds a copy of the key's credential.
    let copy = srv.keyset().creds.get("narrow").cloned().unwrap();
    assert!(srv.authorize(&ctx_as(&copy, "a", "x"), "s3:GetObject", "a", "x").await.is_ok());
    assert!(srv.authorize(&ctx_as(&copy, "b", "x"), "s3:GetObject", "b", "x").await.is_err());
    // Deleted: refused from the next request on, never widened to the empty (all-buckets) scope.
    srv.keys_api(&Method::DELETE, &["narrow"], b"", AK).unwrap();
    for (action, bucket) in [("s3:GetObject", "a"), ("s3:GetObject", "b"), ("s3:PutObject", "b"), ("s3:ListBucket", "b")] {
        assert!(srv.authorize(&ctx_as(&copy, bucket, "x"), action, bucket, "x").await.is_err(), "{action} {bucket}");
    }
    // Edited: the key's current scope applies, not the copy's.
    let spec = serde_json::json!({"access_key": "scoped", "buckets": ["a"]}).to_string();
    srv.keys_api(&Method::POST, &[], spec.as_bytes(), AK).unwrap();
    let copy = srv.keyset().creds.get("scoped").cloned().unwrap();
    assert!(srv.authorize(&ctx_as(&copy, "a", "x"), "s3:PutObject", "a", "x").await.is_ok());
    srv.keys_api(&Method::PATCH, &["scoped"], br#"{"read_only": true}"#, AK).unwrap();
    assert!(srv.authorize(&ctx_as(&copy, "a", "x"), "s3:PutObject", "a", "x").await.is_err());
    assert!(srv.authorize(&ctx_as(&copy, "a", "x"), "s3:GetObject", "a", "x").await.is_ok());
    srv.keys_api(&Method::PATCH, &["scoped"], br#"{"buckets": ["b"]}"#, AK).unwrap();
    assert!(srv.authorize(&ctx_as(&copy, "a", "x"), "s3:GetObject", "a", "x").await.is_err());
}

#[test]
fn iam_user_names_and_access_keys_never_share_a_name() {
    let (srv, _eng, _dir) = server();
    let admin = srv.keyset().creds.get(AK).cloned().unwrap();
    let iam = |action: &str, user: &str| {
        let form = BTreeMap::from([("Action".to_string(), action.to_string()), ("UserName".to_string(), user.to_string())]);
        srv.iam_call("iam", &admin, form, "test").0
    };
    // A policy's user/<name> matches an access key or an IAM user by that name: a new user may not take a key's
    // name (in any case), whether the key is configured or made through the admin API.
    srv.keys_api(&Method::POST, &[], br#"{"access_key": "svc"}"#, AK).unwrap();
    for name in ["svc", "SVC", AK, &AK.to_lowercase()] {
        assert_eq!(iam("CreateUser", name), StatusCode::CONFLICT, "{name}");
    }
    // Nor may a new access key take a user's name.
    assert_eq!(iam("CreateUser", "alice"), StatusCode::OK);
    for name in ["alice", "Alice"] {
        let spec = format!(r#"{{"access_key": "{name}"}}"#);
        assert_eq!(srv.keys_api(&Method::POST, &[], spec.as_bytes(), AK).unwrap_err().status, 409, "{name}");
    }
    assert!(srv.keys_api(&Method::POST, &[], br#"{"access_key": "bob"}"#, AK).is_ok());
}
