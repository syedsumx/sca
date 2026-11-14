//! Transparent compression of stored blobs, in independent chunks so ranged reads stay cheap.
//!
//! An object's bytes are cut into `CHUNK` (1 MiB) chunks; each is compressed with zstd, or kept as it is
//! when compression does not save at least an eighth. The segment records each chunk's stored length
//! (`Segment::z`: LEB128 lengths, base64; the top bit of a length marks a chunk kept raw), so a read at
//! any offset starts at its chunk without touching the ones before it. Compression sits under
//! encryption: chunks are compressed first, and the compressed stream is what gets encrypted.
//!
//! A blob whose first chunk does not compress is written raw from then on (no table), so incompressible
//! data (media, archives, Parquet) costs one trial compression of its first megabyte.

use std::io::{self, Read};

use base64::Engine as _;

pub const CHUNK: usize = 1 << 20;
const RAW: u64 = 1 << 63;
/// zstd level: fast, and most of the ratio.
const LEVEL: i32 = 3;

/// A chunk table: stored length per chunk, with `RAW` set for chunks kept uncompressed.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Table(pub Vec<u64>);

impl Table {
    pub fn encode(&self) -> String {
        let mut out = Vec::with_capacity(self.0.len() * 3);
        for &v in &self.0 {
            let mut v = v;
            loop {
                let b = (v & 0x7f) as u8;
                v >>= 7;
                if v == 0 {
                    out.push(b);
                    break;
                }
                out.push(b | 0x80);
            }
        }
        base64::engine::general_purpose::STANDARD_NO_PAD.encode(out)
    }

    pub fn decode(s: &str) -> Option<Table> {
        let raw = base64::engine::general_purpose::STANDARD_NO_PAD.decode(s).ok()?;
        let mut out = vec![];
        let (mut v, mut shift) = (0u64, 0u32);
        for b in raw {
            if shift > 63 {
                return None;
            }
            v |= ((b & 0x7f) as u64) << shift;
            if b & 0x80 == 0 {
                out.push(v);
                v = 0;
                shift = 0;
            } else {
                shift += 7;
            }
        }
        (shift == 0).then_some(Table(out))
    }

    fn stored(&self, i: usize) -> u64 {
        self.0[i] & !RAW
    }

    /// Offset in the stored (compressed) stream where chunk `i` starts.
    pub fn offset_of(&self, i: usize) -> u64 {
        (0..i).map(|j| self.stored(j)).sum()
    }

    /// The stored stream's length.
    pub fn stored_len(&self) -> u64 {
        self.offset_of(self.0.len())
    }
}

/// Compresses one chunk; None when it is not worth it (kept raw).
pub fn compress(chunk: &[u8]) -> io::Result<Option<Vec<u8>>> {
    let c = zstd::bulk::compress(chunk, LEVEL)?;
    Ok((c.len() < chunk.len() - chunk.len() / 8).then_some(c))
}

/// Appends a chunk's entry to the table and returns the bytes to store.
pub fn pack(table: &mut Table, chunk: &[u8]) -> io::Result<Vec<u8>> {
    match compress(chunk)? {
        Some(c) => {
            table.0.push(c.len() as u64);
            Ok(c)
        }
        None => {
            table.0.push(chunk.len() as u64 | RAW);
            Ok(chunk.to_vec())
        }
    }
}

/// Reads plaintext from a stored (compressed) stream that starts at chunk `first`, skipping `skip` bytes
/// of that chunk's plaintext.
pub struct Reader {
    inner: Box<dyn Read + Send>,
    table: Table,
    next: usize,
    buf: Vec<u8>,
    off: usize,
    skip: usize,
}

impl Reader {
    /// `inner` must be positioned at `table.offset_of(first)` in the stored stream.
    pub fn new(inner: Box<dyn Read + Send>, table: Table, first: usize, skip: usize) -> Reader {
        Reader { inner, table, next: first, buf: vec![], off: 0, skip }
    }

    fn load(&mut self) -> io::Result<bool> {
        if self.next >= self.table.0.len() {
            return Ok(false);
        }
        let entry = self.table.0[self.next];
        let n = (entry & !RAW) as usize;
        let mut stored = vec![0u8; n];
        self.inner.read_exact(&mut stored)?;
        self.buf = if entry & RAW != 0 {
            stored
        } else {
            zstd::bulk::decompress(&stored, CHUNK)
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, format!("chunk {}: {e}", self.next)))?
        };
        self.next += 1;
        self.off = self.skip.min(self.buf.len());
        self.skip = 0;
        Ok(true)
    }
}

impl Read for Reader {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        while self.off >= self.buf.len() {
            if !self.load()? {
                return Ok(0);
            }
        }
        let n = out.len().min(self.buf.len() - self.off);
        out[..n].copy_from_slice(&self.buf[self.off..self.off + n]);
        self.off += n;
        Ok(n)
    }
}

/// Content types not worth compressing (already compressed).
pub fn incompressible(content_type: &str) -> bool {
    let t = content_type.split(';').next().unwrap_or("").trim().to_ascii_lowercase();
    t.starts_with("image/") && t != "image/svg+xml" && t != "image/bmp"
        || t.starts_with("video/")
        || t.starts_with("audio/")
        || matches!(
            t.as_str(),
            "application/zip"
                | "application/gzip"
                | "application/x-gzip"
                | "application/zstd"
                | "application/x-bzip2"
                | "application/x-xz"
                | "application/x-7z-compressed"
                | "application/vnd.apache.parquet"
                | "application/x-parquet"
                | "application/pdf"
        )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_and_chunks_round_trip() {
        let t = Table(vec![1, 127, 128, 300_000, (1 << 20) | RAW]);
        assert_eq!(Table::decode(&t.encode()), Some(t.clone()));
        assert_eq!(t.offset_of(3), 1 + 127 + 128);
        assert!(Table::decode("!!").is_none());
        // Text compresses; random bytes are kept raw.
        let text: Vec<u8> = b"timestamp,user,amount\n".iter().cycle().take(CHUNK).copied().collect();
        let mut x = 0x9e37_79b9_7f4a_7c15u64;
        let noise: Vec<u8> = (0..CHUNK)
            .map(|_| {
                x ^= x << 13;
                x ^= x >> 7;
                x ^= x << 17;
                (x >> 24) as u8
            })
            .collect();
        let mut table = Table::default();
        let mut stored = pack(&mut table, &text).unwrap();
        stored.extend(pack(&mut table, &noise).unwrap());
        stored.extend(pack(&mut table, &text[..1000]).unwrap());
        assert!(table.0[0] & RAW == 0 && table.0[0] < 10_000);
        assert!(table.0[1] & RAW != 0);
        assert_eq!(table.stored_len(), stored.len() as u64);
        let all: Vec<u8> = [&text[..], &noise[..], &text[..1000]].concat();
        let mut r = Reader::new(Box::new(io::Cursor::new(stored.clone())), table.clone(), 0, 0);
        let mut back = vec![];
        r.read_to_end(&mut back).unwrap();
        assert_eq!(back, all);
        // From the middle of the second chunk.
        let off = table.offset_of(1) as usize;
        let mut r = Reader::new(Box::new(io::Cursor::new(stored[off..].to_vec())), table, 1, 500);
        let mut back = vec![];
        r.read_to_end(&mut back).unwrap();
        assert_eq!(back, all[CHUNK + 500..]);
        assert!(incompressible("image/png") && !incompressible("text/csv") && !incompressible("image/svg+xml"));
    }
}
