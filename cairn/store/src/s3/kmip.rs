//! A minimal KMIP 1.4 client: Encrypt and Decrypt with an AES key held by a KMIP server (an HSM or a key
//! manager such as Thales CipherTrust, Fortanix, Entrust KeyControl, Cosmian or PyKMIP), over TLS with a
//! client certificate. Binary TTLV encoding, one request per connection, AES-GCM with the object binding as
//! the authenticated additional data, so a wrapped key copied to another object does not decrypt.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::Arc;
use std::time::Duration;

// Tags (KMIP 1.4, section 9.1.3.1).
const REQUEST_MESSAGE: u32 = 0x420078;
const REQUEST_HEADER: u32 = 0x420077;
const PROTOCOL_VERSION: u32 = 0x420069;
const PROTOCOL_VERSION_MAJOR: u32 = 0x42006A;
const PROTOCOL_VERSION_MINOR: u32 = 0x42006B;
const BATCH_COUNT: u32 = 0x42000D;
const BATCH_ITEM: u32 = 0x42000F;
const OPERATION: u32 = 0x42005C;
const REQUEST_PAYLOAD: u32 = 0x420079;
const RESPONSE_PAYLOAD: u32 = 0x42007C;
const RESULT_STATUS: u32 = 0x42007F;
const RESULT_REASON: u32 = 0x42007E;
const RESULT_MESSAGE: u32 = 0x42007D;
const UNIQUE_IDENTIFIER: u32 = 0x420094;
const CRYPTOGRAPHIC_PARAMETERS: u32 = 0x42002B;
const BLOCK_CIPHER_MODE: u32 = 0x420011;
const CRYPTOGRAPHIC_ALGORITHM: u32 = 0x420028;
const TAG_LENGTH: u32 = 0x4200CE;
const DATA: u32 = 0x4200C2;
const IV_COUNTER_NONCE: u32 = 0x42003D;
const AEAD_DATA: u32 = 0x4200FE;
const AEAD_TAG: u32 = 0x4200FF;

const OP_ENCRYPT: u32 = 0x1F;
const OP_DECRYPT: u32 = 0x20;
const GCM: u32 = 0x09;
const AES: u32 = 0x03;

// Item types.
const T_STRUCTURE: u8 = 0x01;
const T_INTEGER: u8 = 0x02;
const T_ENUMERATION: u8 = 0x05;
const T_TEXT: u8 = 0x07;
const T_BYTES: u8 = 0x08;

/// One TTLV item: a tag, and its value.
#[derive(Debug, Clone, PartialEq)]
pub enum Item {
    Structure(u32, Vec<Item>),
    Integer(u32, i32),
    Enumeration(u32, u32),
    Text(u32, String),
    Bytes(u32, Vec<u8>),
    Other(u32, u8, Vec<u8>),
}

impl Item {
    fn tag(&self) -> u32 {
        match self {
            Item::Structure(t, _) | Item::Integer(t, _) | Item::Enumeration(t, _) | Item::Text(t, _) | Item::Bytes(t, _) => *t,
            Item::Other(t, _, _) => *t,
        }
    }

    pub fn encode(&self, out: &mut Vec<u8>) {
        let (typ, value): (u8, Vec<u8>) = match self {
            Item::Structure(_, items) => {
                let mut v = Vec::new();
                for i in items {
                    i.encode(&mut v);
                }
                (T_STRUCTURE, v)
            }
            Item::Integer(_, n) => (T_INTEGER, n.to_be_bytes().to_vec()),
            Item::Enumeration(_, n) => (T_ENUMERATION, n.to_be_bytes().to_vec()),
            Item::Text(_, s) => (T_TEXT, s.as_bytes().to_vec()),
            Item::Bytes(_, b) => (T_BYTES, b.clone()),
            Item::Other(_, t, b) => (*t, b.clone()),
        };
        out.extend_from_slice(&self.tag().to_be_bytes()[1..]);
        out.push(typ);
        out.extend_from_slice(&(value.len() as u32).to_be_bytes());
        out.extend_from_slice(&value);
        let pad = (8 - value.len() % 8) % 8;
        out.extend(std::iter::repeat_n(0u8, pad));
    }

    /// One item from `b`, and the bytes after it.
    pub fn decode(b: &[u8]) -> Result<(Item, &[u8]), String> {
        if b.len() < 8 {
            return Err("KMIP: a truncated item".into());
        }
        let tag = u32::from_be_bytes([0, b[0], b[1], b[2]]);
        let typ = b[3];
        let len = u32::from_be_bytes([b[4], b[5], b[6], b[7]]) as usize;
        let padded = len + (8 - len % 8) % 8;
        if b.len() < 8 + padded {
            return Err("KMIP: an item longer than the message".into());
        }
        let v = &b[8..8 + len];
        let rest = &b[8 + padded..];
        let four = |v: &[u8]| -> Result<[u8; 4], String> {
            v.get(..4).and_then(|s| s.try_into().ok()).ok_or_else(|| "KMIP: a short number".to_string())
        };
        let item = match typ {
            T_STRUCTURE => {
                let mut items = Vec::new();
                let mut inner = v;
                while !inner.is_empty() {
                    let (i, r) = Item::decode(inner)?;
                    items.push(i);
                    inner = r;
                }
                Item::Structure(tag, items)
            }
            T_INTEGER => Item::Integer(tag, i32::from_be_bytes(four(v)?)),
            T_ENUMERATION => Item::Enumeration(tag, u32::from_be_bytes(four(v)?)),
            T_TEXT => Item::Text(tag, String::from_utf8_lossy(v).into_owned()),
            T_BYTES => Item::Bytes(tag, v.to_vec()),
            other => Item::Other(tag, other, v.to_vec()),
        };
        Ok((item, rest))
    }

    fn children(&self) -> &[Item] {
        match self {
            Item::Structure(_, items) => items,
            _ => &[],
        }
    }

    fn find(&self, tag: u32) -> Option<&Item> {
        self.children().iter().find(|i| i.tag() == tag)
    }

    fn bytes(&self, tag: u32) -> Option<&[u8]> {
        match self.find(tag)? {
            Item::Bytes(_, b) => Some(b),
            _ => None,
        }
    }
}

/// Where the KMIP server is, and the TLS client identity to reach it with.
pub struct Kmip {
    pub host: String,
    pub port: u16,
    pub tls: Arc<rustls::ClientConfig>,
}

fn request(op: u32, payload: Vec<Item>) -> Vec<u8> {
    let msg = Item::Structure(
        REQUEST_MESSAGE,
        vec![
            Item::Structure(
                REQUEST_HEADER,
                vec![
                    Item::Structure(
                        PROTOCOL_VERSION,
                        vec![Item::Integer(PROTOCOL_VERSION_MAJOR, 1), Item::Integer(PROTOCOL_VERSION_MINOR, 4)],
                    ),
                    Item::Integer(BATCH_COUNT, 1),
                ],
            ),
            Item::Structure(BATCH_ITEM, vec![Item::Enumeration(OPERATION, op), Item::Structure(REQUEST_PAYLOAD, payload)]),
        ],
    );
    let mut out = Vec::new();
    msg.encode(&mut out);
    out
}

fn params() -> Item {
    Item::Structure(
        CRYPTOGRAPHIC_PARAMETERS,
        vec![Item::Enumeration(BLOCK_CIPHER_MODE, GCM), Item::Enumeration(CRYPTOGRAPHIC_ALGORITHM, AES), Item::Integer(TAG_LENGTH, 16)],
    )
}

impl Kmip {
    /// One request and its response payload, over a fresh TLS connection (KMIP servers close idle ones).
    fn call(&self, op: u32, payload: Vec<Item>) -> Result<Item, String> {
        let name = rustls::pki_types::ServerName::try_from(self.host.clone()).map_err(|e| format!("KMIP server name: {e}"))?;
        let conn = rustls::ClientConnection::new(self.tls.clone(), name).map_err(|e| e.to_string())?;
        let addr = std::net::ToSocketAddrs::to_socket_addrs(&(self.host.as_str(), self.port))
            .map_err(|e| format!("KMIP {}:{}: {e}", self.host, self.port))?
            .next()
            .ok_or("KMIP: no address")?;
        let sock =
            TcpStream::connect_timeout(&addr, Duration::from_secs(5)).map_err(|e| format!("KMIP {}:{}: {e}", self.host, self.port))?;
        sock.set_read_timeout(Some(Duration::from_secs(10))).ok();
        sock.set_write_timeout(Some(Duration::from_secs(10))).ok();
        let mut tls = rustls::StreamOwned::new(conn, sock);
        tls.write_all(&request(op, payload)).map_err(|e| format!("KMIP: {e}"))?;
        tls.flush().map_err(|e| format!("KMIP: {e}"))?;
        let mut head = [0u8; 8];
        tls.read_exact(&mut head).map_err(|e| format!("KMIP: {e}"))?;
        let len = u32::from_be_bytes([head[4], head[5], head[6], head[7]]) as usize;
        if len > 1 << 20 {
            return Err("KMIP: an oversized response".into());
        }
        let mut body = vec![0u8; len + (8 - len % 8) % 8];
        tls.read_exact(&mut body).map_err(|e| format!("KMIP: {e}"))?;
        let mut all = head.to_vec();
        all.extend_from_slice(&body);
        let (msg, _) = Item::decode(&all)?;
        let item = msg.find(BATCH_ITEM).ok_or("KMIP: no batch item in the response")?;
        match item.find(RESULT_STATUS) {
            Some(Item::Enumeration(_, 0)) => {}
            _ => {
                let reason = match item.find(RESULT_REASON) {
                    Some(Item::Enumeration(_, r)) => format!(" (reason {r:#x})"),
                    _ => String::new(),
                };
                let msg = match item.find(RESULT_MESSAGE) {
                    Some(Item::Text(_, m)) => m.clone(),
                    _ => "refused".into(),
                };
                return Err(format!("KMIP: {msg}{reason}"));
            }
        }
        item.find(RESPONSE_PAYLOAD).cloned().ok_or_else(|| "KMIP: no response payload".to_string())
    }

    /// `plain` encrypted with key `id` (AES-GCM, `aad` authenticated): IV || tag || ciphertext.
    pub fn encrypt(&self, id: &str, plain: &[u8], aad: &[u8]) -> Result<Vec<u8>, String> {
        let p = self.call(
            OP_ENCRYPT,
            vec![
                Item::Text(UNIQUE_IDENTIFIER, id.into()),
                params(),
                Item::Bytes(DATA, plain.to_vec()),
                Item::Bytes(AEAD_DATA, aad.to_vec()),
            ],
        )?;
        let (iv, tag, ct) = (p.bytes(IV_COUNTER_NONCE), p.bytes(AEAD_TAG), p.bytes(DATA));
        let (Some(iv), Some(tag), Some(ct)) = (iv, tag, ct) else {
            return Err("KMIP: the server returned no IV, tag or ciphertext".into());
        };
        if iv.len() > 255 || tag.len() > 255 {
            return Err("KMIP: an unexpected IV or tag".into());
        }
        let mut out = vec![iv.len() as u8, tag.len() as u8];
        out.extend_from_slice(iv);
        out.extend_from_slice(tag);
        out.extend_from_slice(ct);
        Ok(out)
    }

    pub fn decrypt(&self, id: &str, blob: &[u8], aad: &[u8]) -> Result<Vec<u8>, String> {
        let (ivl, tagl) = match blob {
            [a, b, ..] => (*a as usize, *b as usize),
            _ => return Err("KMIP: a damaged wrapped key".into()),
        };
        if blob.len() < 2 + ivl + tagl {
            return Err("KMIP: a damaged wrapped key".into());
        }
        let (iv, rest) = blob[2..].split_at(ivl);
        let (tag, ct) = rest.split_at(tagl);
        let p = self.call(
            OP_DECRYPT,
            vec![
                Item::Text(UNIQUE_IDENTIFIER, id.into()),
                params(),
                Item::Bytes(DATA, ct.to_vec()),
                Item::Bytes(IV_COUNTER_NONCE, iv.to_vec()),
                Item::Bytes(AEAD_DATA, aad.to_vec()),
                Item::Bytes(AEAD_TAG, tag.to_vec()),
            ],
        )?;
        p.bytes(DATA).map(|b| b.to_vec()).ok_or_else(|| "KMIP: the server returned no plaintext".to_string())
    }
}

/// The TLS client configuration for a KMIP server: its CA, and the store's certificate and key.
pub fn tls_config(ca: &std::path::Path, cert: &std::path::Path, key: &std::path::Path) -> Result<Arc<rustls::ClientConfig>, String> {
    let mut roots = rustls::RootCertStore::empty();
    for c in crate::tls::load_certs(ca)? {
        roots.add(c).map_err(|e| format!("{}: {e}", ca.display()))?;
    }
    let chain = crate::tls::load_certs(cert)?;
    let key = crate::tls::load_key(key)?;
    let cfg = rustls::ClientConfig::builder_with_provider(crate::tls::provider())
        .with_safe_default_protocol_versions()
        .map_err(|e| e.to_string())?
        .with_root_certificates(roots)
        .with_client_auth_cert(chain, key)
        .map_err(|e| format!("the KMIP client certificate: {e}"))?;
    Ok(Arc::new(cfg))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ttlv_round_trips_and_pads() {
        let msg = Item::Structure(
            REQUEST_PAYLOAD,
            vec![Item::Text(UNIQUE_IDENTIFIER, "1".into()), params(), Item::Bytes(DATA, vec![1, 2, 3]), Item::Integer(BATCH_COUNT, -2)],
        );
        let mut b = Vec::new();
        msg.encode(&mut b);
        assert_eq!(b.len() % 8, 0);
        // KMIP 1.4 test vector shape: tag 42 00 79, type 01, then the length.
        assert_eq!(&b[..4], &[0x42, 0x00, 0x79, 0x01]);
        let (back, rest) = Item::decode(&b).unwrap();
        assert!(rest.is_empty());
        assert_eq!(back, msg);
        assert!(Item::decode(&b[..b.len() - 9]).is_err());
    }

    #[test]
    fn a_request_has_the_header_and_one_batch_item() {
        let (msg, _) = Item::decode(&request(OP_ENCRYPT, vec![])).unwrap();
        let header = msg.find(REQUEST_HEADER).unwrap();
        assert_eq!(header.find(BATCH_COUNT), Some(&Item::Integer(BATCH_COUNT, 1)));
        let pv = header.find(PROTOCOL_VERSION).unwrap();
        assert_eq!(pv.find(PROTOCOL_VERSION_MINOR), Some(&Item::Integer(PROTOCOL_VERSION_MINOR, 4)));
        assert_eq!(msg.find(BATCH_ITEM).unwrap().find(OPERATION), Some(&Item::Enumeration(OPERATION, OP_ENCRYPT)));
    }
}
