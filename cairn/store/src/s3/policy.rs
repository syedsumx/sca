//! Access policies in the S3/IAM policy language: bucket policies (with a Principal) and key policies
//! (attached to an access key in the credentials file, no Principal).
//!
//! Decision, per request:
//!
//! 1. An explicit `Deny` in the bucket policy or the key's policy denies.
//! 2. Anonymous requests are allowed only by a bucket policy statement for `Principal: "*"`, and only if
//!    public access is not blocked (store-wide `CAIRN_STORE_BLOCK_PUBLIC_ACCESS`, on by default, or the
//!    bucket's public access block).
//! 3. A signed request is allowed by its key's policy, or, for keys without one, by the key's scope
//!    (`buckets`, `read_only`, `admin`); or by a bucket policy statement naming the key.
//! 4. Everything else is denied.
//!
//! Documents are validated when they are stored: an unknown operator, condition key prefix or field makes
//! the policy malformed, so a policy is never stored and then partly ignored.

use std::collections::BTreeMap;
use std::net::IpAddr;

use serde_json::Value;

use super::errors::{self as e, ApiError};

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Effect {
    Allow,
    Deny,
}

#[derive(Clone, Debug, PartialEq)]
enum Principals {
    Any,
    Keys(Vec<String>),
}

#[derive(Clone, Debug)]
struct Condition {
    op: String,
    negated: bool,
    if_exists: bool,
    key: String,
    values: Vec<String>,
}

#[derive(Clone, Debug)]
struct Statement {
    effect: Effect,
    principal: Option<Principals>,
    not_principal: Option<Principals>,
    action: Vec<String>,
    not_action: Vec<String>,
    resource: Vec<String>,
    not_resource: Vec<String>,
    conditions: Vec<Condition>,
}

#[derive(Clone, Debug, Default)]
pub struct Policy {
    statements: Vec<Statement>,
}

/// What a request is, for policy evaluation.
pub struct Request<'a> {
    /// e.g. `s3:GetObject`.
    pub action: &'a str,
    /// e.g. `arn:aws:s3:::bucket/key` or `arn:aws:s3:::bucket`.
    pub resource: &'a str,
    /// The access key; empty for anonymous requests.
    pub principal: &'a str,
    /// Condition keys (lower-cased names) and their values.
    pub context: &'a BTreeMap<String, Vec<String>>,
}

const OPS: [&str; 22] = [
    "StringEquals",
    "StringNotEquals",
    "StringEqualsIgnoreCase",
    "StringNotEqualsIgnoreCase",
    "StringLike",
    "StringNotLike",
    "NumericEquals",
    "NumericNotEquals",
    "NumericLessThan",
    "NumericLessThanEquals",
    "NumericGreaterThan",
    "NumericGreaterThanEquals",
    "DateEquals",
    "DateNotEquals",
    "DateLessThan",
    "DateLessThanEquals",
    "DateGreaterThan",
    "DateGreaterThanEquals",
    "Bool",
    "IpAddress",
    "NotIpAddress",
    "Null",
];

/// Condition keys the store fills in. Tag keys are prefixes (`s3:ExistingObjectTag/<key>`).
const KEYS: [&str; 20] = [
    "s3:object-lock-mode",
    "s3:object-lock-retain-until-date",
    "s3:object-lock-legal-hold",
    "aws:sourceip",
    "aws:securetransport",
    "aws:currenttime",
    "aws:epochtime",
    "aws:username",
    "aws:userid",
    "aws:principaltype",
    "aws:referer",
    "aws:useragent",
    "s3:prefix",
    "s3:delimiter",
    "s3:max-keys",
    "s3:x-amz-acl",
    "s3:signatureversion",
    "s3:authtype",
    "s3:x-amz-content-sha256",
    "s3:x-amz-server-side-encryption",
];
const KEY_PREFIXES: [&str; 2] = ["s3:existingobjecttag/", "s3:requestobjecttag/"];

/// `*` matches any run of characters (including `/`), `?` any one character.
pub fn glob(pat: &str, s: &str) -> bool {
    let (p, t): (Vec<char>, Vec<char>) = (pat.chars().collect(), s.chars().collect());
    let (mut pi, mut ti, mut star, mut mark) = (0, 0, None, 0);
    while ti < t.len() {
        if pi < p.len() && (p[pi] == '?' || p[pi] == t[ti]) {
            pi += 1;
            ti += 1;
        } else if pi < p.len() && p[pi] == '*' {
            star = Some(pi);
            mark = ti;
            pi += 1;
        } else if let Some(sp) = star {
            pi = sp + 1;
            mark += 1;
            ti = mark;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' {
        pi += 1;
    }
    pi == p.len()
}

fn strings(v: &Value) -> Result<Vec<String>, ApiError> {
    match v {
        Value::String(s) => Ok(vec![s.clone()]),
        Value::Array(a) => a.iter().map(|x| x.as_str().map(str::to_string).ok_or_else(|| e::MALFORMED_POLICY.clone())).collect(),
        Value::Bool(b) => Ok(vec![b.to_string()]),
        Value::Number(n) => Ok(vec![n.to_string()]),
        _ => Err(e::MALFORMED_POLICY.clone()),
    }
}

/// An access key from a principal entry: `KEY`, `arn:aws:iam::<account>:user/KEY` or `arn:aws:iam::<account>:root`.
fn principal_key(p: &str) -> String {
    match p.rsplit_once(":user/") {
        Some((_, k)) => k.to_string(),
        None => p.to_string(),
    }
}

fn principals(v: &Value) -> Result<Principals, ApiError> {
    match v {
        Value::String(s) if s == "*" => Ok(Principals::Any),
        Value::Object(m) => {
            let mut keys = vec![];
            for (k, v) in m {
                if k != "AWS" && k != "Cairn" {
                    return Err(e::MALFORMED_POLICY.clone());
                }
                for p in strings(v)? {
                    if p == "*" {
                        return Ok(Principals::Any);
                    }
                    keys.push(principal_key(&p));
                }
            }
            Ok(Principals::Keys(keys))
        }
        _ => Err(e::MALFORMED_POLICY.clone()),
    }
}

impl Policy {
    /// Parses a policy document. `bucket`: it is that bucket's policy (needs Principal, resources in the bucket).
    pub fn parse(doc: &[u8], bucket: Option<&str>) -> Result<Policy, ApiError> {
        let v: Value = serde_json::from_slice(doc).map_err(|_| e::MALFORMED_POLICY.clone())?;
        let m = v.as_object().ok_or_else(|| e::MALFORMED_POLICY.clone())?;
        for k in m.keys() {
            if !["Version", "Id", "Statement"].contains(&k.as_str()) {
                return Err(e::MALFORMED_POLICY.clone());
            }
        }
        if let Some(ver) = m.get("Version") {
            if ver != "2012-10-17" && ver != "2008-10-17" {
                return Err(e::MALFORMED_POLICY.clone());
            }
        }
        let list = match m.get("Statement") {
            Some(Value::Array(a)) => a.clone(),
            Some(o @ Value::Object(_)) => vec![o.clone()],
            _ => return Err(e::MALFORMED_POLICY.clone()),
        };
        if list.is_empty() {
            return Err(e::MALFORMED_POLICY.clone());
        }
        let mut statements = vec![];
        for s in &list {
            let s = s.as_object().ok_or_else(|| e::MALFORMED_POLICY.clone())?;
            for k in s.keys() {
                if !["Sid", "Effect", "Principal", "NotPrincipal", "Action", "NotAction", "Resource", "NotResource", "Condition"]
                    .contains(&k.as_str())
                {
                    return Err(e::MALFORMED_POLICY.clone());
                }
            }
            let effect = match s.get("Effect").and_then(Value::as_str) {
                Some("Allow") => Effect::Allow,
                Some("Deny") => Effect::Deny,
                _ => return Err(e::MALFORMED_POLICY.clone()),
            };
            let principal = s.get("Principal").map(principals).transpose()?;
            let not_principal = s.get("NotPrincipal").map(principals).transpose()?;
            match bucket {
                Some(_) if principal.is_none() && not_principal.is_none() => return Err(e::MALFORMED_POLICY_PRINCIPAL.clone()),
                None if principal.is_some() || not_principal.is_some() => return Err(e::MALFORMED_POLICY.clone()),
                _ => {}
            }
            let action = s.get("Action").map(strings).transpose()?.unwrap_or_default();
            let not_action = s.get("NotAction").map(strings).transpose()?.unwrap_or_default();
            let resource = s.get("Resource").map(strings).transpose()?.unwrap_or_default();
            let not_resource = s.get("NotResource").map(strings).transpose()?.unwrap_or_default();
            if action.is_empty() == not_action.is_empty() || resource.is_empty() == not_resource.is_empty() {
                return Err(e::MALFORMED_POLICY.clone());
            }
            for a in action.iter().chain(&not_action) {
                if a != "*" && !a.to_ascii_lowercase().starts_with("s3:") {
                    return Err(e::MALFORMED_POLICY_ACTION.clone());
                }
            }
            for r in resource.iter().chain(&not_resource) {
                let ok = r == "*"
                    || match bucket {
                        Some(b) => r == &format!("arn:aws:s3:::{b}") || r.starts_with(&format!("arn:aws:s3:::{b}/")),
                        None => r.starts_with("arn:aws:s3:::"),
                    };
                if !ok {
                    return Err(e::MALFORMED_POLICY_RESOURCE.clone());
                }
            }
            let mut conditions = vec![];
            if let Some(c) = s.get("Condition") {
                let c = c.as_object().ok_or_else(|| e::MALFORMED_POLICY.clone())?;
                for (op, body) in c {
                    let (base, if_exists) = match op.strip_suffix("IfExists") {
                        Some(b) => (b, true),
                        None => (op.as_str(), false),
                    };
                    if !OPS.contains(&base) {
                        return Err(e::MALFORMED_POLICY_CONDITION.clone());
                    }
                    let body = body.as_object().ok_or_else(|| e::MALFORMED_POLICY.clone())?;
                    for (key, vals) in body {
                        let lk = key.to_ascii_lowercase();
                        if !KEYS.contains(&lk.as_str()) && !KEY_PREFIXES.iter().any(|p| lk.starts_with(p) && lk.len() > p.len()) {
                            return Err(e::MALFORMED_POLICY_CONDITION.clone());
                        }
                        let values = strings(vals)?;
                        if base.contains("IpAddress") && values.iter().any(|v| parse_cidr(v).is_none()) {
                            return Err(e::MALFORMED_POLICY_CONDITION.clone());
                        }
                        let negated = base.contains("Not") && base != "Null";
                        conditions.push(Condition { op: base.to_string(), negated, if_exists, key: lk, values });
                    }
                }
            }
            statements.push(Statement { effect, principal, not_principal, action, not_action, resource, not_resource, conditions });
        }
        Ok(Policy { statements })
    }

    /// Does any statement grant public (anonymous) access?
    pub fn is_public(&self) -> bool {
        self.statements.iter().any(|s| s.effect == Effect::Allow && s.principal == Some(Principals::Any) && s.conditions.is_empty())
    }

    /// The effects of the statements that apply to the request (Deny first if any).
    pub fn evaluate(&self, r: &Request) -> Option<Effect> {
        let mut allowed = false;
        for s in &self.statements {
            if !s.applies(r) {
                continue;
            }
            match s.effect {
                Effect::Deny => return Some(Effect::Deny),
                Effect::Allow => allowed = true,
            }
        }
        allowed.then_some(Effect::Allow)
    }
}

fn principal_matches(p: &Principals, key: &str) -> bool {
    match p {
        Principals::Any => true,
        Principals::Keys(ks) => !key.is_empty() && ks.iter().any(|k| k == key),
    }
}

impl Statement {
    fn applies(&self, r: &Request) -> bool {
        if let Some(p) = &self.principal {
            if !principal_matches(p, r.principal) {
                return false;
            }
        }
        if let Some(p) = &self.not_principal {
            if principal_matches(p, r.principal) {
                return false;
            }
        }
        let act = |pats: &[String]| pats.iter().any(|p| glob(&p.to_ascii_lowercase(), &r.action.to_ascii_lowercase()));
        if (!self.action.is_empty() && !act(&self.action)) || (!self.not_action.is_empty() && act(&self.not_action)) {
            return false;
        }
        let res = |pats: &[String]| pats.iter().any(|p| glob(p, r.resource));
        if (!self.resource.is_empty() && !res(&self.resource)) || (!self.not_resource.is_empty() && res(&self.not_resource)) {
            return false;
        }
        self.conditions.iter().all(|c| c.holds(r.context))
    }
}

fn parse_cidr(v: &str) -> Option<(IpAddr, u8)> {
    let (ip, bits) = match v.split_once('/') {
        Some((ip, b)) => (ip.parse::<IpAddr>().ok()?, b.parse::<u8>().ok()?),
        None => {
            let ip: IpAddr = v.parse().ok()?;
            (ip, if ip.is_ipv4() { 32 } else { 128 })
        }
    };
    let max = if ip.is_ipv4() { 32 } else { 128 };
    (bits <= max).then_some((ip, bits))
}

fn in_cidr(ip: &str, cidr: &str) -> bool {
    let (Some((net, bits)), Ok(ip)) = (parse_cidr(cidr), ip.parse::<IpAddr>()) else { return false };
    let to_u128 = |a: IpAddr| match a {
        IpAddr::V4(v) => (u32::from(v) as u128, 32u32),
        IpAddr::V6(v) => (u128::from(v), 128u32),
    };
    let ((n, w1), (x, w2)) = (to_u128(net), to_u128(ip));
    if w1 != w2 {
        return false;
    }
    let shift = w1 - bits as u32;
    shift >= w1 || (n >> shift) == (x >> shift)
}

fn time_ms(v: &str) -> Option<i128> {
    if let Ok(n) = v.parse::<i64>() {
        return Some(n as i128 * 1000);
    }
    time::OffsetDateTime::parse(v, &time::format_description::well_known::Rfc3339).ok().map(|t| t.unix_timestamp_nanos() / 1_000_000)
}

impl Condition {
    fn holds(&self, ctx: &BTreeMap<String, Vec<String>>) -> bool {
        let got = ctx.get(&self.key);
        if self.op == "Null" {
            let want_null = self.values.iter().any(|v| v.eq_ignore_ascii_case("true"));
            return want_null == got.is_none();
        }
        let Some(got) = got else { return self.if_exists || self.negated };
        let any = |f: &dyn Fn(&str, &str) -> bool| got.iter().any(|g| self.values.iter().any(|v| f(g, v)));
        let num = |g: &str, v: &str, f: fn(f64, f64) -> bool| matches!((g.parse::<f64>(), v.parse::<f64>()), (Ok(a), Ok(b)) if f(a, b));
        let date = |g: &str, v: &str, f: fn(i128, i128) -> bool| matches!((time_ms(g), time_ms(v)), (Some(a), Some(b)) if f(a, b));
        match self.op.as_str() {
            "StringEquals" => any(&|g, v| g == v),
            "StringNotEquals" => !any(&|g, v| g == v),
            "StringEqualsIgnoreCase" => any(&|g, v| g.eq_ignore_ascii_case(v)),
            "StringNotEqualsIgnoreCase" => !any(&|g, v| g.eq_ignore_ascii_case(v)),
            "StringLike" => any(&|g, v| glob(v, g)),
            "StringNotLike" => !any(&|g, v| glob(v, g)),
            "NumericEquals" => any(&|g, v| num(g, v, |a, b| a == b)),
            "NumericNotEquals" => !any(&|g, v| num(g, v, |a, b| a == b)),
            "NumericLessThan" => any(&|g, v| num(g, v, |a, b| a < b)),
            "NumericLessThanEquals" => any(&|g, v| num(g, v, |a, b| a <= b)),
            "NumericGreaterThan" => any(&|g, v| num(g, v, |a, b| a > b)),
            "NumericGreaterThanEquals" => any(&|g, v| num(g, v, |a, b| a >= b)),
            "DateEquals" => any(&|g, v| date(g, v, |a, b| a == b)),
            "DateNotEquals" => !any(&|g, v| date(g, v, |a, b| a == b)),
            "DateLessThan" => any(&|g, v| date(g, v, |a, b| a < b)),
            "DateLessThanEquals" => any(&|g, v| date(g, v, |a, b| a <= b)),
            "DateGreaterThan" => any(&|g, v| date(g, v, |a, b| a > b)),
            "DateGreaterThanEquals" => any(&|g, v| date(g, v, |a, b| a >= b)),
            "Bool" => any(&|g, v| g.eq_ignore_ascii_case(v)),
            "IpAddress" => any(&|g, v| in_cidr(g, v)),
            "NotIpAddress" => !any(&|g, v| in_cidr(g, v)),
            _ => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx(pairs: &[(&str, &str)]) -> BTreeMap<String, Vec<String>> {
        pairs.iter().map(|(k, v)| (k.to_string(), vec![v.to_string()])).collect()
    }

    fn req<'a>(action: &'a str, resource: &'a str, principal: &'a str, c: &'a BTreeMap<String, Vec<String>>) -> Request<'a> {
        Request { action, resource, principal, context: c }
    }

    #[test]
    fn globbing() {
        assert!(glob("arn:aws:s3:::lake/raw/*", "arn:aws:s3:::lake/raw/a/b.parquet"));
        assert!(!glob("arn:aws:s3:::lake/raw/*", "arn:aws:s3:::lake/curated/x"));
        assert!(glob("s3:get*", "s3:getobject"));
        assert!(glob("a?c", "abc") && !glob("a?c", "ac"));
        assert!(glob("*", ""));
    }

    #[test]
    fn bucket_policy_evaluation() {
        let doc = br#"{"Version":"2012-10-17","Statement":[
            {"Sid":"read","Effect":"Allow","Principal":{"AWS":["arn:aws:iam::000000000000:user/analyst"]},"Action":["s3:GetObject","s3:ListBucket"],"Resource":["arn:aws:s3:::lake","arn:aws:s3:::lake/*"]},
            {"Sid":"no-pii","Effect":"Deny","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::lake/*","Condition":{"StringEquals":{"s3:ExistingObjectTag/pii":"true"}}},
            {"Sid":"office","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":"arn:aws:s3:::lake/*","Condition":{"NotIpAddress":{"aws:SourceIp":["10.0.0.0/8","::1/128"]}}}
        ]}"#;
        let p = Policy::parse(doc, Some("lake")).unwrap();
        let office = ctx(&[("aws:sourceip", "10.1.2.3")]);
        assert_eq!(p.evaluate(&req("s3:GetObject", "arn:aws:s3:::lake/a.parquet", "analyst", &office)), Some(Effect::Allow));
        assert_eq!(p.evaluate(&req("s3:PutObject", "arn:aws:s3:::lake/a.parquet", "analyst", &office)), None);
        assert_eq!(p.evaluate(&req("s3:GetObject", "arn:aws:s3:::lake/a.parquet", "someone", &office)), None);
        let pii = ctx(&[("aws:sourceip", "10.1.2.3"), ("s3:existingobjecttag/pii", "true")]);
        assert_eq!(p.evaluate(&req("s3:GetObject", "arn:aws:s3:::lake/a.parquet", "analyst", &pii)), Some(Effect::Deny));
        let home = ctx(&[("aws:sourceip", "203.0.113.9")]);
        assert_eq!(p.evaluate(&req("s3:GetObject", "arn:aws:s3:::lake/a.parquet", "analyst", &home)), Some(Effect::Deny));
        assert_eq!(
            p.evaluate(&req("s3:GetObject", "arn:aws:s3:::lake/a", "analyst", &ctx(&[("aws:sourceip", "::1")]))),
            Some(Effect::Allow)
        );
        assert!(!p.is_public());
    }

    #[test]
    fn validation() {
        let bad = |doc: &str, bucket: Option<&str>| Policy::parse(doc.as_bytes(), bucket).unwrap_err();
        assert_eq!(bad("not json", None), e::MALFORMED_POLICY);
        assert_eq!(
            bad(r#"{"Statement":[{"Effect":"Allow","Action":"s3:*","Resource":"arn:aws:s3:::lake/*"}]}"#, Some("lake")),
            e::MALFORMED_POLICY_PRINCIPAL
        );
        assert_eq!(
            bad(r#"{"Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:*","Resource":"arn:aws:s3:::other/*"}]}"#, Some("lake")),
            e::MALFORMED_POLICY_RESOURCE
        );
        assert_eq!(
            bad(r#"{"Statement":[{"Effect":"Allow","Principal":"*","Action":"ec2:*","Resource":"arn:aws:s3:::lake/*"}]}"#, Some("lake")),
            e::MALFORMED_POLICY_ACTION
        );
        assert_eq!(
            bad(
                r#"{"Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:*","Resource":"arn:aws:s3:::lake/*","Condition":{"StringMagic":{"s3:prefix":"x"}}}]}"#,
                Some("lake")
            ),
            e::MALFORMED_POLICY_CONDITION
        );
        assert_eq!(
            bad(
                r#"{"Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:*","Resource":"arn:aws:s3:::lake/*","Condition":{"StringEquals":{"aws:Unknown":"x"}}}]}"#,
                Some("lake")
            ),
            e::MALFORMED_POLICY_CONDITION
        );
        let public = Policy::parse(
            br#"{"Statement":{"Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::lake/*"}}"#,
            Some("lake"),
        )
        .unwrap();
        assert!(public.is_public());
    }

    #[test]
    fn key_policy_and_conditions() {
        let p = Policy::parse(
            br#"{"Statement":[
                {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":"arn:aws:s3:::lake","Condition":{"StringLike":{"s3:prefix":["raw/*",""]}}},
                {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":"arn:aws:s3:::lake/raw/*"},
                {"Effect":"Deny","NotAction":"s3:Get*","Resource":"*","Condition":{"DateGreaterThan":{"aws:CurrentTime":"2030-01-01T00:00:00Z"}}}
            ]}"#,
            None,
        )
        .unwrap();
        let now = ctx(&[("s3:prefix", "raw/2026"), ("aws:currenttime", "2026-10-31T00:00:00Z")]);
        assert_eq!(p.evaluate(&req("s3:ListBucket", "arn:aws:s3:::lake", "k", &now)), Some(Effect::Allow));
        let other = ctx(&[("s3:prefix", "curated/"), ("aws:currenttime", "2026-10-31T00:00:00Z")]);
        assert_eq!(p.evaluate(&req("s3:ListBucket", "arn:aws:s3:::lake", "k", &other)), None);
        assert_eq!(p.evaluate(&req("s3:PutObject", "arn:aws:s3:::lake/raw/x", "k", &now)), Some(Effect::Allow));
        let later = ctx(&[("aws:currenttime", "2031-01-01T00:00:00Z")]);
        assert_eq!(p.evaluate(&req("s3:PutObject", "arn:aws:s3:::lake/raw/x", "k", &later)), Some(Effect::Deny));
        // A missing key fails positive operators and satisfies negated ones.
        assert_eq!(p.evaluate(&req("s3:ListBucket", "arn:aws:s3:::lake", "k", &BTreeMap::new())), None);
        assert!(in_cidr("192.168.1.7", "192.168.0.0/16") && !in_cidr("192.169.1.7", "192.168.0.0/16") && in_cidr("1.2.3.4", "0.0.0.0/0"));
    }
}
