//! Kraken spot WebSocket v2 `level3` capture (`wss://ws-l3.kraken.com/v2`, authenticated) -> MBO records, rule set
//! `crypto_l3`, as a **depth-window** source: exact only inside the subscribed depth.
//! Schemas: <https://docs.kraken.com/api/docs/websocket-v2/level3> and the checksum guide
//! <https://docs.kraken.com/exchange/guides/websockets/l3-checksum-v2> (both accessed 2026-10-19); amend priority
//! rules: <https://docs.kraken.com/exchange/guides/general/amends> (quantity down keeps the queue position, quantity
//! up or a price change goes to the back: KeepOnSizeDecrease).
//!
//! Capture lines: `{"recv_ns": N, "msg": <frame>}` or the bare frame. Frames used: subscribe acknowledgements
//! (`{"method": "subscribe", "result": {"channel": "level3", "symbol", "depth"}}`, for the depth),
//! `{"channel": "level3", "type": "snapshot"|"update", "data": [{"symbol", "bids", "asks", "checksum",
//! "timestamp"}]}`; anything else is ignored. Numbers keep their JSON text, so prices and quantities are exact.
//!
//! Window model (per symbol and side; depth D = `Config::depth`, else the acknowledged depth, else the documented
//! default 10 with `depth_assumed`): the feed shows only the best D price levels and sends no delete when a level
//! falls out of scope; when a level leaves the scope "all orders in the next available level will generate an add
//! event". The converter keeps every order it has seen with an in-view / out-of-view state:
//! - the in-view book is truncated to D levels at the event that pushes a level out (an add or modify inside the
//!   best D) and again after each message; orders of the dropped levels become **out-of-view** (`window_exits`): no
//!   record is written (they are not cancels), they stay in the replayed book frozen at their last known state;
//! - an add repeating an unchanged in-view order means its level left and re-entered the window inside one message
//!   and is re-sent whole, so it is reconciled like a re-entry (`add_repeated`);
//! - a level that comes back into view arrives as adds; those adds are reconciled with the out-of-view orders kept
//!   at that price: unchanged orders give no record (`window_reentries`), a smaller quantity gives `M` (keeps the
//!   queue position), orders the level no longer holds were removed while out of view and get `C` at the time this
//!   is learnt (`out_of_view_vanished`), orders never seen get `A` appended. When the venue's queue order cannot be
//!   reached by those records (an unseen order ahead of a kept one, a kept order grown) the kept orders from that
//!   point are re-queued with `C` + `A` (`reentry_requeued`). Every record produced by the window logic carries
//!   `PUBLISHER_SPECIFIC`;
//! - an out-of-view order whose price lies inside the current window (or on a side with fewer than D levels) can no
//!   longer be on the book (the venue would have re-sent it), so it gets `C` (`out_of_view_vanished`).
//!
//! Events: `add` -> `A`; `modify` -> `M` (absolute `order_qty` / `limit_price`); `delete` -> `C` of the tracked
//! quantity. The feed has no executions: `modify` is a fill per the docs (or an amend) and `delete` is "full fill or
//! cancel", so fills cannot be attributed (`no_execution_records` = 1, `deletes_fill_or_cancel`). The venue
//! excludes in-flight orders, unmatched market orders, untriggered stops and iceberg hidden quantity.
//! Integrity: there are no sequence numbers ("no sequencing is required"); the CRC32 `checksum` over the top 10
//! levels (asks low to high then bids high to low, order by order, `.` and leading zeros removed) is verified after
//! every message against the in-view book (`checksums_verified`, `checksum_mismatches`). Quantities sent as JSON
//! floats lose trailing zeros, so `Config::precision` (symbol -> [price decimals, qty decimals]) formats them as
//! the venue does; without it the received text is used. A mismatch opens a feed gap until the next matching
//! checksum or snapshot. `sequence` = per-symbol message count; `ts_event` = message `timestamp`.

use std::collections::{BTreeMap, HashMap, HashSet};

use indexmap::IndexMap;
use qt_schema::json::FeedGap;
use qt_schema::rows::TradeRow;
use qt_types::{MboEvent, QtError, QtResult, flags};

use crate::jsonx::J;
use crate::l3util::{self, rec};
use crate::{Counters, L3Output, units};

/// Depths the venue accepts.
pub const DEPTHS: [u32; 3] = [10, 100, 1000];
/// Documented default depth.
pub const DEFAULT_DEPTH: u32 = 10;
/// Levels covered by the checksum.
pub const CHECKSUM_LEVELS: usize = 10;
/// Flag on every record written by the window logic.
pub const WINDOW_FLAG: u8 = flags::PUBLISHER_SPECIFIC;

/// Converter options.
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    /// Symbol (e.g. `BTC/USD`) -> instrument id; empty = every symbol seen, ids 1.. in sorted order.
    pub instruments: IndexMap<String, u32>,
    /// Price exponent (default -9).
    pub price_exp: i8,
    /// Size exponent (default -8).
    pub size_exp: i8,
    /// Subscribed depth for every symbol (overrides the acknowledgements).
    pub depth: Option<u32>,
    /// Symbol -> (price decimals, quantity decimals) for the checksum text.
    pub precision: HashMap<String, (u32, u32)>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            instruments: IndexMap::new(),
            price_exp: -9,
            size_exp: -8,
            depth: None,
            precision: HashMap::new(),
        }
    }
}

/// Counter names reported even when zero.
pub const ANOMALIES: [&str; 9] = [
    "checksum_mismatches",
    "add_duplicate",
    "modify_unknown",
    "delete_unknown",
    "out_of_view_vanished",
    "reentry_requeued",
    "out_of_view_events",
    "depth_assumed",
    "updates_before_snapshot",
];

/// Window bookkeeping counters (informational; reported even when zero).
pub const WINDOW_COUNTERS: [&str; 7] = [
    "window_exits",
    "window_exit_levels",
    "window_reentries",
    "window_reentries_changed",
    "window_entries",
    "checksums_verified",
    "checksums_missing",
];

/// Caveat / anomaly counters copied (when non-zero) into `qt.data_quality.source_specific` at ingest.
/// `window_truncated_depth` (the depth, max over symbols) is always non-zero: a Kraken L3 dataset is window truth.
pub const DQ_COUNTERS: [&str; 11] = [
    "window_truncated_depth",
    "no_execution_records",
    "checksum_mismatches",
    "add_duplicate",
    "modify_unknown",
    "delete_unknown",
    "out_of_view_vanished",
    "reentry_requeued",
    "out_of_view_events",
    "depth_assumed",
    "updates_before_snapshot",
];

fn fmt_err(line: u64, msg: impl Into<String>) -> QtError {
    QtError::InputFormat {
        location: format!("kraken_l3 line {line}"),
        msg: msg.into(),
    }
}

/// Converts capture lines to MBO records, source trades (none) and counters.
pub fn convert(
    lines: impl Iterator<Item = QtResult<String>>,
    cfg: &Config,
) -> QtResult<(Vec<MboEvent>, Vec<TradeRow>, Counters)> {
    let o = convert_full(lines, cfg)?;
    Ok((o.events, o.trades, o.counters))
}

/// Checksum text of a decimal: `.` removed, leading zeros removed.
pub fn checksum_text(s: &str) -> String {
    let t: String = s.chars().filter(|c| *c != '.').collect();
    let t = t.trim_start_matches('0');
    t.to_string()
}

/// Fixed-point `v` at `10^exp` as the venue's checksum text with `dp` decimals (`None` when not representable).
pub fn fixed_text(v: i128, exp: i8, dp: u32) -> Option<String> {
    let shift = i32::from(exp) + i32::try_from(dp).ok()?;
    let scaled = if shift >= 0 {
        v.checked_mul(10i128.checked_pow(shift.unsigned_abs())?)?
    } else {
        let d = 10i128.checked_pow(shift.unsigned_abs())?;
        if v % d != 0 {
            return None;
        }
        v / d
    };
    if scaled < 0 {
        return None;
    }
    Some(if scaled == 0 { String::new() } else { scaled.to_string() })
}

/// One tracked order.
#[derive(Clone, Debug)]
struct Ord {
    side: u8,
    px: i64,
    qty: u64,
    px_txt: String,
    qty_txt: String,
    in_view: bool,
    num: u64,
}

/// Book state of one symbol.
#[derive(Default)]
struct Sym {
    inst: u32,
    depth: u32,
    seeded: bool,
    msgs: u64,
    orders: HashMap<String, Ord>,
    /// (side, price) -> FIFO of order ids (in-view and out-of-view), mirroring the replayed queue.
    levels: [BTreeMap<i64, Vec<String>>; 2],
    /// Feed gap opened by a checksum mismatch.
    bad_since: Option<i64>,
}

fn si(side: u8) -> usize {
    usize::from(side != b'B')
}

impl Sym {
    fn remove_from_level(&mut self, side: u8, px: i64, id: &str) {
        let lv = &mut self.levels[si(side)];
        if let Some(q) = lv.get_mut(&px) {
            q.retain(|x| x != id);
            if q.is_empty() {
                lv.remove(&px);
            }
        }
    }

    fn push_back(&mut self, side: u8, px: i64, id: &str) {
        self.levels[si(side)].entry(px).or_default().push(id.to_string());
    }

    /// In-view price levels of a side, best first.
    fn in_view_levels(&self, side: u8) -> Vec<i64> {
        let lv = &self.levels[si(side)];
        let has_iv = |q: &Vec<String>| q.iter().any(|id| self.orders.get(id).is_some_and(|o| o.in_view));
        let it: Box<dyn Iterator<Item = (&i64, &Vec<String>)>> = if side == b'B' {
            Box::new(lv.iter().rev())
        } else {
            Box::new(lv.iter())
        };
        it.filter(|(_, q)| has_iv(q)).map(|(p, _)| *p).collect()
    }
}

/// Per-message context for records.
struct Ctx {
    ts: i64,
    recv: i64,
    seq: u64,
}

struct Conv<'a> {
    cfg: &'a Config,
    st: Counters,
    out: Vec<MboEvent>,
    ids: HashMap<String, u64>,
    feed_gaps: Vec<FeedGap>,
}

/// A buffered re-entry add: (order id, price, quantity, price text, quantity text).
type Reentry = (String, i64, u64, String, String);

/// One order event of an update.
struct OrdEv {
    event: String,
    id: String,
    px_txt: String,
    qty_txt: String,
}

impl Conv<'_> {
    fn bump(&mut self, k: &str) {
        *self.st.entry(k.to_string()).or_insert(0) += 1;
    }

    fn num(&mut self, id: &str) -> u64 {
        let n = self.ids.len() as u64 + 1;
        *self.ids.entry(id.to_string()).or_insert(n)
    }

    #[allow(clippy::too_many_arguments)]
    fn emit(&mut self, s: &Sym, c: &Ctx, action: u8, side: u8, px: i64, qty: u64, num: u64, fl: u8) {
        self.out
            .push(rec(c.ts, c.recv, s.inst, action, side, px, qty, num, fl, c.seq));
    }

    fn parse_orders(&self, list: Option<&J>, no: u64, need_event: bool) -> QtResult<Vec<OrdEv>> {
        let mut v = Vec::new();
        for o in list.and_then(J::as_arr).unwrap_or(&[]) {
            let get = |k: &str| o.get(k).and_then(J::num_text).map(str::to_string);
            let (Some(id), Some(px_txt), Some(qty_txt)) = (
                o.get("order_id").and_then(J::as_str).map(str::to_string),
                get("limit_price"),
                get("order_qty"),
            ) else {
                return Err(fmt_err(no, "order without order_id / limit_price / order_qty"));
            };
            let event = match o.get("event").and_then(J::as_str) {
                Some(e) => e.to_string(),
                None if !need_event => "add".into(),
                None => return Err(fmt_err(no, "update order without event")),
            };
            v.push(OrdEv {
                event,
                id,
                px_txt,
                qty_txt,
            });
        }
        Ok(v)
    }

    fn snapshot(&mut self, s: &mut Sym, item: &J, c: &Ctx, no: u64) -> QtResult<()> {
        if s.seeded {
            self.bump("resnapshots");
        }
        self.bump("snapshots");
        // a snapshot replaces the view (and anything kept out of view)
        s.orders.clear();
        s.levels = Default::default();
        s.bad_since = None;
        let start = self.out.len();
        self.emit(s, c, b'R', b'N', 0, 0, 0, flags::SNAPSHOT);
        for (key, side) in [("bids", b'B'), ("asks", b'A')] {
            for o in self.parse_orders(item.get(key), no, false)? {
                let px = units::price(&o.px_txt, self.cfg.price_exp)?;
                let qty = units::size(&o.qty_txt, self.cfg.size_exp)?;
                if qty == 0 {
                    self.bump("snapshot_zero_size_rows");
                    continue;
                }
                if s.orders.contains_key(&o.id) {
                    self.bump("add_duplicate");
                    continue;
                }
                let num = self.num(&o.id);
                s.orders.insert(
                    o.id.clone(),
                    Ord {
                        side,
                        px,
                        qty,
                        px_txt: o.px_txt,
                        qty_txt: o.qty_txt,
                        in_view: true,
                        num,
                    },
                );
                s.push_back(side, px, &o.id);
                self.emit(s, c, b'A', side, px, qty, num, flags::SNAPSHOT);
            }
        }
        l3util::mark_last(&mut self.out, start);
        s.seeded = true;
        Ok(())
    }

    fn update(&mut self, s: &mut Sym, item: &J, c: &Ctx, no: u64) -> QtResult<()> {
        let start = self.out.len();
        for (key, side) in [("bids", b'B'), ("asks", b'A')] {
            let evs = self.parse_orders(item.get(key), no, true)?;
            // adds at levels holding only out-of-view orders are re-entries: reconciled per level
            let mut reentry: BTreeMap<i64, Vec<Reentry>> = BTreeMap::new();
            let mut reentry_order: Vec<i64> = Vec::new();
            for e in evs {
                let px = units::price(&e.px_txt, self.cfg.price_exp)?;
                let qty = units::size(&e.qty_txt, self.cfg.size_exp)?;
                // an event touching a pending re-entry order flushes that level first
                if let Some(lp) = reentry
                    .iter()
                    .find(|(_, v)| v.iter().any(|x| x.0 == e.id))
                    .map(|(p, _)| *p)
                {
                    let v = reentry.remove(&lp).unwrap_or_default();
                    reentry_order.retain(|p| *p != lp);
                    self.reconcile(s, side, lp, v, c);
                }
                match e.event.as_str() {
                    "add" => {
                        self.bump("adds");
                        // an add repeating an unchanged in-view order means its level left and re-entered the
                        // window inside this message and is re-sent whole: orders removed meanwhile are not in the
                        // re-send, so the level's orders are treated as out of view and reconciled like a re-entry
                        let repeat = s
                            .orders
                            .get(&e.id)
                            .is_some_and(|o| o.in_view && o.side == side && o.px == px && o.qty == qty);
                        if repeat && !reentry.contains_key(&px) {
                            self.bump("add_repeated");
                            let ids = s.levels[si(side)].get(&px).cloned().unwrap_or_default();
                            for id in ids {
                                if let Some(o) = s.orders.get_mut(&id) {
                                    o.in_view = false;
                                }
                            }
                        }
                        let level_out_only = s.levels[si(side)]
                            .get(&px)
                            .is_some_and(|q| q.iter().all(|id| s.orders.get(id).is_some_and(|o| !o.in_view)));
                        let known_in_view = s.orders.get(&e.id).is_some_and(|o| o.in_view);
                        if reentry.contains_key(&px) || (level_out_only && !known_in_view) {
                            if !reentry.contains_key(&px) {
                                reentry_order.push(px);
                            }
                            reentry
                                .entry(px)
                                .or_default()
                                .push((e.id, px, qty, e.px_txt, e.qty_txt));
                            continue;
                        }
                        match s.orders.get(&e.id).cloned() {
                            Some(o) => {
                                if o.in_view {
                                    self.bump("add_duplicate");
                                } else {
                                    self.bump("window_reentries_changed");
                                }
                                self.modify_to(s, &e.id, o, px, qty, e.px_txt, e.qty_txt, c, WINDOW_FLAG);
                            }
                            None => {
                                let num = self.num(&e.id);
                                s.orders.insert(
                                    e.id.clone(),
                                    Ord {
                                        side,
                                        px,
                                        qty,
                                        px_txt: e.px_txt,
                                        qty_txt: e.qty_txt,
                                        in_view: true,
                                        num,
                                    },
                                );
                                s.push_back(side, px, &e.id);
                                self.emit(s, c, b'A', side, px, qty, num, 0);
                            }
                        }
                        self.push_out(s, side, px);
                    }
                    "modify" => {
                        self.bump("modifies");
                        match s.orders.get(&e.id).cloned() {
                            Some(o) => {
                                if !o.in_view {
                                    self.bump("out_of_view_events");
                                }
                                self.modify_to(s, &e.id, o, px, qty, e.px_txt, e.qty_txt, c, 0);
                                self.push_out(s, side, px);
                            }
                            None => self.bump("modify_unknown"),
                        }
                    }
                    "delete" => {
                        self.bump("deletes");
                        match s.orders.remove(&e.id) {
                            Some(o) => {
                                if !o.in_view {
                                    self.bump("out_of_view_events");
                                }
                                self.bump("deletes_fill_or_cancel");
                                s.remove_from_level(o.side, o.px, &e.id);
                                self.emit(s, c, b'C', o.side, o.px, o.qty, o.num, 0);
                            }
                            None => self.bump("delete_unknown"),
                        }
                    }
                    _ => self.bump("unknown_events"),
                }
            }
            for px in reentry_order {
                if let Some(v) = reentry.remove(&px) {
                    self.reconcile(s, side, px, v, c);
                }
            }
        }
        for side in *b"BA" {
            self.truncate_and_vanish(s, side, c);
        }
        l3util::mark_last(&mut self.out, start);
        Ok(())
    }

    /// Moves a known order to (px, qty) with an `M` (no record when nothing changed).
    #[allow(clippy::too_many_arguments)]
    fn modify_to(
        &mut self,
        s: &mut Sym,
        id: &str,
        o: Ord,
        px: i64,
        qty: u64,
        px_txt: String,
        qty_txt: String,
        c: &Ctx,
        fl: u8,
    ) {
        let keeps = o.px == px && qty <= o.qty;
        if !keeps {
            s.remove_from_level(o.side, o.px, id);
            s.push_back(o.side, px, id);
        }
        let changed = o.px != px || o.qty != qty;
        if let Some(x) = s.orders.get_mut(id) {
            (x.px, x.qty, x.px_txt, x.qty_txt, x.in_view) = (px, qty, px_txt, qty_txt, true);
        }
        if changed {
            self.emit(s, c, b'M', o.side, px, qty, o.num, fl);
        }
    }

    /// Reconciles the adds `l` (venue queue order) of a level re-entering the view with the out-of-view orders kept
    /// at that price.
    fn reconcile(&mut self, s: &mut Sym, side: u8, px: i64, l: Vec<Reentry>, c: &Ctx) {
        let kept: Vec<String> = s.levels[si(side)]
            .get(&px)
            .map(|q| {
                q.iter()
                    .filter(|id| s.orders.get(*id).is_some_and(|o| !o.in_view))
                    .cloned()
                    .collect()
            })
            .unwrap_or_default();
        let lset: HashSet<&str> = l.iter().map(|x| x.0.as_str()).collect();
        // kept orders the level no longer holds were removed while out of view
        for id in kept.iter().filter(|id| !lset.contains(id.as_str())) {
            if let Some(o) = s.orders.remove(id) {
                self.bump("out_of_view_vanished");
                s.remove_from_level(o.side, o.px, id);
                self.emit(s, c, b'C', o.side, o.px, o.qty, o.num, WINDOW_FLAG);
            }
        }
        let kept: Vec<String> = kept.into_iter().filter(|id| lset.contains(id.as_str())).collect();
        let mut k = 0usize;
        let mut conflict = false;
        for (id, px, qty, px_txt, qty_txt) in l {
            if !conflict {
                let fits =
                    k < kept.len() && kept[k] == id && s.orders.get(&id).is_some_and(|o| o.px == px && qty <= o.qty);
                if fits {
                    k += 1;
                    let o = s.orders.get(&id).cloned();
                    if let Some(o) = o {
                        if o.qty == qty {
                            self.bump("window_reentries");
                            if let Some(x) = s.orders.get_mut(&id) {
                                (x.px_txt, x.qty_txt, x.in_view) = (px_txt, qty_txt, true);
                            }
                        } else {
                            self.bump("window_reentries_changed");
                            self.modify_to(s, &id, o, px, qty, px_txt, qty_txt, c, WINDOW_FLAG);
                        }
                    }
                    continue;
                }
                if k < kept.len() {
                    // the venue's order cannot be reached by appending: re-queue the remaining kept orders
                    conflict = true;
                    for rid in &kept[k..] {
                        if let Some(o) = s.orders.remove(rid) {
                            self.bump("reentry_requeued");
                            s.remove_from_level(o.side, o.px, rid);
                            self.emit(s, c, b'C', o.side, o.px, o.qty, o.num, WINDOW_FLAG);
                        }
                    }
                }
            }
            match s.orders.get(&id).cloned() {
                Some(o) => {
                    // known at another price (amended while out of view) or in view elsewhere
                    self.bump("window_reentries_changed");
                    self.modify_to(s, &id, o, px, qty, px_txt, qty_txt, c, WINDOW_FLAG);
                    // modify_to keeps the position on a same-price decrease; a re-entry is appended
                    s.remove_from_level(side, px, &id);
                    s.push_back(side, px, &id);
                }
                None => {
                    if conflict {
                        self.bump("window_requeue_adds");
                    } else {
                        self.bump("window_entries");
                    }
                    let num = self.num(&id);
                    s.orders.insert(
                        id.clone(),
                        Ord {
                            side,
                            px,
                            qty,
                            px_txt,
                            qty_txt,
                            in_view: true,
                            num,
                        },
                    );
                    s.push_back(side, px, &id);
                    self.emit(s, c, b'A', side, px, qty, num, WINDOW_FLAG);
                }
            }
        }
    }

    /// Marks the orders of the levels `ps` out of view (they left the window; the venue sends no delete).
    fn exit_levels(&mut self, s: &mut Sym, side: u8, ps: &[i64]) {
        for p in ps {
            self.bump("window_exit_levels");
            let ids = s.levels[si(side)].get(p).cloned().unwrap_or_default();
            for id in ids {
                if let Some(o) = s.orders.get_mut(&id) {
                    if o.in_view {
                        o.in_view = false;
                        *self.st.entry("window_exits".into()).or_insert(0) += 1;
                    }
                }
            }
        }
    }

    /// The window right after an event put an order at `px`: when more than D levels are in view and `px` is among
    /// the best D, the levels beyond D left the window at this event, not at the end of the message. A level pushed
    /// out and emptied later in the same message is then caught by the out-of-view rules (re-entry reconciliation,
    /// or `out_of_view_vanished` once the window reaches past it) instead of staying in view. A `px` beyond the best
    /// D (an add the venue ordered before the event that makes room) is left to the end-of-message truncation.
    fn push_out(&mut self, s: &mut Sym, side: u8, px: i64) {
        let depth = s.depth as usize;
        let lv = s.in_view_levels(side);
        if lv.len() > depth && lv[..depth].contains(&px) {
            let out = lv[depth..].to_vec();
            self.exit_levels(s, side, &out);
        }
    }

    fn truncate_and_vanish(&mut self, s: &mut Sym, side: u8, c: &Ctx) {
        let depth = s.depth as usize;
        let lv = s.in_view_levels(side);
        if lv.len() > depth {
            let out = lv[depth..].to_vec();
            self.exit_levels(s, side, &out);
        }
        let lv = s.in_view_levels(side);
        let worst = (lv.len() >= depth).then(|| lv.last().copied()).flatten();
        let inside = |p: i64| match worst {
            None => true,
            Some(w) => {
                if side == b'B' {
                    p >= w
                } else {
                    p <= w
                }
            }
        };
        let gone: Vec<String> = s.levels[si(side)]
            .iter()
            .filter(|(p, _)| inside(**p))
            .flat_map(|(_, q)| q.iter())
            .filter(|id| s.orders.get(*id).is_some_and(|o| !o.in_view))
            .cloned()
            .collect();
        for id in gone {
            if let Some(o) = s.orders.remove(&id) {
                self.bump("out_of_view_vanished");
                s.remove_from_level(o.side, o.px, &id);
                self.emit(s, c, b'C', o.side, o.px, o.qty, o.num, WINDOW_FLAG);
            }
        }
    }

    /// Checksum text of the in-view top 10 levels (asks low to high, then bids high to low).
    fn checksum_input(&self, s: &Sym, symbol: &str) -> String {
        let prec = self.cfg.precision.get(symbol).copied();
        let mut text = String::new();
        for side in *b"AB" {
            for p in s.in_view_levels(side).into_iter().take(CHECKSUM_LEVELS) {
                for id in s.levels[si(side)].get(&p).map(Vec::as_slice).unwrap_or(&[]) {
                    let Some(o) = s.orders.get(id).filter(|o| o.in_view) else {
                        continue;
                    };
                    let (pt, qt) = match prec {
                        Some((pd, qd)) => (
                            fixed_text(i128::from(o.px), self.cfg.price_exp, pd)
                                .unwrap_or_else(|| checksum_text(&o.px_txt)),
                            fixed_text(i128::from(o.qty), self.cfg.size_exp, qd)
                                .unwrap_or_else(|| checksum_text(&o.qty_txt)),
                        ),
                        None => (checksum_text(&o.px_txt), checksum_text(&o.qty_txt)),
                    };
                    text.push_str(&pt);
                    text.push_str(&qt);
                }
            }
        }
        text
    }

    fn verify(&mut self, s: &mut Sym, symbol: &str, item: &J, ts: i64) {
        let Some(want) = item.get("checksum").and_then(J::as_u64) else {
            self.bump("checksums_missing");
            return;
        };
        let got = u64::from(l3util::crc32(self.checksum_input(s, symbol).as_bytes()));
        if got == want {
            self.bump("checksums_verified");
            if let Some(a) = s.bad_since.take() {
                self.feed_gaps.push(FeedGap {
                    instrument_id: s.inst,
                    ts_start: a.min(ts),
                    ts_end: a.max(ts),
                    reason: "kraken checksum mismatch: in-view book differs from the venue's".into(),
                });
            }
        } else {
            self.bump("checksum_mismatches");
            s.bad_since.get_or_insert(ts);
        }
    }
}

/// Full conversion (records, trades, counters, resolved instruments, ingest-level quality numbers).
pub fn convert_full(lines: impl Iterator<Item = QtResult<String>>, cfg: &Config) -> QtResult<L3Output> {
    if let Some(d) = cfg.depth {
        if !DEPTHS.contains(&d) {
            return Err(QtError::Config(format!(
                "kraken_l3 depth: must be one of 10, 100, 1000 (got {d})"
            )));
        }
    }
    let mut frames = Vec::new();
    let mut acked: HashMap<String, u32> = HashMap::new();
    let mut ignored = 0u64;
    for (k, line) in lines.enumerate() {
        let no = k as u64 + 1;
        let t = line?;
        if t.trim().is_empty() {
            continue;
        }
        let f = l3util::unwrap_line(&t, no, fmt_err)?;
        if f.msg.get("method").and_then(J::as_str) == Some("subscribe") {
            if let Some(r) = f
                .msg
                .get("result")
                .filter(|r| r.get("channel").and_then(J::as_str) == Some("level3"))
            {
                if let (Some(sym), Some(d)) = (r.get("symbol").and_then(J::as_str), r.get("depth").and_then(J::as_u64))
                {
                    let d = u32::try_from(d)
                        .ok()
                        .filter(|d| DEPTHS.contains(d))
                        .ok_or_else(|| fmt_err(no, format!("subscribe depth {d} is not 10, 100 or 1000")))?;
                    acked.insert(sym.to_string(), d);
                }
            }
            continue;
        }
        let ty = f.msg.get("type").and_then(J::as_str);
        if f.msg.get("channel").and_then(J::as_str) != Some("level3") || !matches!(ty, Some("snapshot" | "update")) {
            ignored += 1;
            continue;
        }
        frames.push(f);
    }
    let instruments: IndexMap<String, u32> = if cfg.instruments.is_empty() {
        let mut syms: Vec<String> = frames
            .iter()
            .flat_map(|f| f.msg.get("data").and_then(J::as_arr).unwrap_or(&[]).iter())
            .filter_map(|d| d.get("symbol").and_then(J::as_str).map(str::to_string))
            .collect();
        syms.sort();
        syms.dedup();
        syms.into_iter().enumerate().map(|(i, s)| (s, i as u32 + 1)).collect()
    } else {
        cfg.instruments.clone()
    };

    let mut conv = Conv {
        cfg,
        st: Counters::new(),
        out: Vec::new(),
        ids: HashMap::new(),
        feed_gaps: Vec::new(),
    };
    conv.st.insert("ignored".into(), ignored);
    let mut syms: HashMap<String, Sym> = HashMap::new();
    let mut last_ts: HashMap<String, i64> = HashMap::new();
    let mut max_depth = 0u32;
    for f in &frames {
        let snapshot = f.msg.get("type").and_then(J::as_str) == Some("snapshot");
        for item in f.msg.get("data").and_then(J::as_arr).unwrap_or(&[]) {
            let Some(symbol) = item.get("symbol").and_then(J::as_str) else {
                return Err(fmt_err(f.no, "level3 data item without symbol"));
            };
            let Some(&inst) = instruments.get(symbol) else {
                conv.bump("ignored");
                continue;
            };
            let ts = match item.get("timestamp").and_then(J::as_str) {
                Some(t) => units::iso_ns(t).map_err(|_| fmt_err(f.no, "bad timestamp"))?,
                None => f
                    .recv
                    .ok_or_else(|| fmt_err(f.no, "level3 data item without timestamp"))?,
            };
            let s = syms.entry(symbol.to_string()).or_insert_with(|| {
                let depth = match (cfg.depth, acked.get(symbol)) {
                    (Some(d), _) | (None, Some(&d)) => d,
                    (None, None) => {
                        conv.bump("depth_assumed");
                        DEFAULT_DEPTH
                    }
                };
                Sym {
                    inst,
                    depth,
                    ..Sym::default()
                }
            });
            max_depth = max_depth.max(s.depth);
            if !snapshot && !s.seeded {
                conv.bump("updates_before_snapshot");
                continue;
            }
            s.msgs += 1;
            let c = Ctx {
                ts,
                recv: f.recv.unwrap_or(ts),
                seq: s.msgs,
            };
            if snapshot {
                conv.snapshot(s, item, &c, f.no)?;
                // a snapshot deeper than the window is truncated at once
                for side in *b"BA" {
                    conv.truncate_and_vanish(s, side, &c);
                }
            } else {
                conv.update(s, item, &c, f.no)?;
            }
            conv.verify(s, symbol, item, ts);
            last_ts.insert(symbol.to_string(), ts);
        }
    }
    for (symbol, s) in &mut syms {
        if let (Some(a), Some(&b)) = (s.bad_since.take(), last_ts.get(symbol)) {
            conv.feed_gaps.push(FeedGap {
                instrument_id: s.inst,
                ts_start: a.min(b),
                ts_end: a.max(b),
                reason: "kraken checksum mismatch: in-view book differs from the venue's (until the end)".into(),
            });
        }
    }
    let mut feed_gaps = std::mem::take(&mut conv.feed_gaps);
    feed_gaps.sort_by_key(|g| (g.instrument_id, g.ts_start, g.ts_end));
    let out_of_view_at_end: u64 = syms
        .values()
        .map(|s| s.orders.values().filter(|o| !o.in_view).count() as u64)
        .sum();
    let open_at_end: u64 = syms.values().map(|s| s.orders.len() as u64).sum();
    let mut counters = Counters::new();
    for k in ANOMALIES.iter().chain(&WINDOW_COUNTERS) {
        counters.insert((*k).to_string(), 0);
    }
    counters.extend(conv.st);
    let events = conv.out;
    counters.insert("records".into(), events.len() as u64);
    counters.insert("open_at_end".into(), open_at_end);
    counters.insert("out_of_view_at_end".into(), out_of_view_at_end);
    counters.insert("window_truncated_depth".into(), u64::from(max_depth));
    counters.insert("no_execution_records".into(), u64::from(!events.is_empty()));
    Ok(L3Output {
        events,
        trades: Vec::new(),
        counters,
        instruments,
        sequence_gaps: 0,
        pre_snapshot_rows_skipped: 0,
        dq_counters: DQ_COUNTERS.to_vec(),
        feed_gaps,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checksum_text_and_fixed_text() {
        assert_eq!(checksum_text("0.10000000"), "10000000");
        assert_eq!(checksum_text("44939.5"), "449395");
        assert_eq!(checksum_text("0.00064537"), "64537");
        // 420.0 at qty precision 8 -> "42000000000"
        assert_eq!(fixed_text(42_000_000_000, -8, 8).as_deref(), Some("42000000000"));
        assert_eq!(fixed_text(563_000_000, -9, 4).as_deref(), Some("5630"));
        assert_eq!(fixed_text(563_000_001, -9, 4), None);
    }
}
