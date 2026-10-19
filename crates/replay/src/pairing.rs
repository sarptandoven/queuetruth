//! Audit minor 24: the data-quality counter `removal_after_unpaired_fill` (`qt.data_quality.source_specific`).
//!
//! An `F` of a resting order is paired when a `C` or `M` of the same order id arrives in the same event or, on
//! venues with `fill_removal_shares_sequence`, before the first record of the instrument with a different
//! `sequence` (the pairing scope of SPEC 5.2.5 step 5). An `F` left unpaired at the end of its scope whose order then
//! gets a `C` or `M` within `UNPAIRED_FILL_WINDOW_EVENTS` events of its instrument (counted from the `F`'s event)
//! counts once: the removal arrived late, so replay counted the execution twice (as traded volume and as a
//! cancel). An `R` clears the pairing state. A non-zero value makes the verdict `warnings`.

use qt_types::{Action, MboEvent};

/// Events after an unpaired `F` in which a removal of its order still counts.
pub(crate) const UNPAIRED_FILL_WINDOW_EVENTS: u64 = 8;

/// Pairing state of one instrument.
#[derive(Debug, Default)]
pub(crate) struct FillPairing {
    /// Fills of the open scope: (order id, event index).
    pending: Vec<(u64, u64)>,
    /// Sequence of the open scope (`fill_removal_shares_sequence` venues).
    seq: u64,
    /// Unpaired fills of past scopes: (order id, event index).
    unpaired: Vec<(u64, u64)>,
    /// Completed events.
    events: u64,
}

impl FillPairing {
    /// One record (after the book applied it; `known` = the `F`'s order rests). Returns true when it counts.
    #[inline]
    pub(crate) fn record(&mut self, by_sequence: bool, e: &MboEvent, known: bool) -> bool {
        if by_sequence && !self.pending.is_empty() && e.sequence != self.seq {
            self.flush();
        }
        match Action::from_byte(e.action) {
            Some(Action::Clear) => {
                self.pending.clear();
                self.unpaired.clear();
                false
            }
            Some(Action::Fill) if known => {
                if !self.pending.iter().any(|p| p.0 == e.order_id) {
                    self.pending.push((e.order_id, self.events));
                }
                self.seq = e.sequence;
                false
            }
            Some(Action::Cancel | Action::Modify) => self.removal(e.order_id),
            _ => false,
        }
    }

    #[inline]
    fn removal(&mut self, id: u64) -> bool {
        if let Some(i) = self.pending.iter().position(|p| p.0 == id) {
            self.pending.swap_remove(i);
            return false;
        }
        if self.unpaired.is_empty() {
            return false;
        }
        match self.unpaired.iter().position(|p| p.0 == id) {
            Some(i) => {
                let (_, t) = self.unpaired.swap_remove(i);
                self.events - t <= UNPAIRED_FILL_WINDOW_EVENTS
            }
            None => false,
        }
    }

    /// End of an event of the instrument.
    #[inline]
    pub(crate) fn event_end(&mut self, by_sequence: bool) {
        if !by_sequence && !self.pending.is_empty() {
            self.flush();
        }
        self.events += 1;
    }

    fn flush(&mut self) {
        let now = self.events;
        self.unpaired.retain(|&(_, t)| now - t <= UNPAIRED_FILL_WINDOW_EVENTS);
        for (id, t) in self.pending.drain(..) {
            match self.unpaired.iter_mut().find(|p| p.0 == id) {
                Some(p) => p.1 = t,
                None => self.unpaired.push((id, t)),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(action: u8, id: u64, seq: u64) -> MboEvent {
        MboEvent {
            action,
            order_id: id,
            sequence: seq,
            ..MboEvent::default()
        }
    }

    #[test]
    fn late_removal_counts_once_within_the_window() {
        // Event scope (CME): F then C in the same event pairs; a C one event later counts.
        let mut p = FillPairing::default();
        assert!(!p.record(false, &rec(b'F', 1, 1), true));
        assert!(!p.record(false, &rec(b'C', 1, 1), true));
        p.event_end(false);
        assert!(!p.record(false, &rec(b'F', 2, 2), true));
        p.event_end(false);
        assert!(p.record(false, &rec(b'C', 2, 3), true));
        assert!(!p.record(false, &rec(b'C', 2, 3), true), "counted once");
        p.event_end(false);
        // Beyond the window: no count.
        assert!(!p.record(false, &rec(b'F', 3, 4), true));
        for _ in 0..=UNPAIRED_FILL_WINDOW_EVENTS {
            p.event_end(false);
        }
        assert!(!p.record(false, &rec(b'M', 3, 9), true));
        // Sequence scope (Nasdaq): the C may follow in a later event with the same sequence.
        let mut q = FillPairing::default();
        assert!(!q.record(true, &rec(b'F', 5, 7), true));
        q.event_end(true);
        assert!(!q.record(true, &rec(b'C', 5, 7), true));
        assert!(!q.record(true, &rec(b'F', 6, 8), true));
        q.event_end(true);
        assert!(!q.record(true, &rec(b'A', 9, 9), true));
        assert!(q.record(true, &rec(b'C', 6, 10), true));
        // A clear forgets everything; unknown orders never pend.
        assert!(!q.record(true, &rec(b'F', 7, 11), true));
        assert!(!q.record(true, &rec(b'R', 0, 12), true));
        assert!(!q.record(true, &rec(b'C', 7, 13), true));
        assert!(!q.record(true, &rec(b'F', 8, 14), false));
        assert!(!q.record(true, &rec(b'C', 8, 15), true));
    }
}
