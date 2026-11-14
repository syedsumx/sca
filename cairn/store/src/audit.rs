//! The store's audit trail: an append-only, hash-chained log in Cairn's audit format.
//!
//! Each entry's body is `{id, ts, category, action, actor, subject, details}` and its hash is
//! `sha256(prev_hash || canonical_json(body))`, starting from 64 zeros, exactly as Cairn's control plane
//! does (`cairn.common.audit`): canonical JSON is Python's `json.dumps(body, sort_keys=True,
//! separators=(",", ":"))`, ASCII-escaped. So an export from the store can be checked with
//! `cairn audit verify-file`, and with `cairn-store audit-verify` offline.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

pub const GENESIS: &str = "0000000000000000000000000000000000000000000000000000000000000000";

/// What to record.
#[derive(Clone, Debug, Default)]
pub struct Audit {
    pub category: &'static str,
    pub action: String,
    pub actor: String,
    pub subject: String,
    pub details: Value,
}

/// A stored entry (the export format: Cairn's EXPORT_FIELDS).
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Entry {
    pub seq: u64,
    pub id: String,
    pub ts: String,
    pub category: String,
    pub action: String,
    pub actor: String,
    pub subject: Option<String>,
    pub details: Value,
    pub prev_hash: String,
    pub hash: String,
}

fn escape_into(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{u:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Python's `json.dumps(v, sort_keys=True, separators=(",", ":"))`.
pub fn canonical(v: &Value) -> String {
    let mut out = String::new();
    write_canonical(&mut out, v);
    out
}

fn write_canonical(out: &mut String, v: &Value) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => escape_into(out, s),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(out, x);
            }
            out.push(']');
        }
        Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                escape_into(out, k);
                out.push(':');
                write_canonical(out, &m[*k]);
            }
            out.push('}');
        }
    }
}

impl Entry {
    fn body(&self) -> Value {
        serde_json::json!({
            "id": self.id, "ts": self.ts, "category": self.category, "action": self.action,
            "actor": self.actor, "subject": self.subject, "details": self.details,
        })
    }

    pub fn compute_hash(&self) -> String {
        let mut h = Sha256::new();
        h.update(self.prev_hash.as_bytes());
        h.update(canonical(&self.body()).as_bytes());
        hex::encode(h.finalize())
    }

    /// The next entry after `prev_hash`.
    pub fn new(seq: u64, prev_hash: &str, a: &Audit, now: time::OffsetDateTime) -> Entry {
        let b: [u8; 16] = rand::random();
        let mut b = b;
        b[6] = (b[6] & 0x0f) | 0x40; // UUID version 4
        b[8] = (b[8] & 0x3f) | 0x80;
        let h = hex::encode(b);
        let id = format!("{}-{}-{}-{}-{}", &h[0..8], &h[8..12], &h[12..16], &h[16..20], &h[20..32]);
        // Python's datetime.isoformat(): microseconds and a +00:00 offset.
        let ts = now
            .format(time::macros::format_description!("[year]-[month]-[day]T[hour]:[minute]:[second].[subsecond digits:6]+00:00"))
            .unwrap_or_default();
        let mut e = Entry {
            seq,
            id,
            ts,
            category: a.category.to_string(),
            action: a.action.clone(),
            actor: a.actor.clone(),
            subject: (!a.subject.is_empty()).then(|| a.subject.clone()),
            details: if a.details.is_null() { Value::Object(Default::default()) } else { a.details.clone() },
            prev_hash: prev_hash.to_string(),
            hash: String::new(),
        };
        e.hash = e.compute_hash();
        e
    }
}

/// Checks a contiguous run of entries (as Cairn's `verify_entries`).
pub fn verify(entries: &[Entry], from_genesis: bool) -> Value {
    let mut prev: Option<&str> = from_genesis.then_some(GENESIS);
    for (i, e) in entries.iter().enumerate() {
        if prev.is_some_and(|p| p != e.prev_hash) {
            return serde_json::json!({"valid": false, "broken_at_seq": e.seq, "entries_checked": i,
                "reason": "gap or reordering (prev_hash does not match the previous entry)"});
        }
        if e.compute_hash() != e.hash {
            return serde_json::json!({"valid": false, "broken_at_seq": e.seq, "entries_checked": i,
                "reason": "entry content does not match its hash"});
        }
        prev = Some(&e.hash);
    }
    serde_json::json!({"valid": true, "entries_checked": entries.len(), "head": prev.unwrap_or(GENESIS)})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_json_matches_python() {
        // python3 -c 'import json; print(json.dumps({"b":[1,"é\n"],"a":{"z":None,"y":True},"c":"\u0001😀"}, sort_keys=True, separators=(",",":")))'
        let v: Value = serde_json::from_str(r#"{"b":[1,"é\n"],"a":{"z":null,"y":true},"c":"\u0001😀"}"#).unwrap();
        assert_eq!(canonical(&v), r#"{"a":{"y":true,"z":null},"b":[1,"\u00e9\n"],"c":"\u0001\ud83d\ude00"}"#);
    }

    #[test]
    fn chain() {
        let now = time::OffsetDateTime::UNIX_EPOCH;
        let a = Audit {
            category: "store",
            action: "bucket.create".into(),
            actor: "admin".into(),
            subject: "lake".into(),
            details: Value::Null,
        };
        let e1 = Entry::new(1, GENESIS, &a, now);
        let e2 = Entry::new(2, &e1.hash, &a, now);
        assert_eq!(verify(&[e1.clone(), e2.clone()], true)["valid"], true);
        let mut bad = e2.clone();
        bad.actor = "mallory".into();
        assert_eq!(verify(&[e1.clone(), bad], true)["valid"], false);
        assert_eq!(verify(&[e2], true)["valid"], false); // e1 is missing
        assert!(e1.ts.ends_with("+00:00") && e1.id.len() == 36);
    }
}
