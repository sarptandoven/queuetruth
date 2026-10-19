//! Run identity (SPEC 7.3): RFC 8785 (JCS) canonical JSON, execution-only field removal and `run_id`.
//! Mirror of `queuetruth.schemas.runid`; `schemas/run_id_kat.json` holds the shared known answers.
//!
//! Schema 1.1 canonicalisation: spec fields added in 1.1 (SPEC 3.11) are left out of the identity document when
//! they hold their default (`V1_1_DEFAULTS`), so a spec that does not set them keeps its v0.1 `run_id`; non-default
//! values are hashed as ordinary fields. Summary echoes keep the fields.

use qt_types::{QtError, QtResult};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

/// Keys removed (at any depth) before hashing and in summary echoes.
pub const EXECUTION_ONLY: [&str; 2] = ["out", "threads"];
const REF_KEYS: [&str; 4] = ["path", "dataset_id", "upload_id", "job_file"];

/// `(discriminating top-level key, field path, default as JSON text)` of every 1.1 spec field: the spec is
/// recognised by a key only it has (`sweep_gap_ns` = ReplaySpec, `write_fills` = ZooSpec, `markout_horizons_ns` =
/// ScorecardSpec, `fix` = LiveImportSpec, `format` = IngestSpec, `max_events` = WhySpec, `target` = ExportSpec,
/// `gbdt` = HazardFitSpec, `max_why` = ReportSpec). `family` is shared by CalibrationSpec and LiveCalibrationSpec; its
/// entries name fields only LiveCalibrationSpec has, so they never touch a CalibrationSpec.
/// Same table as `queuetruth.schemas.runid.V1_1_DEFAULTS`.
pub const V1_1_DEFAULTS: [(&str, &[&str], &str); 26] = [
    ("sweep_gap_ns", &["fill_rule"], "\"exact\""),
    ("sweep_gap_ns", &["hidden_print_policy"], "null"),
    ("sweep_gap_ns", &["exclude_order_ids"], "[]"),
    ("sweep_gap_ns", &["exclude_order_ids", "*", "session_date"], "null"),
    ("write_fills", &["amends"], "null"),
    ("write_fills", &["exclude_order_ids"], "[]"),
    ("write_fills", &["exclude_order_ids", "*", "session_date"], "null"),
    ("max_events", &["exclude_order_ids"], "[]"),
    ("max_events", &["exclude_order_ids", "*", "session_date"], "null"),
    ("target", &["force"], "false"),
    ("markout_horizons_ns", &["window_ns"], "60000000000"),
    ("markout_horizons_ns", &["match_window_ns"], "1000000"),
    ("markout_horizons_ns", &["self_exclude"], "true"),
    ("markout_horizons_ns", &["markout_past_end"], "null"),
    ("fix", &["tz"], "null"),
    ("fix", &["fix", "venue_name"], "null"),
    ("fix", &["fix", "checksum"], "\"exclude\""),
    ("fix", &["fix", "data_dictionary"], "null"),
    ("fix", &["fix", "session_venues"], "{}"),
    ("fix", &["fix", "instrument_tag"], "null"),
    ("format", &["open_s"], "null"),
    ("format", &["close_s"], "null"),
    ("gbdt", &["zoo_shadows"], "null"),
    ("max_why", &["leaderboard_sort_horizon_ns"], "null"),
    ("family", &["self_exclude"], "true"),
    ("family", &["match_window_ns"], "1000000"),
];

/// Removes `path` from `v` where it holds `default` (`*` = every item of a list).
fn drop_path(v: &mut Value, path: &[&str], default: &str) -> QtResult<()> {
    let Some((head, rest)) = path.split_first() else {
        return Ok(());
    };
    if *head == "*" {
        if let Value::Array(items) = v {
            for x in items {
                drop_path(x, rest, default)?;
            }
        }
        return Ok(());
    }
    let Value::Object(o) = v else {
        return Ok(());
    };
    if rest.is_empty() {
        if let Some(x) = o.get(*head)
            && canonical_json(x)? == default
        {
            o.remove(*head);
        }
        return Ok(());
    }
    match o.get_mut(*head) {
        Some(next) => drop_path(next, rest, default),
        None => Ok(()),
    }
}

/// A copy of `spec` without the 1.1 fields that hold their default (`V1_1_DEFAULTS`; a `*` path element applies to
/// every item of a list); other values unchanged.
pub fn drop_v1_1_defaults(spec: &Value) -> QtResult<Value> {
    let mut out = spec.clone();
    if let Value::Object(top) = spec {
        for (disc, path, default) in V1_1_DEFAULTS {
            if top.contains_key(disc) {
                drop_path(&mut out, path, default)?;
            }
        }
    }
    Ok(out)
}

/// ECMAScript Number::toString of a finite f64 (shortest round-trip digits).
fn format_f64(x: f64) -> QtResult<String> {
    if !x.is_finite() {
        return Err(QtError::Config(format!(
            "non-finite number {x} is not allowed in canonical JSON"
        )));
    }
    if x == 0.0 {
        return Ok("0".into());
    }
    let sign = if x < 0.0 { "-" } else { "" };
    let sci = format!("{:e}", x.abs());
    let (mant, exp) = sci
        .split_once('e')
        .ok_or_else(|| QtError::Engine(format!("float format {sci}")))?;
    let exp: i32 = exp
        .parse()
        .map_err(|_| QtError::Engine(format!("float format {sci}")))?;
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let digits = digits.trim_end_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    let k = digits.len() as i32;
    let n = exp + 1; // value = 0.d1d2... * 10^n
    let out = if k <= n && n <= 21 {
        format!("{digits}{}", "0".repeat((n - k) as usize))
    } else if 0 < n && n <= 21 {
        format!("{}.{}", &digits[..n as usize], &digits[n as usize..])
    } else if -6 < n && n <= 0 {
        format!("0.{}{digits}", "0".repeat((-n) as usize))
    } else {
        let e = n - 1;
        let es = if e >= 0 { format!("+{e}") } else { e.to_string() };
        if k == 1 {
            format!("{digits}e{es}")
        } else {
            format!("{}.{}e{es}", &digits[..1], &digits[1..])
        }
    };
    Ok(format!("{sign}{out}"))
}

fn escape(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_canonical(v: &Value, out: &mut String) -> QtResult<()> {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.push_str(&i.to_string());
            } else if let Some(u) = n.as_u64() {
                out.push_str(&u.to_string());
            } else {
                let f = n
                    .as_f64()
                    .ok_or_else(|| QtError::Config(format!("unrepresentable number {n}")))?;
                out.push_str(&format_f64(f)?);
            }
        }
        Value::String(s) => escape(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(x, out)?;
            }
            out.push(']');
        }
        Value::Object(o) => {
            let mut keys: Vec<&String> = o.keys().collect();
            keys.sort_by(|a, b| a.encode_utf16().cmp(b.encode_utf16()));
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                escape(k, out);
                out.push(':');
                write_canonical(&o[k.as_str()], out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// RFC 8785 canonical JSON: sorted keys (UTF-16 code units), no whitespace, ES number format, integers exact.
pub fn canonical_json(v: &Value) -> QtResult<String> {
    let mut out = String::new();
    write_canonical(v, &mut out)?;
    Ok(out)
}

/// True for a `DatasetRef` / `FileRef` object (exactly one key among path, dataset_id, upload_id, job_file).
pub fn is_ref(v: &Value) -> bool {
    match v {
        Value::Object(o) => o.len() == 1 && o.keys().all(|k| REF_KEYS.contains(&k.as_str())),
        _ => false,
    }
}

/// The spec with execution-only fields (`out`, `threads`) removed at any depth; references kept as given.
pub fn spec_echo(spec: &Value) -> Value {
    match spec {
        Value::Object(o) if !is_ref(spec) => Value::Object(
            o.iter()
                .filter(|(k, _)| !EXECUTION_ONLY.contains(&k.as_str()))
                .map(|(k, v)| (k.clone(), spec_echo(v)))
                .collect(),
        ),
        Value::Array(a) => Value::Array(a.iter().map(spec_echo).collect()),
        other => other.clone(),
    }
}

fn identity(spec: &Value, resolve: &dyn Fn(&Value) -> QtResult<String>) -> QtResult<Value> {
    Ok(match spec {
        Value::Object(_) if is_ref(spec) => {
            let mut m = Map::new();
            m.insert("sha256".into(), Value::String(resolve(spec)?));
            Value::Object(m)
        }
        Value::Object(o) => {
            let mut m = Map::new();
            for (k, v) in o {
                if !EXECUTION_ONLY.contains(&k.as_str()) {
                    m.insert(k.clone(), identity(v, resolve)?);
                }
            }
            Value::Object(m)
        }
        Value::Array(a) => Value::Array(a.iter().map(|x| identity(x, resolve)).collect::<QtResult<_>>()?),
        other => other.clone(),
    })
}

/// Canonical JSON of `{"engine_version": v, "spec": s}` with execution-only fields and defaulted 1.1 fields removed
/// and refs resolved.
pub fn identity_document(
    spec: &Value,
    engine_version: &str,
    resolve: &dyn Fn(&Value) -> QtResult<String>,
) -> QtResult<String> {
    let mut m = Map::new();
    m.insert("engine_version".into(), Value::String(engine_version.to_owned()));
    m.insert("spec".into(), identity(&drop_v1_1_defaults(spec)?, resolve)?);
    canonical_json(&Value::Object(m))
}

/// First 16 hex chars of sha256 of the identity document. Pass the fully defaulted spec (serialized from its
/// `specs` struct) so omitted and explicit defaults give the same id; `resolve` maps a ref object to a sha256.
pub fn run_id(spec: &Value, engine_version: &str, resolve: &dyn Fn(&Value) -> QtResult<String>) -> QtResult<String> {
    let doc = identity_document(spec, engine_version, resolve)?;
    let h = hex::encode(Sha256::digest(doc.as_bytes()));
    Ok(h[..16].to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn es_number_format() {
        let cases: [(f64, &str); 16] = [
            (1e-7, "1e-7"),
            (1e21, "1e+21"),
            (1e20, "100000000000000000000"),
            (123456789012345680000.0, "123456789012345680000"),
            (0.000001, "0.000001"),
            (1.5e-6, "0.0000015"),
            (-0.0, "0"),
            (5e-324, "5e-324"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (333333333.3333333, "333333333.3333333"),
            (9007199254740992.0, "9007199254740992"),
            (2.0, "2"),
            (-3.0, "-3"),
            (0.1, "0.1"),
            (123.456, "123.456"),
            (4.5e-7, "4.5e-7"),
        ];
        for (x, s) in cases {
            assert_eq!(format_f64(x).ok().as_deref(), Some(s), "{x:e}");
        }
        assert!(format_f64(f64::NAN).is_err());
    }

    #[test]
    fn canonical_sorting_and_escapes() {
        let v: Value =
            serde_json::from_str(r#"{"b":[1,2.5,"x\n\u0001"],"a":{"é":1,"z":null},"A":true}"#).unwrap_or(Value::Null);
        assert_eq!(
            canonical_json(&v).ok().as_deref(),
            Some("{\"A\":true,\"a\":{\"z\":null,\"\u{e9}\":1},\"b\":[1,2.5,\"x\\n\\u0001\"]}")
        );
    }
}
