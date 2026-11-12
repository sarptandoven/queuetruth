//! The prediction-market family registry: one `PmFamilyDef` per family. `queuetruth.pm.queue` reads its family
//! lists (`ALL_FAMILIES`, `BOUND_FAMILIES`, `QUEUE_FAMILIES`, `ATOMIC_FAMILIES`) from `_core.pm_families()`, and the
//! engine dispatches on `PmFamily`. To add a family: add a `PmFamily` variant, its row here and its update in
//! `engine.rs`.

use serde::Serialize;

use crate::families::{N, ParamDef};

/// One prediction-market model family.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PmFamily {
    /// No queue ahead; every at-price print fills (the optimistic fill bound).
    FrontOfQueue,
    /// Fills only on a trade through the price (the pessimistic fill bound).
    TradeThroughOnly,
    /// Only prints shrink the queue (the upper queue bound).
    RiskAdverse,
    /// hbt `PowerProbQueueFunc`.
    Power,
    /// hbt `PowerProbQueueFunc2`.
    Power2,
    /// hbt `PowerProbQueueFunc3`.
    Power3,
    /// hbt `LogProbQueueFunc`.
    Log,
    /// hbt `LogProbQueueFunc2`.
    Log2,
    /// Every decrease beyond prints leaves the queue ahead first (the lower queue bound).
    CancelsAhead,
}

/// One prediction-market model family.
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct PmFamilyDef {
    /// The family.
    #[serde(skip)]
    pub family: PmFamily,
    /// Name in model ids (`power3`).
    pub name: &'static str,
    /// `bound` (a fill bound with no queue estimate) or `queue` (a queue-position model).
    pub role: &'static str,
    /// Short human label.
    pub label: &'static str,
    /// One line on what the family models.
    pub summary: &'static str,
    /// Numeric parameters, in key order.
    pub params: &'static [ParamDef],
    /// Their keys.
    pub param_keys: &'static [&'static str],
    /// Splits a decrease beyond the prints between ahead and behind with a probability `pi` (the hbt probability
    /// families and `cancels_ahead`, whose `pi` is 0).
    pub prob: bool,
    /// Takes the one-order-per-change variant (`atomic`).
    pub atomic: bool,
    /// The same model's family in the zoo (`hbt_power3`), when the zoo has it.
    pub zoo_family: Option<&'static str>,
}

const fn row(family: PmFamily, name: &'static str, role: &'static str, label: &'static str) -> PmFamilyDef {
    PmFamilyDef {
        family,
        name,
        role,
        label,
        summary: "",
        params: &[],
        param_keys: &[],
        prob: false,
        atomic: false,
        zoo_family: None,
    }
}

const fn hbt(family: PmFamily, name: &'static str, label: &'static str, zoo: &'static str) -> PmFamilyDef {
    PmFamilyDef {
        prob: true,
        atomic: true,
        zoo_family: Some(zoo),
        ..row(family, name, "queue", label)
    }
}

const fn power(family: PmFamily, name: &'static str, label: &'static str, zoo: &'static str) -> PmFamilyDef {
    PmFamilyDef {
        params: &[N],
        param_keys: &["n"],
        ..hbt(family, name, label, zoo)
    }
}

/// Every family in canonical order: the two bounds, then the queue families.
pub const PM_FAMILIES: [PmFamilyDef; 9] = [
    PmFamilyDef {
        summary: "no queue ahead; fills on every at-price print (the optimistic fill bound)",
        zoo_family: Some("front_of_queue"),
        ..row(PmFamily::FrontOfQueue, "front_of_queue", "bound", "Front of queue")
    },
    PmFamilyDef {
        summary: "fills only on a trade through its price (the pessimistic fill bound)",
        zoo_family: Some("trade_through_only"),
        ..row(
            PmFamily::TradeThroughOnly,
            "trade_through_only",
            "bound",
            "Trade-through only",
        )
    },
    PmFamilyDef {
        summary: "only prints shrink the queue ahead (the upper queue bound)",
        zoo_family: Some("hbt_risk_adverse"),
        ..row(
            PmFamily::RiskAdverse,
            "risk_adverse",
            "queue",
            "hftbacktest RiskAdverse",
        )
    },
    PmFamilyDef {
        summary: "a decrease is behind the order with probability b^n / (b^n + f^n)",
        ..power(PmFamily::Power, "power", "hftbacktest Power", "hbt_power")
    },
    PmFamilyDef {
        summary: "a decrease is behind the order with probability b^n / (b + f)^n",
        ..power(PmFamily::Power2, "power2", "hftbacktest Power2", "hbt_power2")
    },
    PmFamilyDef {
        summary: "a decrease is behind the order with probability 1 - (f / (f + b))^n; spans the queue bounds",
        ..power(PmFamily::Power3, "power3", "hftbacktest Power3", "hbt_power3")
    },
    PmFamilyDef {
        summary: "a decrease is behind the order with probability log1p(b) / (log1p(b) + log1p(f))",
        ..hbt(PmFamily::Log, "log", "hftbacktest Log", "hbt_log")
    },
    PmFamilyDef {
        summary: "a decrease is behind the order with probability log1p(b) / log1p(b + f)",
        ..hbt(PmFamily::Log2, "log2", "hftbacktest Log2", "hbt_log2")
    },
    PmFamilyDef {
        summary: "every decrease beyond the prints leaves the queue ahead first (the lower queue bound)",
        prob: true,
        ..row(PmFamily::CancelsAhead, "cancels_ahead", "queue", "Cancels ahead")
    },
];

impl PmFamily {
    /// The family's registry row.
    pub fn def(self) -> &'static PmFamilyDef {
        PM_FAMILIES
            .iter()
            .find(|d| d.family == self)
            .expect("every PmFamily has a row")
    }

    /// The family named `name` (`power3`), if any.
    pub fn from_name(name: &str) -> Option<PmFamily> {
        PM_FAMILIES.iter().find(|d| d.name == name).map(|d| d.family)
    }
}

/// The registry as a JSON list, one object per family in canonical order (`_core.pm_families()`).
pub fn pm_families_json() -> serde_json::Value {
    serde_json::to_value(PM_FAMILIES).unwrap_or(serde_json::Value::Null)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_is_consistent() {
        for d in &PM_FAMILIES {
            assert_eq!(PmFamily::from_name(d.name), Some(d.family));
            assert_eq!(d.family.def().name, d.name);
            let keys: Vec<&str> = d.params.iter().map(|p| p.key).collect();
            assert_eq!(keys, d.param_keys, "{}", d.name);
            assert!(!d.summary.is_empty(), "{}", d.name);
            assert!(!d.atomic || d.prob, "{}: only probability families take atomic", d.name);
            assert_eq!(d.role == "bound", !d.prob && d.family != PmFamily::RiskAdverse);
            if let Some(z) = d.zoo_family {
                assert!(crate::FAMILIES.iter().any(|f| f.name == z), "{z} is not a zoo family");
            }
        }
        let doc = pm_families_json();
        assert_eq!(doc.as_array().map_or(0, Vec::len), PM_FAMILIES.len());
        assert_eq!(doc[5]["name"], "power3");
        assert_eq!(doc[5]["params"][0]["key"], "n");
    }
}
