// SPDX-License-Identifier: Apache-2.0
// The prompt text in this file is ported from NVIDIA auto-ontology:
// auto_ontology/semantic/{deterministic.py, fk_suggester.py, term_extractor.py, semantic_fk.py,
// sql_attribute_suggester.py, bridge_tables.py, domain.py} and auto_ontology/semantic/models.py.
// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Changes: the prompts are reproduced verbatim; the structured-output models are rewritten as Rust types with
// JSON Schemas for the chat-completions `response_format`.
//! Prompts and the structured answers the model gives to them.

use serde::Deserialize;
use serde_json::{json, Value};

// ------------------------------------------------------------------------------- column descriptions

pub const DESCRIPTION_SYSTEM: &str = "You are a data analyst documenting the columns of a relational table for a \
semantic layer. For every column you are given, write ONE concise sentence \
describing what the column represents in business terms.

Rules:
- Use the column name, data type, and sample values as evidence.
- Keep each description to a single, factual sentence — no speculation.
- Return exactly one entry per column provided, using the physical column name.";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ColumnDescription {
    pub column_name: String,
    #[serde(default)]
    pub description: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ColumnDescriptionResult {
    #[serde(default)]
    pub descriptions: Vec<ColumnDescription>,
}

pub fn column_description_schema() -> Value {
    object(json!({"descriptions": array(object(json!({
        "column_name": {"type": "string", "description": "Physical column name — must match a provided column."},
        "description": {"type": "string", "description": "One concise sentence describing what the column represents."}
    })))}))
}

// ----------------------------------------------------------------------------------- FK suggestion

pub const FK_SYSTEM: &str = "You review relational table metadata and produce three outputs:

1. fk_suggestions — columns that are likely foreign keys but are not already declared as
   FOREIGN_KEY or primary-key columns.

   A likely foreign key typically:
   - ends with _id or Id and has a meaningful prefix naming a different entity (e.g. customer_id,
     orderId) — NOT a bare \"id\"/\"_id\"/\"Id\" column or one whose prefix matches the table name,
     unless it is marked is_unique: false
   - has a description that contains words like \"references\", \"identifier of\", or names another table
   - has an integer or string type consistent with identifiers
   - semantically points to a row in another table
   - is UUID-typed and is not the table's own primary key

   Omit columns that are measures, physical measurements/dimensions (e.g.
   weight, height, length, width, size, volume), name/label columns (e.g.
   name, title, label, description), timestamps, free text, flags, or
   otherwise unlikely to reference another table. Return an empty list when
   none qualify.

2. pk_column_names — columns that appear to be the table's own primary key even if not
   explicitly declared as such. These are typically a bare \"id\", \"uuid\", or \"<table_name>_id\"
   column of integer or UUID type whose description or name conveys it identifies the table's
   own records. Usually empty or one entry. Only list columns from the candidate list.

3. is_junction_table and junction_table_rationale — classify the table's row grain.
   Set is_junction_table true ONLY when each row represents an association between at least
   two entity roles. At least two columns must be foreign-key roles; two distinct roles may
   point to the same entity. Extra columns may be a surrogate key or relationship properties
   such as role, quantity, ordering, status, or effective dates, but they must describe the
   association rather than establish an independent entity grain.

   Set it false for:
   - an entity table that merely contains ordinary foreign keys
   - a satellite, extension, or detail table with one parent FK plus descriptive attributes
   - lookup/reference tables
   - self-hierarchies with only one relationship role (for example id + parent_id)
   - event or transaction tables whose rows have their own business identity or lifecycle

   Be conservative. Multiple foreign keys alone do not make a table a junction table.

The is_unique marker comes from sampled column profiling. A column marked is_unique: false
contains repeated values, so it cannot be the table's own primary key. Never include it in
pk_column_names. If it is identifier-like, including a bare \"id\", evaluate it as a possible
foreign key using its name, type, description, and samples. Non-uniqueness alone does not prove
that a non-identifier attribute is a foreign key.";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PotentialFkSuggestion {
    pub column_name: String,
    #[serde(default)]
    pub rationale: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FkAndPkResult {
    #[serde(default)]
    pub fk_suggestions: Vec<PotentialFkSuggestion>,
    #[serde(default)]
    pub pk_column_names: Vec<String>,
    #[serde(default)]
    pub is_junction_table: bool,
    #[serde(default)]
    pub junction_table_rationale: String,
}

pub fn fk_schema() -> Value {
    object(json!({
        "fk_suggestions": array(object(json!({
            "column_name": {"type": "string", "description": "Physical column name that likely references another table."},
            "rationale": {"type": "string", "description": "Brief reason this column looks like a foreign key."}
        }))),
        "pk_column_names": {"type": "array", "items": {"type": "string"},
            "description": "Column names that appear to be the table's own primary key even if not declared as such. Usually empty or one entry."},
        "is_junction_table": {"type": "boolean",
            "description": "True only when each row represents an association between at least two entity roles."},
        "junction_table_rationale": {"type": "string", "description": "Brief reason for the junction-table decision."}
    }))
}

// ---------------------------------------------------------------------------------- term extraction

pub const TERM_SYSTEM: &str = "You propose business Terms for a relational table and assign candidate columns \
to each Term with user-friendly display labels.

Rules:
1. Strongly prefer ONE Term per table. A second Term is warranted ONLY when the \
table is a true junction between two unrelated entities. Never split a single \
entity into separate Terms for its core fields versus its metadata, lifecycle, \
or audit fields — those all belong to the same Term. If in doubt, use one Term.
2. Term names must be user-friendly with spaces between words (e.g. Purchase Order, \
not purchase_orders or PurchaseOrder).
3. Assign EVERY candidate column to exactly one Term. For each assignment return \
source_column exactly as given and a display_name — a user-friendly ColumnAttribute \
label with spaces between words (e.g. Order Date, Total Amount).
4. Do not propose IS_A, PART_OF, or ROLE relationships.
5. If the table name contains a word that indicates it is a variant of another table \
(e.g. ARC, ARCHIVE, HIST, HISTORY, STAGING, DELETED, TEMP), prefix every display_name \
with a qualifying word derived from that suffix \
(e.g. PEOPLE_ARC → \"Archived Is Sales Person\", ORDERS_HIST → \"Historical Total Amount\").";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TermColumnRef {
    pub source_column: String,
    pub display_name: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawTermProposal {
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub attributes: Vec<TermColumnRef>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawTableTermsResult {
    pub terms: Vec<RawTermProposal>,
}

pub fn terms_schema() -> Value {
    object(json!({"terms": {"type": "array",
    "description": "Usually one Term. Propose multiple only when columns clearly belong to distinct business concepts.",
    "items": object(json!({
        "name": {"type": "string", "description": "User-friendly business Term name with spaces between words (e.g. Purchase Order)."},
        "description": {"type": "string", "description": "Short business definition."},
        "attributes": array(object(json!({
            "source_column": {"type": "string", "description": "Physical column name — must match a candidate column."},
            "display_name": {"type": "string", "description": "User-friendly ColumnAttribute label with spaces between words (e.g. Order Date, Total Amount)."}
        })))
    }))}}))
}

pub const SYNONYM_SYSTEM: &str = "Your only task is to extract alternate names for a business entity from a table description.

Look for abbreviations, acronyms, or aliases that are explicitly written — for example \
through phrases like \"also referred to as\", \"also known as\", \"abbreviated as\", \
\"short for\", \"stands for\", or a parenthetical directly after the entity name such as \
\"business unit (BU)\".

Return each alternate name verbatim as a separate entry in synonyms.
Example: \"business unit (BU)\" → synonyms: [\"BU\"]

Return an empty list if no alternate names are explicitly stated. \
Do NOT infer, guess, or add any name that is not directly written in the description.";

/// Extra fields are ignored here (`extra="ignore"` upstream).
#[derive(Debug, Deserialize)]
pub struct SynonymExtraction {
    #[serde(default)]
    pub synonyms: Vec<String>,
}

pub fn synonyms_schema() -> Value {
    object(json!({"synonyms": {"type": "array", "items": {"type": "string"},
        "description": "Abbreviations, acronyms, or alternate names explicitly written in the description. Empty list if none are stated."}}))
}

// ---------------------------------------------------------------------------------- semantic FKs

pub const FK_HIT_SYSTEM: &str = "You are a database schema expert. You will be given a foreign-key column \
description and a list of candidate primary-key columns retrieved from a \
vector search. Each candidate is prefixed with its unique column_id. \
Your task is to decide which candidate (if any) is the column that the \
foreign key references.

Rules:
- Return the exact column_id of the best matching candidate, or null if none fit.
- Only pick a candidate when you are confident it is the PK being referenced.
- Do NOT guess. If unsure, return null.
";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FkHitSelection {
    pub selected_id: Option<String>,
    #[serde(default)]
    pub rationale: String,
}

pub fn fk_hit_schema() -> Value {
    object(json!({
        "selected_id": {"type": ["string", "null"], "description": "The column_id of the candidate that is the primary-key column this FK references, exactly as shown in the candidate list. Return null if none of the candidates are a confident match."},
        "rationale": {"type": "string"}
    }))
}

pub const JOIN_DEFAULT_SYSTEM: &str = "You are choosing the default foreign-key relationship used to join two database
tables. Several columns on the same source table reference the same target column,
but each column can represent a different business role.

Choose the relationship that most naturally represents an unqualified join between
the source and target tables. Return exactly one supplied source_column_id. Return
null only when the schema context is genuinely insufficient. Never invent an ID.
";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JoinDefaultSelection {
    pub selected_column_id: Option<String>,
    #[serde(default)]
    pub rationale: String,
}

pub fn join_default_schema() -> Value {
    object(json!({
        "selected_column_id": {"type": ["string", "null"], "description": "The exact source-column ID of the relationship that best represents the default join between the supplied tables, or null when uncertain."},
        "rationale": {"type": "string"}
    }))
}

// ------------------------------------------------------------------------------- SqlAttributes

pub const SQL_ATTR_SYSTEM: &str = "You are an expert data engineer reviewing SQL expressions to decide \
which ones represent reusable business logic worth naming. \
A SqlAttribute is a named, reusable SQL expression — a filter, \
aggregation, or calculation — that captures a clear business concept \
and is specific enough to be useful while general enough to recur.";

pub fn sql_attr_user(term_name: &str, term_description: &str, expr_block: &str) -> String {
    let desc = if term_description.is_empty() { "(none)" } else { term_description };
    format!(
        "Business term: {term_name}\n\
Description: {desc}\n\n\
Top SQL expressions extracted from queries on tables for this term \
(ranked by 3-month execution count):\n\n\
{expr_block}\n\n\
Which expressions should become a named SqlAttribute for the term \
'{term_name}'?\n\n\
Rules you MUST follow:\n  \
1. ALWAYS mark an expression as a SqlAttribute if it compares a column \
against a literal value (e.g. status = 'active', amount > 1000, \
type IN ('A','B')). Literal values encode business thresholds and \
categories that are exactly what SqlAttributes are designed to capture. \
Exception: do NOT promote an expression whose only literal is an integer \
that looks like a surrogate or primary-key identifier \
(e.g. request_id = 50000, id = 12). Such values are instance-specific \
record lookups, not reusable business logic. Only promote an expression \
containing such an integer if the surrounding context also encodes a \
reusable business rule (e.g. the same WHERE clause additionally filters \
on a status, category, or date range).\n  \
2. NEVER mark an expression as a SqlAttribute if it only references \
columns from the same table with no literal value, constant, or \
cross-table condition (e.g. start_date < end_date). Such expressions \
describe structural integrity, not reusable business logic.\n  \
4. NEVER mark an IS NULL / IS NOT NULL check on a surrogate or \
primary-key column (any column named 'id' or ending in '_id') as a \
SqlAttribute. These are LEFT JOIN absence checks — structural plumbing \
that detects whether a join matched, not a business concept \
(e.g. request_task_collaborators.id IS NULL). \
Exception: IS NULL on non-key columns that carry business meaning IS \
valid (e.g. deleted_at IS NULL, approved_by IS NULL).\n  \
3. ALWAYS mark a JOIN ON condition or a UNION/INTERSECT/EXCEPT branch \
as a SqlAttribute when it appears frequently AND any of the following \
is true:\n     \
a. It encodes obvious business logic — \
e.g. a join that filters to active records, a union arm that defines a \
named sub-population, or a cross-table condition capturing a business \
relationship.\n     \
b. It is used to define a meaningful business alias — e.g. a SELECT \
branch or subquery whose result column is aliased with a descriptive \
business name (e.g. 'AS active_assignees', 'AS overdue_tasks'). \
The alias itself signals that the expression represents a named concept \
worth capturing.\n     \
c. A UNION combines two or more SELECT branches that each use a \
different join path to reach the same entity type (e.g. one branch joins \
via a direct ownership FK, another via a membership/collaborator table). \
This pattern structurally defines a named aggregate concept — all members \
of a group regardless of their role. Promote the whole UNION as a \
SqlAttribute representing that concept (e.g. 'Task Participants', \
'Order Stakeholders'). When naming and describing it, ignore any \
instance-specific WHERE predicates (like record-ID literals) and focus \
on the reusable structural pattern.\n     \
Ignore pure FK-only joins and UNION branches that select from a \
single table with no join or filter.\n\n\
For each chosen expression provide:\n  \
• name — user-friendly Title Case label with spaces between words, \
matching the ColumnAttribute naming style \
(e.g. 'Active Customer Filter', 'Revenue Above Threshold', \
'Last 90 Days Activity')\n  \
• description — what business logic it captures, why it matters for \
this term, and when a data consumer would apply it\n  \
• expression — the exact SQL expression copied verbatim\n\n\
Return an empty list if none qualify."
    )
}

#[derive(Debug, Deserialize)]
pub struct SuggestedAttr {
    pub name: String,
    pub description: String,
    pub expression: String,
}

#[derive(Debug, Deserialize)]
pub struct Suggestions {
    pub suggestions: Vec<SuggestedAttr>,
}

fn named_sql() -> Value {
    object(json!({"name": {"type": "string"}, "description": {"type": "string"}, "expression": {"type": "string"}}))
}

pub fn suggestions_schema() -> Value {
    object(json!({"suggestions": array(named_sql())}))
}

// --------------------------------------------------------------------------------- bridge tables

pub const BRIDGE_SYSTEM: &str = "You are an expert data engineer defining reusable join patterns \
for many-to-many junction (bridge) tables. Bridge tables connect \
entities through foreign keys, including self-referential cases \
where multiple columns point at the same target table.";

pub const BRIDGE_SELF_HINT: &str = "This is a self-referential bridge: both FK columns point at the same \
entity table. Alias that table twice in the JOIN (e.g. product AS buyer \
and product AS also_bought) so each FK role is clear.";

pub const BRIDGE_JOIN_HINT: &str = "Join the bridge table to each related entity table using the FK pairs.";

pub fn bridge_user(qualified_bridge: &str, related: &str, fk_block: &str, join_hint: &str) -> String {
    let related = if related.is_empty() { "(unknown)" } else { related };
    format!(
        "Bridge table: {qualified_bridge}\n\
Related tables: {related}\n\
Foreign key columns (use these exact join keys):\n\
{fk_block}\n\n\
{join_hint}\n\n\
Produce one SqlAttribute that captures how to join through this \
bridge table to reach all related entities.\n\n\
Rules:\n  \
• expression must be a valid SELECT … FROM … JOIN … statement\n  \
• schema-qualify every table name\n  \
• use ONLY the FK pairs listed above — do not invent columns \
or filters\n  \
• no WHERE clause with literal business filters; structural \
joins only\n  \
• name — user-friendly Title Case label (e.g. 'Order Products', \
'Also Buy')\n  \
• description — what relationship the junction represents\n"
    )
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BridgeProposal {
    pub name: String,
    pub description: String,
    pub expression: String,
}

pub fn bridge_schema() -> Value {
    named_sql()
}

// ----------------------------------------------------------------------------------------- helpers

/// A strict object schema: every property required, nothing else allowed.
fn object(props: Value) -> Value {
    let required: Vec<String> = props.as_object().map(|m| m.keys().cloned().collect()).unwrap_or_default();
    json!({"type": "object", "additionalProperties": false, "required": required, "properties": props})
}

fn array(items: Value) -> Value {
    json!({"type": "array", "items": items})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schemas_are_strict_and_answers_validate() {
        let s = fk_schema();
        assert_eq!(s["additionalProperties"], json!(false));
        assert_eq!(s["required"].as_array().unwrap().len(), 4);
        let ok: FkAndPkResult = serde_json::from_value(json!({"fk_suggestions": [{"column_name": "customer_id", "rationale": "r"}],
            "pk_column_names": ["id"], "is_junction_table": false, "junction_table_rationale": ""}))
        .unwrap();
        assert_eq!(ok.fk_suggestions[0].column_name, "customer_id");
        assert!(serde_json::from_value::<FkAndPkResult>(json!({"fk_suggestions": [], "extra": 1})).is_err());
        // Synonyms ignore extra fields, as upstream.
        assert!(serde_json::from_value::<SynonymExtraction>(json!({"synonyms": ["BU"], "x": 1})).is_ok());
        assert!(sql_attr_user("Order", "", "1. [3 uses] x > 1").contains("Description: (none)"));
    }
}
