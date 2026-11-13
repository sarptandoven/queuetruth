//! Checks a spec against its published JSON Schema (`schemas/json`, generated from the Pydantic spec models), so
//! `queuetruth-core run` refuses what `queuetruth.runner.run` refuses without Python.
//!
//! The spec schemas use a small set of keywords; every one of them is checked here: `type`, `properties`,
//! `required`, `additionalProperties`, `enum`, `const`, `minimum`, `maximum`, `exclusiveMinimum`,
//! `exclusiveMaximum`, `minItems`, `maxItems`, `items`, `anyOf`, `oneOf`, `allOf`, `$ref` (`#/$defs/...`) and
//! `pattern`. A `pattern` must be one of `PATTERNS` (no regular-expression engine here); `KEYWORDS` lists every
//! keyword this module knows, and a test fails when a spec schema uses another one.

use serde_json::{Map, Value};

/// Keywords that constrain a value (checked) or only describe it (ignored).
#[cfg(test)]
pub(crate) const KEYWORDS: &[&str] = &[
    "type",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "items",
    "anyOf",
    "oneOf",
    "allOf",
    "$ref",
    "pattern",
    // descriptive
    "$schema",
    "$id",
    "$defs",
    "title",
    "description",
    "default",
    "examples",
    "discriminator",
];

/// A matcher of one `pattern`.
type Matcher = fn(&str) -> bool;

/// The `pattern`s the spec schemas use, each with its matcher.
pub(crate) const PATTERNS: &[(&str, Matcher)] = &[("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", is_iso_date_shape)];

fn is_iso_date_shape(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 10
        && b.iter().enumerate().all(|(i, c)| match i {
            4 | 7 => *c == b'-',
            _ => c.is_ascii_digit(),
        })
}

/// One violation: where (`latency.feed_ns`, `exclude_order_ids.0.order_id`; empty for the spec itself, as Pydantic
/// writes a location) and what.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Violation {
    pub loc: String,
    pub msg: String,
}

/// Every violation of `value` against the schema `root` (empty: valid).
pub(crate) fn check(value: &Value, root: &Value) -> Vec<Violation> {
    let mut out = Vec::new();
    node(value, root, root, "", &mut out);
    out
}

fn join(loc: &str, key: &str) -> String {
    if loc.is_empty() {
        key.to_owned()
    } else {
        format!("{loc}.{key}")
    }
}

fn resolve<'a>(schema: &'a Value, root: &'a Value) -> &'a Value {
    match schema
        .get("$ref")
        .and_then(Value::as_str)
        .and_then(|r| r.strip_prefix("#/$defs/"))
    {
        Some(name) => root.get("$defs").and_then(|d| d.get(name)).unwrap_or(schema),
        None => schema,
    }
}

fn type_ok(v: &Value, t: &str) -> bool {
    match t {
        "object" => v.is_object(),
        "array" => v.is_array(),
        "string" => v.is_string(),
        "boolean" => v.is_boolean(),
        "null" => v.is_null(),
        "number" => v.is_number(),
        "integer" => v.as_i64().is_some() || v.as_u64().is_some() || v.as_f64().is_some_and(|x| x.fract() == 0.0),
        _ => true,
    }
}

fn short(v: &Value) -> String {
    let s = v.to_string();
    if s.len() > 40 { format!("{}...", &s[..37]) } else { s }
}

fn node(v: &Value, schema: &Value, root: &Value, loc: &str, out: &mut Vec<Violation>) {
    let s = resolve(schema, root);
    let mut push = |msg: String| {
        out.push(Violation {
            loc: loc.to_owned(),
            msg,
        });
    };
    if let Some(t) = s.get("type") {
        let ok = match t {
            Value::String(t) => type_ok(v, t),
            Value::Array(ts) => ts.iter().filter_map(Value::as_str).any(|t| type_ok(v, t)),
            _ => true,
        };
        if !ok {
            push(format!(
                "expected {}, got {}",
                t.as_str().unwrap_or("one of the types"),
                short(v)
            ));
            return;
        }
    }
    if let Some(c) = s.get("const")
        && c != v
    {
        push(format!("expected {c}, got {}", short(v)));
    }
    if let Some(Value::Array(e)) = s.get("enum")
        && !e.contains(v)
    {
        let names: Vec<String> = e.iter().map(Value::to_string).collect();
        push(format!("expected one of {}, got {}", names.join(", "), short(v)));
    }
    if let Some(x) = v.as_f64() {
        let bound = |k: &str| s.get(k).and_then(Value::as_f64);
        if let Some(m) = bound("minimum")
            && x < m
        {
            push(format!("must be >= {}", s["minimum"]));
        }
        if let Some(m) = bound("maximum")
            && x > m
        {
            push(format!("must be <= {}", s["maximum"]));
        }
        if let Some(m) = bound("exclusiveMinimum")
            && x <= m
        {
            push(format!("must be > {}", s["exclusiveMinimum"]));
        }
        if let Some(m) = bound("exclusiveMaximum")
            && x >= m
        {
            push(format!("must be < {}", s["exclusiveMaximum"]));
        }
    }
    if let (Some(p), Some(text)) = (s.get("pattern").and_then(Value::as_str), v.as_str()) {
        match PATTERNS.iter().find(|(q, _)| *q == p) {
            Some((_, matches)) if matches(text) => {}
            Some(_) => push(format!("does not match {p}")),
            None => push(format!("pattern {p} is not supported by queuetruth-core")),
        }
    }
    if let Some(a) = v.as_array() {
        let n = a.len() as u64;
        if let Some(m) = s.get("minItems").and_then(Value::as_u64)
            && n < m
        {
            push(format!("needs at least {m} item{}", if m == 1 { "" } else { "s" }));
        }
        if let Some(m) = s.get("maxItems").and_then(Value::as_u64)
            && n > m
        {
            push(format!("takes at most {m} items"));
        }
        if let Some(items) = s.get("items").filter(|i| i.is_object()) {
            for (i, x) in a.iter().enumerate() {
                node(x, items, root, &join(loc, &i.to_string()), out);
            }
        }
    }
    if let Some(o) = v.as_object() {
        object(o, s, root, loc, out);
    }
    if let Some(Value::Array(branches)) = s.get("allOf") {
        for b in branches {
            node(v, b, root, loc, out);
        }
    }
    for key in ["anyOf", "oneOf"] {
        if let Some(Value::Array(branches)) = s.get(key) {
            let passing: Vec<Vec<Violation>> = branches
                .iter()
                .map(|b| {
                    let mut e = Vec::new();
                    node(v, b, root, loc, &mut e);
                    e
                })
                .collect();
            let ok = passing.iter().filter(|e| e.is_empty()).count();
            if ok == 0 {
                // the branch that got furthest (fewest violations) explains best
                if let Some(best) = passing.into_iter().min_by_key(Vec::len) {
                    out.extend(best);
                }
            } else if key == "oneOf" && ok > 1 {
                out.push(Violation {
                    loc: loc.to_owned(),
                    msg: "matches more than one of the allowed shapes".into(),
                });
            }
        }
    }
}

fn object(o: &Map<String, Value>, s: &Value, root: &Value, loc: &str, out: &mut Vec<Violation>) {
    // in the schema's field order, then the extra fields, as Pydantic lists them
    let props = s.get("properties").and_then(Value::as_object);
    let required: Vec<&str> = s
        .get("required")
        .and_then(Value::as_array)
        .map(|r| r.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    for (k, p) in props.into_iter().flatten() {
        match o.get(k) {
            Some(x) => node(x, p, root, &join(loc, k), out),
            None if required.contains(&k.as_str()) => out.push(Violation {
                loc: join(loc, k),
                msg: "field required".into(),
            }),
            None => {}
        }
    }
    for k in required.iter().filter(|k| props.is_none_or(|p| !p.contains_key(**k))) {
        if !o.contains_key(*k) {
            out.push(Violation {
                loc: join(loc, k),
                msg: "field required".into(),
            });
        }
    }
    let extra = s.get("additionalProperties");
    for (k, x) in o {
        if props.is_some_and(|p| p.contains_key(k)) {
            continue;
        }
        let at = join(loc, k);
        match extra {
            Some(Value::Bool(false)) => out.push(Violation {
                loc: at,
                msg: "extra field not permitted".into(),
            }),
            Some(e @ Value::Object(_)) => node(x, e, root, &at, out),
            _ => {}
        }
    }
}

/// Every keyword used anywhere in `schema` (for the test that `KEYWORDS` covers the spec schemas).
#[cfg(test)]
pub(crate) fn keywords(schema: &Value, acc: &mut std::collections::BTreeSet<String>) {
    if let Value::Object(o) = schema {
        for (k, v) in o {
            acc.insert(k.clone());
            match k.as_str() {
                "properties" | "$defs" => {
                    for x in v.as_object().into_iter().flat_map(|m| m.values()) {
                        keywords(x, acc);
                    }
                }
                "default" | "examples" | "const" | "enum" | "discriminator" => {}
                _ => keywords(v, acc),
            }
        }
    } else if let Value::Array(a) = schema {
        for x in a {
            keywords(x, acc);
        }
    }
}

/// Every `pattern` used anywhere in `schema`.
#[cfg(test)]
pub(crate) fn patterns(schema: &Value, acc: &mut std::collections::BTreeSet<String>) {
    match schema {
        Value::Object(o) => {
            for (k, v) in o {
                match (k.as_str(), v) {
                    ("pattern", Value::String(p)) => {
                        acc.insert(p.clone());
                    }
                    ("default" | "examples" | "const" | "enum", _) => {}
                    _ => patterns(v, acc),
                }
            }
        }
        Value::Array(a) => {
            for x in a {
                patterns(x, acc);
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use serde_json::json;

    use super::*;
    use crate::schemas::JSON_SCHEMAS;

    fn spec_schemas() -> Vec<(&'static str, Value)> {
        let names: BTreeSet<&str> = crate::operations::OPERATIONS
            .iter()
            .filter_map(|o| o.spec_schema)
            .collect();
        assert!(!names.is_empty());
        names
            .into_iter()
            .map(|n| {
                let text = JSON_SCHEMAS.iter().find(|(k, _)| *k == n).expect(n).1;
                (n, serde_json::from_str(text).expect("schema JSON"))
            })
            .collect()
    }

    #[test]
    fn every_keyword_and_pattern_of_the_spec_schemas_is_known() {
        for (name, s) in spec_schemas() {
            let mut kw = BTreeSet::new();
            keywords(&s, &mut kw);
            let unknown: Vec<&String> = kw.iter().filter(|k| !KEYWORDS.contains(&k.as_str())).collect();
            assert!(
                unknown.is_empty(),
                "{name}: keywords not checked by spec_check: {unknown:?}"
            );
            let mut pats = BTreeSet::new();
            patterns(&s, &mut pats);
            for p in pats {
                assert!(
                    PATTERNS.iter().any(|(q, _)| *q == p),
                    "{name}: pattern {p} has no matcher"
                );
            }
        }
    }

    #[test]
    fn the_samples_pass_and_broken_specs_fail() {
        let root = serde_json::from_str::<Value>(
            JSON_SCHEMAS
                .iter()
                .find(|(k, _)| *k == "qt.shadow_sample_spec")
                .expect("schema")
                .1,
        )
        .expect("json");
        let good = json!({"dataset": {"path": "ds"}, "out": "s.parquet", "n": 10});
        assert_eq!(check(&good, &root), vec![]);
        let cases = [
            (
                json!({"dataset": {"path": "ds"}, "out": "s.parquet"}),
                "n",
                "field required",
            ),
            (
                json!({"dataset": {"path": "ds"}, "out": "s.parquet", "n": "ten"}),
                "n",
                "expected integer",
            ),
            (
                json!({"dataset": {"path": "ds"}, "out": "s.parquet", "n": 10, "nn": 1}),
                "nn",
                "extra field",
            ),
            (
                json!({"dataset": {"path": "ds"}, "out": "s.parquet", "n": 10, "seed": -1}),
                "seed",
                "must be >=",
            ),
            (
                json!({"dataset": {"pth": "ds"}, "out": "s.parquet", "n": 10}),
                "dataset",
                "",
            ),
        ];
        for (spec, loc, msg) in cases {
            let errs = check(&spec, &root);
            assert!(
                errs.iter().any(|e| e.loc.starts_with(loc) && e.msg.contains(msg)),
                "{spec}: {errs:?}"
            );
        }
    }

    #[test]
    fn date_pattern() {
        assert!(is_iso_date_shape("2026-01-02"));
        for bad in ["2026-1-02", "2026/01/02", "20260102", "2026-01-0x", ""] {
            assert!(!is_iso_date_shape(bad), "{bad}");
        }
    }
}
