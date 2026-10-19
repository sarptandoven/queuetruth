//! Fixed-size binary encodings of the partition rows, used to spill per-partition buffers to scratch files while a
//! dataset is written (memory stays bounded however many partitions an input touches), and the deterministic
//! chunked Parquet writer that turns a spilled stream back into one table file.

use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Read, Write};
use std::path::{Path, PathBuf};

use arrow_array::RecordBatch;
use qt_schema::codec;
use qt_schema::io::{FileEntry, TableWriter};
use qt_schema::rows::{DictEnum, L2Kind, L2Row, TradeKind, TradeRow};
use qt_types::{MboEvent, QtError, QtResult, Side, Ts};

/// Rows per record batch handed to the Parquet writer (fixed so output bytes never depend on buffering).
pub const CHUNK_ROWS: usize = 65_536;

/// A row with a fixed-size little-endian encoding and a Parquet table.
pub trait FixedRow: Copy + Send + Sync + 'static {
    /// Encoded size in bytes.
    const SIZE: usize;
    /// Table name.
    const TABLE: &'static str;
    /// Encodes into `out` (`SIZE` bytes).
    fn enc(&self, out: &mut Vec<u8>);
    /// Decodes `SIZE` bytes.
    fn dec(b: &[u8]) -> Option<Self>;
    /// Partition timestamp (ts_event).
    fn ts(&self) -> Ts;
    /// Record batch of rows.
    fn batch(rows: &[Self]) -> RecordBatch;
}

struct Cur<'a> {
    b: &'a [u8],
    o: usize,
}

impl Cur<'_> {
    fn take<const N: usize>(&mut self) -> Option<[u8; N]> {
        let s = self.b.get(self.o..self.o + N)?;
        self.o += N;
        s.try_into().ok()
    }
    fn u8(&mut self) -> Option<u8> {
        Some(self.take::<1>()?[0])
    }
    fn u16(&mut self) -> Option<u16> {
        Some(u16::from_le_bytes(self.take()?))
    }
    fn u32(&mut self) -> Option<u32> {
        Some(u32::from_le_bytes(self.take()?))
    }
    fn u64(&mut self) -> Option<u64> {
        Some(u64::from_le_bytes(self.take()?))
    }
    fn i64(&mut self) -> Option<i64> {
        Some(i64::from_le_bytes(self.take()?))
    }
}

impl FixedRow for MboEvent {
    const SIZE: usize = 59;
    const TABLE: &'static str = "qt.mbo";
    fn enc(&self, o: &mut Vec<u8>) {
        o.extend_from_slice(&self.ts_event.to_le_bytes());
        o.extend_from_slice(&self.ts_recv.to_le_bytes());
        o.extend_from_slice(&self.instrument_id.to_le_bytes());
        o.push(self.action);
        o.push(self.side);
        o.extend_from_slice(&self.price.to_le_bytes());
        o.extend_from_slice(&self.size.to_le_bytes());
        o.extend_from_slice(&self.order_id.to_le_bytes());
        o.push(self.flags);
        o.extend_from_slice(&self.sequence.to_le_bytes());
        o.extend_from_slice(&self.channel_id.to_le_bytes());
        o.extend_from_slice(&self.publisher_id.to_le_bytes());
    }
    fn dec(b: &[u8]) -> Option<Self> {
        let mut c = Cur { b, o: 0 };
        Some(Self {
            ts_event: c.i64()?,
            ts_recv: c.i64()?,
            instrument_id: c.u32()?,
            action: c.u8()?,
            side: c.u8()?,
            price: c.i64()?,
            size: c.u64()?,
            order_id: c.u64()?,
            flags: c.u8()?,
            sequence: c.u64()?,
            channel_id: c.u16()?,
            publisher_id: c.u16()?,
        })
    }
    fn ts(&self) -> Ts {
        self.ts_event
    }
    fn batch(rows: &[Self]) -> RecordBatch {
        codec::mbo_to_batch(rows)
    }
}

impl FixedRow for TradeRow {
    const SIZE: usize = 47;
    const TABLE: &'static str = "qt.trades";
    fn enc(&self, o: &mut Vec<u8>) {
        o.extend_from_slice(&self.ts_event.to_le_bytes());
        o.extend_from_slice(&self.instrument_id.to_le_bytes());
        o.extend_from_slice(&self.sequence.to_le_bytes());
        o.extend_from_slice(&self.price.to_le_bytes());
        o.extend_from_slice(&self.size.to_le_bytes());
        o.push(self.aggressor_side.as_byte());
        o.push(self.kind.index() as u8);
        o.push(u8::from(self.maker_order_id.is_some()));
        o.extend_from_slice(&self.maker_order_id.unwrap_or(0).to_le_bytes());
    }
    fn dec(b: &[u8]) -> Option<Self> {
        let mut c = Cur { b, o: 0 };
        let (ts_event, instrument_id, sequence, price, size) = (c.i64()?, c.u32()?, c.u64()?, c.i64()?, c.u64()?);
        let aggressor_side = Side::from_byte(c.u8()?);
        let kind = TradeKind::from_index(usize::from(c.u8()?))?;
        let has = c.u8()? != 0;
        let maker = c.u64()?;
        Some(Self {
            ts_event,
            instrument_id,
            sequence,
            price,
            size,
            aggressor_side,
            kind,
            maker_order_id: has.then_some(maker),
        })
    }
    fn ts(&self) -> Ts {
        self.ts_event
    }
    fn batch(rows: &[Self]) -> RecordBatch {
        codec::trades_to_batch(rows)
    }
}

impl FixedRow for L2Row {
    const SIZE: usize = 83;
    const TABLE: &'static str = "qt.l2";
    fn enc(&self, o: &mut Vec<u8>) {
        o.extend_from_slice(&self.ts_event.to_le_bytes());
        o.extend_from_slice(&self.ts_recv.to_le_bytes());
        o.extend_from_slice(&self.instrument_id.to_le_bytes());
        o.extend_from_slice(&self.event_id.to_le_bytes());
        o.extend_from_slice(&self.sequence.to_le_bytes());
        o.push(self.kind.index() as u8);
        o.push(self.side.as_byte());
        o.push(u8::from(self.price.is_some()));
        o.extend_from_slice(&self.price.unwrap_or(0).to_le_bytes());
        o.extend_from_slice(&self.prev_qty.to_le_bytes());
        o.extend_from_slice(&self.new_qty.to_le_bytes());
        o.push(u8::from(self.prev_ct.is_some()) | (u8::from(self.new_ct.is_some()) << 1));
        o.extend_from_slice(&self.prev_ct.unwrap_or(0).to_le_bytes());
        o.extend_from_slice(&self.new_ct.unwrap_or(0).to_le_bytes());
        o.extend_from_slice(&self.trade_qty.to_le_bytes());
        o.push(self.aggressor.as_byte());
        o.push(self.level);
        o.push(self.flags);
    }
    fn dec(b: &[u8]) -> Option<Self> {
        let mut c = Cur { b, o: 0 };
        let (ts_event, ts_recv, instrument_id, event_id, sequence) = (c.i64()?, c.i64()?, c.u32()?, c.u64()?, c.u64()?);
        let kind = L2Kind::from_index(usize::from(c.u8()?))?;
        let side = Side::from_byte(c.u8()?);
        let has_px = c.u8()? != 0;
        let px = c.i64()?;
        let (prev_qty, new_qty) = (c.u64()?, c.u64()?);
        let cts = c.u8()?;
        let (pc, nc) = (c.u32()?, c.u32()?);
        Some(Self {
            ts_event,
            ts_recv,
            instrument_id,
            event_id,
            sequence,
            kind,
            side,
            price: has_px.then_some(px),
            prev_qty,
            new_qty,
            prev_ct: (cts & 1 != 0).then_some(pc),
            new_ct: (cts & 2 != 0).then_some(nc),
            trade_qty: c.u64()?,
            aggressor: Side::from_byte(c.u8()?),
            level: c.u8()?,
            flags: c.u8()?,
        })
    }
    fn ts(&self) -> Ts {
        self.ts_event
    }
    fn batch(rows: &[Self]) -> RecordBatch {
        codec::l2_to_batch(rows)
    }
}

fn scratch_err(path: &Path, e: impl std::fmt::Display) -> QtError {
    QtError::Engine(format!("scratch file {}: {e}", path.display()))
}

/// Appends rows to a spill file.
pub fn spill_append<T: FixedRow>(path: &Path, rows: &[T]) -> QtResult<()> {
    let f = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| scratch_err(path, e))?;
    let mut w = BufWriter::with_capacity(1 << 20, f);
    let mut buf = Vec::with_capacity(T::SIZE * 4096);
    for chunk in rows.chunks(4096) {
        buf.clear();
        for r in chunk {
            r.enc(&mut buf);
        }
        w.write_all(&buf).map_err(|e| scratch_err(path, e))?;
    }
    w.flush().map_err(|e| scratch_err(path, e))
}

/// Writes one Parquet table from an optional spill file followed by `tail`, in `CHUNK_ROWS` batches.
pub fn write_table<T: FixedRow>(out: &Path, spill: Option<&Path>, tail: &[T]) -> QtResult<FileEntry> {
    let mut w = TableWriter::create(out, T::TABLE)?;
    let mut chunk: Vec<T> = Vec::with_capacity(CHUNK_ROWS);
    let flush = |chunk: &mut Vec<T>, w: &mut TableWriter| -> QtResult<()> {
        if !chunk.is_empty() {
            w.write(&T::batch(chunk))?;
            chunk.clear();
        }
        Ok(())
    };
    if let Some(p) = spill {
        let mut f = File::open(p).map_err(|e| scratch_err(p, e))?;
        let mut buf = vec![0u8; T::SIZE * 16_384];
        let mut have = 0usize;
        loop {
            let n = f.read(&mut buf[have..]).map_err(|e| scratch_err(p, e))?;
            have += n;
            let whole = have - have % T::SIZE;
            for rec in buf[..whole].chunks_exact(T::SIZE) {
                chunk.push(T::dec(rec).ok_or_else(|| scratch_err(p, "corrupt record"))?);
                if chunk.len() == CHUNK_ROWS {
                    flush(&mut chunk, &mut w)?;
                }
            }
            buf.copy_within(whole..have, 0);
            have -= whole;
            if n == 0 {
                if have != 0 {
                    return Err(scratch_err(p, "truncated record"));
                }
                break;
            }
        }
    }
    // Top the partial chunk up to CHUNK_ROWS, then write the tail in place (same batch boundaries as row-by-row).
    let take = (CHUNK_ROWS - chunk.len()).min(tail.len());
    let (head, rest) = tail.split_at(if chunk.is_empty() { 0 } else { take });
    chunk.extend_from_slice(head);
    if chunk.len() == CHUNK_ROWS || rest.is_empty() {
        flush(&mut chunk, &mut w)?;
    }
    for c in rest.chunks(CHUNK_ROWS) {
        w.write(&T::batch(c))?;
    }
    w.finish()
}

/// Scratch directory helper.
pub fn spill_path(dir: &Path, table: &str, date: i32, iid: u32) -> PathBuf {
    dir.join(format!("{}-{date}-{iid}.bin", table.trim_start_matches("qt.")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use qt_schema::rows::l2flags;

    #[test]
    fn fixed_encodings_round_trip() {
        let e = MboEvent {
            ts_event: -1,
            ts_recv: 7,
            instrument_id: u32::MAX,
            action: b'A',
            side: b'B',
            price: i64::MIN,
            size: u64::MAX,
            order_id: 3,
            flags: 0xff,
            sequence: u64::MAX - 1,
            channel_id: 9,
            publisher_id: 65535,
        };
        let mut b = Vec::new();
        e.enc(&mut b);
        assert_eq!(b.len(), MboEvent::SIZE);
        assert_eq!(MboEvent::dec(&b), Some(e));
        assert_eq!(MboEvent::dec(&b[1..]), None);
        let t = TradeRow {
            ts_event: 5,
            instrument_id: 1,
            sequence: 2,
            price: -3,
            size: 4,
            aggressor_side: Side::Ask,
            kind: TradeKind::Cross,
            maker_order_id: Some(77),
        };
        let mut b = Vec::new();
        t.enc(&mut b);
        assert_eq!(b.len(), TradeRow::SIZE);
        assert_eq!(TradeRow::dec(&b), Some(t));
        let r = L2Row {
            ts_event: 1,
            ts_recv: 2,
            instrument_id: 3,
            event_id: 4,
            sequence: 5,
            kind: L2Kind::Snapshot,
            side: Side::Bid,
            price: Some(-6),
            prev_qty: 7,
            new_qty: 8,
            prev_ct: None,
            new_ct: Some(9),
            trade_qty: 10,
            aggressor: Side::None,
            level: 255,
            flags: l2flags::SNAPSHOT | l2flags::LAST_IN_BATCH,
        };
        let mut b = Vec::new();
        r.enc(&mut b);
        assert_eq!(b.len(), L2Row::SIZE);
        assert_eq!(L2Row::dec(&b), Some(r));
    }
}
