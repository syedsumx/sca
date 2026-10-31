//! Request-body checking: aws-chunked framing with chunk and trailer signatures, the declared SHA-256,
//! Content-Length and `x-amz-checksum-*` values. Bytes are pushed in as they arrive; the first failure
//! is reported, and nothing is committed unless `finish` succeeds.

use std::collections::BTreeMap;

use hyper::HeaderMap;
use sha2::{Digest, Sha256};

use super::auth::{self, Auth};
use super::checksum::{self, Hasher};
use super::errors::{self as e, ApiError};

fn header<'a>(h: &'a HeaderMap, name: &str) -> &'a str {
    h.get(name).and_then(|v| v.to_str().ok()).unwrap_or("")
}

pub struct BodyCheck {
    chunked: Option<Chunked>,
    sha: Option<(Sha256, String)>,
    sums: BTreeMap<String, Hasher>,
    declared: BTreeMap<String, String>,
    want_len: Option<u64>,
    n: u64,
}

impl BodyCheck {
    /// Prepares checking for a request whose signature was verified. `compute` adds checksum algorithms
    /// to compute even if the client sent none.
    pub fn new(h: &HeaderMap, content_length: Option<u64>, auth: &Auth, compute: &[&str]) -> Result<BodyCheck, ApiError> {
        let mut b = BodyCheck { chunked: None, sha: None, sums: BTreeMap::new(), declared: BTreeMap::new(), want_len: None, n: 0 };
        let mut algos: Vec<String> = compute.iter().map(|s| s.to_string()).collect();
        for a in checksum::ALGOS {
            let v = header(h, &format!("x-amz-checksum-{a}"));
            if !v.is_empty() {
                b.declared.insert(a.into(), v.into());
                algos.push(a.into());
            }
        }
        for t in header(h, "x-amz-trailer").split(',') {
            if let Some(a) = t.trim().to_ascii_lowercase().strip_prefix("x-amz-checksum-") {
                algos.push(a.to_string());
            }
        }
        algos.push(header(h, "x-amz-sdk-checksum-algorithm").to_ascii_lowercase());
        for a in algos {
            if let Some(hs) = Hasher::new(&a) {
                b.sums.entry(a).or_insert(hs);
            }
        }
        match auth.payload_hash.as_str() {
            auth::STREAMING_SIGNED | auth::STREAMING_SIGNED_TRAILER | auth::STREAMING_UNSIGNED_TRAILER => {
                let n: u64 = header(h, "x-amz-decoded-content-length").parse().map_err(|_| e::MISSING_DECODED_LENGTH.clone())?;
                b.want_len = Some(n);
                b.chunked = Some(Chunked::new(auth));
            }
            auth::UNSIGNED_PAYLOAD => b.want_len = content_length,
            hash if hash.len() == 64 && hash.bytes().all(|c| c.is_ascii_hexdigit()) => {
                b.sha = Some((Sha256::new(), hash.to_ascii_lowercase()));
                b.want_len = content_length;
            }
            _ => return Err(e::CONTENT_SHA_INVALID.clone()),
        }
        let md5 = header(h, "content-md5");
        if !md5.is_empty() {
            use base64::Engine as _;
            if base64::engine::general_purpose::STANDARD.decode(md5).is_err() {
                return Err(e::INVALID_DIGEST.clone());
            }
        }
        Ok(b)
    }

    /// The length the body will decode to, when known up front.
    pub fn expected_len(&self) -> Option<u64> {
        self.want_len
    }

    /// Feeds raw request bytes; decoded payload bytes are appended to `out`.
    pub fn push(&mut self, raw: &[u8], out: &mut Vec<u8>) -> Result<(), ApiError> {
        let start = out.len();
        match &mut self.chunked {
            Some(c) => c.push(raw, out)?,
            None => out.extend_from_slice(raw),
        }
        let data = &out[start..];
        self.n += data.len() as u64;
        if let Some((sha, _)) = &mut self.sha {
            sha.update(data);
        }
        for h in self.sums.values_mut() {
            h.update(data);
        }
        Ok(())
    }

    /// Checks everything that can only be checked at the end of the body.
    pub fn finish(&mut self) -> Result<(), ApiError> {
        if let Some(c) = &mut self.chunked {
            c.finish()?;
            for (k, v) in &c.trailers {
                if let Some(a) = k.strip_prefix("x-amz-checksum-") {
                    if checksum::valid(a) {
                        self.declared.insert(a.into(), v.clone());
                    }
                }
            }
        }
        if let Some(n) = self.want_len {
            if self.n != n {
                return Err(e::INCOMPLETE_BODY.clone());
            }
        }
        if let Some((sha, want)) = &self.sha {
            if hex::encode(sha.clone().finalize()) != *want {
                return Err(e::CONTENT_SHA_MISMATCH.clone());
            }
        }
        for (a, want) in &self.declared {
            if let Some(h) = self.sums.get(a) {
                if h.base64() != *want {
                    return Err(e::CHECKSUM_MISMATCH.clone());
                }
            }
        }
        Ok(())
    }

    /// The computed checksums (base64) of the algorithms in play.
    pub fn checksums(&self) -> BTreeMap<String, String> {
        self.sums.iter().map(|(a, h)| (a.clone(), h.base64())).collect()
    }
}

enum State {
    Header,
    Data(u64),
    DataEnd,
    Trailer,
    Done,
}

/// aws-chunked: `<hex-size>[;chunk-signature=<sig>]\r\n<data>\r\n ... 0[;chunk-signature=<sig>]\r\n[trailers\r\n]\r\n`
struct Chunked {
    state: State,
    line: Vec<u8>,
    signed: bool,
    has_trailer: bool,
    signing_key: Vec<u8>,
    amz_date: String,
    scope: String,
    prev_sig: String,
    want_sig: String,
    chunk: Sha256,
    trailers: BTreeMap<String, String>,
    trailer_lines: Vec<String>,
    trailer_sig: String,
}

impl Chunked {
    fn new(a: &Auth) -> Chunked {
        Chunked {
            state: State::Header,
            line: vec![],
            signed: a.payload_hash != auth::STREAMING_UNSIGNED_TRAILER,
            has_trailer: a.payload_hash != auth::STREAMING_SIGNED,
            signing_key: a.signing_key.clone(),
            amz_date: a.amz_date.clone(),
            scope: a.scope.clone(),
            prev_sig: a.seed_sig.clone(),
            want_sig: String::new(),
            chunk: Sha256::new(),
            trailers: BTreeMap::new(),
            trailer_lines: vec![],
            trailer_sig: String::new(),
        }
    }

    fn check_chunk(&mut self) -> Result<(), ApiError> {
        if !self.signed {
            return Ok(());
        }
        let data_hash = hex::encode(std::mem::take(&mut self.chunk).finalize());
        let to_sign =
            format!("AWS4-HMAC-SHA256-PAYLOAD\n{}\n{}\n{}\n{}\n{data_hash}", self.amz_date, self.scope, self.prev_sig, auth::EMPTY_SHA256);
        let got = hex::encode(auth::hmac_sha256(&self.signing_key, to_sign.as_bytes()));
        if got != self.want_sig {
            return Err(e::CHUNK_SIGNATURE.clone());
        }
        self.prev_sig = got;
        Ok(())
    }

    /// Takes bytes up to and including '\n' into `self.line`; returns how many were consumed and
    /// whether a full line is ready.
    fn take_line(&mut self, raw: &[u8]) -> Result<(usize, bool), ApiError> {
        match raw.iter().position(|&c| c == b'\n') {
            Some(i) => {
                self.line.extend_from_slice(&raw[..=i]);
                Ok((i + 1, true))
            }
            None => {
                self.line.extend_from_slice(raw);
                if self.line.len() > 8192 {
                    return Err(e::INCOMPLETE_BODY.clone());
                }
                Ok((raw.len(), false))
            }
        }
    }

    fn line_str(&mut self) -> String {
        let s = String::from_utf8_lossy(&self.line).trim_end_matches(['\r', '\n']).to_string();
        self.line.clear();
        s
    }

    fn header_line(&mut self, line: &str) -> Result<(), ApiError> {
        let (size, ext) = line.split_once(';').unwrap_or((line, ""));
        let size = u64::from_str_radix(size.trim(), 16).map_err(|_| e::INCOMPLETE_BODY.clone())?;
        if self.signed {
            match ext.split_once('=') {
                Some((k, v)) if k.trim() == "chunk-signature" && !v.trim().is_empty() => self.want_sig = v.trim().to_string(),
                _ => return Err(e::CHUNK_SIGNATURE.clone()),
            }
            self.chunk = Sha256::new();
        }
        if size > 0 {
            self.state = State::Data(size);
        } else {
            self.check_chunk()?; // the final, empty chunk is signed too
            self.state = State::Trailer;
        }
        Ok(())
    }

    fn trailer_line(&mut self, line: &str) -> Result<(), ApiError> {
        if line.is_empty() {
            return self.end_trailers();
        }
        let (k, v) = line.split_once(':').ok_or_else(|| e::INCOMPLETE_BODY.clone())?;
        let (k, v) = (k.trim().to_ascii_lowercase(), v.trim().to_string());
        if k == "x-amz-trailer-signature" {
            self.trailer_sig = v;
        } else {
            self.trailer_lines.push(format!("{k}:{v}"));
            self.trailers.insert(k, v);
        }
        Ok(())
    }

    fn end_trailers(&mut self) -> Result<(), ApiError> {
        if self.signed && self.has_trailer {
            let mut lines = self.trailer_lines.clone();
            lines.sort();
            let canonical: String = lines.iter().map(|l| format!("{l}\n")).collect();
            let to_sign = format!(
                "AWS4-HMAC-SHA256-TRAILER\n{}\n{}\n{}\n{}",
                self.amz_date,
                self.scope,
                self.prev_sig,
                auth::sha256_hex(canonical.as_bytes())
            );
            if hex::encode(auth::hmac_sha256(&self.signing_key, to_sign.as_bytes())) != self.trailer_sig {
                return Err(e::CHUNK_SIGNATURE.clone());
            }
        }
        self.state = State::Done;
        Ok(())
    }

    fn push(&mut self, mut raw: &[u8], out: &mut Vec<u8>) -> Result<(), ApiError> {
        while !raw.is_empty() {
            match self.state {
                State::Header => {
                    let (n, done) = self.take_line(raw)?;
                    raw = &raw[n..];
                    if done {
                        let l = self.line_str();
                        self.header_line(&l)?;
                    }
                }
                State::Data(left) => {
                    let take = (left as usize).min(raw.len());
                    out.extend_from_slice(&raw[..take]);
                    if self.signed {
                        self.chunk.update(&raw[..take]);
                    }
                    raw = &raw[take..];
                    self.state = if left as usize == take { State::DataEnd } else { State::Data(left - take as u64) };
                }
                State::DataEnd => {
                    let (n, done) = self.take_line(raw)?;
                    raw = &raw[n..];
                    if done {
                        if !self.line_str().is_empty() {
                            return Err(e::INCOMPLETE_BODY.clone());
                        }
                        self.check_chunk()?;
                        self.state = State::Header;
                    }
                }
                State::Trailer => {
                    let (n, done) = self.take_line(raw)?;
                    raw = &raw[n..];
                    if done {
                        let l = self.line_str();
                        self.trailer_line(&l)?;
                    }
                }
                State::Done => return Ok(()), // anything after the terminating blank line is ignored
            }
        }
        Ok(())
    }

    fn finish(&mut self) -> Result<(), ApiError> {
        if let State::Trailer = self.state {
            // Some clients end the body right after the last trailer line.
            if !self.line.is_empty() {
                let l = self.line_str();
                self.trailer_line(&l)?;
            }
            if let State::Trailer = self.state {
                self.end_trailers()?;
            }
        }
        match self.state {
            State::Done => Ok(()),
            _ => Err(e::INCOMPLETE_BODY.clone()),
        }
    }
}
