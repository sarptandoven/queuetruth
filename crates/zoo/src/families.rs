//! The zoo family registry: one `FamilyDef` per model family (SPEC 5.3.2). `Family::ALL`, the family predicates,
//! `param_keys`, `summary`, `default_models` (the `all` set) and `zoo --list-models` read this table, and
//! `queuetruth._core.zoo_families()` exposes it to Python. To add a family: add a `Family` variant, its
//! `FamilyDef` here, and its model in `models.rs` or `l3.rs`.

use serde::Serialize;

use crate::Family;
use crate::model_id::DEFAULT_PARTICLES;

/// One numeric parameter of a family (besides `mode` and `exch`).
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct ParamDef {
    /// Key in the model id (`n`, `p`, `k`, `particles`).
    pub key: &'static str,
    /// Value filled in when the id leaves the key out; None when the key is required.
    pub default: Option<f64>,
    /// Lower bound.
    pub min: f64,
    /// Whether `min` itself is allowed.
    pub min_inclusive: bool,
    /// Upper bound (None: unbounded, finite values only).
    pub max: Option<f64>,
    /// Whether `max` itself is allowed.
    pub max_inclusive: bool,
    /// Whole numbers only.
    pub integer: bool,
    /// The range in words, as error messages and docs state it.
    pub range: &'static str,
    /// What the parameter is.
    pub help: &'static str,
}

impl ParamDef {
    /// Whether a value is inside the range.
    pub fn accepts(&self, x: f64) -> bool {
        let lo = if self.min_inclusive {
            x >= self.min
        } else {
            x > self.min
        };
        let hi = match self.max {
            None => x.is_finite(),
            Some(m) if self.max_inclusive => x <= m,
            Some(m) => x < m,
        };
        lo && hi && (!self.integer || x.fract() == 0.0)
    }
}

/// A set of parameter values (`[("n", 2.0)]`).
pub type ParamSet = &'static [(&'static str, f64)];

/// One model family.
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct FamilyDef {
    /// The family.
    #[serde(skip)]
    pub family: Family,
    /// Canonical name (`hbt_power3`).
    pub name: &'static str,
    /// Simulator library whose fill model the family reproduces (None for the two bounds).
    pub library: Option<&'static str>,
    /// Short human label (`hftbacktest Power3`).
    pub label: &'static str,
    /// One line on what the family models (`zoo --list-models`).
    pub summary: &'static str,
    /// What the model reads: `l2` (the synthesized L2 view) or `mbo` (order-level records).
    pub input: &'static str,
    /// Numeric parameters, in key order.
    pub params: &'static [ParamDef],
    /// Their keys (`Family::param_keys`; a test checks they match `params`).
    pub param_keys: &'static [&'static str],
    /// hftbacktest L2 family: the only ones with an `exch` key.
    pub hbt_l2: bool,
    /// NautilusTrader family.
    pub nt: bool,
    /// Monte Carlo over `particles` (its cost grows with them).
    pub particle: bool,
    /// Predictions analytic in the touch count (5.3.3 e/f).
    pub touch: bool,
    /// The parameter sets this family adds to `--models all`, in order (empty: not in `all`).
    pub in_all: &'static [ParamSet],
    /// Calibrations that fit it: `alloc`, `survival`, `nt_touch`, `live` (`calibrate alloc`, ...,
    /// `calibrate-live`).
    pub calibrations: &'static [&'static str],
    /// Its name in the calibration specs and `--family` flags (`power3`, `databento`), when it can be fitted.
    pub calibration_family: Option<&'static str>,
    /// The library's own default parameters, as the trust verdict compares a fit with (docs/concepts/calibration.md).
    pub library_default: Option<ParamSet>,
}

const N: ParamDef = ParamDef {
    key: "n",
    default: None,
    min: 0.0,
    min_inclusive: false,
    max: None,
    max_inclusive: false,
    integer: false,
    range: "finite and > 0",
    help: "exponent of the queue-share function",
};
const P: ParamDef = ParamDef {
    key: "p",
    default: Some(1.0),
    min: 0.0,
    min_inclusive: true,
    max: Some(1.0),
    max_inclusive: true,
    integer: false,
    range: "in [0, 1]",
    help: "prob_fill_on_limit: probability that an at-price touch fills",
};
const K: ParamDef = ParamDef {
    key: "k",
    default: None,
    min: -1.0,
    min_inclusive: false,
    max: Some(1.0),
    max_inclusive: false,
    integer: false,
    range: "in (-1, 1)",
    help: "share of an unexplained decrease taken from ahead of the order",
};
const PARTICLES: ParamDef = ParamDef {
    key: "particles",
    default: Some(DEFAULT_PARTICLES as f64),
    min: 1.0,
    min_inclusive: true,
    max: Some(1_000_000.0),
    max_inclusive: true,
    integer: true,
    range: "an integer in 1..=1000000",
    help: "Monte Carlo queue positions (from --particles when the id leaves it out)",
};

const HBT: Option<&str> = Some("hftbacktest");
const NT: Option<&str> = Some("NautilusTrader");
const POWER_CALIB: &[&str] = &["alloc", "live"];
const N2: Option<ParamSet> = Some(&[("n", 2.0)]);
const P1: Option<ParamSet> = Some(&[("p", 1.0)]);

/// Base row: an L2 family with no parameters, not in `all`, not fitted.
const fn base(family: Family, name: &'static str, label: &'static str, summary: &'static str) -> FamilyDef {
    FamilyDef {
        family,
        name,
        library: None,
        label,
        summary,
        input: "l2",
        params: &[],
        param_keys: &[],
        hbt_l2: false,
        nt: false,
        particle: false,
        touch: false,
        in_all: &[],
        calibrations: &[],
        calibration_family: None,
        library_default: None,
    }
}

/// hftbacktest L2 family.
const fn hbt(family: Family, name: &'static str, label: &'static str, summary: &'static str) -> FamilyDef {
    FamilyDef {
        library: HBT,
        hbt_l2: true,
        in_all: &[&[]],
        ..base(family, name, label, summary)
    }
}

/// hftbacktest power family.
const fn power(
    family: Family,
    name: &'static str,
    label: &'static str,
    summary: &'static str,
    in_all: &'static [ParamSet],
    calib: &'static str,
) -> FamilyDef {
    FamilyDef {
        params: &[N],
        param_keys: &["n"],
        in_all,
        calibrations: POWER_CALIB,
        calibration_family: Some(calib),
        library_default: N2,
        ..hbt(family, name, label, summary)
    }
}

/// Every family in canonical order (`Family::ALL` is this order).
pub const FAMILIES: [FamilyDef; 13] = [
    hbt(
        Family::HbtRiskAdverse,
        "hbt_risk_adverse",
        "hftbacktest RiskAdverse",
        "hftbacktest RiskAdverseQueueModel: every unexplained decrease came from behind you",
    ),
    power(
        Family::HbtPower,
        "hbt_power",
        "hftbacktest Power",
        "hftbacktest PowerProbQueueFunc: share of a decrease from ahead b^n / (b^n + f^n)",
        &[&[("n", 2.0)], &[("n", 3.0)]],
        "power",
    ),
    power(
        Family::HbtPower2,
        "hbt_power2",
        "hftbacktest Power2",
        "hftbacktest PowerProbQueueFunc2: share of a decrease from ahead b^n / (b + f)^n",
        &[&[("n", 2.0)]],
        "power2",
    ),
    power(
        Family::HbtPower3,
        "hbt_power3",
        "hftbacktest Power3",
        "hftbacktest PowerProbQueueFunc3: share of a decrease from ahead 1 - (f / (f + b))^n",
        &[&[("n", 1.0)], &[("n", 2.0)], &[("n", 3.0)]],
        "power3",
    ),
    hbt(
        Family::HbtLog,
        "hbt_log",
        "hftbacktest Log",
        "hftbacktest LogProbQueueFunc",
    ),
    hbt(
        Family::HbtLog2,
        "hbt_log2",
        "hftbacktest Log2",
        "hftbacktest LogProbQueueFunc2",
    ),
    FamilyDef {
        library: HBT,
        input: "mbo",
        in_all: &[&[]],
        ..base(
            Family::HbtL3Fifo,
            "hbt_l3_fifo",
            "hftbacktest L3 FIFO",
            "hftbacktest L3FIFOQueueModel on the MBO records (order-level input, not L2)",
        )
    },
    FamilyDef {
        library: NT,
        nt: true,
        in_all: &[&[]],
        ..base(
            Family::NtQueueL2,
            "nt_queue_l2",
            "NautilusTrader queue_position",
            "NautilusTrader queue position on L2 (queue_position=True)",
        )
    },
    FamilyDef {
        library: NT,
        nt: true,
        touch: true,
        params: &[P],
        param_keys: &["p"],
        in_all: &[&[("p", 1.0)]],
        calibrations: &["nt_touch", "live"],
        calibration_family: Some("nt_prob_touch"),
        library_default: P1,
        ..base(
            Family::NtProbTouch,
            "nt_prob_touch",
            "NautilusTrader touch",
            "NautilusTrader FillModel(prob_fill_on_limit=p): each at-price touch fills with probability p",
        )
    },
    FamilyDef {
        library: NT,
        nt: true,
        touch: true,
        params: &[P],
        param_keys: &["p"],
        calibrations: &["nt_touch", "live"],
        calibration_family: Some("nt_queue_l2_prob_touch"),
        library_default: P1,
        ..base(
            Family::NtQueueL2ProbTouch,
            "nt_queue_l2_prob_touch",
            "NautilusTrader queue_position + touch",
            "NautilusTrader queue position, then prob_fill_on_limit=p once the queue is gone",
        )
    },
    FamilyDef {
        library: Some("Databento"),
        particle: true,
        params: &[K, PARTICLES],
        param_keys: &["k", "particles"],
        in_all: &[&[("k", -0.8)], &[("k", -0.95)]],
        calibrations: &["alloc", "survival", "live"],
        calibration_family: Some("databento"),
        library_default: Some(&[("k", -0.8)]),
        ..base(
            Family::DatabentoMc,
            "databento_mc",
            "Databento",
            "Databento queue-position estimator (Monte Carlo over particles; k in (-1, 1))",
        )
    },
    FamilyDef {
        in_all: &[&[]],
        ..base(
            Family::FrontOfQueue,
            "front_of_queue",
            "Front of queue (upper bound)",
            "upper bound: nothing ahead of you, every at-price trade fills",
        )
    },
    FamilyDef {
        in_all: &[&[]],
        ..base(
            Family::TradeThroughOnly,
            "trade_through_only",
            "Trade through only (lower bound)",
            "lower bound: fills only when a displayed trade goes through your price",
        )
    },
];

impl Family {
    /// This family's row of the registry.
    pub const fn def(self) -> &'static FamilyDef {
        &FAMILIES[self as usize]
    }
}

/// The registry as JSON (`queuetruth._core.zoo_families()`): one object per family, in canonical order, with every
/// `FamilyDef` field; `in_all` and `library_default` are objects of parameter values, and `all_ids` lists the ids
/// the family adds to `--models all` as typed on the command line.
pub fn families_json() -> serde_json::Value {
    let set = |s: &[(&str, f64)]| -> serde_json::Map<String, serde_json::Value> {
        s.iter().map(|(k, v)| ((*k).to_owned(), serde_json::json!(v))).collect()
    };
    let rows: Vec<serde_json::Value> = FAMILIES
        .iter()
        .map(|d| {
            let mut v = serde_json::to_value(d).unwrap_or(serde_json::Value::Null);
            if let Some(o) = v.as_object_mut() {
                o.insert(
                    "in_all".into(),
                    serde_json::Value::Array(d.in_all.iter().map(|s| serde_json::Value::Object(set(s))).collect()),
                );
                o.insert(
                    "library_default".into(),
                    d.library_default
                        .map_or(serde_json::Value::Null, |s| serde_json::Value::Object(set(s))),
                );
                o.insert(
                    "all_ids".into(),
                    serde_json::json!(d.in_all.iter().map(|s| short_id(d.name, s)).collect::<Vec<_>>()),
                );
            }
            v
        })
        .collect();
    serde_json::Value::Array(rows)
}

/// A model id as typed on the command line (`hbt_power3[n=2]`): parameters in key order, no `mode`, `exch` or
/// `particles` (those come from `--mode`, `--exchange` and `--particles`).
pub fn short_id(name: &str, params: &[(&str, f64)]) -> String {
    let mut ps: Vec<(&str, f64)> = params.iter().copied().filter(|(k, _)| *k != "particles").collect();
    ps.sort_by(|a, b| a.0.cmp(b.0));
    if ps.is_empty() {
        name.to_owned()
    } else {
        let kv: Vec<String> = ps.iter().map(|(k, v)| format!("{k}={v}")).collect();
        format!("{name}[{}]", kv.join(";"))
    }
}

#[cfg(test)]
mod tests {
    use super::FAMILIES;
    use crate::{Exchange, Family, Mode, ModelId};

    /// The registry and the `Family` enum agree: one row per variant in variant order, names unique, keys equal to
    /// the parameter definitions, the `all` set exactly the 16 ids of SPEC 5.3.4, and every parameter range the one
    /// the `ModelId` grammar enforces.
    #[test]
    fn registry_matches_the_enum_and_the_grammar() {
        assert_eq!(FAMILIES.len(), Family::ALL.len());
        for (i, (d, f)) in FAMILIES.iter().zip(Family::ALL).enumerate() {
            assert_eq!(d.family, f);
            assert_eq!(f as usize, i, "{f}: FAMILIES must follow the enum order");
            assert_eq!(f.def().name, d.name);
            assert_eq!(Family::from_name(d.name), Some(f));
            let keys: Vec<&str> = d.params.iter().map(|p| p.key).collect();
            assert_eq!(keys, d.param_keys, "{f}");
            let mut sorted = keys.clone();
            sorted.sort_unstable();
            assert_eq!(keys, sorted, "{f}: params in key order");
            assert!(!d.label.is_empty() && !d.summary.is_empty());
            assert!(d.input == "l2" || d.input == "mbo");
            assert_eq!(d.calibration_family.is_some(), !d.calibrations.is_empty(), "{f}");
            for set in d.in_all.iter().chain(d.library_default.iter()) {
                for (k, v) in *set {
                    let p = d.params.iter().find(|p| p.key == *k);
                    assert!(p.is_some_and(|p| p.accepts(*v)), "{f}: {k}={v}");
                }
            }
            // the values at, just inside and just outside each bound parse or fail as the registry says
            for p in d.params {
                let mut probes = vec![p.min, p.min + 0.5, p.min - 0.5];
                if let Some(m) = p.max {
                    probes.extend([m, m + 0.5, m - 0.5]);
                }
                let others: String = d
                    .params
                    .iter()
                    .filter(|q| q.key != p.key && q.default.is_none())
                    .map(|q| format!(";{}=-0.5", q.key))
                    .collect();
                for x in probes {
                    let id = format!("{}[{}={x}{others}]", d.name, p.key);
                    let ok = ModelId::parse_with(&id, Mode::Faithful, Exchange::Partial, 100).is_ok();
                    assert_eq!(ok, p.accepts(x), "{id}");
                }
            }
        }
        let names: std::collections::BTreeSet<&str> = FAMILIES.iter().map(|d| d.name).collect();
        assert_eq!(names.len(), FAMILIES.len());
        let all: Vec<String> = crate::default_models(Mode::Faithful, Exchange::Partial)
            .iter()
            .map(ToString::to_string)
            .collect();
        assert_eq!(
            all,
            [
                "hbt_risk_adverse[exch=partial;mode=faithful]",
                "hbt_power[exch=partial;mode=faithful;n=2]",
                "hbt_power[exch=partial;mode=faithful;n=3]",
                "hbt_power2[exch=partial;mode=faithful;n=2]",
                "hbt_power3[exch=partial;mode=faithful;n=1]",
                "hbt_power3[exch=partial;mode=faithful;n=2]",
                "hbt_power3[exch=partial;mode=faithful;n=3]",
                "hbt_log[exch=partial;mode=faithful]",
                "hbt_log2[exch=partial;mode=faithful]",
                "hbt_l3_fifo[mode=faithful]",
                "nt_queue_l2[mode=faithful]",
                "nt_prob_touch[mode=faithful;p=1]",
                "databento_mc[k=-0.8;mode=faithful;particles=100]",
                "databento_mc[k=-0.95;mode=faithful;particles=100]",
                "front_of_queue[mode=faithful]",
                "trade_through_only[mode=faithful]",
            ]
        );
        let doc = super::families_json();
        let rows = doc.as_array().cloned().unwrap_or_default();
        assert_eq!(rows.len(), FAMILIES.len());
        let ids: usize = rows.iter().map(|r| r["all_ids"].as_array().map_or(0, Vec::len)).sum();
        assert_eq!(ids, 16);
        assert_eq!(rows[3]["all_ids"][1], "hbt_power3[n=2]");
        assert_eq!(rows[10]["params"][1]["default"], 100.0);
    }
}
