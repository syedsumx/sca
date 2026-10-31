//! Cross-origin resource sharing: per-bucket CORS rules, browser preflight (OPTIONS) and the CORS
//! response headers on ordinary requests.
//!
//! Preflight requests carry no signature, so they are answered before authentication, from the bucket's
//! rules alone; they never reveal object data. A bucket without rules refuses every cross-origin request.

use bytes::Bytes;
use hyper::{HeaderMap, StatusCode};
use serde::{Deserialize, Serialize};

use super::errors::{self as e, ApiError};
use super::xml::{self, Writer};
use super::{empty, set_header, xml_resp, Ctx, Resp, Server};

type R = Result<Resp, ApiError>;

const METHODS: [&str; 5] = ["GET", "PUT", "POST", "DELETE", "HEAD"];
const MAX_RULES: usize = 100;

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Rule {
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub id: String,
    pub methods: Vec<String>,
    pub origins: Vec<String>,
    #[serde(default)]
    pub headers: Vec<String>,
    #[serde(default)]
    pub expose: Vec<String>,
    #[serde(default)]
    pub max_age: Option<u64>,
}

/// Matches `s` against a pattern with at most one `*` (any run of characters).
fn wildcard(pat: &str, s: &str) -> bool {
    match pat.split_once('*') {
        None => pat == s,
        Some((pre, post)) => s.len() >= pre.len() + post.len() && s.starts_with(pre) && s.ends_with(post),
    }
}

impl Rule {
    fn allows_origin(&self, origin: &str) -> bool {
        self.origins.iter().any(|o| wildcard(o, origin))
    }

    fn allows_header(&self, h: &str) -> bool {
        self.headers.iter().any(|p| wildcard(&p.to_ascii_lowercase(), &h.to_ascii_lowercase()))
    }

    fn origin_header(&self, origin: &str) -> String {
        if self.origins.iter().any(|o| o == "*") {
            "*".into()
        } else {
            origin.into()
        }
    }
}

/// Parses and validates a CORSConfiguration document.
pub fn parse(doc: &[u8]) -> Result<Vec<Rule>, ApiError> {
    let root =
        xml::parse(&String::from_utf8_lossy(doc)).filter(|r| r.name == "CORSConfiguration").ok_or_else(|| e::MALFORMED_XML.clone())?;
    let mut rules = vec![];
    for r in root.all("CORSRule") {
        let texts = |n: &str| r.all(n).map(|x| x.text.trim().to_string()).collect::<Vec<_>>();
        let rule = Rule {
            id: r.text_of("ID"),
            methods: texts("AllowedMethod"),
            origins: texts("AllowedOrigin"),
            headers: texts("AllowedHeader"),
            expose: texts("ExposeHeader"),
            max_age: match r.text_of("MaxAgeSeconds").as_str() {
                "" => None,
                v => Some(v.parse().map_err(|_| e::MALFORMED_XML.clone())?),
            },
        };
        if rule.methods.is_empty() || rule.origins.is_empty() {
            return Err(e::MALFORMED_XML.clone());
        }
        if rule.methods.iter().any(|m| !METHODS.contains(&m.as_str())) {
            return Err(e::CORS_INVALID_METHOD.clone());
        }
        if rule.origins.iter().chain(&rule.headers).any(|o| o.matches('*').count() > 1) {
            return Err(e::CORS_INVALID_WILDCARD.clone());
        }
        if rule.id.chars().count() > 255 {
            return Err(e::INVALID_ARGUMENT.clone());
        }
        rules.push(rule);
    }
    if rules.is_empty() || rules.len() > MAX_RULES {
        return Err(e::MALFORMED_XML.clone());
    }
    Ok(rules)
}

pub fn to_xml(rules: &[Rule]) -> String {
    let mut w = Writer::new("CORSConfiguration");
    for r in rules {
        w.open("CORSRule").opt("ID", &r.id);
        for (tag, list) in
            [("AllowedHeader", &r.headers), ("AllowedMethod", &r.methods), ("AllowedOrigin", &r.origins), ("ExposeHeader", &r.expose)]
        {
            for v in list {
                w.el(tag, v);
            }
        }
        if let Some(m) = r.max_age {
            w.el("MaxAgeSeconds", m.to_string());
        }
        w.close();
    }
    w.finish()
}

/// The first rule allowing this origin, method and request headers.
fn find<'a>(rules: &'a [Rule], origin: &str, method: &str, req_headers: &[String]) -> Option<&'a Rule> {
    rules
        .iter()
        .find(|r| r.allows_origin(origin) && r.methods.iter().any(|m| m == method) && req_headers.iter().all(|h| r.allows_header(h)))
}

fn header<'a>(h: &'a HeaderMap, name: &str) -> &'a str {
    h.get(name).and_then(|v| v.to_str().ok()).unwrap_or("")
}

impl Server {
    fn cors_rules(&self, bucket: &str) -> Result<Option<Vec<Rule>>, ApiError> {
        let doc = tokio::task::block_in_place(|| self.eng.get_config(bucket, "cors"))?;
        match doc {
            None => Ok(None),
            Some(d) => Ok(Some(serde_json::from_slice(&d).map_err(|_| e::INTERNAL.clone())?)),
        }
    }

    pub(crate) async fn put_cors<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        let rules = parse(&doc)?;
        let v = serde_json::to_vec(&rules).map_err(|_| e::INTERNAL.clone())?;
        tokio::task::block_in_place(|| self.eng.put_config(&c.bucket, "cors", Some(&v)))?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_cors(&self, c: &Ctx) -> R {
        let rules = self.cors_rules(&c.bucket)?.ok_or_else(|| e::NO_SUCH_CORS.clone())?;
        Ok(xml_resp(StatusCode::OK, to_xml(&rules)))
    }

    /// Answers a browser preflight (unauthenticated).
    pub(crate) fn preflight(&self, bucket: &str, headers: &HeaderMap) -> R {
        let origin = header(headers, "origin");
        let method = header(headers, "access-control-request-method");
        if origin.is_empty() || method.is_empty() {
            return Err(e::CORS_BAD_PREFLIGHT.clone());
        }
        if bucket.is_empty() {
            return Err(e::CORS_NOT_ENABLED.clone());
        }
        let rules = self.cors_rules(bucket)?.ok_or_else(|| e::CORS_NOT_ENABLED.clone())?;
        let req_headers: Vec<String> = header(headers, "access-control-request-headers")
            .split(',')
            .map(|h| h.trim().to_ascii_lowercase())
            .filter(|h| !h.is_empty())
            .collect();
        let rule = find(&rules, origin, method, &req_headers).ok_or_else(|| e::CORS_FORBIDDEN.clone())?;
        let mut r = empty(StatusCode::OK);
        set_header(&mut r, "access-control-allow-origin", &rule.origin_header(origin));
        set_header(&mut r, "access-control-allow-methods", &rule.methods.join(", "));
        if !req_headers.is_empty() {
            set_header(&mut r, "access-control-allow-headers", &req_headers.join(", "));
        }
        if !rule.expose.is_empty() {
            set_header(&mut r, "access-control-expose-headers", &rule.expose.join(", "));
        }
        if let Some(m) = rule.max_age {
            set_header(&mut r, "access-control-max-age", &m.to_string());
        }
        set_header(&mut r, "vary", "Origin, Access-Control-Request-Headers, Access-Control-Request-Method");
        Ok(r)
    }

    /// Adds CORS headers to the response of an ordinary request that came with an Origin.
    pub(crate) fn apply_cors(&self, bucket: &str, method: &str, req: &HeaderMap, resp: &mut Resp) {
        let origin = header(req, "origin");
        if origin.is_empty() || bucket.is_empty() {
            return;
        }
        let Ok(Some(rules)) = self.cors_rules(bucket) else { return };
        if let Some(rule) = find(&rules, origin, method, &[]) {
            set_header(resp, "access-control-allow-origin", &rule.origin_header(origin));
            set_header(resp, "access-control-allow-methods", &rule.methods.join(", "));
            if !rule.expose.is_empty() {
                set_header(resp, "access-control-expose-headers", &rule.expose.join(", "));
            }
            if let Some(m) = rule.max_age {
                set_header(resp, "access-control-max-age", &m.to_string());
            }
            set_header(resp, "vary", "Origin, Access-Control-Request-Headers, Access-Control-Request-Method");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rule(origins: &[&str], methods: &[&str], headers: &[&str]) -> Rule {
        Rule {
            methods: methods.iter().map(|s| s.to_string()).collect(),
            origins: origins.iter().map(|s| s.to_string()).collect(),
            headers: headers.iter().map(|s| s.to_string()).collect(),
            ..Default::default()
        }
    }

    #[test]
    fn matching() {
        assert!(wildcard("https://*.cairn.dev", "https://app.cairn.dev"));
        assert!(!wildcard("https://*.cairn.dev", "https://cairn.dev"));
        assert!(wildcard("*", "http://localhost:9002"));
        let rules = vec![rule(&["https://*.cairn.dev"], &["GET", "PUT"], &["content-*", "x-amz-*"]), rule(&["*"], &["GET"], &[])];
        let h = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(find(&rules, "https://app.cairn.dev", "PUT", &h(&["content-type", "x-amz-date"])).unwrap().methods.len(), 2);
        assert!(find(&rules, "https://app.cairn.dev", "PUT", &h(&["authorization"])).is_none());
        assert_eq!(find(&rules, "https://evil.example", "GET", &[]).unwrap().origin_header("https://evil.example"), "*");
        assert!(find(&rules, "https://evil.example", "PUT", &[]).is_none());
    }

    #[test]
    fn documents() {
        let doc = b"<CORSConfiguration><CORSRule><ID>ui</ID><AllowedMethod>GET</AllowedMethod><AllowedOrigin>https://*.cairn.dev</AllowedOrigin><AllowedHeader>*</AllowedHeader><ExposeHeader>ETag</ExposeHeader><MaxAgeSeconds>600</MaxAgeSeconds></CORSRule></CORSConfiguration>";
        let rules = parse(doc).unwrap();
        assert_eq!(rules[0].max_age, Some(600));
        assert_eq!(parse(to_xml(&rules).as_bytes()).unwrap(), rules);
        assert_eq!(
            parse(b"<CORSConfiguration><CORSRule><AllowedMethod>PATCH</AllowedMethod><AllowedOrigin>*</AllowedOrigin></CORSRule></CORSConfiguration>")
                .unwrap_err(),
            e::CORS_INVALID_METHOD
        );
        assert_eq!(
            parse(b"<CORSConfiguration><CORSRule><AllowedMethod>GET</AllowedMethod><AllowedOrigin>https://*.*.x</AllowedOrigin></CORSRule></CORSConfiguration>")
                .unwrap_err(),
            e::CORS_INVALID_WILDCARD
        );
        assert!(parse(b"<CORSConfiguration></CORSConfiguration>").is_err());
    }
}
