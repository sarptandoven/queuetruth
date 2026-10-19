//! Small shared helpers: civil dates, per-instrument rows by time.

use qt_book::IdMap;
use qt_types::{Instrument, Ts};

/// Nanoseconds per day.
pub(crate) const DAY_NS: i64 = 86_400_000_000_000;
/// Nanoseconds per second.
pub(crate) const SEC_NS: i64 = 1_000_000_000;

/// (year, month, day) of a day count since 1970-01-01 (proleptic Gregorian).
pub(crate) fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// `YYYY-MM-DD` of a session date.
pub(crate) fn date_string(days: i32) -> String {
    let (y, m, d) = civil_from_days(i64::from(days));
    format!("{y:04}-{m:02}-{d:02}")
}

/// `YYYYMMDD` of a session date.
pub(crate) fn date_compact(days: i32) -> String {
    let (y, m, d) = civil_from_days(i64::from(days));
    format!("{y:04}{m:02}{d:02}")
}

/// UTC day count of a timestamp.
pub(crate) fn utc_days(ts: Ts) -> i32 {
    i32::try_from(ts.div_euclid(DAY_NS)).unwrap_or(i32::MAX)
}

/// Instrument rows by id, each list sorted by `midnight_utc_ns`.
#[derive(Clone, Debug, Default)]
pub(crate) struct InstrumentTable {
    map: IdMap<u32, Vec<Instrument>>,
}

impl InstrumentTable {
    pub(crate) fn new(rows: &[Instrument]) -> Self {
        let mut map: IdMap<u32, Vec<Instrument>> = IdMap::default();
        for r in rows {
            map.entry(r.instrument_id).or_default().push(r.clone());
        }
        for v in map.values_mut() {
            v.sort_by_key(|i| (i.midnight_utc_ns, i.session_date));
        }
        Self { map }
    }

    /// The row whose session holds `ts` (latest midnight at or before `ts`; else the earliest row).
    pub(crate) fn at(&self, id: u32, ts: Ts) -> Option<&Instrument> {
        let v = self.map.get(&id)?;
        v.iter().rev().find(|i| i.midnight_utc_ns <= ts).or_else(|| v.first())
    }

    /// Any row of the instrument (the earliest).
    pub(crate) fn any(&self, id: u32) -> Option<&Instrument> {
        self.map.get(&id).and_then(|v| v.first())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_dates() {
        assert_eq!(date_string(0), "1970-01-01");
        assert_eq!(date_string(18_260), "2019-12-30");
        assert_eq!(date_compact(18_291), "20200130");
        assert_eq!(date_string(-1), "1969-12-31");
        assert_eq!(utc_days(-1), -1);
    }
}
