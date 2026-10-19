//! Civil dates, US/Eastern midnight (US DST rules since 2007) and the per-venue session conventions used by the
//! generator: Nasdaq = America/New_York, open 09:30 (34,200 s), close 16:00 (57,600 s); CME = UTC day, regular
//! session 13:30-20:00 UTC (48,600 s .. 72,000 s); generic = UTC day, 0 .. 86,400 s.

use qt_types::{Px, Qty, Ts, Venue};

/// Nanoseconds per second.
pub const NS_PER_S: i64 = 1_000_000_000;
/// Nanoseconds per day.
pub const NS_PER_DAY: i64 = 86_400 * NS_PER_S;

/// Days since 1970-01-01 of a proleptic Gregorian date (H. Hinnant's algorithm).
pub fn days_from_civil(y: i32, m: u32, d: u32) -> i32 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let m = m as i32;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d as i32 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// `(year, month, day)` of a day number.
pub fn civil_from_days(z: i32) -> (i32, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// `YYYY-MM-DD` of a day number.
pub fn date_string(days: i32) -> String {
    let (y, m, d) = civil_from_days(days);
    format!("{y:04}-{m:02}-{d:02}")
}

/// Weekday of a day number, 0 = Sunday.
pub fn weekday(days: i32) -> u32 {
    (days + 4).rem_euclid(7) as u32
}

/// Day number of the `n`-th (1-based) Sunday of a month.
fn nth_sunday(y: i32, m: u32, n: u32) -> i32 {
    let first = days_from_civil(y, m, 1);
    let to_sunday = (7 - weekday(first)) % 7;
    first + to_sunday as i32 + 7 * (n as i32 - 1)
}

/// UTC ns of local midnight in America/New_York (EDT from the second Sunday of March to the first Sunday of
/// November; the switch happens at 02:00, so midnight of the switch day keeps the old offset).
pub fn us_eastern_midnight_utc_ns(days: i32) -> Ts {
    let (y, _, _) = civil_from_days(days);
    let dst = days > nth_sunday(y, 3, 2) && days <= nth_sunday(y, 11, 1);
    let offset_h: i64 = if dst { 4 } else { 5 };
    i64::from(days) * NS_PER_DAY + offset_h * 3_600 * NS_PER_S
}

/// Session conventions of a venue for synthetic data.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SessionRules {
    /// IANA time zone written into `qt.instruments`.
    pub tz: &'static str,
    /// Continuous-session open, seconds after local midnight.
    pub open_s: u32,
    /// Continuous-session close, seconds after local midnight.
    pub close_s: u32,
    /// Source venue name.
    pub source_venue: &'static str,
    /// Contract multiplier.
    pub multiplier: f64,
    /// DBN-style publisher id written into records.
    pub publisher_id: u16,
}

/// Session conventions per venue (module doc).
pub fn session_rules(venue: Venue) -> SessionRules {
    match venue {
        Venue::Nasdaq => SessionRules {
            tz: "America/New_York",
            open_s: 34_200,
            close_s: 57_600,
            source_venue: "nasdaq",
            multiplier: 1.0,
            publisher_id: 2,
        },
        Venue::Cme => SessionRules {
            tz: "UTC",
            open_s: 48_600,
            close_s: 72_000,
            source_venue: "cme",
            multiplier: 50.0,
            publisher_id: 1,
        },
        Venue::Coinbase => SessionRules {
            tz: "UTC",
            open_s: 0,
            close_s: 86_400,
            source_venue: "coinbase",
            multiplier: 1.0,
            publisher_id: 0,
        },
        Venue::Generic => SessionRules {
            tz: "UTC",
            open_s: 0,
            close_s: 86_400,
            source_venue: "generic",
            multiplier: 1.0,
            publisher_id: 0,
        },
    }
}

/// UTC ns of the local midnight of `days` for the venue's session time zone.
pub fn local_midnight(venue: Venue, days: i32) -> Ts {
    match venue {
        Venue::Nasdaq => us_eastern_midnight_utc_ns(days),
        _ => i64::from(days) * NS_PER_DAY,
    }
}

/// Local session date (day number) containing `ts` for the venue's time zone.
pub fn local_date(venue: Venue, ts: Ts) -> i32 {
    let mut d = ts.div_euclid(NS_PER_DAY) as i32;
    while local_midnight(venue, d) > ts {
        d -= 1;
    }
    while local_midnight(venue, d + 1) <= ts {
        d += 1;
    }
    d
}

/// A synthetic `qt.instruments` row for `venue` on the local date containing `ts` (price_exp -9, lot 1, size_exp 0).
pub fn instrument_for(
    venue: Venue,
    instrument_id: u32,
    symbol: &str,
    ts: Ts,
    tick: Px,
    lot: Qty,
) -> qt_types::Instrument {
    let sr = session_rules(venue);
    let date = local_date(venue, ts);
    qt_types::Instrument {
        instrument_id,
        session_date: date,
        symbol: symbol.to_owned(),
        venue,
        source_venue: sr.source_venue.to_owned(),
        price_exp: -9,
        tick_size: tick,
        lot_size: lot,
        size_exp: 0,
        contract_multiplier: sr.multiplier,
        currency: "USD".to_owned(),
        session_tz: sr.tz.to_owned(),
        midnight_utc_ns: local_midnight(venue, date),
        open_s: sr.open_s,
        close_s: sr.close_s,
        source: "synthetic".to_owned(),
    }
}
