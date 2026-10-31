//! Static website hosting: a bucket's website configuration (index and error documents, redirects,
//! routing rules) and serving it at `<bucket>.<CAIRN_STORE_WEBSITE_DOMAIN>`.
//!
//! Website requests are anonymous GET/HEAD requests, so a site is served only if the bucket policy grants
//! public read and public access is not blocked (store-wide or for the bucket): hosting cannot expose a
//! bucket by itself. Objects can redirect with `x-amz-website-redirect-location`.

use bytes::Bytes;
use hyper::{HeaderMap, Method, StatusCode};
use serde::{Deserialize, Serialize};

use super::errors::{self as e, ApiError};
use super::xml::{self, Element, Writer};
use super::{empty, full, set_header, xml_resp, Ctx, Resp, Server};

type R = Result<Resp, ApiError>;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Redirect {
    #[serde(default)]
    pub protocol: String,
    #[serde(default)]
    pub host: String,
    #[serde(default)]
    pub replace_key_prefix: Option<String>,
    #[serde(default)]
    pub replace_key: Option<String>,
    #[serde(default)]
    pub code: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct RoutingRule {
    #[serde(default)]
    pub key_prefix: Option<String>,
    #[serde(default)]
    pub error_code: Option<u16>,
    pub redirect: Redirect,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Website {
    #[serde(default)]
    pub index: String,
    #[serde(default)]
    pub error: String,
    /// RedirectAllRequestsTo (host, protocol).
    #[serde(default)]
    pub redirect_all: Option<(String, String)>,
    #[serde(default)]
    pub rules: Vec<RoutingRule>,
}

fn parse_redirect(el: &Element) -> Result<Redirect, ApiError> {
    let opt = |n: &str| el.child(n).map(|c| c.text.clone());
    let r = Redirect {
        protocol: el.text_of("Protocol"),
        host: el.text_of("HostName"),
        replace_key_prefix: opt("ReplaceKeyPrefixWith"),
        replace_key: opt("ReplaceKeyWith"),
        code: el.text_of("HttpRedirectCode"),
    };
    if r.replace_key_prefix.is_some() && r.replace_key.is_some() {
        return Err(e::MALFORMED_XML.clone());
    }
    if !r.protocol.is_empty() && r.protocol != "http" && r.protocol != "https" {
        return Err(e::INVALID_ARGUMENT.clone());
    }
    if !r.code.is_empty() && !["301", "302", "303", "307", "308"].contains(&r.code.as_str()) {
        return Err(e::INVALID_ARGUMENT.clone());
    }
    Ok(r)
}

pub fn parse(doc: &[u8]) -> Result<Website, ApiError> {
    let root =
        xml::parse(&String::from_utf8_lossy(doc)).filter(|r| r.name == "WebsiteConfiguration").ok_or_else(|| e::MALFORMED_XML.clone())?;
    let mut w = Website {
        index: root.child("IndexDocument").map(|i| i.text_of("Suffix")).unwrap_or_default(),
        error: root.child("ErrorDocument").map(|i| i.text_of("Key")).unwrap_or_default(),
        ..Default::default()
    };
    if let Some(all) = root.child("RedirectAllRequestsTo") {
        let host = all.text_of("HostName");
        if host.is_empty() || !w.index.is_empty() || !w.error.is_empty() || root.child("RoutingRules").is_some() {
            return Err(e::MALFORMED_XML.clone());
        }
        w.redirect_all = Some((host, all.text_of("Protocol")));
        return Ok(w);
    }
    if w.index.is_empty() || w.index.contains('/') {
        return Err(e::WEBSITE_INDEX.clone());
    }
    for r in root.child("RoutingRules").map(|r| r.all("RoutingRule").collect::<Vec<_>>()).unwrap_or_default() {
        let c = r.child("Condition");
        let rule = RoutingRule {
            key_prefix: c.and_then(|c| c.child("KeyPrefixEquals")).map(|k| k.text.clone()),
            error_code: match c.map(|c| c.text_of("HttpErrorCodeReturnedEquals")).unwrap_or_default().as_str() {
                "" => None,
                v => Some(v.parse().map_err(|_| e::INVALID_ARGUMENT.clone())?),
            },
            redirect: parse_redirect(r.child("Redirect").ok_or_else(|| e::MALFORMED_XML.clone())?)?,
        };
        w.rules.push(rule);
    }
    if w.rules.len() > 50 {
        return Err(e::INVALID_ARGUMENT.clone());
    }
    Ok(w)
}

pub fn to_xml(w: &Website) -> String {
    let mut x = Writer::new("WebsiteConfiguration");
    if let Some((host, proto)) = &w.redirect_all {
        x.open("RedirectAllRequestsTo").el("HostName", host).opt("Protocol", proto).close();
        return x.finish();
    }
    x.open("IndexDocument").el("Suffix", &w.index).close();
    if !w.error.is_empty() {
        x.open("ErrorDocument").el("Key", &w.error).close();
    }
    if !w.rules.is_empty() {
        x.open("RoutingRules");
        for r in &w.rules {
            x.open("RoutingRule");
            if r.key_prefix.is_some() || r.error_code.is_some() {
                x.open("Condition");
                if let Some(p) = &r.key_prefix {
                    x.el("KeyPrefixEquals", p);
                }
                if let Some(c) = r.error_code {
                    x.el("HttpErrorCodeReturnedEquals", c.to_string());
                }
                x.close();
            }
            let d = &r.redirect;
            x.open("Redirect").opt("Protocol", &d.protocol).opt("HostName", &d.host);
            if let Some(p) = &d.replace_key_prefix {
                x.el("ReplaceKeyPrefixWith", p);
            }
            if let Some(k) = &d.replace_key {
                x.el("ReplaceKeyWith", k);
            }
            x.opt("HttpRedirectCode", &d.code).close().close();
        }
        x.close();
    }
    x.finish()
}

fn redirect_to(host_header: &str, key: &str, d: &Redirect, prefix: Option<&str>) -> (StatusCode, String) {
    let new_key = match (&d.replace_key, &d.replace_key_prefix, prefix) {
        (Some(k), _, _) => k.clone(),
        (None, Some(rp), Some(p)) => format!("{rp}{}", key.strip_prefix(p).unwrap_or(key)),
        (None, Some(rp), None) => format!("{rp}{key}"),
        _ => key.to_string(),
    };
    let host = if d.host.is_empty() { host_header } else { d.host.as_str() };
    let proto = if d.protocol.is_empty() { "http" } else { d.protocol.as_str() };
    let code = d.code.parse::<u16>().ok().and_then(|c| StatusCode::from_u16(c).ok()).unwrap_or(StatusCode::MOVED_PERMANENTLY);
    (code, format!("{proto}://{host}/{new_key}"))
}

fn redirect_resp(code: StatusCode, location: &str) -> Resp {
    let mut r = empty(code);
    set_header(&mut r, "location", location);
    r
}

fn html_error(status: StatusCode, code: &str, message: &str) -> Resp {
    let body = format!(
        "<html><head><title>{0} {1}</title></head><body><h1>{0} {1}</h1><ul><li>Code: {2}</li><li>Message: {3}</li></ul></body></html>\n",
        status.as_u16(),
        status.canonical_reason().unwrap_or(""),
        xml::escape(code),
        xml::escape(message)
    );
    let mut r = full(status, body);
    set_header(&mut r, "content-type", "text/html; charset=utf-8");
    r
}

impl Server {
    pub(crate) fn website_config(&self, bucket: &str) -> Result<Option<Website>, ApiError> {
        match blocking(|| self.eng.get_config(bucket, "website")) {
            Ok(Some(d)) => Ok(Some(serde_json::from_slice(&d).map_err(|_| e::INTERNAL.clone())?)),
            Ok(None) => Ok(None),
            Err(err) => Err(err.into()),
        }
    }

    pub(crate) async fn put_website<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 128 << 10).await?;
        let w = parse(&doc)?;
        let v = serde_json::to_vec(&w).map_err(|_| e::INTERNAL.clone())?;
        self.set_config(c, "website", Some(&v))?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_website(&self, c: &Ctx) -> R {
        blocking(|| self.eng.head_bucket(&c.bucket))?;
        let w = self.website_config(&c.bucket)?.ok_or_else(|| e::NO_SUCH_WEBSITE.clone())?;
        Ok(xml_resp(StatusCode::OK, to_xml(&w)))
    }

    /// Serves a website request for `bucket` (anonymous GET/HEAD on the website endpoint).
    pub(crate) async fn serve_website(
        &self,
        bucket: &str,
        method: &Method,
        raw_path: &str,
        headers: &HeaderMap,
        peer: Option<std::net::IpAddr>,
    ) -> Resp {
        if method != Method::GET && method != Method::HEAD {
            return html_error(
                StatusCode::METHOD_NOT_ALLOWED,
                "MethodNotAllowed",
                "The specified method is not allowed against this resource.",
            );
        }
        let site = match self.website_config(bucket) {
            Ok(Some(w)) => w,
            Ok(None) => {
                return html_error(
                    StatusCode::NOT_FOUND,
                    "NoSuchWebsiteConfiguration",
                    "The specified bucket does not have a website configuration",
                )
            }
            Err(err) => return html_error(StatusCode::from_u16(err.status).unwrap_or(StatusCode::NOT_FOUND), err.code, err.message),
        };
        let host = headers.get("host").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
        let path = String::from_utf8_lossy(&super::auth::percent_decode(raw_path)).into_owned();
        let mut key = path.trim_start_matches('/').to_string();
        if let Some((h, proto)) = &site.redirect_all {
            let proto = if proto.is_empty() { "http" } else { proto.as_str() };
            return redirect_resp(StatusCode::MOVED_PERMANENTLY, &format!("{proto}://{h}{path}"));
        }
        for r in &site.rules {
            if r.error_code.is_none() {
                if let Some(p) = r.key_prefix.as_deref().filter(|p| key.starts_with(p)).or(r.key_prefix.is_none().then_some("")) {
                    let (code, loc) = redirect_to(&host, &key, &r.redirect, Some(p));
                    return redirect_resp(code, &loc);
                }
            }
        }
        if key.is_empty() || key.ends_with('/') {
            key.push_str(&site.index);
        }
        let status = match self.website_object(bucket, &key, method, headers, peer).await {
            Ok(r) => return r,
            Err(s) => s,
        };
        // Missing "dir" with an index document: redirect to "dir/".
        if status == StatusCode::NOT_FOUND && !path.ends_with('/') {
            let idx = format!("{key}/{}", site.index);
            if blocking(|| self.eng.get_object(bucket, &idx)).is_ok() {
                return redirect_resp(StatusCode::FOUND, &format!("{path}/"));
            }
        }
        for r in &site.rules {
            if r.error_code == Some(status.as_u16()) && r.key_prefix.as_deref().is_none_or(|p| key.starts_with(p)) {
                let (code, loc) = redirect_to(&host, &key, &r.redirect, r.key_prefix.as_deref());
                return redirect_resp(code, &loc);
            }
        }
        if !site.error.is_empty() {
            if let Ok(mut r) = self.website_object(bucket, &site.error, method, headers, peer).await {
                *r.status_mut() = status;
                return r;
            }
        }
        let (code, message) = if status == StatusCode::NOT_FOUND {
            ("NoSuchKey", "The specified key does not exist.")
        } else {
            ("AccessDenied", "Access Denied")
        };
        html_error(status, code, message)
    }

    /// Reads one object for the website, as an anonymous request. Err: the status to report.
    async fn website_object(
        &self,
        bucket: &str,
        key: &str,
        method: &Method,
        headers: &HeaderMap,
        peer: Option<std::net::IpAddr>,
    ) -> Result<Resp, StatusCode> {
        let mut h = HeaderMap::new();
        for name in ["range", "if-match", "if-none-match", "if-modified-since", "if-unmodified-since", "user-agent", "referer"] {
            if let Some(v) = headers.get(name) {
                h.insert(hyper::header::HeaderName::from_static(name), v.clone());
            }
        }
        let c = Ctx {
            method: method.clone(),
            bucket: bucket.to_string(),
            key: key.to_string(),
            path: format!("/{bucket}/{key}"),
            query: Default::default(),
            headers: h,
            auth: super::auth::Auth::anonymous(),
            content_length: None,
            op: "GetObject",
            peer,
            request_id: String::new(),
        };
        if self.authorize(&c, "s3:GetObject", bucket, key).await.is_err() {
            return Err(StatusCode::FORBIDDEN);
        }
        let obj = match blocking(|| self.eng.get_object(bucket, key)) {
            Ok(o) => o,
            Err(crate::engine::Error::NoSuchKey) => return Err(StatusCode::NOT_FOUND),
            Err(_) => return Err(StatusCode::FORBIDDEN),
        };
        if let Some(loc) = obj.headers.get("x-amz-website-redirect-location") {
            return Ok(redirect_resp(StatusCode::MOVED_PERMANENTLY, loc));
        }
        if obj.sse.as_ref().is_some_and(|s| s.alg == "SSE-C") {
            return Err(StatusCode::FORBIDDEN); // customer-key objects cannot be served without the key
        }
        match self.get_object(&c, method == Method::HEAD) {
            Ok(r) => Ok(r),
            Err(err) => Err(StatusCode::from_u16(err.status).unwrap_or(StatusCode::FORBIDDEN)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn documents() {
        let doc = br#"<WebsiteConfiguration><IndexDocument><Suffix>index.html</Suffix></IndexDocument><ErrorDocument><Key>404.html</Key></ErrorDocument>
            <RoutingRules><RoutingRule><Condition><KeyPrefixEquals>docs/</KeyPrefixEquals></Condition><Redirect><ReplaceKeyPrefixWith>documents/</ReplaceKeyPrefixWith><HttpRedirectCode>302</HttpRedirectCode></Redirect></RoutingRule>
            <RoutingRule><Condition><HttpErrorCodeReturnedEquals>404</HttpErrorCodeReturnedEquals></Condition><Redirect><HostName>fallback.example</HostName></Redirect></RoutingRule></RoutingRules></WebsiteConfiguration>"#;
        let w = parse(doc).unwrap();
        assert_eq!(w.rules.len(), 2);
        assert_eq!(parse(to_xml(&w).as_bytes()).unwrap(), w);
        let (code, loc) = redirect_to("site.web.local", "docs/a.html", &w.rules[0].redirect, Some("docs/"));
        assert_eq!((code.as_u16(), loc.as_str()), (302, "http://site.web.local/documents/a.html"));
        assert!(parse(b"<WebsiteConfiguration><IndexDocument><Suffix>a/b.html</Suffix></IndexDocument></WebsiteConfiguration>").is_err());
        let all = parse(b"<WebsiteConfiguration><RedirectAllRequestsTo><HostName>new.example</HostName><Protocol>https</Protocol></RedirectAllRequestsTo></WebsiteConfiguration>").unwrap();
        assert_eq!(all.redirect_all, Some(("new.example".into(), "https".into())));
    }
}
