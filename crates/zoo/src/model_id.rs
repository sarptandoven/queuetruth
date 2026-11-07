//! `ModelId` grammar (SPEC 5.3.2): `<family>[<k>=<v>;...]`, keys sorted ascending, `mode` always present, `exch`
//! only for the hbt L2 families, f64 values printed with Rust `{}` (shortest round trip, no exponent).

use std::collections::BTreeMap;

use qt_types::{QtError, QtResult};

use crate::{Exchange, Family, Mode, ModelId};

/// Default particle count of `databento_mc` when neither the id nor the spec gives one.
pub const DEFAULT_PARTICLES: u32 = 100;

impl Family {
    /// Every family in canonical order.
    pub const ALL: [Family; 13] = [
        Family::HbtRiskAdverse,
        Family::HbtPower,
        Family::HbtPower2,
        Family::HbtPower3,
        Family::HbtLog,
        Family::HbtLog2,
        Family::HbtL3Fifo,
        Family::NtQueueL2,
        Family::NtProbTouch,
        Family::NtQueueL2ProbTouch,
        Family::DatabentoMc,
        Family::FrontOfQueue,
        Family::TradeThroughOnly,
    ];

    /// Canonical family name.
    pub const fn name(self) -> &'static str {
        match self {
            Family::HbtRiskAdverse => "hbt_risk_adverse",
            Family::HbtPower => "hbt_power",
            Family::HbtPower2 => "hbt_power2",
            Family::HbtPower3 => "hbt_power3",
            Family::HbtLog => "hbt_log",
            Family::HbtLog2 => "hbt_log2",
            Family::HbtL3Fifo => "hbt_l3_fifo",
            Family::NtQueueL2 => "nt_queue_l2",
            Family::NtProbTouch => "nt_prob_touch",
            Family::NtQueueL2ProbTouch => "nt_queue_l2_prob_touch",
            Family::DatabentoMc => "databento_mc",
            Family::FrontOfQueue => "front_of_queue",
            Family::TradeThroughOnly => "trade_through_only",
        }
    }

    /// Parses a canonical family name.
    pub fn from_name(s: &str) -> Option<Family> {
        Family::ALL.iter().copied().find(|f| f.name() == s)
    }

    /// hbt L2 families (RiskAdverse and ProbQueueModel): the only ones with an `exch` key.
    pub const fn is_hbt_l2(self) -> bool {
        matches!(
            self,
            Family::HbtRiskAdverse
                | Family::HbtPower
                | Family::HbtPower2
                | Family::HbtPower3
                | Family::HbtLog
                | Family::HbtLog2
        )
    }

    /// NautilusTrader-style families.
    pub const fn is_nt(self) -> bool {
        matches!(
            self,
            Family::NtQueueL2 | Family::NtProbTouch | Family::NtQueueL2ProbTouch
        )
    }

    /// Particle (Monte Carlo) families.
    pub const fn is_particle(self) -> bool {
        matches!(self, Family::DatabentoMc)
    }

    /// Families whose predictions are analytic in the touch count (5.3.3 e/f).
    pub const fn is_touch(self) -> bool {
        matches!(self, Family::NtProbTouch | Family::NtQueueL2ProbTouch)
    }

    /// Parameter keys (besides `mode`/`exch`) the family accepts.
    pub const fn param_keys(self) -> &'static [&'static str] {
        match self {
            Family::HbtPower | Family::HbtPower2 | Family::HbtPower3 => &["n"],
            Family::NtProbTouch | Family::NtQueueL2ProbTouch => &["p"],
            Family::DatabentoMc => &["k", "particles"],
            _ => &[],
        }
    }
}

impl Family {
    /// One line on what the family models (`zoo --list-models`; docs/concepts/l2-model-zoo.md has the rules).
    pub const fn summary(self) -> &'static str {
        match self {
            Family::HbtRiskAdverse => {
                "hftbacktest RiskAdverseQueueModel: every unexplained decrease came from behind you"
            }
            Family::HbtPower => "hftbacktest PowerProbQueueFunc: share of a decrease from ahead b^n / (b^n + f^n)",
            Family::HbtPower2 => "hftbacktest PowerProbQueueFunc2: share of a decrease from ahead b^n / (b + f)^n",
            Family::HbtPower3 => "hftbacktest PowerProbQueueFunc3: share of a decrease from ahead 1 - (f / (f + b))^n",
            Family::HbtLog => "hftbacktest LogProbQueueFunc",
            Family::HbtLog2 => "hftbacktest LogProbQueueFunc2",
            Family::HbtL3Fifo => "hftbacktest L3FIFOQueueModel on the MBO records (order-level input, not L2)",
            Family::NtQueueL2 => "NautilusTrader queue position on L2 (queue_position=True)",
            Family::NtProbTouch => {
                "NautilusTrader FillModel(prob_fill_on_limit=p): each at-price touch fills with probability p"
            }
            Family::NtQueueL2ProbTouch => {
                "NautilusTrader queue position, then prob_fill_on_limit=p once the queue is gone"
            }
            Family::DatabentoMc => "Databento queue-position estimator (Monte Carlo over particles; k in (-1, 1))",
            Family::FrontOfQueue => "upper bound: nothing ahead of you, every at-price trade fills",
            Family::TradeThroughOnly => "lower bound: fills only when a displayed trade goes through your price",
        }
    }

    /// The closest family name to an unknown one (`did you mean`): the first name containing it (3 characters or more),
    /// else the longest name it starts with, else the smallest edit distance up to a third of its length (at least 2).
    pub fn suggest(name: &str) -> Option<&'static str> {
        let n = name.trim().to_ascii_lowercase();
        if n.is_empty() {
            return None;
        }
        let names = Family::ALL.iter().map(|f| f.name());
        if n.len() >= 3
            && let Some(hit) = names.clone().find(|f| f.contains(n.as_str()))
        {
            return Some(hit);
        }
        if let Some(hit) = names.clone().filter(|f| n.starts_with(f)).max_by_key(|f| f.len()) {
            return Some(hit);
        }
        let limit = (n.chars().count() / 3).max(2);
        names
            .map(|f| (edit_distance(&n, f), f))
            .filter(|(d, _)| *d <= limit)
            .min_by_key(|(d, _)| *d)
            .map(|(_, f)| f)
    }
}

/// Levenshtein distance over chars.
fn edit_distance(a: &str, b: &str) -> usize {
    let b: Vec<char> = b.chars().collect();
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    for (i, ca) in a.chars().enumerate() {
        let mut cur = vec![i + 1];
        for (j, cb) in b.iter().enumerate() {
            let sub = prev[j] + usize::from(ca != *cb);
            cur.push(sub.min(prev[j + 1] + 1).min(cur[j] + 1));
        }
        prev = cur;
    }
    prev[b.len()]
}

impl std::fmt::Display for Family {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.name())
    }
}

impl Mode {
    /// Canonical value.
    pub const fn name(self) -> &'static str {
        match self {
            Mode::Faithful => "faithful",
            Mode::Corrected => "corrected",
        }
    }
}

impl Exchange {
    /// Canonical value.
    pub const fn name(self) -> &'static str {
        match self {
            Exchange::Partial => "partial",
            Exchange::NoPartial => "no_partial",
        }
    }
}

fn cfg_err(msg: String) -> QtError {
    QtError::Config(msg)
}

impl ModelId {
    /// A model id with parameters, normalized (non-hbt families carry `Exchange::Partial`, which is not printed).
    pub fn new(family: Family, params: &[(&str, f64)], mode: Mode, exchange: Exchange) -> QtResult<Self> {
        let mut map = BTreeMap::new();
        for (k, v) in params {
            map.insert((*k).to_owned(), *v);
        }
        let id = ModelId {
            family,
            params: map,
            mode,
            exchange,
        };
        id.normalized(DEFAULT_PARTICLES)
    }

    /// Parses an id; missing `mode`, `exch`, `particles` and `p` are filled from the given defaults.
    pub fn parse_with(s: &str, mode: Mode, exchange: Exchange, particles: u32) -> QtResult<Self> {
        let s = s.trim();
        let (fam, body) = match s.find('[') {
            Some(i) => {
                let rest = &s[i + 1..];
                let body = rest
                    .strip_suffix(']')
                    .ok_or_else(|| cfg_err(format!("model id {s:?}: missing closing ']'")))?;
                (&s[..i], Some(body))
            }
            None => (s, None),
        };
        let family = Family::from_name(fam).ok_or_else(|| {
            let hint = Family::suggest(fam).map_or(String::new(), |f| format!("; did you mean {f:?}?"));
            cfg_err(format!(
                "model id {s:?}: unknown family {fam:?}{hint} (`queuetruth zoo --list-models` lists every model)"
            ))
        })?;
        let mut params = BTreeMap::new();
        let mut mode_v: Option<Mode> = None;
        let mut exch_v: Option<Exchange> = None;
        if let Some(body) = body {
            for kv in body.split(';').filter(|x| !x.trim().is_empty()) {
                let (k, v) = kv
                    .split_once('=')
                    .ok_or_else(|| cfg_err(format!("model id {s:?}: {kv:?} is not key=value")))?;
                let (k, v) = (k.trim(), v.trim());
                match k {
                    "mode" => {
                        let m = match v {
                            "faithful" => Mode::Faithful,
                            "corrected" => Mode::Corrected,
                            _ => return Err(cfg_err(format!("model id {s:?}: mode {v:?}"))),
                        };
                        if mode_v.replace(m).is_some() {
                            return Err(cfg_err(format!("model id {s:?}: duplicate key mode")));
                        }
                    }
                    "exch" => {
                        if !family.is_hbt_l2() {
                            return Err(cfg_err(format!("model id {s:?}: family {family} has no exch key")));
                        }
                        let e = match v {
                            "partial" => Exchange::Partial,
                            "no_partial" | "no-partial" => Exchange::NoPartial,
                            _ => return Err(cfg_err(format!("model id {s:?}: exch {v:?}"))),
                        };
                        if exch_v.replace(e).is_some() {
                            return Err(cfg_err(format!("model id {s:?}: duplicate key exch")));
                        }
                    }
                    _ => {
                        if !family.param_keys().contains(&k) {
                            return Err(cfg_err(format!("model id {s:?}: unknown key {k:?} for {family}")));
                        }
                        let x: f64 = v
                            .parse()
                            .map_err(|_| cfg_err(format!("model id {s:?}: {k}={v:?} is not a number")))?;
                        if params.insert(k.to_owned(), x).is_some() {
                            return Err(cfg_err(format!("model id {s:?}: duplicate key {k}")));
                        }
                    }
                }
            }
        }
        let id = ModelId {
            family,
            params,
            mode: mode_v.unwrap_or(mode),
            exchange: exch_v.unwrap_or(exchange),
        };
        id.normalized(particles)
    }

    /// Fills family defaults, validates ranges and normalizes `exchange` for non-hbt families.
    fn normalized(mut self, particles: u32) -> QtResult<Self> {
        let name = self.family.name();
        for k in self.params.keys() {
            if !self.family.param_keys().contains(&k.as_str()) {
                return Err(cfg_err(format!("model {name}: unknown key {k:?}")));
            }
        }
        if !self.family.is_hbt_l2() {
            self.exchange = Exchange::Partial;
        }
        match self.family {
            Family::HbtPower | Family::HbtPower2 | Family::HbtPower3 => {
                let n = *self
                    .params
                    .get("n")
                    .ok_or_else(|| cfg_err(format!("model {name}: parameter n is required")))?;
                if !(n.is_finite() && n > 0.0) {
                    return Err(cfg_err(format!("model {name}: n must be finite and > 0, got {n}")));
                }
            }
            Family::NtProbTouch | Family::NtQueueL2ProbTouch => {
                let p = *self.params.entry("p".into()).or_insert(1.0);
                if !(0.0..=1.0).contains(&p) {
                    return Err(cfg_err(format!("model {name}: p must be in [0, 1], got {p}")));
                }
            }
            Family::DatabentoMc => {
                let k = *self
                    .params
                    .get("k")
                    .ok_or_else(|| cfg_err(format!("model {name}: parameter k is required")))?;
                if !(k > -1.0 && k < 1.0) {
                    return Err(cfg_err(format!("model {name}: k must be in (-1, 1), got {k}")));
                }
                let n = *self.params.entry("particles".into()).or_insert(f64::from(particles));
                if !((1.0..=1_000_000.0).contains(&n) && n.fract() == 0.0) {
                    return Err(cfg_err(format!(
                        "model {name}: particles must be an integer in 1..=1000000, got {n}"
                    )));
                }
            }
            _ => {}
        }
        Ok(self)
    }

    /// A numeric parameter.
    pub fn param(&self, key: &str) -> Option<f64> {
        self.params.get(key).copied()
    }

    /// Particle count (`databento_mc`), 0 for other families.
    pub fn particles(&self) -> usize {
        if self.family.is_particle() {
            self.param("particles")
                .map(|p| p as usize)
                .unwrap_or(DEFAULT_PARTICLES as usize)
        } else {
            0
        }
    }
}

impl std::fmt::Display for ModelId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let mut keys: BTreeMap<&str, String> = BTreeMap::new();
        keys.insert("mode", self.mode.name().to_owned());
        if self.family.is_hbt_l2() {
            keys.insert("exch", self.exchange.name().to_owned());
        }
        for (k, v) in &self.params {
            keys.insert(k.as_str(), format!("{v}"));
        }
        f.write_str(self.family.name())?;
        f.write_str("[")?;
        for (i, (k, v)) in keys.iter().enumerate() {
            if i > 0 {
                f.write_str(";")?;
            }
            write!(f, "{k}={v}")?;
        }
        f.write_str("]")
    }
}

impl std::str::FromStr for ModelId {
    type Err = QtError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        ModelId::parse_with(s, Mode::Faithful, Exchange::Partial, DEFAULT_PARTICLES)
    }
}

/// The `all` set of SPEC 5.3.4 (16 models). `hbt_power[n=1]` is not in it: `b / (b + f)` equals
/// `hbt_power3[n=1]` = `1 - f / (f + b)`, so it only added a duplicate row (0.1.1).
pub fn default_models(mode: Mode, exchange: Exchange) -> Vec<ModelId> {
    let mk = |f: Family, p: &[(&str, f64)]| {
        let mut params = BTreeMap::new();
        for (k, v) in p {
            params.insert((*k).to_owned(), *v);
        }
        if f == Family::DatabentoMc {
            params.insert("particles".to_owned(), f64::from(DEFAULT_PARTICLES));
        }
        ModelId {
            family: f,
            params,
            mode,
            exchange: if f.is_hbt_l2() { exchange } else { Exchange::Partial },
        }
    };
    vec![
        mk(Family::HbtRiskAdverse, &[]),
        mk(Family::HbtPower, &[("n", 2.0)]),
        mk(Family::HbtPower, &[("n", 3.0)]),
        mk(Family::HbtPower2, &[("n", 2.0)]),
        mk(Family::HbtPower3, &[("n", 1.0)]),
        mk(Family::HbtPower3, &[("n", 2.0)]),
        mk(Family::HbtPower3, &[("n", 3.0)]),
        mk(Family::HbtLog, &[]),
        mk(Family::HbtLog2, &[]),
        mk(Family::HbtL3Fifo, &[]),
        mk(Family::NtQueueL2, &[]),
        mk(Family::NtProbTouch, &[("p", 1.0)]),
        mk(Family::DatabentoMc, &[("k", -0.8)]),
        mk(Family::DatabentoMc, &[("k", -0.95)]),
        mk(Family::FrontOfQueue, &[]),
        mk(Family::TradeThroughOnly, &[]),
    ]
}

/// Expands a spec model list (`"all"` or ids) into canonical ids; duplicates are removed (first kept).
pub fn expand_models(list: &[String], mode: Mode, exchange: Exchange, particles: u32) -> QtResult<Vec<ModelId>> {
    let mut out: Vec<ModelId> = Vec::new();
    for s in list {
        let ids = if s.trim() == "all" {
            default_models(mode, exchange)
                .into_iter()
                .map(|mut m| {
                    if m.family.is_particle() {
                        m.params.insert("particles".into(), f64::from(particles));
                    }
                    m
                })
                .collect()
        } else {
            vec![ModelId::parse_with(s, mode, exchange, particles)?]
        };
        for id in ids {
            let txt = id.to_string();
            if !out.iter().any(|o| o.to_string() == txt) {
                out.push(id);
            }
        }
    }
    if out.is_empty() {
        return Err(QtError::Config("models: empty model list".into()));
    }
    Ok(out)
}
