//! Shared helpers for the crypto converter tests.
#![allow(dead_code)]

use qt_types::{MboEvent, QtResult};

/// `(action, side, price, size, order_id, flags, sequence)` with action/side as chars.
pub type Act = (char, char, i64, u64, u64, u8, u64);

pub fn acts(rows: &[MboEvent]) -> Vec<Act> {
    rows.iter()
        .map(|r| {
            (
                r.action as char,
                r.side as char,
                r.price,
                r.size,
                r.order_id,
                r.flags,
                r.sequence,
            )
        })
        .collect()
}

pub fn lines(v: &[String]) -> impl Iterator<Item = QtResult<String>> + '_ {
    v.iter().map(|s| Ok(s.clone()))
}

pub fn fixture(rel: &str) -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(rel)
}

/// Fresh empty directory under the system temp dir.
pub fn tmpdir(name: &str) -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!("qt_crypto_{name}_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

pub fn get(c: &qt_crypto::Counters, k: &str) -> u64 {
    *c.get(k).unwrap_or_else(|| panic!("counter {k} missing: {c:?}"))
}

/// Minimal FIFO order book replaying MBO records (KeepOnSizeDecrease), per instrument, for consistency checks of
/// converter output: `(instrument, side) -> price -> [(order_id, size)]`.
/// Price -> queue of (order id, size).
pub type Levels = std::collections::BTreeMap<i64, Vec<(u64, u64)>>;

#[derive(Default, Debug)]
pub struct MiniBook {
    pub levels: std::collections::HashMap<(u32, u8), Levels>,
    pub orders: std::collections::HashMap<(u32, u64), (u8, i64)>,
}

impl MiniBook {
    /// Applies one record; `Err` on a reference to an unknown order, a duplicate add or an oversized cancel.
    pub fn apply(&mut self, e: &MboEvent) -> Result<(), String> {
        let key = (e.instrument_id, e.order_id);
        match e.action {
            b'R' => {
                self.levels.retain(|(i, _), _| *i != e.instrument_id);
                self.orders.retain(|(i, _), _| *i != e.instrument_id);
            }
            b'A' => {
                if self.orders.insert(key, (e.side, e.price)).is_some() {
                    return Err(format!("duplicate add {e:?}"));
                }
                self.levels
                    .entry((e.instrument_id, e.side))
                    .or_default()
                    .entry(e.price)
                    .or_default()
                    .push((e.order_id, e.size));
            }
            b'C' | b'M' => {
                let (side, px) = *self.orders.get(&key).ok_or_else(|| format!("unknown order {e:?}"))?;
                let lv = self.levels.get_mut(&(e.instrument_id, side)).ok_or("missing side")?;
                let q = lv.get_mut(&px).ok_or("missing level")?;
                let pos = q.iter().position(|(id, _)| *id == e.order_id).ok_or("missing order")?;
                let cur = q[pos].1;
                if e.action == b'C' {
                    if e.size > cur {
                        return Err(format!("cancel {} > live {cur}: {e:?}", e.size));
                    }
                    if e.size == cur {
                        q.remove(pos);
                        self.orders.remove(&key);
                    } else {
                        q[pos].1 -= e.size;
                    }
                } else if e.price == px && e.size <= cur {
                    q[pos].1 = e.size;
                    if e.size == 0 {
                        q.remove(pos);
                        self.orders.remove(&key);
                    }
                } else {
                    q.remove(pos);
                    if q.is_empty() {
                        lv.remove(&px);
                    }
                    self.orders.insert(key, (side, e.price));
                    lv.entry(e.price).or_default().push((e.order_id, e.size));
                }
                if let Some(q) = self
                    .levels
                    .get_mut(&(e.instrument_id, side))
                    .and_then(|l| l.get_mut(&px))
                {
                    if q.is_empty() {
                        self.levels.get_mut(&(e.instrument_id, side)).map(|l| l.remove(&px));
                    }
                }
            }
            b'F' if !self.orders.contains_key(&key) => {
                return Err(format!("fill of unknown order {e:?}"));
            }
            _ => {}
        }
        Ok(())
    }

    /// `(price, size)` of every order of a side in queue order, best level first (top `levels` levels).
    pub fn side(&self, inst: u32, side: u8, levels: usize) -> Vec<(i64, u64)> {
        let Some(lv) = self.levels.get(&(inst, side)) else {
            return Vec::new();
        };
        let ps: Vec<&i64> = if side == b'B' {
            lv.keys().rev().collect()
        } else {
            lv.keys().collect()
        };
        ps.into_iter()
            .map(|p| (p, &lv[p]))
            .filter(|(_, q)| !q.is_empty())
            .take(levels)
            .flat_map(|(p, q)| q.iter().map(move |(_, s)| (*p, *s)))
            .collect()
    }
}

/// Replays `events` into a `MiniBook`, failing on the first inconsistency.
pub fn replay(events: &[MboEvent]) -> Result<MiniBook, String> {
    let mut b = MiniBook::default();
    for e in events {
        b.apply(e)?;
    }
    Ok(b)
}
