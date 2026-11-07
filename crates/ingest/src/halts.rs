//! Trading-halt spans recorded at ingest (SPEC 1.2, halt-censoring extension): the spans in which an instrument was
//! not in continuous trading as its feed reported it, written to `qt.data_quality.trading_halts` and applied by
//! replay, `why`, the zoo, calibration and features (`qt_replay::TradingHalt`).
//!
//! A feed reports two independent states per instrument: its trading status (Nasdaq ITCH `H`, IEX DEEP+ `H`) and an
//! operational halt of the venue (ITCH `h` of market `Q`, IEX `O`). The instrument is halted while either is; a
//! span opens when it leaves continuous trading and closes at the message that returns it there. A change between
//! two halted states (ITCH `H` to `Q` before an IPO cross) keeps the span open with its first state.

use std::collections::BTreeMap;

use qt_schema::json::TradingHaltSpan;
use qt_types::Ts;

#[derive(Debug, Default, Clone)]
struct State {
    status: bool,
    operational: bool,
    open: Option<(Ts, String, String)>,
}

/// Which of the two states a message reports.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HaltKind {
    /// Trading status (ITCH `H`, IEX `H`).
    Status,
    /// Operational halt of the venue (ITCH `h` of market `Q`, IEX `O`).
    Operational,
}

/// Trading state of every instrument of one feed.
#[derive(Debug, Default, Clone)]
pub struct HaltTracker {
    states: BTreeMap<u32, State>,
    done: Vec<TradingHaltSpan>,
}

impl HaltTracker {
    /// Empty tracker (every instrument trading until a message says otherwise).
    pub fn new() -> Self {
        Self::default()
    }

    /// Applies one state message of `instrument_id` at `ts`: `halted` is the state it reports, `state` the label
    /// (`<source>_<type>:<byte>`) and `reason` the feed's reason code, kept when the message opens a span.
    pub fn apply(&mut self, ts: Ts, instrument_id: u32, kind: HaltKind, halted: bool, state: String, reason: String) {
        let st = self.states.entry(instrument_id).or_default();
        let was = st.status || st.operational;
        match kind {
            HaltKind::Status => st.status = halted,
            HaltKind::Operational => st.operational = halted,
        }
        match (was, st.status || st.operational) {
            (false, true) => st.open = Some((ts, state, reason)),
            (true, false) => {
                if let Some((ts_start, state, reason)) = st.open.take() {
                    self.done.push(TradingHaltSpan {
                        instrument_id,
                        ts_start,
                        ts_end: ts,
                        state,
                        reason,
                    });
                }
            }
            _ => {}
        }
    }

    /// The spans, sorted by instrument, start, end and state; a span still open ends at `end_of_input` (at least
    /// one nanosecond after its start). Leaves the tracker empty.
    pub fn finish(&mut self, end_of_input: Ts) -> Vec<TradingHaltSpan> {
        for (instrument_id, st) in std::mem::take(&mut self.states) {
            if let Some((ts_start, state, reason)) = st.open {
                self.done.push(TradingHaltSpan {
                    instrument_id,
                    ts_start,
                    ts_end: end_of_input.max(ts_start.saturating_add(1)),
                    state,
                    reason,
                });
            }
        }
        let mut out = std::mem::take(&mut self.done);
        sort(&mut out);
        out
    }
}

/// Spans in a fixed order whatever the order they were found in (several inputs of one ingest).
pub fn sort(spans: &mut [TradingHaltSpan]) {
    spans.sort_by(|a, b| {
        (a.instrument_id, a.ts_start, a.ts_end, &a.state).cmp(&(b.instrument_id, b.ts_start, b.ts_end, &b.state))
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn span(iid: u32, a: Ts, b: Ts, state: &str, reason: &str) -> TradingHaltSpan {
        TradingHaltSpan {
            instrument_id: iid,
            ts_start: a,
            ts_end: b,
            state: state.into(),
            reason: reason.into(),
        }
    }

    #[test]
    fn spans_open_on_leaving_trading_and_close_on_return() {
        let mut t = HaltTracker::new();
        // trading at the start: no span
        t.apply(1, 7, HaltKind::Status, false, "itch_H:T".into(), String::new());
        // halted, then quotation only (the span keeps its first state), then trading
        t.apply(10, 7, HaltKind::Status, true, "itch_H:H".into(), "IPO1".into());
        t.apply(20, 7, HaltKind::Status, true, "itch_H:Q".into(), "IPOQ".into());
        t.apply(30, 7, HaltKind::Status, false, "itch_H:T".into(), String::new());
        // an operational halt inside a status halt extends it until both are lifted
        t.apply(40, 7, HaltKind::Status, true, "itch_H:P".into(), "LUDP".into());
        t.apply(45, 7, HaltKind::Operational, true, "itch_h:H".into(), String::new());
        t.apply(50, 7, HaltKind::Status, false, "itch_H:T".into(), String::new());
        t.apply(55, 7, HaltKind::Operational, false, "itch_h:T".into(), String::new());
        // another instrument halted to the end of the input
        t.apply(60, 3, HaltKind::Operational, true, "iex_O:O".into(), String::new());
        assert_eq!(
            t.finish(1_000),
            vec![
                span(3, 60, 1_000, "iex_O:O", ""),
                span(7, 10, 30, "itch_H:H", "IPO1"),
                span(7, 40, 55, "itch_H:P", "LUDP"),
            ]
        );
        assert!(t.finish(1_000).is_empty(), "finish leaves the tracker empty");
    }

    #[test]
    fn an_open_span_lasts_at_least_one_nanosecond() {
        let mut t = HaltTracker::new();
        t.apply(500, 1, HaltKind::Status, true, "iex_H:H".into(), String::new());
        assert_eq!(t.finish(100), vec![span(1, 500, 501, "iex_H:H", "")]);
    }
}
