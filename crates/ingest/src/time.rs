//! Civil dates and session midnights. ITCH timestamps are ns since local (US Eastern) midnight; every engine
//! timestamp is ns since the Unix epoch, UTC, so each session's local midnight is placed on the UTC axis once.
//! US rule since 2007 (as et `time.rs`): DST from the second Sunday of March to the first Sunday of November,
//! switching at 02:00 local, so midnight of the March switch day is still EST (UTC-5) and midnight of the November
//! switch day is still EDT (UTC-4).

use qt_schema::specs::SessionTz;
use qt_types::{QtError, QtResult, Ts};

/// Nanoseconds per civil day.
pub const NS_PER_DAY: i64 = 86_400_000_000_000;
const NS_PER_HOUR: i64 = 3_600_000_000_000;

/// A proleptic Gregorian civil date.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Date {
    /// Year.
    pub year: i32,
    /// Month 1..=12.
    pub month: u32,
    /// Day 1..=31 (validated against the month).
    pub day: u32,
}

fn is_leap(y: i32) -> bool {
    (y % 4 == 0 && y % 100 != 0) || y % 400 == 0
}

fn days_in_month(y: i32, m: u32) -> u32 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap(y) => 29,
        2 => 28,
        _ => 0,
    }
}

impl Date {
    /// A validated date.
    pub fn new(year: i32, month: u32, day: u32) -> Option<Self> {
        ((1..=9999).contains(&year) && (1..=12).contains(&month) && day >= 1 && day <= days_in_month(year, month))
            .then_some(Self { year, month, day })
    }

    /// Parses `YYYY-MM-DD` (exactly 4-2-2 digits).
    pub fn parse(s: &str) -> QtResult<Self> {
        let t = s.trim();
        let b = t.as_bytes();
        let bad = || QtError::Config(format!("session_date: expected YYYY-MM-DD, got {s:?}"));
        if b.len() != 10 || b[4] != b'-' || b[7] != b'-' {
            return Err(bad());
        }
        let num = |r: std::ops::Range<usize>| -> Option<u32> {
            let f = t.get(r)?;
            if f.bytes().all(|c| c.is_ascii_digit()) {
                f.parse().ok()
            } else {
                None
            }
        };
        let (y, m, d) = (
            num(0..4).ok_or_else(bad)?,
            num(5..7).ok_or_else(bad)?,
            num(8..10).ok_or_else(bad)?,
        );
        let y = i32::try_from(y).map_err(|_| bad())?;
        Self::new(y, m, d).ok_or_else(|| QtError::Config(format!("session_date: no such date {s:?}")))
    }

    /// `MMDDYYYY` at the start of an emi.nasdaq.com file name (`01302019.NASDAQ_ITCH50.gz`).
    pub fn from_itch_filename(name: &str) -> Option<Self> {
        let base = name.rsplit(['/', '\\']).next()?;
        let digits: String = base.chars().take_while(char::is_ascii_digit).collect();
        if digits.len() != 8 {
            return None;
        }
        let month = digits.get(0..2)?.parse().ok()?;
        let day = digits.get(2..4)?.parse().ok()?;
        let year = digits.get(4..8)?.parse().ok()?;
        Self::new(year, month, day)
    }

    /// Days since 1970-01-01 (Howard Hinnant's algorithm).
    pub fn days_from_civil(self) -> i32 {
        let (y, m, d) = (i64::from(self.year), i64::from(self.month), i64::from(self.day));
        let y = if m <= 2 { y - 1 } else { y };
        let era = y.div_euclid(400);
        let yoe = y - era * 400;
        let mp = (m + 9) % 12;
        let doy = (153 * mp + 2) / 5 + d - 1;
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        // Years 1..=9999 keep this far inside i32.
        (era * 146_097 + doe - 719_468) as i32
    }

    /// The date `days` after 1970-01-01 (inverse of `days_from_civil`); `None` outside years 1..=9999.
    pub fn from_days(days: i32) -> Option<Self> {
        let z = i64::from(days) + 719_468;
        let era = z.div_euclid(146_097);
        let doe = z - era * 146_097;
        let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
        let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
        let y = yoe + era * 400 + i64::from(m <= 2);
        Self::new(i32::try_from(y).ok()?, m, d)
    }

    /// 0 = Sunday.
    pub fn weekday(self) -> u32 {
        (i64::from(self.days_from_civil()) + 4).rem_euclid(7) as u32
    }

    /// Hours to add to Eastern wall-clock midnight to reach UTC: 5 (EST) or 4 (EDT), US rule since 2007.
    pub fn eastern_offset_hours(self) -> i64 {
        let nth_sunday = |month: u32, n: u32| -> u32 {
            let first = Self { month, day: 1, ..self };
            1 + (7 - first.weekday()) % 7 + 7 * (n - 1)
        };
        let dst_start = Self {
            month: 3,
            day: nth_sunday(3, 2),
            ..self
        };
        let dst_end = Self {
            month: 11,
            day: nth_sunday(11, 1),
            ..self
        };
        let d = self.days_from_civil();
        if d > dst_start.days_from_civil() && d <= dst_end.days_from_civil() {
            4
        } else {
            5
        }
    }
}

impl std::fmt::Display for Date {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:04}-{:02}-{:02}", self.year, self.month, self.day)
    }
}

/// Parses `YYYY-MM-DD` to days since 1970-01-01.
pub fn parse_date(s: &str) -> QtResult<i32> {
    Date::parse(s).map(Date::days_from_civil)
}

/// `YYYY-MM-DD` of days since 1970-01-01 (`?` for dates outside years 1..=9999).
pub fn format_date(days: i32) -> String {
    Date::from_days(days).map_or_else(|| format!("?{days}"), |d| d.to_string())
}

fn checked_midnight(days: i32, offset_hours: i64) -> QtResult<Ts> {
    i64::from(days)
        .checked_mul(NS_PER_DAY)
        .and_then(|v| v.checked_add(offset_hours * NS_PER_HOUR))
        .filter(|v| *v >= 0)
        .ok_or_else(|| {
            QtError::Config(format!(
                "session date {} is outside the supported range",
                format_date(days)
            ))
        })
}

/// UTC ns of 00:00 US Eastern on `date` (`QT-CONFIG-001` before the epoch).
pub fn us_eastern_midnight_utc_ns(date: Date) -> QtResult<Ts> {
    checked_midnight(date.days_from_civil(), date.eastern_offset_hours())
}

/// UTC ns of local midnight of `days` (days since 1970-01-01) in `tz`.
pub fn midnight_utc_ns(days: i32, tz: SessionTz) -> QtResult<Ts> {
    match tz {
        SessionTz::Utc => checked_midnight(days, 0),
        SessionTz::AmericaNewYork => {
            let d = Date::from_days(days)
                .ok_or_else(|| QtError::Config(format!("session date {days} is outside years 1..=9999")))?;
            us_eastern_midnight_utc_ns(d)
        }
    }
}

/// The local session date (days since 1970-01-01) whose `[midnight, next midnight)` contains `ts`.
pub fn session_date_of(ts: Ts, tz: SessionTz) -> QtResult<i32> {
    let d0 = i32::try_from(ts.div_euclid(NS_PER_DAY)).map_err(|_| QtError::InputFormat {
        location: "timestamp".into(),
        msg: format!("{ts} out of range"),
    })?;
    for d in [d0 + 1, d0, d0 - 1] {
        if midnight_utc_ns(d, tz)? <= ts {
            return Ok(d);
        }
    }
    Err(QtError::InputFormat {
        location: "timestamp".into(),
        msg: format!("{ts} has no session date"),
    })
}

/// The session date whose local midnight is exactly `midnight` in `tz`, else the UTC date of `midnight + 12 h`.
pub fn session_date_of_midnight(midnight: Ts, tz: SessionTz) -> QtResult<i32> {
    let d = session_date_of(midnight, tz)?;
    if midnight_utc_ns(d, tz)? == midnight {
        return Ok(d);
    }
    i32::try_from(midnight.saturating_add(12 * NS_PER_HOUR).div_euclid(NS_PER_DAY))
        .map_err(|_| QtError::Config(format!("midnight_utc_ns {midnight} out of range")))
}

/// The tz name stored in `qt.instruments.session_tz`.
pub fn tz_name(tz: SessionTz) -> &'static str {
    match tz {
        SessionTz::AmericaNewYork => "America/New_York",
        SessionTz::Utc => "UTC",
    }
}

/// Inverse of `tz_name` (unknown names fall back to UTC).
pub fn tz_from_name(name: &str) -> SessionTz {
    if name == "America/New_York" {
        SessionTz::AmericaNewYork
    } else {
        SessionTz::Utc
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dst_midnight_table_matches_et_time_rs() {
        let d = Date::parse("2019-01-30").unwrap();
        assert_eq!(d.days_from_civil(), 17_926);
        assert_eq!(d.eastern_offset_hours(), 5);
        assert_eq!(us_eastern_midnight_utc_ns(d).unwrap(), 1_548_824_400_000_000_000);
        assert_eq!(Date::from_itch_filename("x/01302019.NASDAQ_ITCH50.gz"), Some(d));
        assert_eq!(Date::from_itch_filename("NASDAQ_ITCH50.gz"), None);
        assert_eq!(Date::from_itch_filename("02302019.NASDAQ_ITCH50.gz"), None);
        assert_eq!(Date::parse("1970-01-01").unwrap().days_from_civil(), 0);
        let off = |s: &str| Date::parse(s).unwrap().eastern_offset_hours();
        for (s, h) in [
            ("2019-03-09", 5),
            ("2019-03-10", 5),
            ("2019-03-11", 4),
            ("2019-07-01", 4),
            ("2019-11-03", 4),
            ("2019-11-04", 5),
            ("2024-03-10", 5),
            ("2024-03-11", 4),
            ("2024-11-03", 4),
            ("2024-11-04", 5),
        ] {
            assert_eq!(off(s), h, "{s}");
        }
        let mid = |s: &str| us_eastern_midnight_utc_ns(Date::parse(s).unwrap()).unwrap();
        assert_eq!(mid("2024-07-01"), 1_719_806_400_000_000_000);
        // The two real days et converted (SPEC 6.8 RD-1, RD-6).
        assert_eq!(mid("2019-12-30"), 1_577_682_000_000_000_000);
        assert_eq!(mid("2020-01-30"), 1_580_360_400_000_000_000);
        assert!(Date::parse("2019-13-01").is_err());
        assert!(Date::parse("20190101").is_err());
        assert!(Date::parse("2019-02-29").is_err());
        assert!(Date::parse("2020-02-29").is_ok());
    }

    #[test]
    fn days_round_trip_and_session_dates() {
        for days in [-719_162, -1, 0, 1, 17_926, 18_000, 2_932_896] {
            let d = Date::from_days(days).unwrap();
            assert_eq!(d.days_from_civil(), days, "{d}");
        }
        assert_eq!(format_date(17_926), "2019-01-30");
        let ny = SessionTz::AmericaNewYork;
        let m = midnight_utc_ns(17_926, ny).unwrap();
        assert_eq!(session_date_of(m, ny).unwrap(), 17_926);
        assert_eq!(session_date_of(m - 1, ny).unwrap(), 17_925);
        assert_eq!(session_date_of(m + NS_PER_DAY - 1, ny).unwrap(), 17_926);
        assert_eq!(session_date_of_midnight(m, ny).unwrap(), 17_926);
        assert_eq!(session_date_of(NS_PER_DAY, SessionTz::Utc).unwrap(), 1);
        // DST switch day 2019-03-10 is 23 h long: next midnight is 23 h later.
        let s = parse_date("2019-03-10").unwrap();
        let a = midnight_utc_ns(s, ny).unwrap();
        let b = midnight_utc_ns(s + 1, ny).unwrap();
        assert_eq!(b - a, 23 * NS_PER_HOUR);
        assert_eq!(session_date_of(b - 1, ny).unwrap(), s);
        assert!(midnight_utc_ns(-10, SessionTz::Utc).is_err());
    }
}
