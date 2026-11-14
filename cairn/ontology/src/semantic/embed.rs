// SPDX-License-Identifier: Apache-2.0
// Ported from NVIDIA auto-ontology: auto_ontology/semantic/embed.py (`_build_rows`, `_format_sample_values`).
// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Changes: rewritten in Rust; vectors are kept on the ontology elements instead of a separate vector store.
//! The text each Term and ColumnAttribute is embedded as, and embedding a table's elements.

use super::profile::{stringify_samples, MAX_EMBEDDED_JSON_SAMPLE_LEN, MAX_SAMPLE_VALUE_LEN};
use crate::llm::Llm;
use crate::model::{Attribute, Term};

pub fn term_text(t: &Term) -> String {
    format!("Term: {}. {}", t.name, t.description).trim().to_string()
}

fn sample_block(a: &Attribute) -> String {
    let max = if a.datatype.to_lowercase().contains("json") { MAX_EMBEDDED_JSON_SAMPLE_LEN } else { MAX_SAMPLE_VALUE_LEN };
    let v = stringify_samples(&a.sample_values, Some(max));
    if v.is_empty() {
        String::new()
    } else {
        format!(" Sample values: {}.", v.join(", "))
    }
}

pub fn attribute_text(a: &Attribute, synonyms: &[String]) -> String {
    let syn = if synonyms.is_empty() { String::new() } else { format!(" ({})", synonyms.join(", ")) };
    format!("ColumnAttribute: {} of Term {}{syn}. {}{}", a.name, a.term_name, a.description.as_deref().unwrap_or(""), sample_block(a))
        .trim()
        .to_string()
}

/// Embeds one table's Terms and ColumnAttributes in a single batch (`SemanticEmbedder.embed_term`).
/// Without an embeddings endpoint, or when the call fails, the elements stay unembedded.
pub async fn embed_elements(llm: &Llm, terms: &mut [Term], attrs: &mut [Attribute]) -> usize {
    if !llm.embed_configured() || (terms.is_empty() && attrs.is_empty()) {
        return 0;
    }
    let mut texts: Vec<String> = terms.iter().map(term_text).collect();
    for a in attrs.iter() {
        let syn = terms.iter().find(|t| t.id == a.term_id).map(|t| t.synonyms.clone()).unwrap_or_default();
        texts.push(attribute_text(a, &syn));
    }
    match llm.embed(&texts).await {
        Ok(v) => {
            let n = v.len();
            let mut it = v.into_iter();
            for t in terms.iter_mut() {
                t.embedding = it.next();
            }
            for a in attrs.iter_mut() {
                a.embedding = it.next();
            }
            n
        }
        Err(e) => {
            tracing::warn!(error = %e, "embedding failed; the elements stay unembedded");
            0
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn texts() {
        let t = Term { name: "Purchase Order".into(), description: "An order.".into(), ..Default::default() };
        assert_eq!(term_text(&t), "Term: Purchase Order. An order.");
        let a = Attribute {
            name: "Order Status".into(),
            term_name: "Purchase Order".into(),
            datatype: "varchar".into(),
            description: Some("Where the order is.".into()),
            sample_values: vec![json!("paid"), json!("x".repeat(31)), json!(3)],
            ..Default::default()
        };
        assert_eq!(
            attribute_text(&a, &["PO".into()]),
            "ColumnAttribute: Order Status of Term Purchase Order (PO). Where the order is. Sample values: paid, 3."
        );
    }
}
