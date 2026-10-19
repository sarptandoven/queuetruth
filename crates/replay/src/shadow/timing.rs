//! Shadow construction (SPEC 3.1 latency semantics, 5.2.1, 5.2.6): validation, delays with per-shadow jitter streams
//! and checked time arithmetic.
//!
//! Delays: submit = `latency_ns + feed + decision + gateway + J_submit`; cancel = `latency_ns + gateway + J_cancel`;
//! amend i = `latency_ns + gateway + J_amend_i`. Jitter stream per shadow: `Xoshiro256StarStar::new(stable_seed(seed,
//! [b"latency", shadow_id]))`; draw 1 = J_submit, draw 2 = J_cancel (always drawn), then one draw per amend in amend
//! row order, each `range_inclusive(0, jitter_ns)`; `jitter_ns = 0` draws nothing.

use std::collections::HashMap;

use qt_schema::codec::validate_id;
use qt_schema::rows::{PriceSpec, ShadowAmend, ShadowSpec};
use qt_types::{Dur, QtError, QtResult, Ts, UNDEF_PRICE, Xoshiro256StarStar, stable_seed};

use super::Shadow;
use crate::EngineConfig;
use crate::util::InstrumentTable;

fn add(a: Ts, b: Dur, what: &str, id: &str) -> QtResult<Ts> {
    a.checked_add(b)
        .ok_or_else(|| QtError::Config(format!("shadow {id}: {what} overflows the timestamp range")))
}

fn input(id: &str, msg: impl Into<String>) -> QtError {
    QtError::InputFormat {
        location: format!("shadow {id}"),
        msg: msg.into(),
    }
}

/// Validated shadows (input order) and the id index.
pub(crate) fn build(
    specs: &[ShadowSpec],
    amends: &[ShadowAmend],
    cfg: &EngineConfig,
    instruments: &InstrumentTable,
) -> QtResult<(Vec<Shadow>, HashMap<String, u32>)> {
    let lm = cfg.latency;
    for (name, v) in [
        ("latency.feed_ns", lm.feed_ns),
        ("latency.decision_ns", lm.decision_ns),
        ("latency.gateway_ns", lm.gateway_ns),
        ("latency.jitter_ns", lm.jitter_ns),
    ] {
        if v < 0 {
            return Err(QtError::Config(format!("{name} must be >= 0 (got {v})")));
        }
    }
    if u32::try_from(specs.len()).is_err() {
        return Err(QtError::Config("more than 2^32 - 1 shadows".into()));
    }
    let mut by_id: HashMap<String, u32> = HashMap::with_capacity(specs.len());
    for (i, s) in specs.iter().enumerate() {
        validate_id("shadow_id", &s.shadow_id, &format!("shadow {i}"))?;
        if by_id.insert(s.shadow_id.clone(), i as u32).is_some() {
            return Err(QtError::Duplicate {
                what: "shadow_id".into(),
                id: s.shadow_id.clone(),
            });
        }
    }
    let mut per: Vec<Vec<&ShadowAmend>> = vec![Vec::new(); specs.len()];
    for a in amends {
        let Some(&i) = by_id.get(&a.shadow_id) else {
            return Err(input(&a.shadow_id, "amend references an unknown shadow_id"));
        };
        per[i as usize].push(a);
    }
    let fixed = lm
        .feed_ns
        .checked_add(lm.decision_ns)
        .and_then(|x| x.checked_add(lm.gateway_ns))
        .ok_or_else(|| QtError::Config("latency model sum overflows".into()))?;
    let mut out = Vec::with_capacity(specs.len());
    for (spec, am) in specs.iter().zip(&per) {
        let id = spec.shadow_id.as_str();
        if spec.qty == 0 {
            return Err(input(id, "qty must be > 0"));
        }
        if spec.latency_ns < 0 {
            return Err(input(id, "latency_ns must be >= 0"));
        }
        let tick = match spec.price {
            PriceSpec::Abs(p) if p == UNDEF_PRICE => return Err(input(id, "abs price is undefined")),
            // The instrument tick for abs shadows too (hidden-print tick grid, review M9).
            PriceSpec::Abs(_) => instruments
                .at(spec.instrument_id, spec.ts_submit)
                .map_or(1, |i| i.tick_size.max(1)),
            PriceSpec::Touch { .. } => {
                let tick = instruments.any(spec.instrument_id).map_or(0, |i| i.tick_size);
                if tick <= 0 {
                    return Err(QtError::Config(format!(
                        "shadow {id}: touch pricing needs instrument {} with tick_size > 0",
                        spec.instrument_id
                    )));
                }
                tick
            }
        };
        let mut prev_qty = spec.qty;
        let mut prev_send = spec.ts_submit;
        for a in am {
            if a.ts_send < prev_send {
                return Err(input(
                    id,
                    format!(
                        "amend ts_send {} is before the previous amend's {prev_send} (amends must be in time order)",
                        a.ts_send
                    ),
                ));
            }
            prev_send = a.ts_send;
            if a.ts_send < spec.ts_submit {
                return Err(input(
                    id,
                    format!("amend ts_send {} < ts_submit {}", a.ts_send, spec.ts_submit),
                ));
            }
            if a.new_qty >= prev_qty {
                return Err(input(
                    id,
                    format!(
                        "amend new_qty {} must be < the current qty {prev_qty} (priority-keeping reductions only)",
                        a.new_qty
                    ),
                ));
            }
            prev_qty = a.new_qty;
        }
        let mut rng =
            (lm.jitter_ns > 0).then(|| Xoshiro256StarStar::new(stable_seed(cfg.seed, &[b"latency", id.as_bytes()])));
        let mut jitter = || -> Dur {
            rng.as_mut()
                .map_or(0, |r| r.range_inclusive(0, lm.jitter_ns as u64) as Dur)
        };
        let j_submit = jitter();
        let j_cancel = jitter();
        let submit_delay = spec
            .latency_ns
            .checked_add(fixed)
            .and_then(|x| x.checked_add(j_submit))
            .ok_or_else(|| QtError::Config(format!("shadow {id}: submit delay overflows")))?;
        let base = spec
            .latency_ns
            .checked_add(lm.gateway_ns)
            .ok_or_else(|| QtError::Config(format!("shadow {id}: cancel delay overflows")))?;
        let mut s = Shadow::new(spec.instrument_id, spec.side, spec.price, tick, spec.qty);
        s.ts_live = add(spec.ts_submit, submit_delay, "ts_live", id)?;
        s.cancel_eff = match spec.ts_cancel {
            Some(c) => Some(add(c, add(base, j_cancel, "cancel delay", id)?, "cancel time", id)?),
            None => None,
        };
        for a in am {
            let d = add(base, jitter(), "amend delay", id)?;
            s.amends.push((add(a.ts_send, d, "amend time", id)?, a.new_qty));
        }
        out.push(s);
    }
    Ok((out, by_id))
}
