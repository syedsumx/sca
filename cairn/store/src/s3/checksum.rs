//! The checksums S3 clients send (`x-amz-checksum-*`): CRC32, CRC32C, CRC64NVME, SHA-1, SHA-256.

use sha1::Sha1;
use sha2::{Digest, Sha256};

pub const ALGOS: [&str; 5] = ["crc32", "crc32c", "crc64nvme", "sha1", "sha256"];

pub fn valid(algo: &str) -> bool {
    ALGOS.contains(&algo)
}

const fn crc32_table(poly: u32) -> [u32; 256] {
    let mut t = [0u32; 256];
    let mut i = 0;
    while i < 256 {
        let mut c = i as u32;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 { (c >> 1) ^ poly } else { c >> 1 };
            k += 1;
        }
        t[i] = c;
        i += 1;
    }
    t
}

const fn crc64_table(poly: u64) -> [u64; 256] {
    let mut t = [0u64; 256];
    let mut i = 0;
    while i < 256 {
        let mut c = i as u64;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 { (c >> 1) ^ poly } else { c >> 1 };
            k += 1;
        }
        t[i] = c;
        i += 1;
    }
    t
}

/// CRC-32C (Castagnoli), reflected.
static CRC32C: [u32; 256] = crc32_table(0x82F6_3B78);
/// CRC-64/NVME, reflected (polynomial 0xAD93D23594C93659).
static CRC64NVME: [u64; 256] = crc64_table(0x9A6C_9329_AC4B_C9B5);

pub enum Hasher {
    Crc32(crc32fast::Hasher),
    Crc32c(u32),
    Crc64(u64),
    Sha1(Sha1),
    Sha256(Sha256),
}

impl Hasher {
    pub fn new(algo: &str) -> Option<Hasher> {
        Some(match algo {
            "crc32" => Hasher::Crc32(crc32fast::Hasher::new()),
            "crc32c" => Hasher::Crc32c(!0),
            "crc64nvme" => Hasher::Crc64(!0),
            "sha1" => Hasher::Sha1(Sha1::new()),
            "sha256" => Hasher::Sha256(Sha256::new()),
            _ => return None,
        })
    }

    pub fn update(&mut self, data: &[u8]) {
        match self {
            Hasher::Crc32(h) => h.update(data),
            Hasher::Crc32c(c) => {
                for &b in data {
                    *c = CRC32C[((*c ^ b as u32) & 0xff) as usize] ^ (*c >> 8);
                }
            }
            Hasher::Crc64(c) => {
                for &b in data {
                    *c = CRC64NVME[((*c ^ b as u64) & 0xff) as usize] ^ (*c >> 8);
                }
            }
            Hasher::Sha1(h) => h.update(data),
            Hasher::Sha256(h) => h.update(data),
        }
    }

    /// The digest in S3's byte order (big-endian for CRCs).
    pub fn digest(&self) -> Vec<u8> {
        match self {
            Hasher::Crc32(h) => h.clone().finalize().to_be_bytes().to_vec(),
            Hasher::Crc32c(c) => (!*c).to_be_bytes().to_vec(),
            Hasher::Crc64(c) => (!*c).to_be_bytes().to_vec(),
            Hasher::Sha1(h) => h.clone().finalize().to_vec(),
            Hasher::Sha256(h) => h.clone().finalize().to_vec(),
        }
    }

    pub fn base64(&self) -> String {
        use base64::Engine as _;
        base64::engine::general_purpose::STANDARD.encode(self.digest())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn check_values() {
        // The standard "123456789" check values of each CRC.
        let cases = [("crc32", "cbf43926"), ("crc32c", "e3069283"), ("crc64nvme", "ae8b14860a799888")];
        for (algo, want) in cases {
            let mut h = Hasher::new(algo).unwrap();
            h.update(b"1234");
            h.update(b"56789");
            assert_eq!(hex::encode(h.digest()), want, "{algo}");
        }
    }
}
