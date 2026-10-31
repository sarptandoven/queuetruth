//! Real-order lives from MBO (SPEC 5.2.9, port of et `fidelity/orders.py`): one life per `A` (or `M` of an unknown
//! id, which the book treats as an add) and per priority-losing `Replace`, which ends the old life `replaced` and
//! starts a new one with the same id. Queue state at add = the level before the add (the order joins the tail).
//! Fills come from `F` records; `n_reduces` counts reductions not paired with an `F` (pairing as for shadows: within
//! the event, or by `sequence` where fills and removals share it). A full removal ends the life `filled` iff it is
//! paired with an `F` of the order (audit item 17), else `cancelled`; `R` ends lives `cleared`; end of
//! input leaves them `open_at_end`. Rows are emitted when a life ends; lives ended together are emitted in start
//! order.

use qt_book::{Book, Effect, IdMap};
use qt_schema::rows::{RealOrderEnd, RealOrderRow};
use qt_types::{Action, BookSide, MboEvent, Px, QtResult, Qty, Ts, flags};

use crate::Sink;

struct Life {
    row: RealOrderRow,
    start: u64,
    pend_fill: Qty,
    pend_key: u64,
}

/// Lives of one instrument.
pub(crate) struct RealOrders {
    instrument_id: u32,
    session_date: i32,
    by_sequence: bool,
    lives: IdMap<u64, Life>,
    next_start: u64,
    events: u64,
}

impl RealOrders {
    pub(crate) fn new(instrument_id: u32, session_date: i32, by_sequence: bool) -> Self {
        Self {
            instrument_id,
            session_date,
            by_sequence,
            lives: IdMap::default(),
            next_start: 0,
            events: 0,
        }
    }

    fn key(&self, epoch: u64) -> u64 {
        if self.by_sequence { epoch } else { self.events }
    }

    fn start(&mut self, e: &MboEvent, side: BookSide, price: Px, size: Qty, book: &Book) {
        let lvl = book.level(side, price);
        let (total, count) = lvl.map_or((size, 1), |l| (l.total, l.count));
        let before = total.saturating_sub(size);
        let row = RealOrderRow {
            instrument_id: self.instrument_id,
            session_date: self.session_date,
            order_id: e.order_id,
            side,
            price,
            size_at_add: size,
            ts_add: e.ts_event,
            sequence_add: e.sequence,
            queue_ahead_at_add: before,
            rank_at_add: count.saturating_sub(1),
            level_qty_at_add: before,
            ts_first_fill: None,
            ts_last_fill: None,
            filled_qty: 0,
            n_fills: 0,
            n_reduces: 0,
            ts_end: None,
            end_reason: RealOrderEnd::OpenAtEnd,
            in_snapshot: e.flags & flags::SNAPSHOT != 0,
        };
        self.lives.insert(
            e.order_id,
            Life {
                row,
                start: self.next_start,
                pend_fill: 0,
                pend_key: 0,
            },
        );
        self.next_start += 1;
    }

    fn end(&mut self, order_id: u64, reason: RealOrderEnd, ts: Ts, sink: &mut dyn Sink) -> QtResult<()> {
        if let Some(mut l) = self.lives.remove(&order_id) {
            l.row.end_reason = reason;
            l.row.ts_end = Some(ts);
            sink.real_order(&l.row)?;
        }
        Ok(())
    }

    /// Unpaired part of a removal of `by` (consumes pending fill quantity); `None` for an unknown life.
    fn pair(&mut self, order_id: u64, by: Qty, key: u64) -> Option<(Qty, bool)> {
        let l = self.lives.get_mut(&order_id)?;
        if l.pend_key != key {
            l.pend_fill = 0;
            l.pend_key = key;
        }
        let paired = by.min(l.pend_fill);
        let had_fill = l.pend_fill > 0;
        l.pend_fill -= paired;
        Some((by - paired, had_fill))
    }

    /// One record after the book applied it; `dup` is the resting order a duplicate add replaced.
    pub(crate) fn on_record(
        &mut self,
        e: &MboEvent,
        effect: &Effect,
        dup: bool,
        book: &Book,
        epoch: u64,
        sink: &mut dyn Sink,
    ) -> QtResult<()> {
        let key = self.key(epoch);
        if dup {
            self.end(e.order_id, RealOrderEnd::Replaced, e.ts_event, sink)?;
        }
        match *effect {
            Effect::Add { side, price, size, .. } => self.start(e, side, price, size, book),
            Effect::Reduce { order_id, by, .. } => {
                if let Some((unpaired, _)) = self.pair(order_id, by, key)
                    && unpaired > 0
                    && let Some(l) = self.lives.get_mut(&order_id)
                {
                    l.row.n_reduces += 1;
                }
            }
            Effect::Remove { order_id, size, .. } => {
                if let Some((_, had_fill)) = self.pair(order_id, size, key) {
                    let reason = if had_fill {
                        RealOrderEnd::Filled
                    } else {
                        RealOrderEnd::Cancelled
                    };
                    self.end(order_id, reason, e.ts_event, sink)?;
                }
            }
            Effect::Replace {
                order_id,
                side,
                new_price,
                new_size,
                ..
            } => {
                self.end(order_id, RealOrderEnd::Replaced, e.ts_event, sink)?;
                self.start(e, side, new_price, new_size, book);
            }
            Effect::Clear => self.end_all(RealOrderEnd::Cleared, Some(e.ts_event), sink)?,
            Effect::None => {}
        }
        if e.action == Action::Fill.as_byte()
            && let Some(l) = self.lives.get_mut(&e.order_id)
        {
            if l.pend_key != key {
                l.pend_fill = 0;
                l.pend_key = key;
            }
            l.pend_fill = l.pend_fill.saturating_add(e.size);
            l.row.filled_qty = l.row.filled_qty.saturating_add(e.size);
            l.row.n_fills += 1;
            l.row.ts_first_fill.get_or_insert(e.ts_event);
            l.row.ts_last_fill = Some(e.ts_event);
        }
        Ok(())
    }

    /// Event end (pairing scope for venues that pair within the event).
    pub(crate) fn on_event_end(&mut self) {
        self.events += 1;
    }

    fn end_all(&mut self, reason: RealOrderEnd, ts: Option<Ts>, sink: &mut dyn Sink) -> QtResult<()> {
        let mut all: Vec<Life> = self.lives.drain().map(|(_, l)| l).collect();
        all.sort_by_key(|l| l.start);
        for mut l in all {
            l.row.end_reason = reason;
            l.row.ts_end = ts;
            sink.real_order(&l.row)?;
        }
        Ok(())
    }

    /// End of input: open lives in start order.
    pub(crate) fn finish(&mut self, sink: &mut dyn Sink) -> QtResult<()> {
        self.end_all(RealOrderEnd::OpenAtEnd, None, sink)
    }
}
