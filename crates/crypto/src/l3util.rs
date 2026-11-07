//! Helpers shared by the exact order-level converters added in 1.2 (`coinbase_l3`, `kraken_l3`, `hyperliquid`):
//! capture-line unwrapping, record building, lenient UTC timestamps and CRC-32.

use qt_types::{MboEvent, QtError, QtResult, flags};

use crate::jsonx::{self, J};
use crate::units;

/// One capture line: the receive time (when the line carries one) and the feed frame.
pub(crate) struct Framed {
    /// 1-based line number in the input stream.
    pub no: u64,
    /// `recv_ns` of the wrapper (or of the frame object itself).
    pub recv: Option<i64>,
    /// The frame.
    pub msg: J,
}

/// Parses a capture line: `{"recv_ns": N, "msg": <frame>}` (wrapper written by the capture script), or the bare
/// frame (an object, possibly with its own `recv_ns`, or an array).
pub(crate) fn unwrap_line(t: &str, no: u64, err: impl Fn(u64, String) -> QtError) -> QtResult<Framed> {
    let mut v = jsonx::parse(t).map_err(|e| err(no, e))?;
    let recv = v.get("recv_ns").and_then(J::as_i64);
    if let J::Obj(f) = &mut v
        && let Some(i) = f.iter().position(|(k, _)| k == "msg")
    {
        let msg = f.swap_remove(i).1;
        return Ok(Framed { no, recv, msg });
    }
    Ok(Framed { no, recv, msg: v })
}

/// MBO record with channel / publisher 0.
#[allow(clippy::too_many_arguments)]
pub(crate) fn rec(
    ts: i64,
    recv: i64,
    inst: u32,
    action: u8,
    side: u8,
    price: i64,
    size: u64,
    order_id: u64,
    fl: u8,
    sequence: u64,
) -> MboEvent {
    MboEvent {
        ts_event: ts,
        ts_recv: recv,
        instrument_id: inst,
        action,
        side,
        price,
        size,
        order_id,
        flags: fl,
        sequence,
        channel_id: 0,
        publisher_id: 0,
    }
}

/// Sets `LAST` on the final record of `out` from index `from` on (one venue event = `out[from..]`).
pub(crate) fn mark_last(out: &mut [MboEvent], from: usize) {
    if out.len() > from
        && let Some(l) = out.last_mut()
    {
        l.flags |= flags::LAST;
    }
}

/// ISO-8601 time in UTC; a time without a zone suffix (Hyperliquid node files) is read as UTC.
pub(crate) fn iso_utc(s: &str) -> QtResult<i64> {
    let t = s.trim();
    if t.ends_with('Z') || t.ends_with("+00:00") {
        units::iso_ns(t)
    } else {
        units::iso_ns(&format!("{t}Z"))
    }
}

/// CRC-32 (IEEE 802.3, reflected, polynomial 0xEDB88320), as zlib / Python `binascii.crc32`.
pub fn crc32(data: &[u8]) -> u32 {
    const fn table() -> [u32; 256] {
        let mut t = [0u32; 256];
        let mut i = 0;
        while i < 256 {
            let mut c = i as u32;
            let mut k = 0;
            while k < 8 {
                c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
                k += 1;
            }
            t[i] = c;
            i += 1;
        }
        t
    }
    const T: [u32; 256] = table();
    let mut c = !0u32;
    for &b in data {
        c = T[((c ^ u32::from(b)) & 0xFF) as usize] ^ (c >> 8);
    }
    !c
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32_check_value() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(crc32(b""), 0);
    }

    #[test]
    fn iso_utc_accepts_naive_times() {
        assert_eq!(
            iso_utc("2024-07-26T08:31:48.717").unwrap(),
            units::iso_ns("2024-07-26T08:31:48.717Z").unwrap()
        );
        assert!(iso_utc("nope").is_err());
    }
}
