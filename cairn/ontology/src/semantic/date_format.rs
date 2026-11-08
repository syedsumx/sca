// SPDX-License-Identifier: Apache-2.0
// Ported from NVIDIA auto-ontology: auto_ontology/semantic/date_format.py (and its tests).
// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Changes: rewritten in Rust; a small strptime with Python's directive grammar replaces `datetime.strptime`.
//! Infer how a date column stores its values.
//!
//! Date columns are kept out of sample-value prompts because a concrete date carries no business meaning, but
//! a query has to compare against the stored form: `950324` and `1995-03-24` describe the same day and admit no
//! common predicate. The format is inferred by validation, not by shape: each candidate pattern is tried against
//! every sampled value, and only one that parses all of them is reported, so an impossible reading fails on its
//! own (`13/07/2011` cannot be month-first) and a column that disagrees with itself yields no claim at all.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

/// Declared type substrings that make a column temporal.
pub const DATE_TYPE_TOKENS: &[&str] = &["date", "time", "timestamp", "datetime"];
const MAX_VALUES: usize = 200;
const MAX_VALUE_LEN: usize = 40;
const YEAR_MIN: i32 = 1900;
const YEAR_MAX: i32 = 2099;

/// Candidates in descending specificity, each with the notation reported to the model.
const CANDIDATES: &[(&str, &str)] = &[
    ("%Y-%m-%d %H:%M:%S.%f", "YYYY-MM-DD HH:MM:SS.f"),
    ("%Y-%m-%dT%H:%M:%S.%f", "YYYY-MM-DDTHH:MM:SS.f"),
    ("%Y-%m-%d %H:%M:%S", "YYYY-MM-DD HH:MM:SS"),
    ("%Y-%m-%dT%H:%M:%S", "YYYY-MM-DDTHH:MM:SS"),
    ("%Y-%m-%d %H:%M", "YYYY-MM-DD HH:MM"),
    ("%Y-%m-%d", "YYYY-MM-DD"),
    ("%Y/%m/%d", "YYYY/MM/DD"),
    ("%d/%m/%Y", "DD/MM/YYYY"),
    ("%m/%d/%Y", "MM/DD/YYYY"),
    ("%d-%m-%Y", "DD-MM-YYYY"),
    ("%m-%d-%Y", "MM-DD-YYYY"),
    ("%d.%m.%Y", "DD.MM.YYYY"),
    ("%d/%m/%y", "DD/MM/YY"),
    ("%m/%d/%y", "MM/DD/YY"),
    ("%d-%m-%y", "DD-MM-YY"),
    ("%m-%d-%y", "MM-DD-YY"),
    ("%d.%m.%y", "DD.MM.YY"),
    ("%Y%m%d", "YYYYMMDD"),
    ("%Y%m", "YYYYMM"),
    ("%y%m%d", "YYMMDD"),
    ("%H:%M:%S", "HH:MM:SS"),
    ("%H:%M", "HH:MM"),
    ("%Y", "YYYY"),
];

/// Readings that differ only in which component comes first: when both parse every value, say nothing.
const INDISTINGUISHABLE: &[(&str, &str)] = &[
    ("DD/MM/YYYY", "MM/DD/YYYY"),
    ("DD-MM-YYYY", "MM-DD-YYYY"),
    ("DD/MM/YY", "MM/DD/YY"),
    ("DD-MM-YY", "MM-DD-YY"),
    ("DD.MM.YY", "MM.DD.YY"),
    ("D/M/YY", "M/D/YY"),
    ("D-M-YY", "M-D-YY"),
    ("D.M.YY", "M.D.YY"),
    ("D/M/YYYY", "M/D/YYYY"),
    ("D-M-YYYY", "M-D-YYYY"),
    ("D.M.YYYY", "M.D.YYYY"),
];

pub fn is_date_type(data_type: Option<&str>) -> bool {
    let Some(t) = data_type else { return false };
    let l = t.to_lowercase();
    DATE_TYPE_TOKENS.iter().any(|tok| l.contains(tok))
}

/// A strptime pattern compiled the way CPython's `_strptime` does (same sub-patterns per directive), plus
/// which directive each capture group is.
struct Compiled {
    re: Regex,
    groups: Vec<char>,
}

fn compile(pattern: &str) -> Compiled {
    let mut re = String::from("(?i)^");
    let mut groups = vec![];
    let mut chars = pattern.chars();
    while let Some(c) = chars.next() {
        if c == '%' {
            let d = chars.next().unwrap_or('%');
            let sub = match d {
                'Y' => r"(\d\d\d\d)",
                'y' => r"(\d\d)",
                'm' => r"(1[0-2]|0[1-9]|[1-9])",
                'd' => r"(3[01]|[12]\d|0[1-9]|[1-9]| [1-9])",
                'H' => r"(2[0-3]|[0-1]\d|\d)",
                'M' => r"([0-5]\d|\d)",
                'S' => r"(6[0-1]|[0-5]\d|\d)",
                'f' => r"([0-9]{1,6})",
                _ => r"(%)",
            };
            re.push_str(sub);
            groups.push(d);
        } else if c.is_whitespace() {
            re.push_str(r"\s+");
        } else {
            re.push_str(&regex::escape(&c.to_string()));
        }
    }
    re.push('$');
    Compiled { re: Regex::new(&re).expect("date pattern"), groups }
}

fn compiled() -> &'static Vec<Compiled> {
    static C: OnceLock<Vec<Compiled>> = OnceLock::new();
    C.get_or_init(|| CANDIDATES.iter().map(|(p, _)| compile(p)).collect())
}

fn days_in_month(year: i32, month: u32) -> u32 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 => 29,
        _ => 28,
    }
}

/// The year a value reads as under `c`, when it parses (date validity included); None otherwise.
fn parse(c: &Compiled, value: &str) -> Option<i32> {
    let caps = c.re.captures(value)?;
    let (mut year, mut month, mut day) = (1900i32, 1u32, 1u32);
    for (i, d) in c.groups.iter().enumerate() {
        let s = caps.get(i + 1)?.as_str().trim();
        match d {
            'Y' => year = s.parse().ok()?,
            'y' => {
                let y: i32 = s.parse().ok()?;
                year = if y < 69 { 2000 + y } else { 1900 + y };
            }
            'm' => month = s.parse().ok()?,
            'd' => day = s.parse().ok()?,
            _ => {}
        }
    }
    if year < 1 || day > days_in_month(year, month) {
        return None;
    }
    Some(year)
}

/// Whether every value reads as the pattern in a believable year (`%Y` only: two digits carry no century).
fn parses_all(i: usize, values: &[String]) -> bool {
    let c = &compiled()[i];
    let century = CANDIDATES[i].0.contains("%Y");
    values.iter().all(|v| match parse(c, v) {
        Some(y) => !century || (YEAR_MIN..=YEAR_MAX).contains(&y),
        None => false,
    })
}

/// A fractional-seconds notation restated at the width actually stored (the widest, when it varies).
fn widen_fraction(label: &str, values: &[String]) -> String {
    let widest = values.iter().filter(|v| v.contains('.')).map(|v| v.rsplit('.').next().unwrap_or("").len()).max().unwrap_or(1);
    label.replace(".f", &format!(".{}", "f".repeat(widest)))
}

/// A date-only notation restated at the digit width stored: `4/4/20` is `M/D/YY`, which `04/04/20` never matches.
fn narrow_padding(label: &str, values: &[String]) -> String {
    if label.contains(' ') || label.contains('T') || label.contains(':') {
        return label.to_string();
    }
    let Some(sep) = ['/', '-', '.'].into_iter().find(|s| label.contains(*s)) else { return label.to_string() };
    if values.iter().any(|v| v.split(sep).any(|p| p.len() == 1)) {
        label.replace("MM", "M").replace("DD", "D")
    } else {
        label.to_string()
    }
}

fn render(v: &Value) -> Option<String> {
    match v {
        Value::Null => None,
        Value::String(s) => Some(s.clone()),
        other => Some(other.to_string()),
    }
}

/// The notation every sampled value follows (`YYYY-MM-DD`), or None when that is not one thing.
pub fn infer_date_format<'a>(values: impl IntoIterator<Item = &'a Value>) -> Option<String> {
    let mut seen: Vec<String> = vec![];
    for v in values {
        let Some(text) = render(v) else { continue };
        let text = text.trim().to_string();
        if text.is_empty() || text.chars().count() > MAX_VALUE_LEN || seen.contains(&text) {
            continue;
        }
        seen.push(text);
        if seen.len() >= MAX_VALUES {
            break;
        }
    }
    if seen.is_empty() {
        return None;
    }
    let matches: Vec<(usize, String)> =
        (0..CANDIDATES.len()).filter(|i| parses_all(*i, &seen)).map(|i| (i, narrow_padding(CANDIDATES[i].1, &seen))).collect();
    let (first, winner) = matches.first()?.clone();
    let labels: Vec<&str> = matches.iter().map(|(_, l)| l.as_str()).collect();
    for (a, b) in INDISTINGUISHABLE {
        if (winner == *a || winner == *b) && labels.contains(a) && labels.contains(b) {
            return None;
        }
    }
    Some(if CANDIDATES[first].0.contains("%f") { widen_fraction(&winner, &seen) } else { winner })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn f(values: &[&str]) -> Option<String> {
        let v: Vec<Value> = values.iter().map(|s| json!(s)).collect();
        infer_date_format(v.iter())
    }

    #[test]
    fn unambiguous_notations_are_reported() {
        assert_eq!(f(&["2012-08-24", "1990-01-01", "2019-12-31"]).as_deref(), Some("YYYY-MM-DD"));
        assert_eq!(f(&["2012-08-24 15:04:05", "1990-01-01 00:00:00"]).as_deref(), Some("YYYY-MM-DD HH:MM:SS"));
        assert_eq!(f(&["2012-08-24T15:04:05", "1990-01-01T00:00:00"]).as_deref(), Some("YYYY-MM-DDTHH:MM:SS"));
        assert_eq!(f(&["2014/07/09", "2015/01/31"]).as_deref(), Some("YYYY/MM/DD"));
        assert_eq!(f(&["20120824", "19900101"]).as_deref(), Some("YYYYMMDD"));
        assert_eq!(f(&["12:30:00", "09:15:45"]).as_deref(), Some("HH:MM:SS"));
    }

    #[test]
    fn fractional_seconds_are_reported_at_the_width_stored() {
        assert_eq!(f(&["2010-07-19 19:39:07.0", "2010-09-15 21:08:26.0"]).as_deref(), Some("YYYY-MM-DD HH:MM:SS.f"));
        assert_eq!(f(&["2010-07-19 19:39:07.123456"]).as_deref(), Some("YYYY-MM-DD HH:MM:SS.ffffff"));
        assert_eq!(f(&["2010-07-19 19:39:07.0", "2010-09-15 21:08:26.123"]).as_deref(), Some("YYYY-MM-DD HH:MM:SS.fff"));
    }

    #[test]
    fn day_first_is_settled_by_a_day_past_the_twelfth() {
        assert_eq!(f(&["03/04/2011", "13/07/2011"]).as_deref(), Some("DD/MM/YYYY"));
        assert_eq!(f(&["04/03/2011", "07/13/2011"]).as_deref(), Some("MM/DD/YYYY"));
        assert_eq!(f(&["03/04/2011", "05/06/2011"]), None);
    }

    #[test]
    fn six_digits_split_by_whether_the_leading_four_are_a_year() {
        assert_eq!(f(&["201208", "201301", "201412"]).as_deref(), Some("YYYYMM"));
        assert_eq!(f(&["950324", "970213", "981231"]).as_deref(), Some("YYMMDD"));
    }

    #[test]
    fn mixed_or_non_dates_yield_nothing() {
        assert_eq!(f(&["2012-08-24", "24/08/2012"]), None);
        assert_eq!(f(&["not a date", "also not"]), None);
        assert_eq!(f(&[]), None);
        assert_eq!(infer_date_format([json!(null), json!(""), json!("   ")].iter()), None);
        assert_eq!(f(&["9503-01-01"]), None);
        assert_eq!(f(&["2011-02-30"]), None);
    }

    #[test]
    fn nulls_duplicates_and_padding() {
        let v = [json!(null), json!("2012-08-24"), json!("2012-08-24"), json!(""), json!("1990-01-01")];
        assert_eq!(infer_date_format(v.iter()).as_deref(), Some("YYYY-MM-DD"));
        assert_eq!(f(&["4/4/20", "12/25/20"]).as_deref(), Some("M/D/YY"));
    }

    #[test]
    fn date_types() {
        assert!(is_date_type(Some("DATE")));
        assert!(is_date_type(Some("TIMESTAMP WITHOUT TIME ZONE")));
        assert!(!is_date_type(Some("TEXT")));
        assert!(!is_date_type(None));
    }
}
