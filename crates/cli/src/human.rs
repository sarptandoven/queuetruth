//! Human rendering of a summary document: one `key: value` line per scalar, nested objects flattened one level,
//! output files as a table; the spec echo and warnings (printed to stderr) are left out.

use serde_json::Value;

fn scalar(v: &Value) -> Option<String> {
    match v {
        Value::Null => Some("-".into()),
        Value::Bool(b) => Some(b.to_string()),
        Value::Number(n) => Some(match n.as_f64() {
            Some(f) if n.is_f64() => {
                if f.abs() >= 1e-4 || f == 0.0 {
                    format!("{f:.6}").trim_end_matches('0').trim_end_matches('.').to_owned()
                } else {
                    format!("{f:e}")
                }
            }
            _ => n.to_string(),
        }),
        Value::String(s) => Some(s.clone()),
        _ => None,
    }
}

fn short_list(a: &[Value]) -> String {
    if a.is_empty() {
        return "none".to_owned();
    }
    let items: Vec<String> = a.iter().take(8).filter_map(scalar).collect();
    if items.len() == a.len() {
        items.join(", ")
    } else {
        format!("{} items", a.len())
    }
}

/// Renders a summary for a terminal.
pub fn render_human(path: &[String], v: &Value) -> String {
    let p: Vec<&str> = path.iter().map(String::as_str).collect();
    if p == ["schema", "show"] {
        return serde_json::to_string_pretty(v).unwrap_or_default();
    }
    let mut out = String::new();
    let Value::Object(o) = v else {
        return v.to_string();
    };
    if p == ["schema", "list"] {
        for (k, list) in o {
            out.push_str(&format!("{k}:\n"));
            for n in list.as_array().into_iter().flatten() {
                out.push_str(&format!("  {}\n", n.as_str().unwrap_or_default()));
            }
        }
        return out.trim_end().to_owned();
    }
    if let Some(s) = o.get("schema").and_then(Value::as_str) {
        out.push_str(&format!("{} ({})\n", path.join(" "), s));
    }
    for (k, val) in o {
        if matches!(k.as_str(), "schema" | "schema_version" | "spec" | "warnings" | "files") {
            continue;
        }
        if let Some(s) = scalar(val) {
            out.push_str(&format!("{k}: {s}\n"));
            continue;
        }
        match val {
            Value::Array(a) => out.push_str(&format!("{k}: {}\n", short_list(a))),
            Value::Object(m) => {
                for (k2, v2) in m {
                    if matches!(k2.as_str(), "schema" | "schema_version") {
                        continue;
                    }
                    let s = scalar(v2).unwrap_or_else(|| match v2 {
                        Value::Array(a) => short_list(a),
                        Value::Object(x) => format!("{} fields", x.len()),
                        _ => String::new(),
                    });
                    out.push_str(&format!("{k}.{k2}: {s}\n"));
                }
            }
            _ => {}
        }
    }
    if let Some(Value::Array(files)) = o.get("files") {
        out.push_str(&format!("files ({}):\n", files.len()));
        for f in files {
            let name = f.get("name").and_then(Value::as_str).unwrap_or_default();
            let rows = f
                .get("rows")
                .and_then(Value::as_u64)
                .map_or_else(String::new, |r| format!("{r} rows, "));
            let bytes = f.get("bytes").and_then(Value::as_u64).unwrap_or(0);
            out.push_str(&format!("  {name} ({rows}{bytes} bytes)\n"));
        }
    }
    out.trim_end().to_owned()
}

// ---- answer-first views (same content as the Python console script's views) --------------------------------------

const NS: i64 = 1_000_000_000;

/// `HH:MM:SS.ffffff` (UTC) of epoch ns, with `YYYY-MM-DD ` in front when `date`.
pub(crate) fn utc(ns: i64, digits: usize, date: bool) -> String {
    let days = ns.div_euclid(86_400 * NS);
    let rem = ns.rem_euclid(86_400 * NS);
    let (h, m, s, frac) = (rem / (3600 * NS), rem / (60 * NS) % 60, rem / NS % 60, rem % NS);
    let mut out = String::new();
    if date {
        out.push_str(&qt_ingest::time::format_date(i32::try_from(days).unwrap_or(i32::MAX)));
        out.push(' ');
    }
    out.push_str(&format!("{h:02}:{m:02}:{s:02}"));
    if digits > 0 {
        out.push_str(&format!(".{frac:09}")[..=digits.min(9)]);
    }
    out
}

/// A fixed-point integer `v * 10^exp` as a decimal string (trailing zeros trimmed).
pub(crate) fn scaled(v: i128, exp: i32) -> String {
    if exp >= 0 {
        return (v * 10i128.pow(exp.unsigned_abs())).to_string();
    }
    let d = exp.unsigned_abs() as usize;
    let neg = v < 0;
    let digits = v.unsigned_abs().to_string();
    let padded = format!("{digits:0>width$}", width = d + 1);
    let (int, frac) = padded.split_at(padded.len() - d);
    let frac = frac.trim_end_matches('0');
    let body = if frac.is_empty() {
        int.to_owned()
    } else {
        format!("{int}.{frac}")
    };
    if neg { format!("-{body}") } else { body }
}

fn table(header: &[String], rows: &[Vec<String>], right: &[usize]) -> Vec<String> {
    let mut widths: Vec<usize> = header.iter().map(String::len).collect();
    for r in rows {
        for (i, c) in r.iter().enumerate() {
            widths[i] = widths[i].max(c.chars().count());
        }
    }
    let line = |r: &[String]| {
        r.iter()
            .enumerate()
            .map(|(i, c)| {
                if right.contains(&i) {
                    format!("{c:>w$}", w = widths[i])
                } else {
                    format!("{c:<w$}", w = widths[i])
                }
            })
            .collect::<Vec<_>>()
            .join("  ")
            .trim_end()
            .to_owned()
    };
    std::iter::once(line(header))
        .chain(rows.iter().map(|r| line(r)))
        .collect()
}

struct Units {
    symbol: Option<String>,
    price_exp: i32,
    size_exp: i32,
}

fn units(cmd: &crate::CliCommand, iid: u32, ts: i64) -> Units {
    let dataset = cmd
        .args
        .get("dataset")
        .and_then(|d| d.get("path"))
        .and_then(Value::as_str);
    let inst = dataset
        .and_then(|p| qt_ingest::Dataset::open(std::path::Path::new(p)).ok())
        .and_then(|ds| {
            let date = ds.partition_for(iid, ts).map(|p| p.session_date)?;
            ds.instrument(iid, date).cloned()
        });
    inst.map_or(
        Units {
            symbol: None,
            price_exp: 0,
            size_exp: 0,
        },
        |i| Units {
            symbol: Some(i.symbol),
            price_exp: i32::from(i.price_exp),
            size_exp: i32::from(i.size_exp),
        },
    )
}

fn why_view(cmd: &crate::CliCommand, v: &Value) -> Option<String> {
    let o = v.as_object()?;
    let iid = u32::try_from(o.get("instrument_id")?.as_u64()?).ok()?;
    let ts_live = o.get("ts_live").and_then(Value::as_i64);
    let u = units(cmd, iid, ts_live.unwrap_or(0));
    let px = |x: Option<i64>| x.map_or_else(|| "-".to_owned(), |p| scaled(i128::from(p), u.price_exp));
    let sz = |x: u64| scaled(i128::from(x), u.size_exp);
    let subj = o.get("subject")?;
    let who = match subj.get("order_key").and_then(Value::as_str) {
        Some(k) => format!("live order {k}"),
        None => format!(
            "shadow {}",
            subj.get("shadow_id").and_then(Value::as_str).unwrap_or("?")
        ),
    };
    let side = o.get("side").and_then(Value::as_str).unwrap_or("?");
    let qty = o.get("qty").and_then(Value::as_u64).unwrap_or(0);
    let inst = u
        .symbol
        .as_ref()
        .map_or_else(|| format!("instrument {iid}"), |s| format!("{s} (instrument {iid})"));
    let outcome = o.get("outcome")?;
    let filled = outcome.get("filled_qty").and_then(Value::as_u64).unwrap_or(0);
    let end = outcome.get("end_state").and_then(Value::as_str).unwrap_or("?");
    let mut lines = vec![
        format!(
            "why: {who} | {inst} | {} {} @ {} | live {} UTC",
            match side {
                "B" => "buy",
                "A" => "sell",
                s => s,
            },
            sz(qty),
            px(o.get("price").and_then(Value::as_i64)),
            ts_live.map_or_else(|| "-".to_owned(), |t| utc(t, 6, true))
        ),
        format!("outcome: {end}, filled {} of {}", sz(filled), sz(qty)),
        String::new(),
    ];
    let header: Vec<String> = [
        "time (UTC)",
        "act",
        "order",
        "side",
        "price",
        "size",
        "effect",
        "",
        "q.ahead",
        "rank",
        "level",
        "fill",
        "note",
    ]
    .iter()
    .map(|s| (*s).to_owned())
    .collect();
    let events = o.get("events")?.as_array()?;
    let mut rows = Vec::new();
    let mut fill_ts = None;
    let mut fill_note = String::new();
    for e in events {
        let g = |k: &str| e.get(k).and_then(Value::as_u64).unwrap_or(0);
        let fill = g("fill_qty");
        let ts = e.get("ts_event").and_then(Value::as_i64).unwrap_or(0);
        let note = e.get("note").and_then(Value::as_str).unwrap_or("").to_owned();
        if fill > 0 && fill_ts.is_none() {
            fill_ts = Some(ts);
            fill_note.clone_from(&note);
        }
        let oid = g("order_id");
        rows.push(vec![
            utc(ts, 6, false),
            e.get("action").and_then(Value::as_str).unwrap_or("").to_owned(),
            if oid == 0 { "-".to_owned() } else { oid.to_string() },
            {
                // Resting side; on a trade print (T) the aggressor.
                let trade = e.get("action").and_then(Value::as_str) == Some("T");
                match (e.get("side").and_then(Value::as_str).unwrap_or(""), trade) {
                    ("B", false) => "bid".to_owned(),
                    ("A", false) => "ask".to_owned(),
                    ("B", true) => "buy".to_owned(),
                    ("A", true) => "sell".to_owned(),
                    _ => "-".to_owned(),
                }
            },
            px(e.get("price").and_then(Value::as_i64)),
            sz(g("size")),
            e.get("effect").and_then(Value::as_str).unwrap_or("").to_owned(),
            if e.get("is_ahead").and_then(Value::as_bool).unwrap_or(false) {
                "ahead".to_owned()
            } else {
                String::new()
            },
            sz(g("queue_ahead_after")),
            g("rank_after").to_string(),
            sz(g("level_qty_after")),
            if fill > 0 { sz(fill) } else { String::new() },
            if fill > 0 {
                format!("{note} << FILL {}", sz(fill)).trim().to_owned()
            } else {
                note
            },
        ]);
    }
    if rows.is_empty() {
        lines.push("(no event at the order's price while it was live)".into());
    } else {
        lines.extend(table(&header, &rows, &[4, 5, 8, 9, 10, 11]));
        lines.push("(side: bid/ask of the resting order; on T rows the aggressor, buy or sell)".into());
    }
    if o.get("truncated").and_then(Value::as_bool).unwrap_or(false) {
        lines.push(format!(
            "(trace truncated at {} events; raise --max-events)",
            events.len()
        ));
    }
    let first_q = events
        .first()
        .and_then(|e| e.get("queue_ahead_after"))
        .and_then(Value::as_u64);
    let last_q = events
        .last()
        .and_then(|e| e.get("queue_ahead_after"))
        .and_then(Value::as_u64);
    lines.push(String::new());
    lines.push(match (fill_ts, first_q, last_q) {
        (Some(t), _, _) => format!(
            "conclusion: filled {} of {} at {} UTC ({}){}",
            sz(filled),
            sz(qty),
            utc(t, 3, true),
            fill_note,
            ts_live.map_or_else(String::new, |l| format!(
                ", {:.3} s after going live",
                (t - l) as f64 / 1e9
            ))
        ),
        (None, Some(a), Some(b)) => format!(
            "conclusion: not filled (end state {end}); queue ahead after the first event {} and at the end {}",
            sz(a),
            sz(b)
        ),
        _ => format!("conclusion: not filled (end state {end}); nothing happened at the order's price"),
    });
    Some(lines.join("\n"))
}

fn num(v: Option<&Value>) -> String {
    match v.and_then(Value::as_f64) {
        Some(f) => format!("{f:.4}").trim_end_matches('0').trim_end_matches('.').to_owned(),
        None => "-".to_owned(),
    }
}

/// Calibration fit (`queuetruth-core calibrate`: Rust fit only): the fitted value with its CIs and the flags that
/// make it untrustworthy (no events, grid edge, zero-width or one-sided CI).
fn calibration_view(v: &Value) -> Option<String> {
    let o = v.as_object()?;
    if o.get("schema").and_then(Value::as_str) != Some("qt.calibration_result") {
        return None;
    }
    let fam = o.get("family").and_then(Value::as_str).unwrap_or("?");
    let pn = o.get("param_name").and_then(Value::as_str).unwrap_or("estimate");
    let est = o.get("estimate").and_then(Value::as_f64);
    let n_events = o.get("n_events").and_then(Value::as_u64).unwrap_or(0);
    let pair = |k: &str| {
        o.get(k)
            .and_then(Value::as_array)
            .filter(|a| a.len() == 2)
            .map(|a| (a[0].as_f64(), a[1].as_f64()))
    };
    let mut cis = Vec::new();
    if let Some((lo, hi)) = pair("ci_profile") {
        cis.push(format!(
            "profile 95% CI {} to {}",
            num(lo.map(Value::from).as_ref()),
            num(hi.map(Value::from).as_ref())
        ));
    }
    if let Some((lo, hi)) = pair("ci_bootstrap") {
        cis.push(format!(
            "bootstrap {} to {}",
            num(lo.map(Value::from).as_ref()),
            num(hi.map(Value::from).as_ref())
        ));
    }
    let mut flags = Vec::new();
    if n_events == 0 {
        flags.push("fitted on 0 events".to_owned());
    }
    if est.is_none() {
        flags.push("no estimate".to_owned());
    }
    if let Some((Some(lo), Some(hi))) = pair("ci_bootstrap")
        && lo == hi
    {
        flags.push("zero-width bootstrap CI (degenerate, not precision)".to_owned());
    }
    if let Some((lo, hi)) = pair("ci_profile")
        && lo.is_none() != hi.is_none()
    {
        flags.push("profile CI open on one side (grid edge)".to_owned());
    }
    let grid = o.get("spec").and_then(|s| s.get("grid"));
    let sub = if fam.starts_with("power") {
        grid.and_then(|g| g.get("alloc"))
    } else {
        grid.and_then(|g| g.get(o.get("kind").and_then(Value::as_str).unwrap_or("")))
            .or_else(|| grid.and_then(|g| g.get(fam)))
    };
    if let (Some(e), Some(g)) = (est, sub) {
        let (lo, hi, n) = (
            g.get("lo").and_then(Value::as_f64),
            g.get("hi").and_then(Value::as_f64),
            g.get("n").and_then(Value::as_f64),
        );
        if let (Some(lo), Some(hi), Some(n)) = (lo, hi, n) {
            let theta = if fam.starts_with("power") && e > 0.0 { e.ln() } else { e };
            let step = (hi - lo) / (n - 1.0).max(1.0);
            if theta <= lo + step / 2.0 || theta >= hi - step / 2.0 {
                flags.push(format!(
                    "at the grid edge ({pn} = {}): the optimum may lie outside the grid",
                    num(Some(&Value::from(e)))
                ));
            }
        }
    }
    let mut lines =
        vec![
        format!("calibrate: {fam} (run {})", o.get("run_id").and_then(Value::as_str).unwrap_or("-")),
        format!(
            "fitted: {pn} = {}{} ({n_events} events, {} shadows)",
            num(est.map(Value::from).as_ref()),
            if cis.is_empty() { String::new() } else { format!(" ({})", cis.join("; ")) },
            o.get("n_shadows").and_then(Value::as_u64).unwrap_or(0)
        ),
        format!("flags: {}", if flags.is_empty() { "none".to_owned() } else { flags.join("; ") }),
        "held-out: not checked by queuetruth-core (fitted vs default needs the Python package: pip install queuetruth)"
            .to_owned(),
    ];
    lines.push(if flags.is_empty() {
        "verdict: not validated out of sample; run `queuetruth calibrate` for the held-out comparison".to_owned()
    } else {
        format!("verdict: DO NOT USE: {}", flags[0])
    });
    Some(lines.join("\n"))
}

/// Renders a command's summary for a terminal: the `why` event table (UTC times, decimal prices, bid/ask, the fill
/// row marked, a conclusion line) and the calibration verdict lead; other commands (and `--verbose`, appended) use
/// `render_human`.
pub fn render_human_cmd(cmd: &crate::CliCommand, v: &Value) -> String {
    let p: Vec<&str> = cmd.path.iter().map(String::as_str).collect();
    let view = match p.as_slice() {
        ["why"] => why_view(cmd, v),
        ["calibrate", _] => calibration_view(v),
        _ => None,
    };
    match view {
        Some(t) if cmd.global.verbose => format!("{t}\n\nall fields:\n{}", render_human(&cmd.path, v)),
        Some(t) => t,
        None => render_human(&cmd.path, v),
    }
}
