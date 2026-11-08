// SPDX-License-Identifier: Apache-2.0
//! The ontology as a graph: JSON (nodes and edges), JSON-LD and RDF Turtle (OWL classes for Terms, datatype
//! properties for ColumnAttributes, object properties for relationships, SKOS labels for synonyms).

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{json, Value};

use crate::catalog::TableIndex;
use crate::model::Ontology;

/// The part of the ontology over `tables` only (every element's tables must be in the set). None: everything.
pub fn restrict(o: &Ontology, tables: Option<&BTreeSet<String>>) -> Ontology {
    let Some(keep) = tables else { return o.clone() };
    let ok = |t: &String| keep.contains(t);
    Ontology {
        run_id: o.run_id.clone(),
        version: o.version,
        terms: o.terms.iter().filter(|t| ok(&t.table_id)).cloned().collect(),
        attributes: o.attributes.iter().filter(|a| ok(&a.table_id)).cloned().collect(),
        relationships: o.relationships.iter().filter(|r| ok(&r.source_table) && ok(&r.target_table)).cloned().collect(),
        metrics: o.metrics.iter().filter(|m| m.tables.iter().all(ok)).cloned().collect(),
        bridges: o.bridges.iter().filter(|b| ok(&b.table_id) && b.fk_pairs.iter().all(|p| ok(&p.target_table))).cloned().collect(),
        tables: o.tables.iter().filter(|t| ok(&t.table_id)).cloned().collect(),
    }
}

fn term_of_table(o: &Ontology) -> BTreeMap<&str, &str> {
    let mut m = BTreeMap::new();
    for t in &o.terms {
        m.entry(t.table_id.as_str()).or_insert(t.id.as_str());
    }
    m
}

pub fn to_json(o: &Ontology, tables: &TableIndex) -> Value {
    let mut nodes = vec![];
    let mut edges = vec![];
    let mut seen_tables = BTreeSet::new();
    let mut table_node = |id: &str, nodes: &mut Vec<Value>| {
        if seen_tables.insert(id.to_string()) {
            let label = tables.get(id).map(|t| t.qualified()).unwrap_or_else(|| id.to_string());
            nodes.push(json!({"id": id, "kind": "table", "label": label}));
        }
    };
    for t in &o.terms {
        nodes.push(json!({"id": t.id, "kind": "term", "label": t.name, "description": t.description, "synonyms": t.synonyms}));
        table_node(&t.table_id, &mut nodes);
        edges.push(json!({"source": t.id, "target": t.table_id, "kind": "represents"}));
    }
    for a in &o.attributes {
        nodes.push(json!({"id": a.id, "kind": "attribute", "label": a.name, "column": a.column, "datatype": a.datatype}));
        edges.push(json!({"source": a.term_id, "target": a.id, "kind": "has_attribute"}));
    }
    let terms = term_of_table(o);
    for r in &o.relationships {
        table_node(&r.source_table, &mut nodes);
        table_node(&r.target_table, &mut nodes);
        edges.push(json!({"source": r.source_table, "target": r.target_table, "kind": "references", "id": r.id,
            "on": format!("{} = {}", r.source_column, r.target_column), "confidence": r.confidence,
            "provenance": r.provenance.iter().map(|p| p.source.clone()).collect::<Vec<_>>(),
            "source_term": terms.get(r.source_table.as_str()), "target_term": terms.get(r.target_table.as_str())}));
    }
    for m in &o.metrics {
        nodes.push(json!({"id": m.id, "kind": "metric", "label": m.name, "sql": m.sql, "source": m.source}));
        edges.push(json!({"source": m.term_id, "target": m.id, "kind": "has_metric"}));
    }
    json!({"run_id": o.run_id, "version": o.version, "nodes": nodes, "edges": edges})
}

const NS: &str = "urn:cairn:ontology:";

fn iri(kind: &str, id: &str) -> String {
    format!("{NS}{kind}:{}", id.replace(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '-'), "_"))
}

pub fn to_jsonld(o: &Ontology, tables: &TableIndex) -> Value {
    let mut graph = vec![];
    let terms = term_of_table(o);
    for t in &o.terms {
        graph.push(json!({"@id": iri("term", &t.id), "@type": "owl:Class", "rdfs:label": t.name,
            "rdfs:comment": t.description, "skos:altLabel": t.synonyms,
            "cairn:table": t.table_id, "cairn:tableName": t.table_name}));
    }
    for a in &o.attributes {
        graph.push(json!({"@id": iri("attribute", &a.id), "@type": "owl:DatatypeProperty", "rdfs:label": a.name,
            "rdfs:comment": a.description, "rdfs:domain": {"@id": iri("term", &a.term_id)},
            "cairn:column": a.column, "cairn:datatype": a.datatype, "cairn:format": a.format}));
    }
    for r in &o.relationships {
        let st = tables.get(&r.source_table).map(|t| t.qualified()).unwrap_or_default();
        let tt = tables.get(&r.target_table).map(|t| t.qualified()).unwrap_or_default();
        let mut node = json!({"@id": iri("relationship", &r.id), "@type": "owl:ObjectProperty",
            "rdfs:label": format!("{st}.{} → {tt}.{}", r.source_column, r.target_column),
            "cairn:joinOn": format!("{st}.{} = {tt}.{}", r.source_column, r.target_column),
            "cairn:cardinality": r.cardinality, "cairn:confidence": r.confidence,
            "cairn:provenance": r.provenance.iter().map(|p| p.source.clone()).collect::<Vec<_>>()});
        if let Some(s) = terms.get(r.source_table.as_str()) {
            node["rdfs:domain"] = json!({"@id": iri("term", s)});
        }
        if let Some(t) = terms.get(r.target_table.as_str()) {
            node["rdfs:range"] = json!({"@id": iri("term", t)});
        }
        graph.push(node);
    }
    for m in &o.metrics {
        graph.push(json!({"@id": iri("metric", &m.id), "@type": "cairn:Metric", "rdfs:label": m.name,
            "rdfs:comment": m.description, "cairn:sql": m.sql, "cairn:term": {"@id": iri("term", &m.term_id)}}));
    }
    json!({
        "@context": {"owl": "http://www.w3.org/2002/07/owl#", "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
                     "skos": "http://www.w3.org/2004/02/skos/core#", "cairn": NS},
        "@id": format!("{NS}run:{}", o.run_id),
        "@graph": graph,
    })
}

fn lit(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

pub fn to_turtle(o: &Ontology, tables: &TableIndex) -> String {
    let mut s = String::new();
    s.push_str("@prefix owl: <http://www.w3.org/2002/07/owl#> .\n@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n");
    s.push_str("@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n");
    s.push_str(&format!("@prefix cairn: <{NS}> .\n\n"));
    let terms = term_of_table(o);
    for t in &o.terms {
        s.push_str(&format!(
            "<{}> a owl:Class ;\n  rdfs:label {} ;\n  rdfs:comment {} ;\n",
            iri("term", &t.id),
            lit(&t.name),
            lit(&t.description)
        ));
        for syn in &t.synonyms {
            s.push_str(&format!("  skos:altLabel {} ;\n", lit(syn)));
        }
        s.push_str(&format!("  cairn:table {} .\n\n", lit(&t.table_id)));
    }
    for a in &o.attributes {
        s.push_str(&format!(
            "<{}> a owl:DatatypeProperty ;\n  rdfs:label {} ;\n  rdfs:domain <{}> ;\n  cairn:column {} ;\n  cairn:datatype {} .\n\n",
            iri("attribute", &a.id),
            lit(&a.name),
            iri("term", &a.term_id),
            lit(&a.column),
            lit(&a.datatype)
        ));
    }
    for r in &o.relationships {
        let st = tables.get(&r.source_table).map(|t| t.qualified()).unwrap_or_default();
        let tt = tables.get(&r.target_table).map(|t| t.qualified()).unwrap_or_default();
        s.push_str(&format!("<{}> a owl:ObjectProperty ;\n", iri("relationship", &r.id)));
        if let Some(x) = terms.get(r.source_table.as_str()) {
            s.push_str(&format!("  rdfs:domain <{}> ;\n", iri("term", x)));
        }
        if let Some(x) = terms.get(r.target_table.as_str()) {
            s.push_str(&format!("  rdfs:range <{}> ;\n", iri("term", x)));
        }
        s.push_str(&format!(
            "  cairn:joinOn {} ;\n  cairn:confidence {} ;\n  cairn:provenance {} .\n\n",
            lit(&format!("{st}.{} = {tt}.{}", r.source_column, r.target_column)),
            lit(&format!("{:.2}", r.confidence)),
            lit(&r.provenance.iter().map(|p| p.source.clone()).collect::<Vec<_>>().join(","))
        ));
    }
    for m in &o.metrics {
        s.push_str(&format!(
            "<{}> a cairn:Metric ;\n  rdfs:label {} ;\n  rdfs:comment {} ;\n  cairn:term <{}> ;\n  cairn:sql {} .\n\n",
            iri("metric", &m.id),
            lit(&m.name),
            lit(&m.description),
            iri("term", &m.term_id),
            lit(&m.sql)
        ));
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Relationship, Table, Term};

    #[test]
    fn exports_and_restriction() {
        let o = Ontology {
            run_id: "r".into(),
            version: 1,
            terms: vec![
                Term {
                    id: "t1".into(),
                    name: "Order".into(),
                    description: "An \"order\"\nplaced".into(),
                    table_id: "O".into(),
                    synonyms: vec!["PO".into()],
                    ..Default::default()
                },
                Term { id: "t2".into(), name: "Customer".into(), table_id: "C".into(), ..Default::default() },
            ],
            relationships: vec![Relationship {
                id: "r1".into(),
                source_table: "O".into(),
                source_column: "customer_id".into(),
                target_table: "C".into(),
                target_column: "id".into(),
                ..Default::default()
            }],
            ..Default::default()
        };
        let idx = TableIndex::new(vec![
            Table { id: "O".into(), schema: "s".into(), name: "orders".into(), ..Default::default() },
            Table { id: "C".into(), schema: "s".into(), name: "customers".into(), ..Default::default() },
        ]);
        let ttl = to_turtle(&o, &idx);
        assert!(ttl.contains("rdfs:comment \"An \\\"order\\\"\\nplaced\""));
        assert!(ttl.contains("cairn:joinOn \"s.orders.customer_id = s.customers.id\""));
        assert!(ttl.contains("skos:altLabel \"PO\""));
        let ld = to_jsonld(&o, &idx);
        assert_eq!(ld["@graph"].as_array().unwrap().len(), 3);
        let j = to_json(&o, &idx);
        assert!(j["edges"].as_array().unwrap().iter().any(|e| e["kind"] == "references"));
        let only: BTreeSet<String> = ["O".to_string()].into();
        let r = restrict(&o, Some(&only));
        assert_eq!((r.terms.len(), r.relationships.len()), (1, 0));
    }
}
