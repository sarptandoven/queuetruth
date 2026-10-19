//! `run_native`: the Rust commands and the native part of the hybrid commands of SPEC 4.4.

use qt_schema::specs::{
    CalibrationSpec, ExportSpec, FeatureSpec, IngestSpec, MirrorSpec, RefereeSpec, ReplaySpec, ShadowSampleSpec,
    WhySpec, ZooSpec,
};
use qt_types::{QtError, QtResult, Venue};
use serde_json::{Value, json};

use crate::ops::{self, add_warning, spec, to_value};
use crate::{CliCommand, NativeOutput};

fn ok(summary: Value) -> QtResult<NativeOutput> {
    Ok(NativeOutput {
        summary,
        gate_failed: false,
    })
}

fn python_only(path: &str) -> QtError {
    QtError::Usage(format!("`{path}` requires the Python package (pip install queuetruth)"))
}

fn str_arg<'a>(args: &'a Value, key: &str) -> QtResult<&'a str> {
    args.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| QtError::Usage(format!("{key}: required")))
}

pub(crate) fn run(cmd: &CliCommand) -> QtResult<NativeOutput> {
    let path: Vec<&str> = cmd.path.iter().map(String::as_str).collect();
    let mut args = cmd.args.clone();
    let extra = match &mut args {
        Value::Object(o) => o.shift_remove("_cli").unwrap_or(Value::Null),
        _ => Value::Null,
    };
    let a = &args;
    match path.as_slice() {
        ["ingest"] => {
            let s: IngestSpec = spec(a, "ingest")?;
            let (summary, gate_failed) = ops::ingest_gate(&s)?;
            Ok(NativeOutput { summary, gate_failed })
        }
        ["replay"] => {
            // OC-34: --exclude-order-ids (and any option still given outside the spec) becomes a spec field first,
            // so it enters run_id
            let s = ops::fold_replay_options(&spec::<ReplaySpec>(a, "replay")?, &extra)?;
            let (summary, gate_failed) = ops::replay_gate(&s, &ops::merged_replay_options(&s, &Value::Null)?)?;
            Ok(NativeOutput {
                summary: to_value(&summary)?,
                gate_failed,
            })
        }
        ["shadows", "sample"] => ok(to_value(&qt_replay::sample_shadows(&spec::<ShadowSampleSpec>(
            a,
            "shadows sample",
        )?)?)?),
        ["shadows", "mirror-real"] => ok(to_value(&qt_replay::mirror_real_orders(&spec::<MirrorSpec>(
            a,
            "shadows mirror-real",
        )?)?)?),
        ["zoo"] => {
            let mut s: ZooSpec = spec(a, "zoo")?;
            if let Some(ids) = extra.get("exclude_order_ids") {
                s.exclude_order_ids = ops::resolve_exclude_order_ids(ids, &s.dataset)?;
            }
            let mut v = to_value(&qt_zoo::run_zoo(&s)?)?;
            add_warning(
                &mut v,
                "leaderboard: computed by queuetruth.eval in the Python package; empty in queuetruth-core",
            );
            ok(v)
        }
        ["calibrate", "alloc" | "survival" | "nt-touch"] => {
            let mut v = to_value(&qt_calib::calibrate(&spec::<CalibrationSpec>(a, "calibrate")?)?)?;
            add_warning(
                &mut v,
                "queuetruth-core: only fit.json is written; held_out, baseline_held_out and exports need the Python \
                 package",
            );
            ok(v)
        }
        ["features"] => ok(to_value(&qt_features::extract(&spec::<FeatureSpec>(a, "features")?)?)?),
        ["referee", "run"] => {
            let s: RefereeSpec = spec(a, "referee run")?;
            let out = s
                .out
                .as_deref()
                .ok_or_else(|| QtError::Config("out: required".into()))?;
            let exact = std::path::Path::new(out).join("exact");
            let rs: ReplaySpec = spec(
                &json!({
                    "dataset": to_value(&s.dataset)?,
                    "out": exact.display().to_string(),
                    "shadows": to_value(&s.schedule)?,
                    "bbo": "none",
                    "trajectory": "none",
                }),
                "referee run",
            )?;
            let mut v = to_value(&qt_replay::run_dataset(&rs)?)?;
            add_warning(
                &mut v,
                &format!(
                    "queuetruth-core: the referee comparison (qt.referee_report) needs the Python package; the exact \
                     arm was written to {}",
                    exact.display()
                ),
            );
            ok(v)
        }
        ["referee", "fixtures"] => ok(to_value(&ops::referee_fixtures(str_arg(a, "out")?)?)?),
        ["fixtures", "synth"] => {
            let venue = match a.get("venue").and_then(Value::as_str) {
                Some("nasdaq") => Venue::Nasdaq,
                _ => Venue::Cme,
            };
            let sessions = a.get("sessions").and_then(Value::as_u64).unwrap_or(2);
            let sessions = u32::try_from(sessions).map_err(|_| QtError::Config("sessions: out of range".into()))?;
            ok(to_value(&ops::fixtures_synth(str_arg(a, "out")?, venue, sessions)?)?)
        }
        ["why"] => {
            let s: WhySpec = spec(a, "why")?;
            if s.live.is_some() && s.shadows.is_none() {
                return Err(python_only("why --live"));
            }
            if extra.get("model").is_some() || extra.get("zoo").is_some() {
                return Err(python_only("why --model/--zoo"));
            }
            ok(to_value(&qt_replay::why(&s)?)?)
        }
        ["export", "l2-npz"] => ok(to_value(&ops::export_l2_npz(&spec::<ExportSpec>(a, "export")?)?)?),
        ["schema", "list"] => ok(ops::schema_list()),
        ["schema", "show"] => ok(ops::schema_show(str_arg(a, "name")?)?),
        ["doctor"] => {
            let d = ops::doctor();
            let failed = !d.problems.is_empty();
            Ok(NativeOutput {
                summary: to_value(&d)?,
                gate_failed: failed,
            })
        }
        ["bench", kind] => {
            let mut b = a.clone();
            if let (Value::Object(o), Some(m)) = (&mut b, a.get("mode").and_then(Value::as_str)) {
                o.insert("mode".into(), Value::String(m.replace('_', "-")));
            }
            ok(to_value(&crate::bench::run(kind, &b)?)?)
        }
        _ => Err(python_only(&cmd.path.join(" "))),
    }
}
