//! Bucket versioning: the versioning configuration and ListObjectVersions.

use bytes::Bytes;
use hyper::StatusCode;

use super::errors::{self as e, ApiError};
use super::xml::{self, Writer};
use super::{empty, iso_time, xml_resp, Ctx, Resp, Server};

type R = Result<Resp, ApiError>;

fn blocking<T>(f: impl FnOnce() -> T) -> T {
    tokio::task::block_in_place(f)
}

impl Server {
    pub(crate) async fn put_versioning<B>(&self, c: &Ctx, body: B) -> R
    where
        B: hyper::body::Body<Data = Bytes> + Send + Unpin,
        B::Error: std::fmt::Display,
    {
        let doc = self.read_small(c, body, 64 << 10).await?;
        let root = xml::parse(&String::from_utf8_lossy(&doc))
            .filter(|r| r.name == "VersioningConfiguration")
            .ok_or_else(|| e::MALFORMED_XML.clone())?;
        if root.text_of("MfaDelete") == "Enabled" {
            return Err(e::MFA_DELETE_UNSUPPORTED.clone());
        }
        let status = root.text_of("Status");
        if status != "Enabled" && status != "Suspended" {
            return Err(e::MALFORMED_XML.clone());
        }
        let info = blocking(|| self.eng.head_bucket(&c.bucket))?;
        if status == "Suspended" && blocking(|| self.eng.get_config(&c.bucket, "object-lock"))?.is_some() {
            // Object lock needs versioning; it cannot be suspended on a bucket that has it.
            return Err(e::LOCK_NEEDS_VERSIONING.clone());
        }
        if info.versioning != status && !(info.versioning.is_empty() && status == "Suspended") {
            blocking(|| {
                self.eng.set_versioning_audited(
                    &c.bucket,
                    &status,
                    Some(&self.audit_rec(c, format!("versioning.{}", status.to_lowercase()), serde_json::json!({}))),
                )
            })?;
        } else if info.versioning.is_empty() {
            // Suspending a bucket that never had versioning changes nothing, but it records the state.
            blocking(|| {
                self.eng.set_versioning_audited(
                    &c.bucket,
                    &status,
                    Some(&self.audit_rec(c, format!("versioning.{}", status.to_lowercase()), serde_json::json!({}))),
                )
            })?;
        }
        Ok(empty(StatusCode::OK))
    }

    pub(crate) fn list_versions(&self, c: &Ctx) -> R {
        let max = match c.q("max-keys") {
            "" => 1000,
            v => v.parse::<usize>().map_err(|_| e::INVALID_ARGUMENT.clone())?.min(1000),
        };
        let url = match c.q("encoding-type") {
            "" => false,
            "url" => true,
            _ => return Err(e::INVALID_ARGUMENT.clone()),
        };
        let enc = |s: &str| if url { super::auth::aws_encode(s.as_bytes(), true) } else { s.to_string() };
        let (prefix, delim, key_marker, vid_marker) = (c.q("prefix"), c.q("delimiter"), c.q("key-marker"), c.q("version-id-marker"));
        if !vid_marker.is_empty() && key_marker.is_empty() {
            return Err(e::INVALID_ARGUMENT.clone());
        }
        let res = blocking(|| self.eng.list_versions(&c.bucket, prefix, delim, key_marker, vid_marker, max))?;
        let mut w = Writer::new("ListVersionsResult");
        w.el("Name", &c.bucket)
            .el("Prefix", enc(prefix))
            .el("KeyMarker", enc(key_marker))
            .el("VersionIdMarker", vid_marker)
            .el("MaxKeys", max.to_string())
            .opt("Delimiter", enc(delim));
        if url {
            w.el("EncodingType", "url");
        }
        w.el("IsTruncated", res.truncated.to_string());
        if res.truncated {
            w.el("NextKeyMarker", enc(&res.next_key));
            w.opt("NextVersionIdMarker", &res.next_version);
        }
        let id = &c.auth.cred.access_key;
        for v in &res.entries {
            let o = &v.obj;
            w.open(if o.delete_marker { "DeleteMarker" } else { "Version" })
                .el("Key", enc(&o.key))
                .el("VersionId", o.version())
                .el("IsLatest", v.latest.to_string())
                .el("LastModified", iso_time(o.modified));
            if !o.delete_marker {
                w.el("ETag", format!("\"{}\"", o.etag)).el("Size", o.size.to_string()).el("StorageClass", "STANDARD");
            }
            w.open("Owner").el("ID", id).el("DisplayName", id).close();
            w.close();
        }
        for p in &res.common_prefixes {
            w.open("CommonPrefixes").el("Prefix", enc(p)).close();
        }
        Ok(xml_resp(StatusCode::OK, w.finish()))
    }
}
