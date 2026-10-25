//! Naive reference book, independent of `qt_book`: every resting order lives in one `Vec` in arrival order, so the
//! FIFO of a level is the subsequence of that `Vec` at (side, price). Record semantics are SPEC 5.1.1 (et): `A`
//! ignored at `UNDEF_PRICE` or size 0, duplicate id = remove + append (counted unless inside a flagged snapshot on a
//! venue with flagged snapshots); `C` size = quantity removed (`<` resting = reduce, else remove); `M` per the venue's
//! priority rule (unknown id = add, size 0 = remove, unchanged = no effect); `R` clears; `F`/`T`/`N` change nothing.
//! Audit ruling A6: on CME an `M` keeps priority only for a strict size decrease at the same price (same price and
//! same size, e.g. an iceberg refresh or account change, loses priority); other venues keep their `PriorityRule`.
//! Oracle ruling A6b (CME): the size an `M` is compared with is the order's post-fill remainder, its resting size
//! minus what `F` records of the same event reported for it and no `C`/`M` has removed yet; an `M` to exactly that
//! remainder after such a fill is the execution's own book update and keeps priority, anything above it loses.
//! Also hosts the data-quality counters and verdict of SPEC 5.1.3 / 5.1.5 (`DqTracker`), including the audit minor
//! 24 counter `removal_after_unpaired_fill` (see `DqTracker`).

use indexmap::IndexMap;
use qt_schema::json::{DataQuality, Verdict};
use qt_types::{Action, BookSide, MboEvent, Px, Qty, Side, Ts, UNDEF_PRICE, VenueRules, flags};

/// Priority rule of SPEC 3.1 with audit ruling A6 applied on CME (strict decrease at the same price).
pub fn keeps_priority(rules: &VenueRules, old_px: Px, old_sz: Qty, new_px: Px, new_sz: Qty) -> bool {
    if rules.venue == qt_types::Venue::Cme {
        old_px == new_px && new_sz < old_sz
    } else {
        rules.modify_priority.keeps_priority(old_px, old_sz, new_px, new_sz)
    }
}

/// One resting order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RefOrder {
    /// Order id.
    pub order_id: u64,
    /// Side.
    pub side: BookSide,
    /// Price.
    pub price: Px,
    /// Displayed size.
    pub size: Qty,
}

/// Book effect of one record (mirror of the SPEC 3.5 `Effect`, own type).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RefEffect {
    /// No book change.
    None,
    /// Order appended.
    Add {
        /// Order id.
        order_id: u64,
        /// Side.
        side: BookSide,
        /// Price.
        price: Px,
        /// Size.
        size: Qty,
    },
    /// Order removed.
    Remove {
        /// Order id.
        order_id: u64,
        /// Side.
        side: BookSide,
        /// Price.
        price: Px,
        /// Size removed (the resting size).
        size: Qty,
    },
    /// Size reduced in place (priority kept).
    Reduce {
        /// Order id.
        order_id: u64,
        /// Side.
        side: BookSide,
        /// Price.
        price: Px,
        /// Reduction.
        by: Qty,
    },
    /// Removed and appended at the tail of the new level (priority lost).
    Replace {
        /// Order id.
        order_id: u64,
        /// Side.
        side: BookSide,
        /// Old price.
        old_price: Px,
        /// Old size.
        old_size: Qty,
        /// New price.
        new_price: Px,
        /// New size.
        new_size: Qty,
    },
    /// Book cleared.
    Clear,
}

/// The eight book invariant counters of SPEC 3.5.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RefInvariants {
    /// `C.size` above the resting size.
    pub cancels_exceeding_resting: u64,
    /// `F.size` above the resting size.
    pub fills_exceeding_resting: u64,
    /// `F` for an unknown order.
    pub fills_unknown_order: u64,
    /// `C`/`M` for an unknown order.
    pub unknown_order_refs: u64,
    /// Duplicate `A` outside a flagged snapshot.
    pub duplicate_adds: u64,
    /// `M` on a venue without modifies.
    pub unexpected_modifies: u64,
    /// Event ends with `bid >= ask`.
    pub crossed_books_after_last: u64,
    /// Event ends with `bid > ask`.
    pub negative_spreads: u64,
}

/// Vec-based reference book (O(n) per operation).
#[derive(Clone, Debug)]
pub struct NaiveBook {
    rules: VenueRules,
    orders: Vec<RefOrder>,
    inv: RefInvariants,
    /// CME: fills of the current event per order id, not yet removed (A6b).
    event_fills: Vec<(u64, Qty)>,
}

impl NaiveBook {
    /// Empty book for a venue rule set.
    pub fn new(rules: VenueRules) -> Self {
        Self {
            rules,
            orders: Vec::new(),
            inv: RefInvariants::default(),
            event_fills: Vec::new(),
        }
    }

    fn find(&self, order_id: u64) -> Option<usize> {
        self.orders.iter().position(|o| o.order_id == order_id)
    }

    /// The resting order with this id.
    pub fn order(&self, order_id: u64) -> Option<RefOrder> {
        self.find(order_id).map(|i| self.orders[i])
    }

    /// Number of resting orders.
    pub fn len(&self) -> usize {
        self.orders.len()
    }

    /// True when no order rests.
    pub fn is_empty(&self) -> bool {
        self.orders.is_empty()
    }

    /// Invariant counters so far.
    pub fn invariants(&self) -> RefInvariants {
        self.inv
    }

    /// `(total, count)` at a level.
    pub fn level(&self, side: BookSide, price: Px) -> (Qty, u32) {
        self.orders
            .iter()
            .filter(|o| o.side == side && o.price == price)
            .fold((0, 0), |(t, c), o| (t + o.size, c + 1))
    }

    /// Order ids at a level in FIFO order.
    pub fn ids_at(&self, side: BookSide, price: Px) -> Vec<u64> {
        self.orders
            .iter()
            .filter(|o| o.side == side && o.price == price)
            .map(|o| o.order_id)
            .collect()
    }

    /// Orders at a level in FIFO order.
    pub fn orders_at(&self, side: BookSide, price: Px) -> Vec<RefOrder> {
        self.orders
            .iter()
            .filter(|o| o.side == side && o.price == price)
            .copied()
            .collect()
    }

    /// Best price of a side.
    pub fn best_px(&self, side: BookSide) -> Option<Px> {
        let it = self.orders.iter().filter(|o| o.side == side).map(|o| o.price);
        match side {
            BookSide::Bid => it.max(),
            BookSide::Ask => it.min(),
        }
    }

    /// Best level `(price, total, count)`.
    pub fn best(&self, side: BookSide) -> Option<(Px, Qty, u32)> {
        self.best_px(side).map(|p| {
            let (t, c) = self.level(side, p);
            (p, t, c)
        })
    }

    /// Every level of a side, best first.
    pub fn levels(&self, side: BookSide) -> Vec<(Px, Qty, u32)> {
        let mut prices: Vec<Px> = self.orders.iter().filter(|o| o.side == side).map(|o| o.price).collect();
        prices.sort_unstable();
        prices.dedup();
        if side == BookSide::Bid {
            prices.reverse();
        }
        prices
            .into_iter()
            .map(|p| {
                let (t, c) = self.level(side, p);
                (p, t, c)
            })
            .collect()
    }

    fn remove_at(&mut self, i: usize) -> RefOrder {
        self.orders.remove(i)
    }

    fn add(&mut self, e: &MboEvent, side: BookSide) -> RefEffect {
        if e.price == UNDEF_PRICE || e.size == 0 {
            return RefEffect::None;
        }
        if let Some(i) = self.find(e.order_id) {
            if !(self.rules.snapshot_records_flagged && e.flags & flags::SNAPSHOT != 0) {
                self.inv.duplicate_adds += 1;
            }
            self.remove_at(i);
        }
        self.orders.push(RefOrder {
            order_id: e.order_id,
            side,
            price: e.price,
            size: e.size,
        });
        RefEffect::Add {
            order_id: e.order_id,
            side,
            price: e.price,
            size: e.size,
        }
    }

    /// Applies one record and returns its effect.
    pub fn apply(&mut self, e: &MboEvent) -> RefEffect {
        let cme = self.rules.venue == qt_types::Venue::Cme;
        let mut unremoved = 0;
        if cme {
            match Action::from_byte(e.action) {
                Some(Action::Fill) if self.find(e.order_id).is_some() => self.event_fills.push((e.order_id, e.size)),
                Some(Action::Cancel | Action::Modify) => {
                    unremoved = self.event_fills.iter().filter(|f| f.0 == e.order_id).map(|f| f.1).sum();
                    self.event_fills.retain(|f| f.0 != e.order_id);
                }
                Some(Action::Clear) => self.event_fills.clear(),
                _ => {}
            }
        }
        let out = self.apply_record(e, unremoved);
        if e.is_last() {
            self.event_fills.clear();
        }
        out
    }

    fn apply_record(&mut self, e: &MboEvent, unremoved: Qty) -> RefEffect {
        let Some(action) = Action::from_byte(e.action) else {
            return RefEffect::None;
        };
        match action {
            Action::Clear => {
                self.orders.clear();
                RefEffect::Clear
            }
            Action::Add => match BookSide::from_byte(e.side) {
                Some(side) => self.add(e, side),
                None => RefEffect::None,
            },
            Action::Cancel => {
                let Some(i) = self.find(e.order_id) else {
                    self.inv.unknown_order_refs += 1;
                    return RefEffect::None;
                };
                let o = self.orders[i];
                if e.size < o.size {
                    self.orders[i].size -= e.size;
                    RefEffect::Reduce {
                        order_id: o.order_id,
                        side: o.side,
                        price: o.price,
                        by: e.size,
                    }
                } else {
                    if e.size > o.size {
                        self.inv.cancels_exceeding_resting += 1;
                    }
                    self.remove_at(i);
                    RefEffect::Remove {
                        order_id: o.order_id,
                        side: o.side,
                        price: o.price,
                        size: o.size,
                    }
                }
            }
            Action::Modify if !self.rules.modify_records_present => {
                self.inv.unexpected_modifies += 1;
                RefEffect::None
            }
            Action::Modify => {
                let Some(i) = self.find(e.order_id) else {
                    self.inv.unknown_order_refs += 1;
                    return match BookSide::from_byte(e.side) {
                        Some(side) => self.add(e, side),
                        None => RefEffect::None,
                    };
                };
                let o = self.orders[i];
                if e.size == 0 {
                    self.remove_at(i);
                    return RefEffect::Remove {
                        order_id: o.order_id,
                        side: o.side,
                        price: o.price,
                        size: o.size,
                    };
                }
                let keeps = if self.rules.venue == qt_types::Venue::Cme && unremoved > 0 {
                    let left = o.size.saturating_sub(unremoved);
                    o.price == e.price && e.size <= left
                } else {
                    keeps_priority(&self.rules, o.price, o.size, e.price, e.size)
                };
                if keeps {
                    if o.size == e.size {
                        return RefEffect::None;
                    }
                    self.orders[i].size = e.size;
                    return RefEffect::Reduce {
                        order_id: o.order_id,
                        side: o.side,
                        price: o.price,
                        by: o.size - e.size,
                    };
                }
                self.remove_at(i);
                self.orders.push(RefOrder {
                    order_id: o.order_id,
                    side: o.side,
                    price: e.price,
                    size: e.size,
                });
                RefEffect::Replace {
                    order_id: o.order_id,
                    side: o.side,
                    old_price: o.price,
                    old_size: o.size,
                    new_price: e.price,
                    new_size: e.size,
                }
            }
            Action::Fill => {
                match self.find(e.order_id) {
                    None => self.inv.fills_unknown_order += 1,
                    Some(i) if e.size > self.orders[i].size => self.inv.fills_exceeding_resting += 1,
                    Some(_) => {}
                }
                RefEffect::None
            }
            Action::Trade | Action::None => RefEffect::None,
        }
    }

    /// End-of-event checks: crossed (`bid >= ask`) and negative-spread (`bid > ask`) counters.
    pub fn note_event_end(&mut self) {
        if let (Some(b), Some(a)) = (self.best_px(BookSide::Bid), self.best_px(BookSide::Ask)) {
            if b >= a {
                self.inv.crossed_books_after_last += 1;
            }
            if b > a {
                self.inv.negative_spreads += 1;
            }
        }
    }
}

/// Per-channel sequence tracker (SPEC 5.1.3, et): snapshot records reset their channel; otherwise a jump of more
/// than one is a gap and a decrease a regression.
#[derive(Clone, Debug, Default)]
pub struct SeqTracker {
    last: std::collections::BTreeMap<u16, Option<u64>>,
}

/// Outcome of one record for the sequence tracker.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SeqCheck {
    /// Contiguous, first on its channel, or a snapshot record.
    Ok,
    /// Jump of more than one.
    Gap,
    /// Decrease.
    Regression,
}

impl SeqTracker {
    /// Checks and records one record.
    pub fn check(&mut self, e: &MboEvent) -> SeqCheck {
        let slot = self.last.entry(e.channel_id).or_insert(None);
        if e.flags & flags::SNAPSHOT != 0 {
            *slot = None;
            return SeqCheck::Ok;
        }
        let out = match *slot {
            Some(prev) if e.sequence > prev.wrapping_add(1) => SeqCheck::Gap,
            Some(prev) if e.sequence < prev => SeqCheck::Regression,
            _ => SeqCheck::Ok,
        };
        *slot = Some(e.sequence);
        out
    }
}

/// Events after an unpaired `F` in which a `C`/`M` of its order counts as `removal_after_unpaired_fill`.
pub const UNPAIRED_FILL_WINDOW_EVENTS: u64 = 8;

/// Ingest-time data-quality counters of one partition (own computation, SPEC 5.1.3 / 5.1.5).
///
/// `source_specific.removal_after_unpaired_fill` (audit minor 24, only when non-zero): an `F` of a resting order is
/// paired by any `C` or `M` of the same id in its scope: the same event, or on venues with
/// `fill_removal_shares_sequence` until the first record with another `sequence`. An `F` still unpaired when its
/// scope closes, whose order gets a `C` or `M` at most `UNPAIRED_FILL_WINDOW_EVENTS` events after the `F`'s event,
/// counts once. `R` forgets every open and unpaired fill.
#[derive(Clone, Debug)]
pub struct DqTracker {
    rules: VenueRules,
    book: NaiveBook,
    seq: SeqTracker,
    dq: DataQuality,
    events: u64,
    in_event: bool,
    last_ts: Option<Ts>,
    first_is_snapshot: Option<bool>,
    open_fills: std::collections::BTreeMap<u64, u64>,
    open_seq: u64,
    unpaired_fills: std::collections::BTreeMap<u64, u64>,
    late_removals: u64,
}

impl DqTracker {
    /// New tracker for one partition.
    pub fn new(rules: VenueRules) -> Self {
        Self {
            rules,
            book: NaiveBook::new(rules),
            seq: SeqTracker::default(),
            dq: DataQuality::default(),
            events: 0,
            in_event: false,
            last_ts: None,
            first_is_snapshot: None,
            open_fills: std::collections::BTreeMap::new(),
            open_seq: 0,
            unpaired_fills: std::collections::BTreeMap::new(),
            late_removals: 0,
        }
    }

    /// Closes the pairing scope: its fills without a removal become unpaired.
    fn close_fill_scope(&mut self) {
        let open = std::mem::take(&mut self.open_fills);
        self.unpaired_fills.extend(open);
    }

    fn pair_fills(&mut self, e: &MboEvent) {
        if self.rules.fill_removal_shares_sequence && !self.open_fills.is_empty() && e.sequence != self.open_seq {
            self.close_fill_scope();
        }
        match Action::from_byte(e.action) {
            Some(Action::Clear) => {
                self.open_fills.clear();
                self.unpaired_fills.clear();
            }
            Some(Action::Fill) if self.book.order(e.order_id).is_some() => {
                self.open_fills.entry(e.order_id).or_insert(self.events);
                self.open_seq = e.sequence;
            }
            Some(Action::Cancel | Action::Modify) => {
                let now = self.events;
                if self.open_fills.remove(&e.order_id).is_none()
                    && self
                        .unpaired_fills
                        .remove(&e.order_id)
                        .is_some_and(|t| now - t <= UNPAIRED_FILL_WINDOW_EVENTS)
                {
                    self.late_removals += 1;
                }
            }
            _ => {}
        }
    }

    /// Feeds one record (partition feed order).
    pub fn push(&mut self, e: &MboEvent) {
        let dq = &mut self.dq;
        dq.records += 1;
        self.in_event = true;
        if self.first_is_snapshot.is_none() {
            self.first_is_snapshot = Some(e.flags & flags::SNAPSHOT != 0);
        }
        if e.flags & flags::MAYBE_BAD_BOOK != 0 {
            dq.maybe_bad_book_records += 1;
        }
        if e.flags & flags::BAD_TS_RECV != 0 {
            dq.bad_ts_recv_records += 1;
        }
        if e.flags & flags::SNAPSHOT != 0 {
            dq.snapshot_records += 1;
        }
        // Snapshot records carry the snapshot's own timestamps: no ordering check, no clock move.
        if e.flags & flags::SNAPSHOT == 0 {
            if self.last_ts.is_some_and(|t| e.ts_event < t) {
                dq.out_of_order_ts_event += 1;
            }
            self.last_ts = Some(e.ts_event);
        }
        if self.rules.sequence_meaningful {
            match self.seq.check(e) {
                SeqCheck::Gap => dq.sequence_gaps += 1,
                SeqCheck::Regression => dq.sequence_regressions += 1,
                SeqCheck::Ok => {}
            }
        }
        match Action::from_byte(e.action) {
            Some(Action::Trade) if e.side() == Side::None && self.rules.supports_hidden_orders => {
                dq.hidden_prints += 1;
            }
            Some(Action::Fill) => {
                if let Some(o) = self.book.order(e.order_id) {
                    if o.price != e.price {
                        dq.off_price_executions += 1;
                    }
                }
            }
            _ => {}
        }
        self.pair_fills(e);
        self.book.apply(e);
        if e.is_last() {
            self.end_event();
        }
    }

    fn end_event(&mut self) {
        if !self.rules.fill_removal_shares_sequence {
            self.close_fill_scope();
        }
        self.events += 1;
        self.in_event = false;
        self.book.note_event_end();
    }

    /// Final counters with the verdict of SPEC 5.1.5. Warnings = any anomaly counter non-zero: every counter except
    /// the volumes `records` and `snapshot_records` and the INFO counters `hidden_prints` and `off_price_executions`
    /// (normal on Nasdaq, E36(b): reported, never part of the verdict), and the `source_specific` INFO counters of
    /// `qt_ingest::dq::INFO_SOURCE_COUNTERS`.
    pub fn finish(mut self) -> (DataQuality, u64) {
        let mut unterminated = 0;
        if self.in_event {
            unterminated = 1;
            self.end_event();
        }
        let inv = self.book.invariants();
        let mut dq = self.dq;
        dq.unknown_order_refs = inv.unknown_order_refs + inv.fills_unknown_order;
        dq.duplicate_adds = inv.duplicate_adds;
        dq.unexpected_modifies = inv.unexpected_modifies;
        dq.crossed_books_after_last = inv.crossed_books_after_last;
        let mut ss = IndexMap::new();
        ss.insert("unterminated_events".to_owned(), unterminated);
        if self.rules.snapshot_begins_with_clear {
            let missing = u64::from(self.first_is_snapshot == Some(false));
            ss.insert("files_without_snapshot".to_owned(), missing);
        }
        if self.late_removals > 0 {
            ss.insert("removal_after_unpaired_fill".to_owned(), self.late_removals);
        }
        dq.source_specific = ss;
        finalize_verdict(&mut dq, self.events);
        (dq, self.events)
    }
}

/// Sets `verdict` and `reasons` from the counters (SPEC 5.1.5).
pub fn finalize_verdict(dq: &mut DataQuality, events: u64) {
    let mut reasons = Vec::new();
    let mut verdict = Verdict::Clean;
    if dq.maybe_bad_book_records > 0 {
        reasons.push(format!("maybe_bad_book_records = {}", dq.maybe_bad_book_records));
        verdict = Verdict::Unusable;
    }
    if events > 0 && dq.crossed_books_after_last as f64 / events as f64 > 1e-3 {
        reasons.push(format!(
            "crossed_books_after_last / events > 1e-3 ({})",
            dq.crossed_books_after_last
        ));
        verdict = Verdict::Unusable;
    }
    if dq.records > 0 && dq.unknown_order_refs as f64 / dq.records as f64 > 1e-3 {
        reasons.push(format!(
            "unknown_order_refs / records > 1e-3 ({})",
            dq.unknown_order_refs
        ));
        verdict = Verdict::Unusable;
    }
    let counters = [
        ("maybe_bad_book_records", dq.maybe_bad_book_records),
        ("bad_ts_recv_records", dq.bad_ts_recv_records),
        ("pre_snapshot_rows_skipped", dq.pre_snapshot_rows_skipped),
        ("unknown_order_refs", dq.unknown_order_refs),
        ("duplicate_adds", dq.duplicate_adds),
        ("unexpected_modifies", dq.unexpected_modifies),
        ("sequence_gaps", dq.sequence_gaps),
        ("sequence_regressions", dq.sequence_regressions),
        ("out_of_order_ts_event", dq.out_of_order_ts_event),
        ("crossed_books_after_last", dq.crossed_books_after_last),
    ];
    if verdict == Verdict::Clean {
        for (name, v) in counters {
            if v > 0 {
                reasons.push(format!("{name} = {v}"));
            }
        }
        for (k, v) in &dq.source_specific {
            // INFO source counters (`qt_ingest::dq::INFO_SOURCE_COUNTERS`) never raise the verdict.
            if *v > 0 && !qt_ingest::dq::is_info_source_counter(k) {
                reasons.push(format!("{k} = {v}"));
            }
        }
        if !dq.feed_gaps.is_empty() {
            reasons.push(format!("feed_gaps = {}", dq.feed_gaps.len()));
        }
        if !reasons.is_empty() {
            verdict = Verdict::Warnings;
        }
    }
    dq.verdict = verdict;
    dq.reasons = reasons;
}

/// Sums labelled partition counters into a dataset-level document; verdict = worst partition; reasons =
/// `"<label>: <reason>"` for every non-clean partition, in the given order.
pub fn merge_dq(parts: &[(String, DataQuality)]) -> DataQuality {
    let mut out = DataQuality::default();
    for (label, p) in parts {
        out.records += p.records;
        out.maybe_bad_book_records += p.maybe_bad_book_records;
        out.bad_ts_recv_records += p.bad_ts_recv_records;
        out.snapshot_records += p.snapshot_records;
        out.pre_snapshot_rows_skipped += p.pre_snapshot_rows_skipped;
        out.unknown_order_refs += p.unknown_order_refs;
        out.duplicate_adds += p.duplicate_adds;
        out.unexpected_modifies += p.unexpected_modifies;
        out.sequence_gaps += p.sequence_gaps;
        out.sequence_regressions += p.sequence_regressions;
        out.out_of_order_ts_event += p.out_of_order_ts_event;
        out.crossed_books_after_last += p.crossed_books_after_last;
        out.hidden_prints += p.hidden_prints;
        out.off_price_executions += p.off_price_executions;
        out.feed_gaps.extend(p.feed_gaps.iter().cloned());
        for (k, v) in &p.source_specific {
            *out.source_specific.entry(k.clone()).or_insert(0) += v;
        }
        out.verdict = out.verdict.max(p.verdict);
        for r in &p.reasons {
            out.reasons.push(format!("{label}: {r}"));
        }
    }
    out
}
