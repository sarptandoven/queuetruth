//! hftbacktest event `.npz` export (SPEC 5.3.5): a structured array of 64-byte records with numpy dtype
//! `[("ev","<u8"),("exch_ts","<i8"),("local_ts","<i8"),("px","<f8"),("qty","<f8"),("order_id","<u8"),("ival","<i8"),
//! ("fval","<f8")]`, saved as `np.savez_compressed(path, data=arr)` does: a zip archive with one DEFLATE member
//! `data.npy` (NPY format 1.0, header padded to a multiple of 64 bytes), written by the in-crate `zipw` writer
//! (raw deflate, level 6; ZIP64 only for a member of 4 GiB or more). Zip timestamps and compressor settings are fixed
//! (1980-01-01) so the file is byte-reproducible. A trade row without an aggressor that is not a hidden print takes its side from
//! the prevailing visible BBO (at the bid: sell, at the ask: buy) and is omitted if neither applies. Each npz gets an
//! orders sidecar `<stem>.orders.json` (format of the hftbacktest cross-check driver, O-ZOO-3). Prices and sizes become `price * 10^price_exp`, `size * 10^size_exp` (division by
//! the exact power of ten for negative exponents); hidden trades are omitted.

use std::path::Path;

use qt_schema::io::{FileEntry, sha256_file};
use qt_schema::rows::{L2Kind, L2Row, l2flags};
use qt_types::{BookSide, Dur, Instrument, Px, QtError, QtResult, Qty, Side, Ts};

/// Bytes per event record.
pub const EVENT_SIZE: usize = 64;
/// Exchange-side event flag.
pub const EXCH_EVENT: u64 = 1 << 31;
/// Local-side event flag.
pub const LOCAL_EVENT: u64 = 1 << 30;
/// Buy-side flag.
pub const BUY_EVENT: u64 = 1 << 29;
/// Sell-side flag.
pub const SELL_EVENT: u64 = 1 << 28;
/// Depth update kind.
pub const DEPTH_EVENT: u64 = 1;
/// Trade kind.
pub const TRADE_EVENT: u64 = 2;
/// Depth clear kind.
pub const DEPTH_CLEAR_EVENT: u64 = 3;
/// Depth snapshot kind.
pub const DEPTH_SNAPSHOT_EVENT: u64 = 4;

const DESCR: &str = "[('ev', '<u8'), ('exch_ts', '<i8'), ('local_ts', '<i8'), ('px', '<f8'), ('qty', '<f8'), \
                     ('order_id', '<u8'), ('ival', '<i8'), ('fval', '<f8')]";

fn scale(v: f64, exp: i8) -> f64 {
    if exp < 0 {
        v / 10f64.powi(-i32::from(exp))
    } else {
        v * 10f64.powi(i32::from(exp))
    }
}

fn side_flag(s: Side) -> u64 {
    match s {
        Side::Bid => BUY_EVENT,
        _ => SELL_EVENT,
    }
}

/// The 64-byte hftbacktest records of `rows` (little endian).
pub fn hbt_events(rows: &[L2Row], inst: &Instrument, feed_latency_ns: Dur) -> QtResult<Vec<u8>> {
    let mut out = Vec::with_capacity(rows.len() * EVENT_SIZE);
    let base = EXCH_EVENT | LOCAL_EVENT;
    // Prevailing visible book, for trades that carry no aggressor (rows of other producers, e.g. L2-only datasets).
    let mut vb = crate::VisibleBook::new();
    for r in rows {
        let prevailing = (vb.best(BookSide::Bid).map(|b| b.0), vb.best(BookSide::Ask).map(|a| a.0));
        vb.apply(r);
        let (ev, px, qty) = match r.kind {
            L2Kind::Depth => (DEPTH_EVENT | side_flag(r.side) | base, r.price, r.new_qty),
            L2Kind::Snapshot => (DEPTH_SNAPSHOT_EVENT | side_flag(r.side) | base, r.price, r.new_qty),
            L2Kind::Clear => (DEPTH_CLEAR_EVENT | base, None, 0),
            L2Kind::Trade => {
                if r.flags & l2flags::HIDDEN_TRADE != 0 {
                    continue;
                }
                let aggr = match r.aggressor {
                    Side::None => match (r.price, prevailing) {
                        (Some(p), (Some(b), a)) if p == b && a != Some(p) => Side::Ask,
                        (Some(p), (b, Some(a))) if p == a && b != Some(p) => Side::Bid,
                        // Unresolvable aggressor: an hbt trade needs a side; the row is omitted.
                        _ => continue,
                    },
                    s => s,
                };
                let s = if aggr == Side::Bid { BUY_EVENT } else { SELL_EVENT };
                (TRADE_EVENT | s | base, r.price, r.trade_qty)
            }
        };
        let local = r.ts_recv.checked_add(feed_latency_ns).ok_or_else(|| {
            QtError::Config(format!(
                "feed_latency_ns: ts_recv {} + {feed_latency_ns} overflows",
                r.ts_recv
            ))
        })?;
        out.extend_from_slice(&ev.to_le_bytes());
        out.extend_from_slice(&r.ts_event.to_le_bytes());
        out.extend_from_slice(&local.to_le_bytes());
        out.extend_from_slice(&scale(px.unwrap_or(0) as f64, inst.price_exp).to_le_bytes());
        out.extend_from_slice(&scale(qty as f64, inst.size_exp).to_le_bytes());
        out.extend_from_slice(&0u64.to_le_bytes());
        out.extend_from_slice(&0i64.to_le_bytes());
        out.extend_from_slice(&0f64.to_le_bytes());
    }
    Ok(out)
}

/// A complete `.npy` (format 1.0) file holding `events` (`EVENT_SIZE`-byte records) as a 1-d structured array.
pub fn npy_bytes(events: &[u8]) -> Vec<u8> {
    let n = events.len() / EVENT_SIZE;
    let dict = format!("{{'descr': {DESCR}, 'fortran_order': False, 'shape': ({n},), }}");
    let unpadded = 10 + dict.len() + 1;
    let pad = (64 - unpadded % 64) % 64;
    let header_len = dict.len() + pad + 1;
    let mut out = Vec::with_capacity(10 + header_len + events.len());
    out.extend_from_slice(b"\x93NUMPY\x01\x00");
    // The descr is fixed and short: the header length always fits in u16.
    out.extend_from_slice(&u16::try_from(header_len).unwrap_or(u16::MAX).to_le_bytes());
    out.extend_from_slice(dict.as_bytes());
    out.extend(std::iter::repeat_n(b' ', pad));
    out.push(b'\n');
    out.extend_from_slice(&events[..n * EVENT_SIZE]);
    out
}

/// Own-order action for the hftbacktest cross-check sidecar.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct HbtAction {
    /// Action time (ns).
    pub ts: Ts,
    /// `true` = submit, `false` = cancel.
    pub submit: bool,
    /// Order id.
    pub order_id: u64,
    /// Side.
    pub side: BookSide,
    /// Limit price (price units).
    pub price: Px,
    /// Quantity (size units).
    pub qty: Qty,
    /// GTX (post-only) instead of GTC.
    pub gtx: bool,
}

/// Path of the orders sidecar of an npz: `<dir>/<stem>.orders.json`.
pub fn hbt_orders_path(npz: &Path) -> std::path::PathBuf {
    let stem = npz
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    npz.with_file_name(format!("{stem}.orders.json"))
}

/// Writes the orders sidecar read by the hftbacktest cross-check driver (O-ZOO-3):
/// `{"id", "tick_size", "lot_size", "actions": [{"ts", "kind": "submit"|"cancel", "order_id", "side": "B"|"A",
/// "px", "qty", "tif": "GTX"|"GTC"}]}` with prices and sizes in real units.
pub fn write_hbt_orders(npz: &Path, inst: &Instrument, actions: &[HbtAction]) -> QtResult<FileEntry> {
    let path = hbt_orders_path(npz);
    let id = npz
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    let acts: Vec<serde_json::Value> = actions
        .iter()
        .map(|a| {
            serde_json::json!({
                "ts": a.ts,
                "kind": if a.submit { "submit" } else { "cancel" },
                "order_id": a.order_id,
                "side": if a.side == BookSide::Bid { "B" } else { "A" },
                "px": scale(a.price as f64, inst.price_exp),
                "qty": scale(a.qty as f64, inst.size_exp),
                "tif": if a.gtx { "GTX" } else { "GTC" },
            })
        })
        .collect();
    let doc = serde_json::json!({
        "id": id,
        "tick_size": scale(inst.tick_size as f64, inst.price_exp),
        "lot_size": scale(inst.lot_size as f64, inst.size_exp),
        "actions": acts,
    });
    let dir = path.parent().unwrap_or_else(|| Path::new("."));
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    qt_ingest::write_json(dir, &name, &doc)
}

/// `write_hbt_npz` plus its orders sidecar with `actions`.
pub fn write_hbt_npz_with_orders(
    rows: &[L2Row],
    inst: &Instrument,
    feed_latency_ns: Dur,
    path: &Path,
    actions: &[HbtAction],
) -> QtResult<(FileEntry, FileEntry)> {
    let npz = write_hbt_npz(rows, inst, feed_latency_ns, path)?;
    let side = write_hbt_orders(path, inst, actions)?;
    Ok((npz, side))
}

/// hftbacktest event `.npz` (64-byte records, SPEC 5.3.5), plus an orders sidecar (`<stem>.orders.json`, no
/// actions) next to it; use `write_hbt_npz_with_orders` to put shadow orders in the sidecar.
pub fn write_hbt_npz(rows: &[L2Row], inst: &Instrument, feed_latency_ns: Dur, path: &Path) -> QtResult<FileEntry> {
    let events = hbt_events(rows, inst, feed_latency_ns)?;
    let n_events = (events.len() / EVENT_SIZE) as u64;
    let npy = npy_bytes(&events);
    drop(events);
    let err = |e: &dyn std::fmt::Display| QtError::Engine(format!("{}: {e}", path.display()));
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent).map_err(|e| err(&e))?;
    }
    let f = std::fs::File::create(path).map_err(|e| err(&e))?;
    let w = std::io::BufWriter::with_capacity(1 << 20, f);
    crate::zipw::write_zip(w, &[("data.npy", npy.as_slice())], crate::zipw::Method::Deflate, false)
        .map_err(|e| err(&e))?;
    drop(npy);
    let bytes = std::fs::metadata(path).map_err(|e| err(&e))?.len();
    write_hbt_orders(path, inst, &[])?;
    Ok(FileEntry {
        name: path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
        rows: Some(n_events),
        bytes,
        sha256: sha256_file(path)?,
    })
}
