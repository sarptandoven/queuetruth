//! C3 NT `prob_fill_on_limit` closed-form geometric MLE (SPEC 5.4):
//! `p_hat = sum delta_i / sum [delta_i m_i + (1 - delta_i) M_i]`.
//!
//! Per shadow (rows grouped by `shadow_id`): `delta_i = 1` when a row has `truth_first_fill_here`, `m_i` = that row's
//! `touch_index`; otherwise `M_i` = the largest `touch_index` (touches survived before censoring). Audit ruling A7: a shadow first filled by a trade-through, a cross or any non-touch event is not excluded;
//! it is censored at that fill with `delta_i = 0` and `M_i` = touches strictly before it (`collect` emits touch rows
//! only up to the exact first-fill event, so the rule is the same `M_i`). Queue-gated variant: only touches with
//! `nt_excess > 0` count; a first fill at a non-gated touch is impossible under the gated model and censors the shadow
//! before that touch.

use std::collections::BTreeMap;

use qt_schema::rows::TouchRecordRow;

use crate::TouchFit;

#[derive(Default)]
struct Acc {
    first_fill: Option<(u32, bool)>,
    max_index: u32,
    rows: Vec<(u32, bool)>,
}

/// Closed-form NT touch MLE; `p_hat` is NaN when no touch was observed.
pub fn fit_nt_touch(records: &[TouchRecordRow], queue_gated: bool) -> TouchFit {
    let mut by: BTreeMap<&str, Acc> = BTreeMap::new();
    for r in records {
        let a = by.entry(r.shadow_id.as_str()).or_default();
        let gated = r.nt_excess > 0.0;
        if r.truth_first_fill_here && a.first_fill.is_none_or(|(m, _)| r.touch_index < m) {
            a.first_fill = Some((r.touch_index, gated));
        }
        a.max_index = a.max_index.max(r.touch_index);
        a.rows.push((r.touch_index, gated));
    }
    let (mut n_orders, mut n_filled, mut touches) = (0u64, 0u64, 0u64);
    for a in by.values() {
        n_orders += 1;
        match (a.first_fill, queue_gated) {
            (Some((m, _)), false) => {
                n_filled += 1;
                touches += u64::from(m);
            }
            (None, false) => touches += u64::from(a.max_index),
            (Some((m, gated)), true) => {
                if gated {
                    n_filled += 1;
                    touches += a.rows.iter().filter(|(i, g)| *g && *i <= m).count() as u64;
                } else {
                    touches += a.rows.iter().filter(|(i, g)| *g && *i < m).count() as u64;
                }
            }
            (None, true) => touches += a.rows.iter().filter(|(_, g)| *g).count() as u64,
        }
    }
    let p_hat = if touches > 0 {
        n_filled as f64 / touches as f64
    } else {
        f64::NAN
    };
    TouchFit {
        p_hat,
        n_orders,
        n_filled,
        touches,
    }
}
