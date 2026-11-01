//! An AWS IAM-compatible API (and STS GetCallerIdentity), so IAM tools and SDKs (`aws iam`, boto3,
//! Terraform's aws provider) manage the store's users, access keys and policies.
//!
//! Requests are the IAM Query API: `POST /` with a form body (`Action=CreateUser&UserName=...`), signed
//! with SigV4 for the `iam` (or `sts`) service. The caller is an admin key, or a key whose policy allows
//! the `iam:<Action>` on the entity's ARN (`arn:aws:iam::000000000000:user/<name>`, `group/`, `policy/`).
//!
//! Users, groups and managed policies are one document in the index; access keys are ordinary stored
//! keys that name their user. Every change (with its audit entry, and the keys it creates or deletes) is
//! one index transaction. A user's keys get the user's effective policy: its inline and attached
//! policies and those of its groups, evaluated as IAM evaluates identity policies (an explicit Deny wins;
//! otherwise something must Allow). A user with no policies may do nothing. Bucket policies can name an
//! IAM user as a principal by its ARN. Inactive keys are refused like unknown ones.

use std::collections::{BTreeMap, BTreeSet};

use hyper::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::keys::StoredKey;
use super::policy;
use super::xml::{escape, Writer};
use super::Server;

pub const ACCOUNT: &str = "000000000000";
const IAM_NS: &str = "https://iam.amazonaws.com/doc/2010-05-08/";
const STS_NS: &str = "https://sts.amazonaws.com/doc/2011-06-15/";
const MAX_KEYS_PER_USER: usize = 2;
const MAX_VERSIONS: usize = 5;
const MAX_ATTACHED: usize = 20;

/// The IAM document.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct IamDoc {
    #[serde(default)]
    pub users: BTreeMap<String, User>,
    #[serde(default)]
    pub groups: BTreeMap<String, Group>,
    #[serde(default)]
    pub policies: BTreeMap<String, ManagedPolicy>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct User {
    pub id: String,
    pub path: String,
    pub created: String,
    #[serde(default)]
    pub inline: BTreeMap<String, Value>,
    #[serde(default)]
    pub attached: BTreeSet<String>,
    #[serde(default)]
    pub groups: BTreeSet<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Group {
    pub id: String,
    pub path: String,
    pub created: String,
    #[serde(default)]
    pub inline: BTreeMap<String, Value>,
    #[serde(default)]
    pub attached: BTreeSet<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct PolicyVersion {
    pub doc: Value,
    pub created: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct ManagedPolicy {
    pub id: String,
    pub path: String,
    pub description: String,
    pub created: String,
    pub updated: String,
    pub default_version: u32,
    pub next_version: u32,
    pub versions: BTreeMap<u32, PolicyVersion>,
}

pub fn user_arn(path: &str, name: &str) -> String {
    format!("arn:aws:iam::{ACCOUNT}:user{path}{name}")
}

fn group_arn(path: &str, name: &str) -> String {
    format!("arn:aws:iam::{ACCOUNT}:group{path}{name}")
}

fn policy_arn(path: &str, name: &str) -> String {
    format!("arn:aws:iam::{ACCOUNT}:policy{path}{name}")
}

impl IamDoc {
    pub fn parse(raw: Option<&[u8]>) -> Result<IamDoc, String> {
        match raw {
            None => Ok(IamDoc::default()),
            Some(b) => serde_json::from_slice(b).map_err(|e| format!("IAM document: {e}")),
        }
    }

    fn attachments(&self, policy: &str) -> usize {
        self.users.values().filter(|u| u.attached.contains(policy)).count()
            + self.groups.values().filter(|g| g.attached.contains(policy)).count()
    }

    fn managed_doc(&self, name: &str) -> Option<&Value> {
        let p = self.policies.get(name)?;
        p.versions.get(&p.default_version).map(|v| &v.doc)
    }

    /// The statements that apply to a user: inline, attached, and those of its groups.
    pub fn statements(&self, user: &str) -> Vec<Value> {
        let Some(u) = self.users.get(user) else { return vec![] };
        let mut docs: Vec<&Value> = u.inline.values().collect();
        docs.extend(u.attached.iter().filter_map(|p| self.managed_doc(p)));
        for g in u.groups.iter().filter_map(|g| self.groups.get(g)) {
            docs.extend(g.inline.values());
            docs.extend(g.attached.iter().filter_map(|p| self.managed_doc(p)));
        }
        let mut out = vec![];
        for d in docs {
            match &d["Statement"] {
                Value::Array(a) => out.extend(a.iter().cloned()),
                o @ Value::Object(_) => out.push(o.clone()),
                _ => {}
            }
        }
        out
    }

    /// The policy a user's keys carry (no statements: may do nothing).
    pub fn effective_policy(&self, user: &str) -> policy::Policy {
        let st = self.statements(user);
        if st.is_empty() {
            return policy::Policy::default();
        }
        let doc = serde_json::json!({"Version": "2012-10-17", "Statement": st});
        policy::Policy::parse(doc.to_string().as_bytes(), None).unwrap_or_default()
    }
}

/// An IAM error response.
pub struct IamError {
    status: StatusCode,
    code: &'static str,
    message: String,
}

fn fail(status: StatusCode, code: &'static str, message: impl Into<String>) -> IamError {
    IamError { status, code, message: message.into() }
}

fn no_such(what: &str, name: &str) -> IamError {
    fail(StatusCode::NOT_FOUND, "NoSuchEntity", format!("The {what} with name {name} cannot be found."))
}

fn exists(what: &str, name: &str) -> IamError {
    fail(StatusCode::CONFLICT, "EntityAlreadyExists", format!("{what} with name {name} already exists."))
}

fn conflict(msg: impl Into<String>) -> IamError {
    fail(StatusCode::CONFLICT, "DeleteConflict", msg)
}

fn invalid(msg: impl Into<String>) -> IamError {
    fail(StatusCode::BAD_REQUEST, "ValidationError", msg)
}

fn limit(msg: impl Into<String>) -> IamError {
    fail(StatusCode::CONFLICT, "LimitExceeded", msg)
}

impl From<crate::engine::Error> for IamError {
    fn from(e: crate::engine::Error) -> IamError {
        tracing::error!(error = %e, "IAM");
        fail(StatusCode::INTERNAL_SERVER_ERROR, "ServiceFailure", "The request could not be completed.")
    }
}

/// Is the request an IAM or STS call (by its SigV4 credential scope)?
pub fn service_of(headers: &hyper::HeaderMap) -> Option<&'static str> {
    let a = headers.get("authorization")?.to_str().ok()?;
    let cred = a.strip_prefix("AWS4-HMAC-SHA256 ")?.split(',').find_map(|p| p.trim().strip_prefix("Credential="))?;
    match cred.split('/').nth(3)? {
        "iam" => Some("iam"),
        "sts" => Some("sts"),
        _ => None,
    }
}

struct Form(BTreeMap<String, String>);

impl Form {
    fn get(&self, k: &str) -> &str {
        self.0.get(k).map(String::as_str).unwrap_or("")
    }

    fn req(&self, k: &str) -> Result<&str, IamError> {
        match self.get(k) {
            "" => Err(invalid(format!(
                "1 validation error detected: Value null at '{k}' failed to satisfy constraint: Member must not be null"
            ))),
            v => Ok(v),
        }
    }

    fn name(&self, k: &str, max: usize) -> Result<&str, IamError> {
        let v = self.req(k)?;
        if v.len() > max || !v.chars().all(|c| c.is_ascii_alphanumeric() || "+=,.@_-".contains(c)) {
            return Err(invalid(format!("The specified value for {k} is invalid.")));
        }
        Ok(v)
    }

    fn path(&self) -> Result<String, IamError> {
        match self.get("Path") {
            "" => Ok("/".into()),
            p if p.starts_with('/') && p.ends_with('/') && p.len() <= 512 && p.chars().all(|c| c.is_ascii_graphic()) => Ok(p.into()),
            _ => Err(invalid("The specified value for path is invalid. It must begin and end with / and contain only printable ASCII.")),
        }
    }

    /// A policy document (checked as the store checks key policies), within IAM's size limit.
    fn document(&self, k: &str, max: usize) -> Result<Value, IamError> {
        let raw = self.req(k)?;
        if raw.chars().filter(|c| !c.is_whitespace()).count() > max {
            return Err(limit(format!("Cannot exceed quota for PolicySize: {max}")));
        }
        let v: Value =
            serde_json::from_str(raw).map_err(|_| fail(StatusCode::BAD_REQUEST, "MalformedPolicyDocument", "Syntax errors in policy."))?;
        policy::Policy::parse(raw.as_bytes(), None)
            .map_err(|e| fail(StatusCode::BAD_REQUEST, "MalformedPolicyDocument", e.message.to_string()))?;
        Ok(v)
    }

    fn policy_name_from_arn(&self) -> Result<String, IamError> {
        let arn = self.req("PolicyArn")?;
        arn.strip_prefix(&format!("arn:aws:iam::{ACCOUNT}:policy/"))
            .and_then(|r| r.rsplit('/').next())
            .filter(|n| !n.is_empty())
            .map(str::to_string)
            .ok_or_else(|| fail(StatusCode::BAD_REQUEST, "InvalidInput", format!("ARN {arn} is not valid.")))
    }

    fn version_id(&self) -> Result<u32, IamError> {
        self.req("VersionId")?.strip_prefix('v').and_then(|n| n.parse().ok()).ok_or_else(|| invalid("Invalid VersionId."))
    }

    fn max_items(&self) -> Result<usize, IamError> {
        match self.get("MaxItems") {
            "" => Ok(100),
            n => n.parse().ok().filter(|n| (1..=1000).contains(n)).ok_or_else(|| invalid("MaxItems must be 1 to 1000.")),
        }
    }
}

fn now() -> String {
    super::iso_time(crate::engine::now_ms())
}

fn new_id(prefix: &str) -> String {
    const A: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    let r: [u8; 17] = rand::random();
    format!("{prefix}{}", r.iter().map(|b| A[(*b as usize) % 32] as char).collect::<String>())
}

/// One page of names after `Marker`.
type Page<'a, T> = (Vec<(&'a String, T)>, Option<String>);

fn page<'a, T>(items: impl Iterator<Item = (&'a String, T)>, f: &Form) -> Result<Page<'a, T>, IamError> {
    let max = f.max_items()?;
    let marker = f.get("Marker");
    let mut out: Vec<(&String, T)> = items.filter(|(k, _)| marker.is_empty() || k.as_str() > marker).collect();
    let next = if out.len() > max {
        out.truncate(max);
        out.last().map(|(k, _)| k.to_string())
    } else {
        None
    };
    Ok((out, next))
}

fn truncation(w: &mut Writer, next: Option<String>) {
    w.el("IsTruncated", if next.is_some() { "true" } else { "false" });
    if let Some(m) = next {
        w.el("Marker", m);
    }
}

fn write_user(w: &mut Writer, name: &str, u: &User) {
    w.el("Path", &u.path).el("UserName", name).el("UserId", &u.id).el("Arn", user_arn(&u.path, name)).el("CreateDate", &u.created);
}

fn write_group(w: &mut Writer, name: &str, g: &Group) {
    w.el("Path", &g.path).el("GroupName", name).el("GroupId", &g.id).el("Arn", group_arn(&g.path, name)).el("CreateDate", &g.created);
}

fn write_policy(w: &mut Writer, doc: &IamDoc, name: &str, p: &ManagedPolicy) {
    w.el("PolicyName", name)
        .el("PolicyId", &p.id)
        .el("Arn", policy_arn(&p.path, name))
        .el("Path", &p.path)
        .el("DefaultVersionId", format!("v{}", p.default_version))
        .el("AttachmentCount", doc.attachments(name).to_string())
        .el("PermissionsBoundaryUsageCount", "0")
        .el("IsAttachable", "true")
        .opt("Description", &p.description)
        .el("CreateDate", &p.created)
        .el("UpdateDate", &p.updated);
}

fn write_version(w: &mut Writer, p: &ManagedPolicy, n: u32, v: &PolicyVersion, with_doc: bool) {
    if with_doc {
        w.el("Document", super::auth::aws_encode(v.doc.to_string().as_bytes(), false));
    }
    w.el("VersionId", format!("v{n}"))
        .el("IsDefaultVersion", if n == p.default_version { "true" } else { "false" })
        .el("CreateDate", &v.created);
}

fn encoded(doc: &Value) -> String {
    super::auth::aws_encode(doc.to_string().as_bytes(), false)
}

/// What a change does besides rewriting the document.
#[derive(Default)]
pub struct Change {
    pub keys: Vec<(String, Option<StoredKey>)>,
}

impl Server {
    /// Handles an IAM or STS request (already authenticated as `cred`).
    pub(crate) fn iam_call(
        &self,
        service: &str,
        cred: &super::auth::Credential,
        form: BTreeMap<String, String>,
        request_id: &str,
    ) -> (StatusCode, String) {
        let f = Form(form);
        let action = f.get("Action").to_string();
        let ns = if service == "sts" { STS_NS } else { IAM_NS };
        let result = if service == "sts" {
            match action.as_str() {
                "GetCallerIdentity" => Ok(self.caller_identity(cred)),
                _ => {
                    Err(fail(StatusCode::BAD_REQUEST, "InvalidAction", format!("Could not find operation {action} for version 2011-06-15")))
                }
            }
        } else {
            self.iam_action(&action, cred, &f)
        };
        match result {
            Ok(inner) => {
                let mut out = format!("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<{action}Response xmlns=\"{ns}\">");
                if !inner.is_empty() {
                    out.push_str(&format!("<{action}Result>{inner}</{action}Result>"));
                }
                out.push_str(&format!("<ResponseMetadata><RequestId>{request_id}</RequestId></ResponseMetadata></{action}Response>"));
                (StatusCode::OK, out)
            }
            Err(e) => (
                e.status,
                format!(
                    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<ErrorResponse xmlns=\"{ns}\"><Error><Type>Sender</Type><Code>{}</Code><Message>{}</Message></Error><RequestId>{request_id}</RequestId></ErrorResponse>",
                    e.code,
                    escape(&e.message)
                ),
            ),
        }
    }

    fn caller_identity(&self, cred: &super::auth::Credential) -> String {
        let doc = self.iam_doc();
        let (arn, id) = match doc.users.get(&cred.user) {
            Some(u) if !cred.user.is_empty() => (user_arn(&u.path, &cred.user), u.id.clone()),
            _ => (user_arn("/", &cred.access_key), cred.access_key.clone()),
        };
        format!("<Arn>{}</Arn><UserId>{}</UserId><Account>{ACCOUNT}</Account>", escape(&arn), escape(&id))
    }

    /// The current IAM document (empty when unreadable: callers that change it re-read it in their transaction).
    pub(crate) fn iam_doc(&self) -> IamDoc {
        self.eng.iam_doc().ok().and_then(|raw| IamDoc::parse(raw.as_deref()).ok()).unwrap_or_default()
    }

    /// May `cred` run `action` on `resource`?
    fn iam_allowed(&self, cred: &super::auth::Credential, action: &str, resource: &str) -> bool {
        if cred.admin {
            return true;
        }
        let ks = self.keyset();
        let Some(p) = ks.policies.get(&cred.access_key) else { return false };
        let ctx = BTreeMap::new();
        let r =
            policy::Request { action: &format!("iam:{action}"), resource, principal: &cred.access_key, user: &cred.user, context: &ctx };
        p.evaluate(&r) == Some(policy::Effect::Allow)
    }

    /// Changes the IAM document (and keys) in one transaction with its audit entry.
    fn iam_change<T>(
        &self,
        cred: &super::auth::Credential,
        action: &str,
        subject: &str,
        f: impl FnOnce(&mut IamDoc, &BTreeMap<String, StoredKey>, &mut Change) -> Result<T, IamError>,
    ) -> Result<T, IamError> {
        let audit = crate::audit::Audit {
            category: "store",
            action: format!("iam.{action}"),
            actor: cred.access_key.clone(),
            subject: subject.to_string(),
            details: serde_json::json!({}),
        };
        let mut change = Change::default();
        let out = self.eng.iam_update(|raw, keys| {
            let mut doc = IamDoc::parse(raw).map_err(|e| fail(StatusCode::INTERNAL_SERVER_ERROR, "ServiceFailure", e))?;
            let stored: BTreeMap<String, StoredKey> =
                keys.iter().filter_map(|(k, v)| Some((k.clone(), serde_json::from_slice(v).ok()?))).collect();
            let t = f(&mut doc, &stored, &mut change)?;
            let raw = serde_json::to_vec(&doc).map_err(|_| fail(StatusCode::INTERNAL_SERVER_ERROR, "ServiceFailure", "encode"))?;
            let keys: Vec<(String, Option<Vec<u8>>)> =
                change.keys.iter().map(|(ak, k)| (ak.clone(), k.as_ref().map(|k| serde_json::to_vec(k).unwrap_or_default()))).collect();
            Ok::<_, IamError>((raw, keys, audit, t))
        })??;
        if let Err(err) = self.reload_keys() {
            tracing::error!(error = %err, "key reload after an IAM change");
        }
        Ok(out)
    }

    fn iam_action(&self, action: &str, cred: &super::auth::Credential, f: &Form) -> Result<String, IamError> {
        let deny =
            || fail(StatusCode::FORBIDDEN, "AccessDenied", format!("User: {} is not authorized to perform: iam:{action}", cred.access_key));
        let doc = self.iam_doc();
        // The resource the action is on, for authorization.
        let user_res = |n: &str| user_arn(doc.users.get(n).map_or("/", |u| u.path.as_str()), n);
        let resource = match action {
            // A new user's ARN has the path it asks for.
            "CreateUser" => user_arn(&f.path()?, f.get("UserName")),
            "GetUser"
            | "DeleteUser"
            | "CreateAccessKey"
            | "ListAccessKeys"
            | "UpdateAccessKey"
            | "DeleteAccessKey"
            | "PutUserPolicy"
            | "GetUserPolicy"
            | "DeleteUserPolicy"
            | "ListUserPolicies"
            | "AttachUserPolicy"
            | "DetachUserPolicy"
            | "ListAttachedUserPolicies"
            | "ListGroupsForUser" => {
                let n = match f.get("UserName") {
                    "" => cred.user.clone(),
                    n => n.to_string(),
                };
                user_res(&n)
            }
            "CreateGroup"
            | "GetGroup"
            | "DeleteGroup"
            | "AddUserToGroup"
            | "RemoveUserFromGroup"
            | "PutGroupPolicy"
            | "GetGroupPolicy"
            | "DeleteGroupPolicy"
            | "ListGroupPolicies"
            | "AttachGroupPolicy"
            | "DetachGroupPolicy"
            | "ListAttachedGroupPolicies" => {
                let n = f.get("GroupName");
                group_arn(doc.groups.get(n).map_or("/", |g| g.path.as_str()), n)
            }
            "CreatePolicy" => policy_arn(&f.path()?, f.get("PolicyName")),
            "GetPolicy"
            | "DeletePolicy"
            | "GetPolicyVersion"
            | "CreatePolicyVersion"
            | "DeletePolicyVersion"
            | "ListPolicyVersions"
            | "SetDefaultPolicyVersion" => f.get("PolicyArn").to_string(),
            _ => "*".to_string(),
        };
        if !self.iam_allowed(cred, action, &resource) {
            return Err(deny());
        }
        let mut w = Writer::fragment();
        match action {
            // ------------------------------------------------------------------------------- users
            "CreateUser" => {
                let name = f.name("UserName", 64)?.to_string();
                let path = f.path()?;
                let u = self.iam_change(cred, action, &name, |d, _, _| {
                    if d.users.keys().any(|k| k.eq_ignore_ascii_case(&name)) {
                        return Err(exists("User", &name));
                    }
                    let u = User { id: new_id("AIDA"), path, created: now(), ..Default::default() };
                    d.users.insert(name.clone(), u.clone());
                    Ok(u)
                })?;
                w.open("User");
                write_user(&mut w, &name, &u);
                w.close();
            }
            "GetUser" => {
                let name = match f.get("UserName") {
                    "" if !cred.user.is_empty() => cred.user.clone(),
                    "" => return Err(invalid("UserName is required for keys that are not IAM users.")),
                    n => n.to_string(),
                };
                let u = doc.users.get(&name).ok_or_else(|| no_such("user", &name))?;
                w.open("User");
                write_user(&mut w, &name, u);
                w.close();
            }
            "ListUsers" => {
                let prefix = match f.get("PathPrefix") {
                    "" => "/",
                    p => p,
                };
                let (items, next) = page(doc.users.iter().filter(|(_, u)| u.path.starts_with(prefix)), f)?;
                w.open("Users");
                for (n, u) in items {
                    w.open("member");
                    write_user(&mut w, n, u);
                    w.close();
                }
                w.close();
                truncation(&mut w, next);
            }
            "DeleteUser" => {
                let name = f.name("UserName", 64)?.to_string();
                self.iam_change(cred, action, &name, |d, keys, _| {
                    let u = d.users.get(&name).ok_or_else(|| no_such("user", &name))?;
                    if keys.values().any(|k| k.user == name) {
                        return Err(conflict("Cannot delete entity, must delete access keys first."));
                    }
                    if !u.inline.is_empty() {
                        return Err(conflict("Cannot delete entity, must delete policies first."));
                    }
                    if !u.attached.is_empty() {
                        return Err(conflict("Cannot delete entity, must detach all policies first."));
                    }
                    if !u.groups.is_empty() {
                        return Err(conflict("Cannot delete entity, must remove users from group first."));
                    }
                    d.users.remove(&name);
                    Ok(())
                })?;
            }
            // --------------------------------------------------------------------------- access keys
            "CreateAccessKey" => {
                let name = match f.get("UserName") {
                    "" if !cred.user.is_empty() => cred.user.clone(),
                    "" => return Err(invalid("UserName is required for keys that are not IAM users.")),
                    n => n.to_string(),
                };
                let ak = format!("AKIA{}", &new_id("")[..16]);
                let mut k = StoredKey {
                    access_key: ak.clone(),
                    user: name.clone(),
                    created: now(),
                    created_by: cred.access_key.clone(),
                    ..Default::default()
                };
                let secret = self.new_secret(&mut k);
                let created = k.created.clone();
                self.iam_change(cred, action, &name, |d, keys, ch| {
                    if !d.users.contains_key(&name) {
                        return Err(no_such("user", &name));
                    }
                    if keys.values().filter(|k| k.user == name).count() >= MAX_KEYS_PER_USER {
                        return Err(limit(format!("Cannot exceed quota for AccessKeysPerUser: {MAX_KEYS_PER_USER}")));
                    }
                    if keys.contains_key(&ak) || self.keyset().fixed.contains(&ak) {
                        return Err(fail(StatusCode::INTERNAL_SERVER_ERROR, "ServiceFailure", "Try again."));
                    }
                    ch.keys.push((ak.clone(), Some(k)));
                    Ok(())
                })?;
                w.open("AccessKey")
                    .el("UserName", &name)
                    .el("AccessKeyId", &ak)
                    .el("Status", "Active")
                    .el("SecretAccessKey", &secret)
                    .el("CreateDate", &created)
                    .close();
            }
            "ListAccessKeys" => {
                let name = match f.get("UserName") {
                    "" if !cred.user.is_empty() => cred.user.clone(),
                    "" => return Err(invalid("UserName is required for keys that are not IAM users.")),
                    n => n.to_string(),
                };
                if !doc.users.contains_key(&name) {
                    return Err(no_such("user", &name));
                }
                w.open("AccessKeyMetadata");
                for k in self.stored_keys()?.into_iter().filter(|k| k.user == name) {
                    w.open("member")
                        .el("UserName", &name)
                        .el("AccessKeyId", &k.access_key)
                        .el("Status", if k.inactive { "Inactive" } else { "Active" })
                        .el("CreateDate", &k.created)
                        .close();
                }
                w.close();
                w.el("IsTruncated", "false");
            }
            "UpdateAccessKey" | "DeleteAccessKey" => {
                let ak = f.req("AccessKeyId")?.to_string();
                let status = match (action, f.get("Status")) {
                    ("DeleteAccessKey", _) => None,
                    (_, "Active") => Some(false),
                    (_, "Inactive") => Some(true),
                    _ => return Err(invalid("Status must be Active or Inactive.")),
                };
                if ak == cred.access_key && status != Some(false) {
                    return Err(conflict("A key cannot disable or delete itself."));
                }
                let user = f.get("UserName").to_string();
                self.iam_change(cred, action, &ak, |_, keys, ch| {
                    let k = keys.get(&ak).filter(|k| !k.user.is_empty() && (user.is_empty() || k.user == user));
                    let mut k = k.cloned().ok_or_else(|| no_such("access key", &ak))?;
                    match status {
                        None => ch.keys.push((ak.clone(), None)),
                        Some(off) => {
                            k.inactive = off;
                            ch.keys.push((ak.clone(), Some(k)));
                        }
                    }
                    Ok(())
                })?;
            }
            // ------------------------------------------------------------------ inline policies
            "PutUserPolicy" | "PutGroupPolicy" => {
                let user = action == "PutUserPolicy";
                let owner = f.name(if user { "UserName" } else { "GroupName" }, 128)?.to_string();
                let pname = f.name("PolicyName", 128)?.to_string();
                let pdoc = f.document("PolicyDocument", if user { 2048 } else { 5120 })?;
                self.iam_change(cred, action, &owner, |d, _, _| {
                    let inline = if user {
                        &mut d.users.get_mut(&owner).ok_or_else(|| no_such("user", &owner))?.inline
                    } else {
                        &mut d.groups.get_mut(&owner).ok_or_else(|| no_such("group", &owner))?.inline
                    };
                    inline.insert(pname, pdoc);
                    Ok(())
                })?;
            }
            "GetUserPolicy" | "GetGroupPolicy" => {
                let user = action == "GetUserPolicy";
                let field = if user { "UserName" } else { "GroupName" };
                let owner = f.req(field)?;
                let pname = f.req("PolicyName")?;
                let inline = if user {
                    &doc.users.get(owner).ok_or_else(|| no_such("user", owner))?.inline
                } else {
                    &doc.groups.get(owner).ok_or_else(|| no_such("group", owner))?.inline
                };
                let p = inline.get(pname).ok_or_else(|| no_such("policy", pname))?;
                w.el(field, owner).el("PolicyName", pname).el("PolicyDocument", encoded(p));
            }
            "DeleteUserPolicy" | "DeleteGroupPolicy" => {
                let user = action == "DeleteUserPolicy";
                let owner = f.req(if user { "UserName" } else { "GroupName" })?.to_string();
                let pname = f.req("PolicyName")?.to_string();
                self.iam_change(cred, action, &owner, |d, _, _| {
                    let inline = if user {
                        &mut d.users.get_mut(&owner).ok_or_else(|| no_such("user", &owner))?.inline
                    } else {
                        &mut d.groups.get_mut(&owner).ok_or_else(|| no_such("group", &owner))?.inline
                    };
                    inline.remove(&pname).ok_or_else(|| no_such("policy", &pname))?;
                    Ok(())
                })?;
            }
            "ListUserPolicies" | "ListGroupPolicies" => {
                let user = action == "ListUserPolicies";
                let owner = f.req(if user { "UserName" } else { "GroupName" })?;
                let inline = if user {
                    &doc.users.get(owner).ok_or_else(|| no_such("user", owner))?.inline
                } else {
                    &doc.groups.get(owner).ok_or_else(|| no_such("group", owner))?.inline
                };
                let (items, next) = page(inline.iter(), f)?;
                w.open("PolicyNames");
                for (n, _) in items {
                    w.el("member", n);
                }
                w.close();
                truncation(&mut w, next);
            }
            // ---------------------------------------------------------------------------- groups
            "CreateGroup" => {
                let name = f.name("GroupName", 128)?.to_string();
                let path = f.path()?;
                let g = self.iam_change(cred, action, &name, |d, _, _| {
                    if d.groups.keys().any(|k| k.eq_ignore_ascii_case(&name)) {
                        return Err(exists("Group", &name));
                    }
                    let g = Group { id: new_id("AGPA"), path, created: now(), ..Default::default() };
                    d.groups.insert(name.clone(), g.clone());
                    Ok(g)
                })?;
                w.open("Group");
                write_group(&mut w, &name, &g);
                w.close();
            }
            "GetGroup" => {
                let name = f.req("GroupName")?;
                let g = doc.groups.get(name).ok_or_else(|| no_such("group", name))?;
                w.open("Group");
                write_group(&mut w, name, g);
                w.close();
                let (items, next) = page(doc.users.iter().filter(|(_, u)| u.groups.contains(name)), f)?;
                w.open("Users");
                for (n, u) in items {
                    w.open("member");
                    write_user(&mut w, n, u);
                    w.close();
                }
                w.close();
                truncation(&mut w, next);
            }
            "ListGroups" | "ListGroupsForUser" => {
                let groups: Vec<(&String, &Group)> = if action == "ListGroups" {
                    let prefix = match f.get("PathPrefix") {
                        "" => "/",
                        p => p,
                    };
                    doc.groups.iter().filter(|(_, g)| g.path.starts_with(prefix)).collect()
                } else {
                    let n = f.req("UserName")?;
                    let u = doc.users.get(n).ok_or_else(|| no_such("user", n))?;
                    doc.groups.iter().filter(|(k, _)| u.groups.contains(*k)).collect()
                };
                let (items, next) = page(groups.into_iter(), f)?;
                w.open("Groups");
                for (n, g) in items {
                    w.open("member");
                    write_group(&mut w, n, g);
                    w.close();
                }
                w.close();
                truncation(&mut w, next);
            }
            "DeleteGroup" => {
                let name = f.req("GroupName")?.to_string();
                self.iam_change(cred, action, &name, |d, _, _| {
                    let g = d.groups.get(&name).ok_or_else(|| no_such("group", &name))?;
                    if d.users.values().any(|u| u.groups.contains(&name)) {
                        return Err(conflict("Cannot delete entity, must remove users from group first."));
                    }
                    if !g.inline.is_empty() || !g.attached.is_empty() {
                        return Err(conflict("Cannot delete entity, must delete or detach policies first."));
                    }
                    d.groups.remove(&name);
                    Ok(())
                })?;
            }
            "AddUserToGroup" | "RemoveUserFromGroup" => {
                let group = f.req("GroupName")?.to_string();
                let user = f.req("UserName")?.to_string();
                let add = action == "AddUserToGroup";
                self.iam_change(cred, action, &user, |d, _, _| {
                    if !d.groups.contains_key(&group) {
                        return Err(no_such("group", &group));
                    }
                    let u = d.users.get_mut(&user).ok_or_else(|| no_such("user", &user))?;
                    if add {
                        u.groups.insert(group.clone());
                    } else if !u.groups.remove(&group) {
                        return Err(no_such("user in group", &user));
                    }
                    Ok(())
                })?;
            }
            // ------------------------------------------------------------------ managed policies
            "CreatePolicy" => {
                let name = f.name("PolicyName", 128)?.to_string();
                let path = f.path()?;
                let pdoc = f.document("PolicyDocument", 6144)?;
                let description = f.get("Description").to_string();
                if description.len() > 1000 {
                    return Err(invalid("Description is at most 1000 characters."));
                }
                let (p, attachments) = self.iam_change(cred, action, &name, |d, _, _| {
                    if d.policies.contains_key(&name) {
                        return Err(exists("A policy", &name));
                    }
                    let t = now();
                    let mut p = ManagedPolicy {
                        id: new_id("ANPA"),
                        path,
                        description,
                        created: t.clone(),
                        updated: t.clone(),
                        default_version: 1,
                        next_version: 2,
                        ..Default::default()
                    };
                    p.versions.insert(1, PolicyVersion { doc: pdoc, created: t });
                    d.policies.insert(name.clone(), p.clone());
                    Ok((p, d.clone()))
                })?;
                w.open("Policy");
                write_policy(&mut w, &attachments, &name, &p);
                w.close();
            }
            "GetPolicy" => {
                let name = f.policy_name_from_arn()?;
                let p = doc.policies.get(&name).ok_or_else(|| no_such("policy", &name))?;
                w.open("Policy");
                write_policy(&mut w, &doc, &name, p);
                w.close();
            }
            "ListPolicies" => {
                let only_attached = f.get("OnlyAttached") == "true";
                let local = matches!(f.get("Scope"), "" | "All" | "Local");
                let prefix = match f.get("PathPrefix") {
                    "" => "/",
                    p => p,
                };
                let (items, next) = page(
                    doc.policies.iter().filter(|(n, p)| local && p.path.starts_with(prefix) && (!only_attached || doc.attachments(n) > 0)),
                    f,
                )?;
                w.open("Policies");
                for (n, p) in items {
                    w.open("member");
                    write_policy(&mut w, &doc, n, p);
                    w.close();
                }
                w.close();
                truncation(&mut w, next);
            }
            "DeletePolicy" => {
                let name = f.policy_name_from_arn()?;
                self.iam_change(cred, action, &name, |d, _, _| {
                    if !d.policies.contains_key(&name) {
                        return Err(no_such("policy", &name));
                    }
                    if d.attachments(&name) > 0 {
                        return Err(conflict("Cannot delete a policy attached to entities."));
                    }
                    d.policies.remove(&name);
                    Ok(())
                })?;
            }
            "GetPolicyVersion" => {
                let name = f.policy_name_from_arn()?;
                let n = f.version_id()?;
                let p = doc.policies.get(&name).ok_or_else(|| no_such("policy", &name))?;
                let v = p.versions.get(&n).ok_or_else(|| no_such("policy version", &format!("v{n}")))?;
                w.open("PolicyVersion");
                write_version(&mut w, p, n, v, true);
                w.close();
            }
            "ListPolicyVersions" => {
                let name = f.policy_name_from_arn()?;
                let p = doc.policies.get(&name).ok_or_else(|| no_such("policy", &name))?;
                w.open("Versions");
                for (n, v) in p.versions.iter().rev() {
                    w.open("member");
                    write_version(&mut w, p, *n, v, false);
                    w.close();
                }
                w.close();
                w.el("IsTruncated", "false");
            }
            "CreatePolicyVersion" => {
                let name = f.policy_name_from_arn()?;
                let pdoc = f.document("PolicyDocument", 6144)?;
                let default = f.get("SetAsDefault") == "true";
                let (p, n) = self.iam_change(cred, action, &name, |d, _, _| {
                    let p = d.policies.get_mut(&name).ok_or_else(|| no_such("policy", &name))?;
                    if p.versions.len() >= MAX_VERSIONS {
                        return Err(limit(format!("A managed policy can have up to {MAX_VERSIONS} versions.")));
                    }
                    let n = p.next_version;
                    p.next_version += 1;
                    p.updated = now();
                    p.versions.insert(n, PolicyVersion { doc: pdoc, created: p.updated.clone() });
                    if default {
                        p.default_version = n;
                    }
                    Ok((p.clone(), n))
                })?;
                w.open("PolicyVersion");
                write_version(&mut w, &p, n, &p.versions[&n], false);
                w.close();
            }
            "DeletePolicyVersion" | "SetDefaultPolicyVersion" => {
                let name = f.policy_name_from_arn()?;
                let n = f.version_id()?;
                let delete = action == "DeletePolicyVersion";
                self.iam_change(cred, action, &name, |d, _, _| {
                    let p = d.policies.get_mut(&name).ok_or_else(|| no_such("policy", &name))?;
                    if !p.versions.contains_key(&n) {
                        return Err(no_such("policy version", &format!("v{n}")));
                    }
                    if delete {
                        if p.default_version == n {
                            return Err(conflict("Cannot delete the default version of a policy."));
                        }
                        p.versions.remove(&n);
                    } else {
                        p.default_version = n;
                    }
                    p.updated = now();
                    Ok(())
                })?;
            }
            "AttachUserPolicy" | "DetachUserPolicy" | "AttachGroupPolicy" | "DetachGroupPolicy" => {
                let user = action.ends_with("UserPolicy");
                let owner = f.req(if user { "UserName" } else { "GroupName" })?.to_string();
                let pname = f.policy_name_from_arn()?;
                let attach = action.starts_with("Attach");
                self.iam_change(cred, action, &owner, |d, _, _| {
                    if !d.policies.contains_key(&pname) {
                        return Err(no_such("policy", &pname));
                    }
                    let set = if user {
                        &mut d.users.get_mut(&owner).ok_or_else(|| no_such("user", &owner))?.attached
                    } else {
                        &mut d.groups.get_mut(&owner).ok_or_else(|| no_such("group", &owner))?.attached
                    };
                    if attach {
                        if set.len() >= MAX_ATTACHED && !set.contains(&pname) {
                            return Err(limit(format!("Cannot exceed quota for PoliciesPerEntity: {MAX_ATTACHED}")));
                        }
                        set.insert(pname.clone());
                    } else if !set.remove(&pname) {
                        return Err(no_such("attached policy", &pname));
                    }
                    Ok(())
                })?;
            }
            "ListAttachedUserPolicies" | "ListAttachedGroupPolicies" => {
                let user = action == "ListAttachedUserPolicies";
                let owner = f.req(if user { "UserName" } else { "GroupName" })?;
                let set = if user {
                    &doc.users.get(owner).ok_or_else(|| no_such("user", owner))?.attached
                } else {
                    &doc.groups.get(owner).ok_or_else(|| no_such("group", owner))?.attached
                };
                w.open("AttachedPolicies");
                for n in set {
                    let path = doc.policies.get(n).map_or("/", |p| p.path.as_str());
                    w.open("member").el("PolicyName", n).el("PolicyArn", policy_arn(path, n)).close();
                }
                w.close();
                w.el("IsTruncated", "false");
            }
            _ => {
                return Err(fail(
                    StatusCode::BAD_REQUEST,
                    "InvalidAction",
                    format!("Could not find operation {action} for version 2010-05-08"),
                ))
            }
        }
        Ok(w.finish())
    }
}

impl Server {
    /// `POST /` signed for the iam or sts service: reads the form, checks the signature over it, runs it.
    pub(crate) async fn iam_api<B>(&self, req: hyper::Request<B>, request_id: &str) -> Result<super::Resp, super::errors::ApiError>
    where
        B: hyper::body::Body<Data = bytes::Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        use http_body_util::BodyExt;
        let (parts, body) = req.into_parts();
        let service = service_of(&parts.headers).unwrap_or("iam");
        let ns = if service == "sts" { STS_NS } else { IAM_NS };
        let reply = |status: StatusCode, doc: String| {
            let mut r = super::full(status, doc);
            super::set_header(&mut r, "content-type", "text/xml");
            r
        };
        let error = |err: &super::errors::ApiError| {
            reply(
                StatusCode::from_u16(err.status).unwrap_or(StatusCode::FORBIDDEN),
                format!(
                    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<ErrorResponse xmlns=\"{ns}\"><Error><Type>Sender</Type><Code>{}</Code><Message>{}</Message></Error><RequestId>{request_id}</RequestId></ErrorResponse>",
                    err.code,
                    escape(err.message)
                ),
            )
        };
        let body = match body.collect().await {
            Ok(b) => b.to_bytes(),
            Err(_) => return Ok(error(&super::errors::INCOMPLETE_BODY)),
        };
        if body.len() > 1 << 20 {
            return Ok(error(&super::errors::ENTITY_TOO_LARGE));
        }
        let raw_path = parts.uri.path().to_string();
        let raw_query = parts.uri.query().unwrap_or("").to_string();
        let host = parts.headers.get("host").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        let request = super::auth::Request {
            method: parts.method.as_str(),
            raw_path: &raw_path,
            raw_query: &raw_query,
            host: &host,
            headers: &parts.headers,
            now: (self.clock)(),
            vhost_bucket: None,
        };
        let auth = match super::auth::verify_with_payload(&self.keyset().creds, &request, &super::auth::sha256_hex(&body)) {
            Ok(a) => a,
            Err(err) => return Ok(error(&err)),
        };
        let mut form = BTreeMap::new();
        for src in [raw_query.as_str(), std::str::from_utf8(&body).unwrap_or("")] {
            for p in src.split('&').filter(|p| !p.is_empty()) {
                let (k, v) = p.split_once('=').unwrap_or((p, ""));
                form.insert(super::auth::query_decode(k), super::auth::query_decode(v));
            }
        }
        let (status, doc) = tokio::task::block_in_place(|| self.iam_call(service, &auth.cred, form, request_id));
        Ok(reply(status, doc))
    }
}
