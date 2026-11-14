//! Cluster blob ids: where the copies (or erasure-coded shards) of one blob live.
//!
//! - replicas: `r:<stored length>;<node>@<local id>;<node>@<local id>...`
//! - erasure coding: `e:<k>+<m>:<stored length>:<stripe unit>;<node>@<local id>;...` (k data shards, then m
//!   parity shards, in order)
//!
//! A local id is a blob id on that node's own disk. The index carries these ids like any other blob
//! id; repair and rebalancing rewrite them in place (one transaction, only where unchanged).

#[derive(Clone, Debug, PartialEq)]
pub struct Copy {
    pub node: String,
    pub local: String,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Placement {
    Replicas { len: u64, copies: Vec<Copy> },
    Erasure { k: usize, m: usize, len: u64, unit: u64, shards: Vec<Copy> },
}

pub fn is_cluster(id: &str) -> bool {
    id.starts_with("r:") || id.starts_with("e:")
}

fn copies(parts: &[&str]) -> Option<Vec<Copy>> {
    parts.iter().map(|p| p.split_once('@').map(|(n, l)| Copy { node: n.to_string(), local: l.to_string() })).collect()
}

pub fn parse(id: &str) -> Option<Placement> {
    let mut parts = id.split(';');
    let head = parts.next()?;
    let rest: Vec<&str> = parts.collect();
    if let Some(len) = head.strip_prefix("r:") {
        return Some(Placement::Replicas { len: len.parse().ok()?, copies: copies(&rest)? });
    }
    let h = head.strip_prefix("e:")?;
    let mut f = h.split(':');
    let (k, m) = f.next()?.split_once('+')?;
    let (k, m): (usize, usize) = (k.parse().ok()?, m.parse().ok()?);
    let len = f.next()?.parse().ok()?;
    let unit = f.next()?.parse().ok()?;
    let shards = copies(&rest)?;
    (shards.len() == k + m).then_some(Placement::Erasure { k, m, len, unit, shards })
}

pub fn format(p: &Placement) -> String {
    let list = |cs: &[Copy]| cs.iter().map(|c| format!("{}@{}", c.node, c.local)).collect::<Vec<_>>().join(";");
    match p {
        Placement::Replicas { len, copies } => format!("r:{len};{}", list(copies)),
        Placement::Erasure { k, m, len, unit, shards } => format!("e:{k}+{m}:{len}:{unit};{}", list(shards)),
    }
}

impl Placement {
    pub fn len(&self) -> u64 {
        match self {
            Placement::Replicas { len, .. } | Placement::Erasure { len, .. } => *len,
        }
    }

    pub fn holders(&self) -> &[Copy] {
        match self {
            Placement::Replicas { copies, .. } => copies,
            Placement::Erasure { shards, .. } => shards,
        }
    }

    pub fn holders_mut(&mut self) -> &mut Vec<Copy> {
        match self {
            Placement::Replicas { copies, .. } => copies,
            Placement::Erasure { shards, .. } => shards,
        }
    }
}

/// The local blob ids a cluster id keeps on `node`.
pub fn local_ids(id: &str, node: &str) -> Vec<String> {
    parse(id).map(|p| p.holders().iter().filter(|c| c.node == node).map(|c| c.local.clone()).collect()).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        for id in ["r:42;a@0011;b@00ff", "e:2+1:100:65536;a@01;b@02;c@03"] {
            assert_eq!(format(&parse(id).unwrap()), id);
        }
        assert_eq!(local_ids("r:42;a@0011;b@00ff", "b"), vec!["00ff"]);
        assert!(parse("e:2+1:100:65536;a@01;b@02").is_none(), "shard count must match k+m");
        assert!(!is_cluster("p:x:0:1") && is_cluster("r:1;a@b"));
    }
}
