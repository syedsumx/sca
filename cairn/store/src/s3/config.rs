//! Bucket and object configuration: tags, ACLs and object ownership.
//!
//! Cairn Store enforces bucket-owner object ownership (S3's "BucketOwnerEnforced"): access is decided by
//! keys and policies, never by ACLs. ACL requests that only restate owner full control succeed; any other
//! grant is refused with AccessControlListNotSupported, as S3 does for such buckets.

use std::collections::BTreeMap;

use bytes::Bytes;
use hyper::StatusCode;

use super::errors::{self as e, ApiError};
use super::xml::{self, Writer};
use super::{empty, xml_resp, Ctx, Resp, Server};

type R = Result<Resp, ApiError>;

pub const MAX_OBJECT_TAGS: usize = 10;
pub const MAX_BUCKET_TAGS: usize = 50;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

fn valid_tag_text(s: &str) -> bool {
    s.chars().all(|c| c.is_alphanumeric() || c.is_whitespace() || "+-=._:/@".contains(c))
}

/// Checks a tag set against S3's rules.
pub fn validate_tags(tags: &[(String, String)], max: usize) -> Result<BTreeMap<String, String>, ApiError> {
    if tags.len() > max {
        return Err(if max == MAX_OBJECT_TAGS { e::TOO_MANY_OBJECT_TAGS.clone() } else { e::TOO_MANY_BUCKET_TAGS.clone() });
    }
    let mut out = BTreeMap::new();
    for (k, v) in tags {
        let (kl, vl) = (k.chars().count(), v.chars().count());
        if kl == 0 || kl > 128 || vl > 256 || !valid_tag_text(k) || !valid_tag_text(v) || k.to_ascii_lowercase().starts_with("aws:") {
            return Err(e::INVALID_TAG.clone());
        }
        if out.insert(k.clone(), v.clone()).is_some() {
            return Err(e::DUPLICATE_TAG.clone());
        }
    }
    Ok(out)
}

/// Parses a `<Tagging><TagSet><Tag><Key/><Value/></Tag>...` document.
pub fn parse_tagging(doc: &[u8], max: usize) -> Result<BTreeMap<String, String>, ApiError> {
    let root = xml::parse(&String::from_utf8_lossy(doc)).filter(|r| r.name == "Tagging").ok_or_else(|| e::MALFORMED_XML.clone())?;
    let set = root.child("TagSet").ok_or_else(|| e::MALFORMED_XML.clone())?;
    let mut tags = vec![];
    for t in set.all("Tag") {
        let k = t.child("Key").ok_or_else(|| e::MALFORMED_XML.clone())?;
        tags.push((k.text.clone(), t.child("Value").map(|v| v.text.clone()).unwrap_or_default()));
    }
    validate_tags(&tags, max)
}

/// Parses the `x-amz-tagging` header (URL query encoding: `k1=v1&k2=v2`).
pub fn parse_tag_header(h: &str) -> Result<BTreeMap<String, String>, ApiError> {
    if h.is_empty() {
        return Ok(BTreeMap::new());
    }
    let tags: Vec<(String, String)> = h
        .split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            (super::auth::query_decode(k), super::auth::query_decode(v))
        })
        .collect();
    validate_tags(&tags, MAX_OBJECT_TAGS)
}

pub fn tagging_xml(tags: &BTreeMap<String, String>) -> String {
    let mut w = Writer::new("Tagging");
    w.open("TagSet");
    for (k, v) in tags {
        w.open("Tag").el("Key", k).el("Value", v).close();
    }
    w.finish()
}

/// Refuses ACL grants other than owner full control (canned `private` / `bucket-owner-full-control`).
pub fn check_acl_headers(c: &Ctx) -> Result<(), ApiError> {
    match c.header("x-amz-acl") {
        "" | "private" | "bucket-owner-full-control" => {}
        _ => return Err(e::ACL_NOT_SUPPORTED.clone()),
    }
    if c.headers.keys().any(|k| k.as_str().starts_with("x-amz-grant-")) {
        return Err(e::ACL_NOT_SUPPORTED.clone());
    }
    Ok(())
}

/// Accepts an AccessControlPolicy document only if every grant is FULL_CONTROL for the owner.
fn check_acl_body(c: &Ctx, doc: &[u8]) -> Result<(), ApiError> {
    if doc.iter().all(u8::is_ascii_whitespace) {
        return Ok(());
    }
    let root =
        xml::parse(&String::from_utf8_lossy(doc)).filter(|r| r.name == "AccessControlPolicy").ok_or_else(|| e::MALFORMED_XML.clone())?;
    let owner = &c.auth.cred.access_key;
    let list = root.child("AccessControlList").ok_or_else(|| e::MALFORMED_XML.clone())?;
    for g in list.all("Grant") {
        let id = g.child("Grantee").map(|x| x.text_of("ID")).unwrap_or_default();
        if &id != owner || g.text_of("Permission") != "FULL_CONTROL" {
            return Err(e::ACL_NOT_SUPPORTED.clone());
        }
    }
    Ok(())
}

impl Server {
    // ------------------------------------------------------------------------------- object tags

    pub(crate) async fn put_object_tagging<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        let tags = parse_tagging(&doc, MAX_OBJECT_TAGS)?;
        blocking(|| {
            self.eng.update_object_ev(
                &c.bucket,
                &c.key,
                Some(c.q("versionId")),
                |o| {
                    o.tags = tags;
                    Ok(())
                },
                &self.ev(c, "ObjectTagging:Put"),
            )
        })?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_object_tagging(&self, c: &Ctx) -> R {
        let o = blocking(|| self.eng.get_version(&c.bucket, &c.key, Some(c.q("versionId"))))?;
        if o.delete_marker {
            return Err(e::METHOD_NOT_ALLOWED.clone());
        }
        let mut r = xml_resp(StatusCode::OK, tagging_xml(&o.tags));
        if !o.version_id.is_empty() {
            super::set_header(&mut r, "x-amz-version-id", o.version());
        }
        Ok(r)
    }

    pub(crate) fn delete_object_tagging(&self, c: &Ctx) -> R {
        blocking(|| {
            self.eng.update_object_ev(
                &c.bucket,
                &c.key,
                Some(c.q("versionId")),
                |o| {
                    o.tags.clear();
                    Ok(())
                },
                &self.ev(c, "ObjectTagging:Delete"),
            )
        })?;
        Ok(empty(StatusCode::NO_CONTENT))
    }

    // ------------------------------------------------------------------------------- bucket tags

    pub(crate) async fn put_bucket_tagging<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        let tags = parse_tagging(&doc, MAX_BUCKET_TAGS)?;
        let v = serde_json::to_vec(&tags).map_err(|_| e::INTERNAL.clone())?;
        self.set_config(c, "tagging", Some(&v))?;
        Ok(empty(StatusCode::NO_CONTENT))
    }

    pub(crate) fn get_bucket_tagging(&self, c: &Ctx) -> R {
        let doc = blocking(|| self.eng.get_config(&c.bucket, "tagging"))?.ok_or_else(|| e::NO_SUCH_TAG_SET.clone())?;
        let tags: BTreeMap<String, String> = serde_json::from_slice(&doc).map_err(|_| e::INTERNAL.clone())?;
        Ok(xml_resp(StatusCode::OK, tagging_xml(&tags)))
    }

    pub(crate) fn delete_config(&self, c: &Ctx, kind: &str) -> R {
        self.set_config(c, kind, None)?;
        Ok(empty(StatusCode::NO_CONTENT))
    }

    // ------------------------------------------------------------------------------- ACLs, ownership

    pub(crate) async fn put_acl<B>(&self, c: &Ctx, body: B, object: bool) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        check_acl_headers(c)?;
        check_acl_body(c, &doc)?;
        blocking(|| {
            if object {
                self.eng.get_version(&c.bucket, &c.key, Some(c.q("versionId"))).map(|_| ())
            } else {
                self.eng.head_bucket(&c.bucket).map(|_| ())
            }
        })?;
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn get_ownership(&self, c: &Ctx) -> R {
        blocking(|| self.eng.head_bucket(&c.bucket))?;
        let mut w = Writer::new("OwnershipControls");
        w.open("Rule").el("ObjectOwnership", "BucketOwnerEnforced").close();
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }

    pub(crate) async fn put_ownership<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        let root =
            xml::parse(&String::from_utf8_lossy(&doc)).filter(|r| r.name == "OwnershipControls").ok_or_else(|| e::MALFORMED_XML.clone())?;
        let rules: Vec<String> = root.all("Rule").map(|r| r.text_of("ObjectOwnership")).collect();
        if rules.is_empty() || rules.iter().any(|r| r != "BucketOwnerEnforced") {
            return Err(e::ACL_NOT_SUPPORTED.clone());
        }
        blocking(|| self.eng.head_bucket(&c.bucket))?;
        Ok(empty(StatusCode::OK))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tag_rules() {
        let ok = validate_tags(&[("team".into(), "finance".into()), ("pii".into(), "".into())], 10).unwrap();
        assert_eq!(ok.len(), 2);
        assert_eq!(validate_tags(&[("a".into(), "1".into()), ("a".into(), "2".into())], 10).unwrap_err(), e::DUPLICATE_TAG);
        assert_eq!(validate_tags(&[("".into(), "1".into())], 10).unwrap_err(), e::INVALID_TAG);
        assert_eq!(validate_tags(&[("aws:x".into(), "1".into())], 10).unwrap_err(), e::INVALID_TAG);
        assert_eq!(validate_tags(&[("k".into(), "x".repeat(257))], 10).unwrap_err(), e::INVALID_TAG);
        assert_eq!(validate_tags(&[("k<".into(), "v".into())], 10).unwrap_err(), e::INVALID_TAG);
        let many: Vec<(String, String)> = (0..11).map(|i| (format!("k{i}"), "v".into())).collect();
        assert_eq!(validate_tags(&many, 10).unwrap_err(), e::TOO_MANY_OBJECT_TAGS);
        assert!(validate_tags(&many, 50).is_ok());
    }

    #[test]
    fn tag_header_and_document() {
        let t = parse_tag_header("team=finance&cost%20centre=a%2Fb").unwrap();
        assert_eq!(t.get("cost centre").unwrap(), "a/b");
        let doc = tagging_xml(&t);
        assert_eq!(parse_tagging(doc.as_bytes(), 10).unwrap(), t);
        assert_eq!(parse_tagging(b"<Nope/>", 10).unwrap_err(), e::MALFORMED_XML);
    }
}
