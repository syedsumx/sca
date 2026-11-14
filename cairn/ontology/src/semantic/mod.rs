// SPDX-License-Identifier: Apache-2.0
//! The semantic compilation pipeline, ported from NVIDIA auto-ontology's `auto_ontology/semantic`.

pub mod date_format;
pub mod embed;
pub mod pipeline;
pub mod profile;
pub mod semantic_fk;
pub mod sql_attributes;
pub mod table;

use std::future::Future;
use std::pin::Pin;
use std::task::Poll;

/// Awaits several futures together (so their model calls overlap) and returns their outputs in order.
pub async fn join_all<F: Future>(futs: impl Iterator<Item = F>) -> Vec<F::Output> {
    let mut pinned: Vec<Pin<Box<F>>> = futs.map(Box::pin).collect();
    let mut results: Vec<Option<F::Output>> = (0..pinned.len()).map(|_| None).collect();
    std::future::poll_fn(|cx| {
        let mut pending = false;
        for (i, f) in pinned.iter_mut().enumerate() {
            if results[i].is_none() {
                match f.as_mut().poll(cx) {
                    Poll::Ready(v) => results[i] = Some(v),
                    Poll::Pending => pending = true,
                }
            }
        }
        if pending {
            Poll::Pending
        } else {
            Poll::Ready(())
        }
    })
    .await;
    results.into_iter().flatten().collect()
}
