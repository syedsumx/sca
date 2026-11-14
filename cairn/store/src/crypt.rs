//! Encryption at rest: chunked AES-256-GCM for object bytes, and key wrapping for data keys.
//!
//! Each object has its own random 256-bit data key. A blob is a sequence of chunks of `CHUNK` plaintext
//! bytes (the last may be shorter), each stored as ciphertext followed by its 16-byte tag. Chunk `i` of a
//! blob is encrypted with the nonce `base || i` (8 random bytes per blob, then the chunk index, big
//! endian), so chunks can be read and checked independently (ranged reads), and a reordered, altered or
//! truncated blob fails to decrypt. Data keys are stored only wrapped (AES-256-GCM) by a key from the
//! store's keyring; customer-provided keys (SSE-C) are never stored.

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};

pub const CHUNK: usize = 64 << 10;
pub const TAG: usize = 16;

pub type Key = [u8; 32];

pub fn random_key() -> Key {
    rand::random()
}

pub fn random_nonce_base() -> [u8; 8] {
    rand::random()
}

fn nonce(base: &[u8; 8], idx: u32) -> [u8; 12] {
    let mut n = [0u8; 12];
    n[..8].copy_from_slice(base);
    n[8..].copy_from_slice(&idx.to_be_bytes());
    n
}

pub struct ChunkCipher {
    aead: Aes256Gcm,
    base: [u8; 8],
}

impl ChunkCipher {
    pub fn new(key: &Key, base: [u8; 8]) -> ChunkCipher {
        ChunkCipher { aead: Aes256Gcm::new(key.into()), base }
    }

    pub fn seal(&self, idx: u32, plain: &[u8]) -> Vec<u8> {
        self.aead.encrypt(Nonce::from_slice(&nonce(&self.base, idx)), plain).expect("AES-GCM encryption does not fail")
    }

    pub fn open(&self, idx: u32, sealed: &[u8]) -> std::io::Result<Vec<u8>> {
        self.aead
            .decrypt(Nonce::from_slice(&nonce(&self.base, idx)), sealed)
            .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "encrypted chunk failed authentication"))
    }
}

/// The stored size of `plain` bytes of plaintext.
#[cfg(test)]
pub fn sealed_len(plain: u64) -> u64 {
    plain + plain.div_ceil(CHUNK as u64) * TAG as u64
}

/// Wraps a data key with a key-encryption key; `aad` binds it to its use (e.g. the bucket and key).
pub fn wrap(kek: &Key, dek: &Key, aad: &[u8]) -> Vec<u8> {
    let n: [u8; 12] = rand::random();
    let mut out = n.to_vec();
    out.extend(
        Aes256Gcm::new(kek.into()).encrypt(Nonce::from_slice(&n), Payload { msg: dek, aad }).expect("AES-GCM encryption does not fail"),
    );
    out
}

pub fn unwrap(kek: &Key, wrapped: &[u8], aad: &[u8]) -> Option<Key> {
    if wrapped.len() != 12 + 32 + TAG {
        return None;
    }
    let plain = Aes256Gcm::new(kek.into()).decrypt(Nonce::from_slice(&wrapped[..12]), Payload { msg: &wrapped[12..], aad }).ok()?;
    plain.try_into().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunks_and_wrapping() {
        let k = random_key();
        let c = ChunkCipher::new(&k, random_nonce_base());
        let s = c.seal(3, b"hello");
        assert_eq!(s.len(), 5 + TAG);
        assert_eq!(c.open(3, &s).unwrap(), b"hello");
        assert!(c.open(4, &s).is_err()); // a chunk moved to another position fails
        let mut bad = s.clone();
        bad[0] ^= 1;
        assert!(c.open(3, &bad).is_err());
        assert_eq!(sealed_len(0), 0);
        assert_eq!(sealed_len(CHUNK as u64), CHUNK as u64 + 16);
        assert_eq!(sealed_len(CHUNK as u64 + 1), CHUNK as u64 + 1 + 32);
        let kek = random_key();
        let w = wrap(&kek, &k, b"b/k");
        assert_eq!(unwrap(&kek, &w, b"b/k"), Some(k));
        assert_eq!(unwrap(&kek, &w, b"b/other"), None);
        assert_eq!(unwrap(&random_key(), &w, b"b/k"), None);
    }
}
